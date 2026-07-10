from __future__ import annotations

import hashlib
import json
import math
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
    return {
        "schema_version": 1,
        "name": "unit-cuda-validation",
        "service_ids": dict(FORMAL_SERVICE_IDS),
        "references": references,
        "gpt_weights": {
            "v2ProPlus": {"gpt": "worker:/weights/v2ProPlus.ckpt", "sovits": "worker:/weights/v2ProPlus.pth"},
            "v2Pro": {"gpt": "worker:/weights/v2Pro.ckpt", "sovits": "worker:/weights/v2Pro.pth"},
        },
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
