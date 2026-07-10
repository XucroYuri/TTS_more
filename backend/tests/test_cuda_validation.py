from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import subprocess
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.adapters.base import SynthesisResult
from app.cuda_validation import (
    FORMAL_SERVICE_IDS,
    VALIDATION_MODES,
    CUDAValidationRunner,
    NvidiaSmiMonitor,
    character_error_rate,
    create_transcriber,
    evaluate_cer,
    evaluate_performance,
    load_fixture,
    main,
    measure_wav,
    validation_cases,
    _sanitize_evidence,
)


def _write_wav(path: Path, *, seconds: float = 1.0, amplitude: int = 10_000) -> None:
    sample_rate = 16_000
    samples = [
        int(amplitude * math.sin(2 * math.pi * 440 * index / sample_rate))
        for index in range(int(sample_rate * seconds))
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))


def _write_float_wav(path: Path, *, seconds: float = 1.0) -> None:
    sample_rate = 16_000
    samples = [math.sin(2 * math.pi * 440 * index / sample_rate) * 0.25 for index in range(int(sample_rate * seconds))]
    data = b"".join(struct.pack("<f", sample) for sample in samples)
    fmt = struct.pack("<HHIIHH", 3, 1, sample_rate, sample_rate * 4, 4, 32)
    riff_size = 4 + 8 + len(fmt) + 8 + len(data)
    path.write_bytes(b"RIFF" + struct.pack("<I", riff_size) + b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data)


def _fixture_payload(tmp_path: Path, *, asr_required: bool = True) -> dict:
    references = {}
    for name in ("gpt_sovits", "indextts", "cosyvoice"):
        path = tmp_path / f"{name}.wav"
        _write_wav(path)
        references[name] = str(path)
    weights = {}
    for version in ("v2ProPlus", "v2Pro"):
        pair = {}
        for kind, suffix in (("gpt", ".ckpt"), ("sovits", ".pth")):
            path = tmp_path / f"{version}{suffix}"
            path.write_bytes(b"unit-test-weight")
            pair[kind] = str(path)
        weights[version] = pair
    return {
        "schema_version": 1,
        "name": "unit-cuda-validation",
        "service_ids": dict(FORMAL_SERVICE_IDS),
        "references": references,
        "gpt_weights": weights,
        "prompts": {
            "gpt": {"text": "参考文本。", "language": "zh"},
            "cosyvoice": {"text": "参考提示。", "language": "zh"},
            "index_emotion": "克制但坚定",
        },
        "test_texts": {
            "gpt_v2ProPlus": "默认模型验证。",
            "gpt_v2Pro": "兼容模型验证。",
            "index_emotion": "情绪文本验证。",
            "cosyvoice_zero_shot": "零样本验证。",
            "cosyvoice_cross_lingual": "Cross-lingual validation.",
        },
        "reviewers": [
            {"id": "reviewer-a", "name": "Reviewer A"},
            {"id": "reviewer-b", "name": "Reviewer B"},
        ],
        "asr": {"required": asr_required, "model": "large-v3", "language": "zh"},
        "performance_baseline": {"warm_p95_seconds": 10},
        "worker_logs": {service_id: f"logs/{service_id}.log" for service_id in FORMAL_SERVICE_IDS.values()},
    }


def _write_fixture(tmp_path: Path, *, asr_required: bool = True) -> Path:
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(_fixture_payload(tmp_path, asr_required=asr_required)), encoding="utf-8")
    return path


def _expected_transcriber(audio_path: Path, _language: str | None = None) -> str:
    names = {
        "gpt-v2ProPlus": "默认模型验证。",
        "gpt-v2ProPlus-artifact": "默认模型验证。",
        "gpt-v2Pro": "兼容模型验证。",
        "index-emotion-text": "情绪文本验证。",
        "cosyvoice-zero-shot": "零样本验证。",
        "cosyvoice-cross-lingual": "Cross-lingual validation.",
    }
    return names[audio_path.stem]


def _write_services(tmp_path: Path) -> Path:
    providers = {
        "local-gpt-sovits-main": ("gpt-sovits", "gpt-sovits"),
        "local-indextts": ("indextts", "indextts"),
        "local-cosyvoice": ("cosyvoice", "cosyvoice"),
    }
    payload = []
    for index, (service_id, (engine, provider)) in enumerate(providers.items()):
        payload.append(
            {
                "service_id": service_id,
                "display_name": service_id,
                "engine": engine,
                "provider_type": provider,
                "api_contract": "tts-more-v1",
                "base_url": f"http://worker-{index}.lan:988{index}",
                "mode": "external",
                "network_scope": "lan",
                "managed": False,
                "enabled": True,
                "resource_group": f"worker-{index}:cuda-0",
                "capabilities": ["tts", "artifact-transfer"],
            }
        )
    path = tmp_path / "services.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_topology(tmp_path: Path) -> Path:
    path = tmp_path / "topology.json"
    path.write_text('{"schema_version":1,"name":"unit-distributed"}', encoding="utf-8")
    return path


def _distributed_orchestration(tmp_path: Path, *, commit: str = "a" * 40) -> dict:
    topology_path = _write_topology(tmp_path)
    token = f"unit-orchestration-{commit[0]}"
    preflight_path = tmp_path / f"distributed-preflight-{commit[0]}.json"
    preflight_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "distributed",
                "topology_sha256": hashlib.sha256(topology_path.read_bytes()).hexdigest(),
                "controller_commit": commit,
                "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    return {
        "topology_path": topology_path,
        "distributed_preflight_path": preflight_path,
        "distributed_orchestration_token": token,
        "expected_commit": commit,
    }


def test_validation_modes_and_fixture_expand_exact_required_cases(tmp_path: Path) -> None:
    assert VALIDATION_MODES == ("single-clean", "single-release", "distributed")
    fixture = load_fixture(_write_fixture(tmp_path))

    cases = validation_cases(fixture)

    assert [case.name for case in cases] == [
        "gpt-v2ProPlus",
        "gpt-v2Pro",
        "index-emotion-text",
        "cosyvoice-zero-shot",
        "cosyvoice-cross-lingual",
    ]
    assert cases[0].parameters["gpt_weights_path"].endswith("v2ProPlus.ckpt")
    assert cases[1].parameters["sovits_weights_path"].endswith("v2Pro.pth")
    assert cases[2].parameters["emotion_mode"] == "emotion_text"
    assert cases[3].parameters["mode"] == "zero_shot"
    assert cases[4].parameters["mode"] == "cross_lingual"


def test_fixture_rejects_non_formal_service_ids(tmp_path: Path) -> None:
    payload = _fixture_payload(tmp_path)
    payload["service_ids"]["gpt_sovits"] = "local-gpt-sovits"
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="local-gpt-sovits-main"):
        load_fixture(path)


def test_fixture_rejects_disabled_asr_gate(tmp_path: Path) -> None:
    payload = _fixture_payload(tmp_path)
    payload["asr"]["required"] = False
    path = tmp_path / "asr-disabled.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="required"):
        load_fixture(path)


@pytest.mark.parametrize(
    "baseline",
    [{}, {"warm_p95_seconds": None}, {"warm_p95_seconds": 1.5e308}, {"warm_p95_seconds": float("inf")}],
)
def test_fixture_rejects_invalid_performance_baseline(tmp_path: Path, baseline: dict) -> None:
    payload = _fixture_payload(tmp_path)
    payload["performance_baseline"] = baseline
    path = tmp_path / "empty-baseline.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="warm_p95_seconds"):
        load_fixture(path)


def test_evidence_sanitizer_redacts_hosts_and_absolute_paths_from_errors() -> None:
    sanitized = _sanitize_evidence(
        {
            "errors": [
                "request http://tts-gpt.lan:9880/private failed at "
                r"C:\Users\Alice\models\voice.wav and /Users/alice/private/ref.wav via 192.168.10.42"
            ]
        }
    )
    rendered = json.dumps(sanitized)

    for secret in ("tts-gpt.lan", "192.168.10.42", "Alice", "/Users/alice"):
        assert secret not in rendered
    assert "/private" in rendered


def test_wav_quality_metrics_enforce_all_thresholds(tmp_path: Path) -> None:
    good = tmp_path / "good.wav"
    silent = tmp_path / "silent.wav"
    _write_wav(good)
    _write_wav(silent, amplitude=0)

    good_result = measure_wav(good)
    silent_result = measure_wav(silent)

    assert good_result["passed"] is True
    assert good_result["size_bytes"] > 1024
    assert 0.5 <= good_result["duration_seconds"] <= 30
    assert good_result["rms_dbfs"] > -50
    assert good_result["clipping_ratio"] <= 0.01
    assert good_result["silence_ratio"] < 0.90
    assert silent_result["passed"] is False
    assert silent_result["checks"]["rms"] is False
    assert silent_result["checks"]["silence"] is False


def test_wav_quality_metrics_accept_ieee_float_wav_from_scipy(tmp_path: Path) -> None:
    output = tmp_path / "float.wav"
    _write_float_wav(output)

    result = measure_wav(output)

    assert result["passed"] is True
    assert result["sample_width_bytes"] == 4
    assert result["encoding"] == "ieee-float"


def test_cer_reports_per_item_and_aggregate_thresholds() -> None:
    assert character_error_rate("你好世界", "你好世") == pytest.approx(0.25)
    assert character_error_rate("", "") == 0
    assert character_error_rate("", "x") == 1

    result = evaluate_cer([("a", "你好世界", "你好世"), ("b", "测试", "测试")])
    assert result["items"][0]["cer"] == pytest.approx(0.25)
    assert result["items"][0]["passed"] is True
    assert result["aggregate_cer"] == pytest.approx(1 / 6)
    assert result["passed"] is True

    failed = evaluate_cer([("bad", "abcd", "x")])
    assert failed["items"][0]["cer"] > 0.40
    assert failed["aggregate_cer"] > 0.25
    assert failed["passed"] is False


def test_required_asr_has_clear_lazy_preflight_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = __import__

    def guarded_import(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)
    transcriber, error = create_transcriber(required=True, model_name="large-v3", language="zh")

    assert transcriber is None
    assert "faster-whisper" in error
    assert "large-v3" in error


def test_performance_gate_enforces_resource_and_regression_thresholds() -> None:
    passing = evaluate_performance(
        {
            "oom": False,
            "minimum_free_mib": 768,
            "baseline_memory_mib": 1000,
            "unload_memory_mib": 1800,
            "unload_recovery_seconds": 20,
            "cold_load_seconds": 500,
            "short_synthesis_seconds": [120, 180],
            "warm_p95_seconds": 13,
        },
        baseline={"warm_p95_seconds": 10},
    )
    assert passing["passed"] is True
    assert all(passing["checks"].values())

    failed = evaluate_performance(
        {
            "oom": True,
            "minimum_free_mib": 100,
            "baseline_memory_mib": 1000,
            "unload_memory_mib": 2500,
            "unload_recovery_seconds": 31,
            "cold_load_seconds": 601,
            "short_synthesis_seconds": [301],
            "warm_p95_seconds": 13.1,
        },
        baseline={"warm_p95_seconds": 10},
    )
    assert failed["passed"] is False
    assert not any(failed["checks"].values())


def test_nvidia_smi_monitor_writes_time_series_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    csv_path = tmp_path / "nvidia-smi.csv"
    monitor = NvidiaSmiMonitor(csv_path)
    monkeypatch.setattr("app.cuda_validation.shutil.which", lambda _: None)
    monkeypatch.setattr(
        "app.cuda_validation.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout="2026/07/10 12:00:00.000, 0, GPU-test, 16384, 12000, 4384, 42\n",
            stderr="",
        ),
    )

    monitor.start()
    monitor.capture_once()
    monitor.stop()

    rows = csv_path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2
    assert "memory_free_mib" in rows[0]
    assert "GPU-test" in rows[1]


class _FakeClient:
    def __init__(self, endpoint, calls: list[tuple], state: dict[str, str | None], fail_case: str | None = None) -> None:
        self.endpoint = endpoint
        self.calls = calls
        self.state = state
        self.fail_case = fail_case

    def health(self) -> dict:
        self.calls.append((self.endpoint.service_id, "health"))
        return {"ready": True, "tts_more_commit": "a" * 40}

    def capabilities(self) -> dict:
        self.calls.append((self.endpoint.service_id, "capabilities"))
        return {"capabilities": ["tts", "artifact-transfer"]}

    def load(self, profile: str, parameters: dict | None = None) -> None:
        self.calls.append((self.endpoint.service_id, "load", profile, parameters))
        self.state[self.endpoint.service_id] = profile

    def synthesize(self, request) -> SynthesisResult:
        self.calls.append((self.endpoint.service_id, "synthesize", request.profile, request.parameters))
        if request.profile == self.fail_case:
            raise RuntimeError("intentional synthesis failure")
        _write_wav(request.output_path)
        return SynthesisResult(audio_path=request.output_path, metadata={"artifact_verified": True})

    def unload(self) -> None:
        self.calls.append((self.endpoint.service_id, "unload"))
        self.state[self.endpoint.service_id] = None


def test_runner_writes_full_artifacts_and_continues_after_case_failure(tmp_path: Path) -> None:
    calls: list[tuple] = []
    state: dict[str, str | None] = {service_id: None for service_id in FORMAL_SERVICE_IDS.values()}
    services_path = _write_services(tmp_path)
    fixture_path = _write_fixture(tmp_path)
    output_dir = tmp_path / "report"

    runner = CUDAValidationRunner(
        mode="distributed",
        services_path=services_path,
        fixture_path=fixture_path,
        output_dir=output_dir,
        **_distributed_orchestration(tmp_path),
        client_factory=lambda endpoint: _FakeClient(endpoint, calls, state, fail_case="cosyvoice-zero-shot"),
        status_probe=lambda endpoint: {
            "device": "cuda:0",
            "device_uuid": f"GPU-{endpoint.service_id}",
            "cuda_runtime": "12.8",
            "loaded": state[endpoint.service_id] is not None,
            "model": state[endpoint.service_id],
            "memory": {
                "free_bytes": 2 * 1024**3,
                "total_bytes": 16 * 1024**3,
                "reserved_bytes": 0,
                "allocated_bytes": 0,
            },
        },
        clock=lambda: 10.0,
        sleeper=lambda _: None,
        transcriber=_expected_transcriber,
    )

    report = runner.run()

    assert report["passed"] is False
    assert report["certifiable"] is False
    assert report["certification_status"] == "core_failed"
    assert len(report["cases"]) == 5
    assert [case["name"] for case in report["cases"] if not case["passed"]] == ["cosyvoice-zero-shot"]
    assert any(call[1] == "synthesize" and call[2] == "cosyvoice-cross-lingual" for call in calls)
    assert {call[0] for call in calls if call[1] == "health"} == set(FORMAL_SERVICE_IDS.values())
    assert len([call for call in calls if call[1] == "unload"]) == 5
    assert (output_dir / "summary.json").is_file()
    assert (output_dir / "junit.xml").is_file()
    assert (output_dir / "human-listening-review.md").is_file()
    assert (output_dir / "worker-log-references.json").is_file()
    assert (output_dir / "nvidia-smi.csv").is_file()
    assert len(list((output_dir / "wav").glob("*.wav"))) == 4
    assert "Reviewer A" in (output_dir / "human-listening-review.md").read_text(encoding="utf-8")
    assert "cosyvoice-zero-shot" in (output_dir / "junit.xml").read_text(encoding="utf-8")
    assert str(tmp_path) not in (output_dir / "summary.json").read_text(encoding="utf-8")
    log_references = (output_dir / "worker-log-references.json").read_text(encoding="utf-8")
    assert ".lan" not in log_references
    assert str(tmp_path) not in log_references


def test_single_runner_validates_explicit_local_artifact_delivery(tmp_path: Path) -> None:
    calls: list[tuple] = []
    state: dict[str, str | None] = {service_id: None for service_id in FORMAL_SERVICE_IDS.values()}
    services_path = _write_services(tmp_path)
    services = json.loads(services_path.read_text(encoding="utf-8"))
    for service in services:
        service.update(mode="local", network_scope="localhost", managed=True, resource_group="cuda-0")
    services_path.write_text(json.dumps(services), encoding="utf-8")
    created_endpoints = []

    def factory(endpoint):
        created_endpoints.append(endpoint)
        return _FakeClient(endpoint, calls, state)

    runner = CUDAValidationRunner(
        mode="single-release",
        services_path=services_path,
        fixture_path=_write_fixture(tmp_path),
        output_dir=tmp_path / "single",
        client_factory=factory,
        status_probe=lambda endpoint: {
            "device": "cuda:0",
            "device_uuid": "GPU-single",
            "cuda_runtime": "12.8",
            "loaded": state[endpoint.service_id] is not None,
            "model": state[endpoint.service_id],
            "memory": {
                "free_bytes": 2 * 1024**3,
                "total_bytes": 16 * 1024**3,
                "reserved_bytes": 0,
                "allocated_bytes": 0,
            },
        },
        clock=lambda: 10.0,
        sleeper=lambda _: None,
        transcriber=_expected_transcriber,
    )

    report = runner.run()

    assert report["passed"] is True
    assert report["certifiable"] is False
    assert report["certification_status"] == "core_passed_ui_pending"
    assert any(case["name"] == "gpt-v2ProPlus-artifact" for case in report["cases"])
    assert any(endpoint.default_params.get("delivery") == "artifact" for endpoint in created_endpoints)


def test_runner_preflight_failure_is_reported_without_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = __import__

    def guarded_import(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)
    runner = CUDAValidationRunner(
        mode="single-clean",
        services_path=_write_services(tmp_path),
        fixture_path=_write_fixture(tmp_path, asr_required=True),
        output_dir=tmp_path / "failed-report",
        client_factory=lambda endpoint: pytest.fail("network/client construction must not happen"),
    )

    report = runner.run()

    assert report["passed"] is False
    assert "faster-whisper" in report["preflight"][0]["message"]
    assert (tmp_path / "failed-report" / "summary.json").is_file()


@pytest.mark.parametrize("mode", ["single-clean", "single-release"])
def test_input_preflight_rejects_missing_weight_files_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    payload = _fixture_payload(tmp_path)
    payload["gpt_weights"]["v2Pro"]["gpt"] = str(tmp_path / "missing.ckpt")
    fixture_path = tmp_path / f"{mode}-missing-weight.json"
    fixture_path.write_text(json.dumps(payload), encoding="utf-8")
    output_dir = tmp_path / f"{mode}-input-preflight"
    output_dir.mkdir()
    (output_dir / "nvidia-smi.csv").write_text("stale-gpu-evidence\n", encoding="utf-8")
    network_calls: list[str] = []
    monitor_starts = 0

    def monitor_factory(_path: Path):
        nonlocal monitor_starts
        monitor_starts += 1
        pytest.fail("nvidia-smi monitor must not start during input preflight")

    monkeypatch.setattr(
        "app.cuda_validation.create_transcriber",
        lambda **_kwargs: pytest.fail("input preflight must not import or construct an ASR transcriber"),
    )
    runner = CUDAValidationRunner(
        mode=mode,
        services_path=tmp_path / "services-must-not-be-read.json",
        fixture_path=fixture_path,
        output_dir=output_dir,
        client_factory=lambda _endpoint: network_calls.append("client"),
        status_probe=lambda _endpoint: network_calls.append("status") or {},
        monitor_factory=monitor_factory,
    )

    report = runner.run_input_preflight()

    assert report["passed"] is False
    assert report["stage"] == "input-preflight"
    assert report["blocker_count"] == 1
    assert "weight v2Pro.gpt not found" in report["preflight"][0]["message"]
    assert report["next_action"] == (
        "补齐或修正 reference audio 和 GPT/SoVITS weight 路径，然后重新运行相同命令。"
    )
    assert network_calls == []
    assert monitor_starts == 0
    for artifact in (
        "summary.json",
        "junit.xml",
        "human-listening-review.md",
        "worker-log-references.json",
        "nvidia-smi.csv",
    ):
        assert (output_dir / artifact).is_file()
    assert (output_dir / "nvidia-smi.csv").read_text(encoding="utf-8").splitlines() == [
        ",".join(NvidiaSmiMonitor.HEADER)
    ]


def test_distributed_input_preflight_requires_controller_reference_but_not_remote_weights(tmp_path: Path) -> None:
    payload = _fixture_payload(tmp_path)
    payload["gpt_weights"] = {
        "v2ProPlus": {"gpt": "D:/worker/weights/v2ProPlus.ckpt", "sovits": "D:/worker/weights/v2ProPlus.pth"},
        "v2Pro": {"gpt": "D:/worker/weights/v2Pro.ckpt", "sovits": "D:/worker/weights/v2Pro.pth"},
    }
    fixture_path = tmp_path / "distributed-remote-weights.json"
    fixture_path.write_text(json.dumps(payload), encoding="utf-8")
    runner = CUDAValidationRunner(
        mode="distributed",
        services_path=tmp_path / "services-must-not-be-read.json",
        fixture_path=fixture_path,
        output_dir=tmp_path / "distributed-remote-weights",
    )

    report = runner.run_input_preflight()

    assert report["passed"] is True
    payload["references"]["gpt_sovits"] = str(tmp_path / "controller-reference-missing.wav")
    fixture_path.write_text(json.dumps(payload), encoding="utf-8")
    report = runner.run_input_preflight()
    assert report["passed"] is False
    assert "reference gpt_sovits not found" in report["preflight"][0]["message"]


@pytest.mark.parametrize(
    "raw_path",
    ["${REMOTE_GPT_WEIGHT}", "$REMOTE_GPT_WEIGHT", "%REMOTE_GPT_WEIGHT%"],
)
def test_distributed_input_preflight_rejects_unresolved_remote_weight(
    tmp_path: Path, raw_path: str
) -> None:
    payload = _fixture_payload(tmp_path)
    payload["gpt_weights"]["v2ProPlus"]["gpt"] = raw_path
    fixture_path = tmp_path / "distributed-unresolved-weight.json"
    fixture_path.write_text(json.dumps(payload), encoding="utf-8")
    runner = CUDAValidationRunner(
        mode="distributed",
        services_path=tmp_path / "services-must-not-be-read.json",
        fixture_path=fixture_path,
        output_dir=tmp_path / "distributed-unresolved-weight",
    )

    report = runner.run_input_preflight()

    assert report["passed"] is False
    assert "weight v2ProPlus.gpt contains an unresolved environment variable" in report["preflight"][0]["message"]


@pytest.mark.parametrize(
    "raw_path",
    ["", "   ", "weights/model.ckpt", "../weights/model.ckpt", "/weights/model.ckpt"],
)
def test_distributed_input_preflight_rejects_empty_or_non_windows_absolute_weight(
    tmp_path: Path, raw_path: str
) -> None:
    payload = _fixture_payload(tmp_path)
    payload["gpt_weights"]["v2Pro"]["gpt"] = raw_path
    fixture_path = tmp_path / "distributed-invalid-weight-path.json"
    fixture_path.write_text(json.dumps(payload), encoding="utf-8")

    report = CUDAValidationRunner(
        mode="distributed",
        services_path=tmp_path / "services-must-not-be-read.json",
        fixture_path=fixture_path,
        output_dir=tmp_path / "distributed-invalid-weight-path",
    ).run_input_preflight()

    assert report["passed"] is False
    expected = "is empty" if not raw_path.strip() else "must be a Windows absolute path"
    assert expected in report["preflight"][0]["message"]


@pytest.mark.parametrize(
    "raw_path",
    [
        r"D:\worker\weights\model.ckpt",
        "D:/worker/weights/model.ckpt",
        r"\\worker-host\models\model.ckpt",
    ],
)
def test_distributed_input_preflight_accepts_windows_drive_and_unc_weight_paths(
    tmp_path: Path, raw_path: str
) -> None:
    payload = _fixture_payload(tmp_path)
    for pair in payload["gpt_weights"].values():
        pair["gpt"] = raw_path
        pair["sovits"] = raw_path
    fixture_path = tmp_path / "distributed-valid-weight-path.json"
    fixture_path.write_text(json.dumps(payload), encoding="utf-8")

    report = CUDAValidationRunner(
        mode="distributed",
        services_path=tmp_path / "services-must-not-be-read.json",
        fixture_path=fixture_path,
        output_dir=tmp_path / "distributed-valid-weight-path",
    ).run_input_preflight()

    assert report["passed"] is True


def test_preflight_only_cli_returns_zero_for_valid_local_inputs(tmp_path: Path) -> None:
    output = tmp_path / "preflight-only"

    exit_code = main(
        [
            "--mode",
            "single-release",
            "--services",
            str(tmp_path / "services-must-not-be-read.json"),
            "--fixture",
            str(_write_fixture(tmp_path)),
            "--output",
            str(output),
            "--preflight-only",
        ]
    )

    assert exit_code == 0
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["passed"] is True
    assert summary["stage"] == "input-preflight"
    assert summary["next_action"] == "继续部署和完整 CUDA 验证。"


def test_diagnostic_cli_marks_preflight_non_certifiable(tmp_path: Path) -> None:
    output = tmp_path / "diagnostic-preflight"

    exit_code = main(
        [
            "--mode",
            "single-clean",
            "--services",
            str(tmp_path / "services-must-not-be-read.json"),
            "--fixture",
            str(_write_fixture(tmp_path)),
            "--output",
            str(output),
            "--preflight-only",
            "--diagnostic",
        ]
    )

    assert exit_code == 0
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["certifiable"] is False
    assert summary["certification_status"] == "diagnostic"


@pytest.mark.parametrize("stage", ["deployment", "worker-wait"])
def test_blocker_cli_atomically_replaces_valid_preflight_evidence(
    tmp_path: Path, stage: str
) -> None:
    output = tmp_path / f"{stage}-failure"
    fixture_path = _write_fixture(tmp_path)
    common_args = [
        "--mode",
        "single-release",
        "--services",
        str(tmp_path / "services-not-required.json"),
        "--fixture",
        str(fixture_path),
        "--output",
        str(output),
    ]
    assert main([*common_args, "--preflight-only"]) == 0
    passing = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert passing["passed"] is True
    (output / "nvidia-smi.csv").write_text("stale-gpu-evidence\n", encoding="utf-8")

    exit_code = main(
        [
            *common_args,
            "--write-blocker-stage",
            stage,
            "--blocker-message",
            f"simulated safe {stage} failure",
        ]
    )

    assert exit_code == 1
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["passed"] is False
    assert summary["stage"] == stage
    assert summary["certifiable"] is False
    assert summary["certification_status"] == "blocked"
    assert summary["blocker_count"] == 1
    assert f"simulated safe {stage} failure" in summary["preflight"][0]["message"]
    assert stage in summary["next_action"]
    assert "simulated safe" in (output / "junit.xml").read_text(encoding="utf-8")
    assert "Automated gate: `FAIL`" in (output / "human-listening-review.md").read_text(encoding="utf-8")
    assert len(json.loads((output / "worker-log-references.json").read_text(encoding="utf-8"))) == 3
    assert (output / "nvidia-smi.csv").read_text(encoding="utf-8").splitlines() == [
        ",".join(NvidiaSmiMonitor.HEADER)
    ]
    assert list(output.glob(".*.tmp")) == []


@pytest.mark.parametrize("stage", ["fault-recovery", "evidence-collection"])
def test_post_core_blocker_preserves_completed_core_evidence(
    tmp_path: Path, stage: str
) -> None:
    output = tmp_path / f"post-core-{stage}"
    output.mkdir()
    fixture_path = _write_fixture(tmp_path)
    core_report = {
        "schema_version": 1,
        "name": "cuda-e2e-validation",
        "stage": "cuda-validation",
        "mode": "single-release",
        "started_at": "2026-07-10T00:00:00+00:00",
        "finished_at": "2026-07-10T00:01:00+00:00",
        "passed": True,
        "certifiable": False,
        "certification_status": "core_passed_ui_pending",
        "preflight": [],
        "services": [
            {"service_id": service_id, "passed": True, "errors": []}
            for service_id in FORMAL_SERVICE_IDS.values()
        ],
        "cases": [
            {"name": "core-case-a", "service_id": "local-gpt-sovits-main", "passed": True, "errors": [], "output_path": "core-a.wav"},
            {"name": "core-case-b", "service_id": "local-cosyvoice", "passed": True, "errors": [], "output_path": "core-b.wav"},
        ],
        "cer": {
            "required": True,
            "items": [{"name": "core-case-a", "cer": 0.0, "passed": True}],
            "aggregate_cer": 0.0,
            "passed": True,
        },
        "performance": {
            "passed": True,
            "checks": {"no_oom": True, "warm_p95_regression": True},
            "metrics": {"warm_p95_seconds": 1.25},
        },
    }
    (output / "summary.json").write_text(json.dumps(core_report), encoding="utf-8")
    (output / "junit.xml").write_text("<old-core-junit />", encoding="utf-8")
    (output / "human-listening-review.md").write_text("old listening", encoding="utf-8")
    worker_references = json.dumps(
        [{"service_id": "local-gpt-sovits-main", "configured_log": "core-worker.log", "status_path": "/status"}],
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    (output / "worker-log-references.json").write_text(worker_references, encoding="utf-8")
    gpu_evidence = ",".join(NvidiaSmiMonitor.HEADER) + "\n2026-07-10T00:00:30Z,2026/07/10,0,GPU-core,16384,12000,4384,42\n"
    (output / "nvidia-smi.csv").write_text(gpu_evidence, encoding="utf-8")
    common_args = [
        "--mode",
        "single-release",
        "--services",
        str(tmp_path / "services-not-required.json"),
        "--fixture",
        str(fixture_path),
        "--output",
        str(output),
    ]

    exit_code = main(
        [
            *common_args,
            "--write-blocker-stage",
            stage,
            "--blocker-message",
            f"simulated {stage} pipeline failure",
            "--preserve-existing",
        ]
    )

    assert exit_code == 1
    report = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    for field in ("preflight", "services", "cases", "cer", "performance"):
        assert report[field] == core_report[field]
    assert report["passed"] is False
    assert report["certifiable"] is False
    assert report["certification_status"] == "post_core_failed"
    assert report["stage"] == stage
    assert report["pipeline_failures"] == [
        {"stage": stage, "message": f"simulated {stage} pipeline failure", "passed": False}
    ]
    assert stage in report["next_action"]
    junit = (output / "junit.xml").read_text(encoding="utf-8")
    assert int(re.search(r'failures="(\d+)"', junit).group(1)) >= 1
    assert "pipeline" in junit
    listening = (output / "human-listening-review.md").read_text(encoding="utf-8")
    assert "core-case-a" in listening
    assert "core-case-b" in listening
    assert (output / "worker-log-references.json").read_text(encoding="utf-8") == worker_references
    assert (output / "nvidia-smi.csv").read_text(encoding="utf-8") == gpu_evidence


def test_preserve_existing_blocker_rejects_non_post_core_stage(tmp_path: Path) -> None:
    runner = CUDAValidationRunner(
        mode="single-release",
        services_path=tmp_path / "services.json",
        fixture_path=_write_fixture(tmp_path),
        output_dir=tmp_path / "invalid-preserve-stage",
    )

    with pytest.raises(ValueError, match="post-core"):
        runner.write_blocker_report(
            stage="deployment",
            message="must not preserve stale preflight evidence",
            preserve_existing=True,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell entrypoint is Windows-only")
@pytest.mark.parametrize(
    ("stage", "diagnostic"),
    [("deployment", False), ("worker-wait", True)],
)
def test_powershell_rewrites_valid_preflight_pass_after_safe_stage_failure(
    tmp_path: Path, stage: str, diagnostic: bool
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    fixture_path = _write_fixture(tmp_path)
    services_path = tmp_path / "empty-services.json"
    services_path.write_text("[]", encoding="utf-8")
    output = tmp_path / f"powershell-{stage}"
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(repo_root / "scripts" / "run-cuda-validation.ps1"),
        "-Mode",
        "single-release",
        "-Fixture",
        str(fixture_path),
        "-Services",
        str(services_path),
        "-Output",
        str(output),
    ]
    if stage == "deployment":
        command.extend(["-RepoPaths", str(tmp_path / "missing-repo-paths.json")])
    else:
        command.append("-SkipDeploy")

    completed = subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode != 0
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["passed"] is False
    assert summary["stage"] == stage
    assert summary["certifiable"] is False
    assert summary["certification_status"] == ("diagnostic" if diagnostic else "blocked")
    assert stage in summary["preflight"][0]["message"]
    for artifact in (
        "summary.json",
        "junit.xml",
        "human-listening-review.md",
        "worker-log-references.json",
        "nvidia-smi.csv",
    ):
        assert (output / artifact).is_file()
    assert "failures=\"1\"" in (output / "junit.xml").read_text(encoding="utf-8")
    assert (output / "nvidia-smi.csv").read_text(encoding="utf-8").splitlines() == [
        ",".join(NvidiaSmiMonitor.HEADER)
    ]


def test_input_preflight_checks_asr_availability_without_loading_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.cuda_validation.importlib.util.find_spec", lambda _name: None)
    monkeypatch.setattr(
        "app.cuda_validation.create_transcriber",
        lambda **_kwargs: pytest.fail("input preflight must not import or construct an ASR transcriber"),
    )
    runner = CUDAValidationRunner(
        mode="single-release",
        services_path=tmp_path / "services-must-not-be-read.json",
        fixture_path=_write_fixture(tmp_path),
        output_dir=tmp_path / "missing-asr",
    )

    report = runner.run_input_preflight()

    assert report["passed"] is False
    assert "ASR gate requires faster-whisper with large-v3" in report["preflight"][0]["message"]
    assert "安装 faster-whisper 并确保 large-v3 可用" in report["next_action"]


def test_input_preflight_requires_two_reviewers_only_for_single_clean(tmp_path: Path) -> None:
    payload = _fixture_payload(tmp_path)
    payload["reviewers"] = payload["reviewers"][:1]
    fixture_path = tmp_path / "one-reviewer.json"
    fixture_path.write_text(json.dumps(payload), encoding="utf-8")

    clean_report = CUDAValidationRunner(
        mode="single-clean",
        services_path=tmp_path / "services-must-not-be-read.json",
        fixture_path=fixture_path,
        output_dir=tmp_path / "clean-one-reviewer",
    ).run_input_preflight()
    release_report = CUDAValidationRunner(
        mode="single-release",
        services_path=tmp_path / "services-must-not-be-read.json",
        fixture_path=fixture_path,
        output_dir=tmp_path / "release-one-reviewer",
    ).run_input_preflight()

    assert clean_report["passed"] is False
    assert "single-clean requires 2 listening reviewers" in clean_report["preflight"][0]["message"]
    assert "补充当前模式所需的 listening reviewers" in clean_report["next_action"]
    assert release_report["passed"] is True


def test_input_preflight_next_action_identifies_fixture_schema_failure(tmp_path: Path) -> None:
    fixture_path = tmp_path / "invalid-schema.json"
    fixture_path.write_text("{}", encoding="utf-8")

    report = CUDAValidationRunner(
        mode="single-release",
        services_path=tmp_path / "services-must-not-be-read.json",
        fixture_path=fixture_path,
        output_dir=tmp_path / "invalid-schema",
    ).run_input_preflight()

    assert report["passed"] is False
    assert "fixture validation failed" in report["preflight"][0]["message"]
    assert "修复 fixture JSON 或 schema" in report["next_action"]


def test_input_preflight_next_action_combines_multiple_blocker_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _fixture_payload(tmp_path)
    payload["gpt_weights"]["v2Pro"]["gpt"] = str(tmp_path / "missing.ckpt")
    payload["reviewers"] = payload["reviewers"][:1]
    fixture_path = tmp_path / "multiple-blockers.json"
    fixture_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("app.cuda_validation.importlib.util.find_spec", lambda _name: None)

    report = CUDAValidationRunner(
        mode="single-clean",
        services_path=tmp_path / "services-must-not-be-read.json",
        fixture_path=fixture_path,
        output_dir=tmp_path / "multiple-blockers",
    ).run_input_preflight()

    assert report["blocker_count"] == 3
    assert "reference audio 和 GPT/SoVITS weight" in report["next_action"]
    assert "faster-whisper" in report["next_action"]
    assert "listening reviewers" in report["next_action"]
    assert report["next_action"].count("重新运行相同命令") == 1


def test_runner_preflight_rejects_unresolved_weight_environment_without_network(tmp_path: Path) -> None:
    payload = _fixture_payload(tmp_path)
    payload["gpt_weights"]["v2ProPlus"]["gpt"] = "${MISSING_GPT_WEIGHT}"
    fixture_path = tmp_path / "unresolved-weight.json"
    fixture_path.write_text(json.dumps(payload), encoding="utf-8")
    runner = CUDAValidationRunner(
        mode="single-release",
        services_path=_write_services(tmp_path),
        fixture_path=fixture_path,
        output_dir=tmp_path / "failed-weight",
        client_factory=lambda endpoint: pytest.fail("network/client construction must not happen"),
    )

    report = runner.run()

    assert report["passed"] is False
    assert "weight v2ProPlus.gpt contains an unresolved environment variable" in report["preflight"][0]["message"]


def test_release_runner_requires_approved_performance_baseline(tmp_path: Path) -> None:
    payload = _fixture_payload(tmp_path)
    payload.pop("performance_baseline")
    fixture_path = tmp_path / "no-baseline.json"
    fixture_path.write_text(json.dumps(payload), encoding="utf-8")
    runner = CUDAValidationRunner(
        mode="single-release",
        services_path=_write_services(tmp_path),
        fixture_path=fixture_path,
        output_dir=tmp_path / "no-baseline",
        expected_commit=None,
        require_baseline=True,
        transcriber=_expected_transcriber,
        client_factory=lambda endpoint: pytest.fail("network/client construction must not happen"),
    )

    report = runner.run()

    assert report["passed"] is False
    assert "approved performance baseline" in report["preflight"][0]["message"]
    assert "补充已批准的 performance baseline" in report["next_action"]


@pytest.mark.parametrize(
    ("mode", "require_baseline"),
    [("single-clean", True), ("distributed", False)],
)
def test_first_certification_can_establish_performance_baseline(
    tmp_path: Path, mode: str, require_baseline: bool
) -> None:
    payload = _fixture_payload(tmp_path)
    payload.pop("performance_baseline")
    fixture_path = tmp_path / f"{mode}-first-certification.json"
    fixture_path.write_text(json.dumps(payload), encoding="utf-8")
    runner = CUDAValidationRunner(
        mode=mode,
        services_path=_write_services(tmp_path),
        fixture_path=fixture_path,
        output_dir=tmp_path / mode,
        require_baseline=require_baseline,
        transcriber=_expected_transcriber,
        **(_distributed_orchestration(tmp_path) if mode == "distributed" else {}),
    )
    report: dict = {"preflight": []}

    fixture, endpoints = runner._preflight(report)

    assert fixture is not None
    assert set(endpoints) == set(FORMAL_SERVICE_IDS.values())
    assert report["preflight"] == []


def test_runner_rejects_worker_from_stale_tts_more_commit(tmp_path: Path) -> None:
    calls: list[tuple] = []
    state: dict[str, str | None] = {service_id: None for service_id in FORMAL_SERVICE_IDS.values()}
    runner = CUDAValidationRunner(
        mode="distributed",
        services_path=_write_services(tmp_path),
        fixture_path=_write_fixture(tmp_path),
        output_dir=tmp_path / "stale",
        **_distributed_orchestration(tmp_path, commit="b" * 40),
        client_factory=lambda endpoint: _FakeClient(endpoint, calls, state),
        status_probe=lambda endpoint: {
            "device": "cuda:0",
            "device_uuid": f"GPU-{endpoint.service_id}",
            "cuda_runtime": "12.8",
            "loaded": False,
            "model": None,
            "memory": {"free_bytes": 1, "total_bytes": 16 * 1024**3, "reserved_bytes": 0, "allocated_bytes": 0},
        },
        transcriber=_expected_transcriber,
    )

    report = runner.run()

    assert report["passed"] is False
    assert all("TTS More commit" in service["errors"][0] for service in report["services"])


def test_distributed_contract_rejects_workers_on_same_gpu(tmp_path: Path) -> None:
    state: dict[str, str | None] = {service_id: None for service_id in FORMAL_SERVICE_IDS.values()}
    runner = CUDAValidationRunner(
        mode="distributed",
        services_path=_write_services(tmp_path),
        fixture_path=_write_fixture(tmp_path),
        output_dir=tmp_path / "duplicate-gpu",
        **_distributed_orchestration(tmp_path),
        client_factory=lambda endpoint: _FakeClient(endpoint, [], state),
        status_probe=lambda _endpoint: {
            "device": "cuda:0",
            "device_uuid": "GPU-shared",
            "cuda_runtime": "12.8",
            "loaded": False,
            "model": None,
            "memory": {
                "free_bytes": 2 * 1024**3,
                "total_bytes": 16 * 1024**3,
                "reserved_bytes": 0,
                "allocated_bytes": 0,
            },
        },
        transcriber=_expected_transcriber,
    )
    report: dict = {"preflight": [], "services": []}
    fixture, endpoints = runner._preflight(report)
    assert fixture is not None

    _clients, ready = runner._check_service_contracts(report, fixture, endpoints)

    assert ready == set()
    assert all("distinct CUDA device UUID" in service["errors"][-1] for service in report["services"])


def test_distributed_runner_requires_powershell_orchestration(tmp_path: Path) -> None:
    runner = CUDAValidationRunner(
        mode="distributed",
        services_path=_write_services(tmp_path),
        fixture_path=_write_fixture(tmp_path),
        output_dir=tmp_path / "not-orchestrated",
        transcriber=_expected_transcriber,
    )
    report: dict = {"preflight": []}

    runner._preflight(report)

    assert "PowerShell orchestration" in report["preflight"][0]["message"]


def test_distributed_preflight_is_bound_to_topology_hash(tmp_path: Path) -> None:
    orchestration = _distributed_orchestration(tmp_path)
    orchestration["topology_path"].write_text('{"schema_version":1,"name":"changed"}', encoding="utf-8")
    runner = CUDAValidationRunner(
        mode="distributed",
        services_path=_write_services(tmp_path),
        fixture_path=_write_fixture(tmp_path),
        output_dir=tmp_path / "mismatched-topology",
        transcriber=_expected_transcriber,
        **orchestration,
    )
    report: dict = {"preflight": []}

    runner._preflight(report)

    assert "topology hash" in report["preflight"][0]["message"]


def test_cli_and_powershell_entrypoints_declare_required_arguments() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    python_script = repo_root / "scripts" / "run-cuda-validation.py"
    powershell_script = (repo_root / "scripts" / "run-cuda-validation.ps1").read_text(encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(python_script), "--help"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    for argument in ("--mode", "--services", "--fixture", "--output", "--require-baseline"):
        assert argument in completed.stdout
    assert "single-clean" in powershell_script
    assert "single-release" in powershell_script
    assert "distributed" in powershell_script
    assert "Topology" in powershell_script
    assert "Node" in powershell_script
    assert "ssh" in powershell_script
    assert "EncodedCommand" in powershell_script
    assert "scp" in powershell_script
    assert "deploy-local-tts.ps1" in powershell_script
    assert "Invoke-DistributedFaultRecovery" in powershell_script
    assert "Get-NetTCPConnection" in powershell_script
    assert "degraded_within_seconds" in powershell_script
    assert "fault-recovery.json" in powershell_script
    assert "Start-RemoteGpuMonitor" in powershell_script
    assert "Collect-DistributedEvidence" in powershell_script
    assert "worker-logs" in powershell_script
    assert "rev-parse HEAD" in powershell_script
    assert "checkout --detach" in powershell_script
    assert "status --porcelain --untracked-files=all" in powershell_script
    assert "Remote TTS More checkout is dirty" in powershell_script
    assert "[System.Net.Dns]::GetHostAddresses" in powershell_script
    assert "resolve to distinct IP addresses" in powershell_script
    assert "distributed mode does not allow -SkipDeploy" in powershell_script
    assert "distributed mode does not allow -Node" in powershell_script
    assert "distributed mode does not allow -SkipStart" in powershell_script
    assert "distributed mode does not allow -SkipFaultRecovery" in powershell_script
    assert "MachineGuid" in powershell_script
    assert "distinct Windows machine identity" in powershell_script
    assert "orchestration-preflight.json" in powershell_script
    assert "TTS_MORE_DISTRIBUTED_ORCHESTRATION_TOKEN" in powershell_script
    assert "Remove-Item Env:TTS_MORE_DISTRIBUTED_ORCHESTRATION_TOKEN" in powershell_script
    assert "RequireBaseline" in powershell_script
    assert 'if (-not $RequireBaseline) { $remoteDeploy += " -CleanRepos" }' in powershell_script


def test_cli_returns_nonzero_and_writes_summary_when_gate_fails(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    output = tmp_path / "cli-failure"

    completed = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "run-cuda-validation.py"),
            "--mode",
            "single-clean",
            "--services",
            str(tmp_path / "missing-services.json"),
            "--fixture",
            str(tmp_path / "missing-fixture.json"),
            "--output",
            str(output),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert (output / "summary.json").is_file()


def test_committed_fixture_example_is_sanitized_and_schema_valid() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "deployment" / "validation" / "fixture.example.json"
    raw = path.read_text(encoding="utf-8")

    fixture = load_fixture(path)

    assert fixture.service_ids.model_dump() == FORMAL_SERVICE_IDS
    assert "/Users/" not in raw
    assert "C:\\" not in raw
    assert ".wav" not in raw
    assert "${TTS_MORE_VALIDATION_" in raw
