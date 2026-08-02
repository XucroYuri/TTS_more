from __future__ import annotations

import json
import hashlib
import math
import os
import shutil
import socket
import struct
import subprocess
import time
import warnings
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
import app.comfyui.reliability_validation as reliability_validation

from app.comfyui.reliability_validation import (
    AudioProof,
    BoundaryEvidence,
    CaseEvidence,
    CleanupEvidence,
    ComfyQueueEvidence,
    GpuSnapshot,
    ProcessEvidence,
    RepositorySnapshot,
    ReliabilityFixture,
    ReliabilityRunSummary,
    RequiredCase,
    finalize_run,
    validate_case,
    write_atomic_json,
)


def _fixture_document() -> dict[str, object]:
    return {
        "version": 1,
        "base_urls": {"tts_more": "http://127.0.0.1:8000", "comfyui": "http://127.0.0.1:8188"},
        "resources": {
            "gpt-sovits": {"resource_id": "gpt-main", "reference_audio": "fixtures/gpt.wav", "reference_text": "gpt"},
            "indextts": {"resource_id": "index-main", "reference_audio": "fixtures/index.wav", "reference_text": "index"},
            "cosyvoice": {"resource_id": "cosy-main", "reference_audio": "fixtures/cosy.wav", "reference_text": "cosy"},
        },
        "rounds": 10,
    }


def _case(case_id: str, engine: str, *, phase: str = "steady", expected: str = "completed", actual: str = "completed", cleanup_ok: bool = True, audio: AudioProof | None = None) -> CaseEvidence:
    started_at = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    finished_at = started_at + timedelta(seconds=10)
    if actual == "completed" and audio is None:
        audio = AudioProof(
            sha256="e" * 64,
            size_bytes=1600,
            sample_rate=16000,
            frames=800,
            peak=0.25,
        )
    tts_more = None
    if phase == "fault" and expected in {"cancelled", "timeout"} and actual == expected:
        terminal_status = "cancelled" if expected == "cancelled" else "failed"
        control_code = "cancelled" if expected == "cancelled" else "timeout"
        prompt_id = f"prompt-{case_id}"
        tts_more = reliability_validation.TtsTerminalEvidence(
            job_status=terminal_status,
            item_status=terminal_status,
            version_status=terminal_status,
            manifest_version_absent=False,
            version_audio_absent=True,
            control=reliability_validation.FaultControlEvidence(
                control_code=control_code,
                failure_stage=None if expected == "cancelled" else "timeout",
                prompt_id=prompt_id,
                initial_state="running",
                final_state="interrupted",
                actions=["interrupt"],
                duration_seconds=0.5,
                converged=True,
            ),
        )
    return CaseEvidence(
        case_id=case_id,
        phase=phase,
        engine=engine,
        expected=expected,
        actual=actual,
        job_id=f"job-{case_id}",
        prompt_id=f"prompt-{case_id}",
        version_id=f"v-{case_id}",
        started_at=started_at,
        finished_at=finished_at,
        audio=audio,
        tts_more=tts_more,
        cleanup=CleanupEvidence(ok=cleanup_ok, owned_processes_stopped=True, temp_paths_removed=True),
        processes=[ProcessEvidence(pid=123, ownership="validator-owned", command_hash="a" * 64, creation_time=started_at + timedelta(seconds=2), parent_pid=1, parent_creation_time=started_at + timedelta(seconds=1), stopped_at=finished_at - timedelta(seconds=1), executable_name="python.exe", executable_hash="a" * 64, ownership_hash="b" * 64, started=True, stopped=True, descendants_stopped=True, alive_after=False)],
        comfyui=ComfyQueueEvidence(queue_empty=True, history_present=True, prompt_id=f"prompt-{case_id}", queue_before_prompt_ids=[f"prompt-{case_id}"], queue_after_prompt_ids=[], history_prompt_ids=[f"prompt-{case_id}"], terminal_history_status=actual),
        gpu_before=GpuSnapshot(used_mib=1, free_mib=2),
        gpu_peak=GpuSnapshot(used_mib=2, free_mib=1),
        gpu_after=GpuSnapshot(used_mib=1, free_mib=2),
        boundary=BoundaryEvidence(
            before_hash="b" * 64,
            after_hash="b" * 64,
            private_registry_hash="c" * 64,
            reference_hashes={"reference": "d" * 64},
            repositories_before=[RepositorySnapshot(label=label, head="a" * 40, branch="feature", porcelain_hash="f" * 64) for label in ("tts-more", "tts-audio-suite", "comfyui", "gpt-sovits", "indextts", "cosyvoice")],
            repositories_after=[RepositorySnapshot(label=label, head="a" * 40, branch="feature", porcelain_hash="f" * 64) for label in ("tts-more", "tts-audio-suite", "comfyui", "gpt-sovits", "indextts", "cosyvoice")],
            private_registry_before_hash="c" * 64,
            private_registry_after_hash="c" * 64,
            reference_hashes_before={"reference": "d" * 64},
            reference_hashes_after={"reference": "d" * 64},
        ),
    )


def _steady_cases() -> list[CaseEvidence]:
    return [
        _case(f"steady-{engine}-{index:02d}", engine)
        for engine in ("gpt-sovits", "indextts", "cosyvoice")
        for index in range(1, 11)
    ]


def _required_cases() -> list[RequiredCase]:
    return [
        RequiredCase(case_id="cancel-index", engine="indextts", phase="fault", expected="cancelled"),
        RequiredCase(case_id="restart-cosy", engine="cosyvoice", phase="recovery", expected="completed"),
    ]


def _complete_cases() -> list[CaseEvidence]:
    return _steady_cases() + [
        _case("cancel-index", "indextts", phase="fault", expected="cancelled", actual="cancelled"),
        _case("restart-cosy", "cosyvoice", phase="recovery"),
    ]


def _write_voiced_wav(path: Path) -> None:
    frames = [int(8_000 * math.sin(index / 8)) for index in range(800)]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"".join(struct.pack("<h", frame) for frame in frames))


def _comfy_terminal_entry(outcome: str) -> dict[str, object]:
    if outcome == "completed":
        return {
            "outputs": {"9": {"audio": [{"filename": "result.wav"}]}},
            "status": {"status_str": "success", "completed": True, "messages": []},
        }
    return {
        "outputs": {},
        "status": {
            "status_str": "error",
            "completed": False,
            "messages": [["execution_interrupted", {"prompt_id": "prompt"}]],
        },
    }


def _assert_scrubbed_atomic_error(error: BaseException, target: Path) -> None:
    message = str(error)
    assert "atomic evidence" in message
    assert "injected-sentinel" not in message
    assert str(target.parent) not in message


def _atomic_artifacts(target: Path, suffix: str) -> list[Path]:
    return sorted(target.parent.glob(f".{target.name}.*.{suffix}"))


_REGISTERED_SERVICE_CAPABILITIES = [
    "tts",
    "reference_audio_voice",
    "wav_output",
    "comfyui",
    "tts-audio-suite",
]


def _registered_service_document(
    fixture: ReliabilityFixture,
    engine: str,
    *,
    service_id: str | None = None,
) -> dict[str, object]:
    return {
        "service_id": service_id or f"derived-{engine}",
        "service_kind": "tts",
        "engine": engine,
        "provider_type": engine,
        "api_contract": "comfyui-tts-audio-suite-v1",
        "base_url": fixture.base_urls["comfyui"] + "/",
        "enabled": True,
        "ready": True,
        "resource_group": "comfyui-local-0",
        "capacity": 1,
        "capabilities": list(_REGISTERED_SERVICE_CAPABILITIES),
        "default_params": {
            "engine": engine,
            "resource_id": fixture.resources[engine].resource_id,
        },
    }


def _registered_service_transport(
    fixture: ReliabilityFixture,
    services: list[dict[str, object]],
    calls: list[tuple[str, str, object | None]],
    *,
    generation_preflight_document: dict[str, object] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {"cuda": True}})
        if request.url.path == "/object_info":
            return httpx.Response(200, json={"TTSExternalEngine": {}})
        if request.url.path == "/api/tts-audio-suite/v1/capabilities":
            bridge_engines = {
                "gpt-sovits": "gpt_sovits",
                "indextts": "index_tts",
                "cosyvoice": "cosyvoice",
            }
            return httpx.Response(
                200,
                json={
                    "protocol_version": 1,
                    "resources": [
                        {
                            "engine": bridge_engines[engine],
                            "resource_id": resource.resource_id,
                            "ready": True,
                        }
                        for engine, resource in fixture.resources.items()
                    ],
                },
            )
        if request.url.path == "/api/services":
            return httpx.Response(200, json={"services": services})
        if request.url.path == "/api/generation/preflight":
            return httpx.Response(
                200,
                json=generation_preflight_document
                or {"status": "ready", "items": [{"status": "ready"}]},
            )
        if request.url.path == "/api/queue/status":
            return httpx.Response(200, json={"jobs": [], "queued": 0, "running": 0})
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _preflight_http_probe_for_case(
    probe: reliability_validation.HttpReliabilityProbe,
    fixture: ReliabilityFixture,
) -> None:
    case_transport = probe.transport
    calls: list[tuple[str, str, object | None]] = []
    try:
        probe.transport = _registered_service_transport(
            fixture,
            [
                _registered_service_document(fixture, engine)
                for engine in reliability_validation.ENGINE_ORDER
            ],
            calls,
        )
        probe.preflight(fixture)
    finally:
        probe.transport = case_transport


def test_fixture_models_reject_extra_and_bool_like_rounds() -> None:
    document = _fixture_document()
    document["rounds"] = True
    with pytest.raises(ValidationError):
        ReliabilityFixture.model_validate(document)


def test_case_models_reject_private_path_or_secret_like_identifiers() -> None:
    with pytest.raises(ValidationError):
        CaseEvidence.model_validate({
            **_case("steady-gpt-01", "gpt-sovits").model_dump(),
            "job_id": "token=private-value",
        })
    document = _fixture_document()
    document["unexpected"] = "no"
    with pytest.raises(ValidationError):
        ReliabilityFixture.model_validate(document)


def test_validate_case_records_non_silent_wav_without_private_path(tmp_path: Path) -> None:
    audio_path = tmp_path / "private" / "voice.wav"
    audio_path.parent.mkdir()
    _write_voiced_wav(audio_path)
    result = validate_case(_case("steady-gpt-01", "gpt-sovits"), wav_path=audio_path)
    assert result.valid is True
    assert result.evidence.audio is not None
    assert result.evidence.audio.sha256
    assert str(audio_path) not in result.evidence.model_dump_json()


def test_validate_case_rejects_per_repository_or_registry_boundary_drift() -> None:
    case = _case("steady-gpt-01", "gpt-sovits")
    stable = RepositorySnapshot(label="tts-more", head="a" * 40, branch="feature", porcelain_hash="f" * 64)
    drifted = RepositorySnapshot(label="tts-more", head="b" * 40, branch="feature", porcelain_hash="f" * 64)
    case = case.model_copy(update={
        "boundary": case.boundary.model_copy(update={
            "repositories_before": [stable],
            "repositories_after": [drifted],
            "private_registry_before_hash": "c" * 64,
            "private_registry_after_hash": "e" * 64,
            "reference_hashes_before": {"gpt": "d" * 64},
            "reference_hashes_after": {"gpt": "d" * 64},
        }),
    })
    result = validate_case(case)
    assert result.valid is False
    assert "repository/model/private-registry boundary drift detected" in result.diagnostics


def test_validate_case_fails_closed_without_detailed_boundary_observations() -> None:
    case = _case("steady-gpt-01", "gpt-sovits")
    case = case.model_copy(update={
        "boundary": BoundaryEvidence(
            before_hash="b" * 64,
            after_hash="b" * 64,
            private_registry_hash="c" * 64,
            reference_hashes={"reference": "d" * 64},
        ),
    })
    result = validate_case(case)
    assert result.valid is False
    assert "boundary observations are incomplete" in result.diagnostics


def test_finalize_run_requires_exact_successful_steady_matrix_and_named_cases() -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    cases = _steady_cases()
    cases.extend([
        _case("cancel-index", "indextts", phase="fault", expected="cancelled", actual="cancelled"),
        _case("restart-cosy", "cosyvoice", phase="recovery"),
    ])
    required = _required_cases()
    passed = finalize_run(fixture, cases, required_cases=required)
    assert passed.status == "passed"

    duplicate = finalize_run(fixture, cases + [_case("steady-gpt-sovits-01", "gpt-sovits")], required_cases=required)
    assert duplicate.status == "failed"
    assert duplicate.duplicate_case_ids == ["steady-gpt-sovits-01"]

    failed = finalize_run(fixture, [_case("steady-gpt-01", "gpt-sovits"), _case("cancel-index", "indextts", phase="fault", expected="cancelled", actual="cancelled", cleanup_ok=False)], required_cases=required)
    assert failed.status == "failed"
    assert failed.missing_cases == ["restart-cosy"]
    assert failed.cleanup_failures == ["cancel-index"]


def test_atomic_existing_publish_replace_failure_never_moves_live_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    original = b'{"status":"passed"}\n'
    target.write_bytes(original)
    unrelated = tmp_path / ".summary.json.unrelated.bak"
    unrelated.write_bytes(b"unrelated")
    replace_calls: list[tuple[Path, Path]] = []

    def fail_replace(source: object, destination: object) -> None:
        replace_calls.append((Path(source), Path(destination)))
        raise OSError(f"injected-sentinel replace failure at {target}")

    monkeypatch.setattr(os, "link", lambda *_args, **_kwargs: pytest.fail("existing write must not link"))
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError) as exc_info:
        write_atomic_json(target, {"status": "failed"})
    _assert_scrubbed_atomic_error(exc_info.value, target)
    assert len(replace_calls) == 1
    assert replace_calls[0][0] != target
    assert replace_calls[0][1] == target
    assert target.read_bytes() == original
    assert _atomic_artifacts(target, "tmp") == []
    assert [path for path in _atomic_artifacts(target, "bak") if path != unrelated] == []
    assert unrelated.read_bytes() == b"unrelated"


def test_atomic_existing_dirsync_failure_restores_old_bytes_and_resyncs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    original = b'{"status":"passed"}\n'
    target.write_bytes(original)
    sync_calls = 0

    def fail_publish_sync(_directory: Path) -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 2:
            raise OSError(f"injected-sentinel dirsync failure at {target}")

    monkeypatch.setattr(reliability_validation, "_fsync_directory", fail_publish_sync)
    with pytest.raises(OSError) as exc_info:
        write_atomic_json(target, {"status": "failed"})
    _assert_scrubbed_atomic_error(exc_info.value, target)
    assert sync_calls == 3
    assert target.read_bytes() == original
    assert _atomic_artifacts(target, "tmp") == []
    assert _atomic_artifacts(target, "bak") == []


def test_atomic_existing_restore_failure_retains_byte_identical_last_good_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    original = b'{"status":"passed"}\n'
    target.write_bytes(original)
    real_replace = os.replace
    target_replace_calls = 0
    sync_calls = 0

    def fail_publish_sync(_directory: Path) -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 2:
            raise OSError(f"injected-sentinel dirsync failure at {target}")

    def fail_restore(source: object, destination: object) -> None:
        nonlocal target_replace_calls
        if Path(destination) == target:
            target_replace_calls += 1
            if target_replace_calls == 2:
                raise OSError(f"injected-sentinel restore failure at {target}")
        real_replace(source, destination)

    monkeypatch.setattr(reliability_validation, "_fsync_directory", fail_publish_sync)
    monkeypatch.setattr(os, "replace", fail_restore)
    with pytest.raises(OSError) as exc_info:
        write_atomic_json(target, {"status": "failed"})
    _assert_scrubbed_atomic_error(exc_info.value, target)
    assert "recovery incomplete" in str(exc_info.value)
    backups = _atomic_artifacts(target, "bak")
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "failed"}
    assert _atomic_artifacts(target, "tmp") == []


def test_atomic_committed_backup_cleanup_failure_returns_success_and_keeps_new_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    original = b'{"status":"passed"}\n'
    target.write_bytes(original)
    real_unlink = Path.unlink

    def fail_backup_cleanup(path: Path, missing_ok: bool = False) -> None:
        if path.suffix == ".bak":
            raise OSError(f"injected-sentinel cleanup failure at {target}")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_backup_cleanup)
    write_atomic_json(target, {"status": "failed"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "failed"}
    backups = _atomic_artifacts(target, "bak")
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert _atomic_artifacts(target, "tmp") == []


def test_atomic_first_write_link_failure_leaves_no_target_or_owned_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    unrelated = tmp_path / ".summary.json.unrelated.tmp"
    unrelated.write_bytes(b"unrelated")
    link_calls: list[tuple[Path, Path]] = []

    def fail_link(source: object, destination: object) -> None:
        link_calls.append((Path(source), Path(destination)))
        raise OSError(f"injected-sentinel link failure at {target}")

    monkeypatch.setattr(os, "link", fail_link)
    monkeypatch.setattr(os, "replace", lambda *_args, **_kwargs: pytest.fail("first write must not replace"))
    with pytest.raises(OSError) as exc_info:
        write_atomic_json(target, {"status": "passed"})
    _assert_scrubbed_atomic_error(exc_info.value, target)
    assert len(link_calls) == 1
    assert link_calls[0][0].parent == target.parent
    assert link_calls[0][1] == target
    assert target.exists() is False
    assert [path for path in _atomic_artifacts(target, "tmp") if path != unrelated] == []
    assert _atomic_artifacts(target, "bak") == []
    assert unrelated.read_bytes() == b"unrelated"


def test_fix_round_3_atomic_first_write_never_clobbers_a_concurrent_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    racing_bytes = b'{"status":"racing-writer"}\n'
    real_link = os.link
    link_calls: list[tuple[Path, Path]] = []

    def install_racer_then_link(source: object, destination: object) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        link_calls.append((source_path, destination_path))
        destination_path.write_bytes(racing_bytes)
        real_link(source_path, destination_path)

    monkeypatch.setattr(os, "link", install_racer_then_link)
    monkeypatch.setattr(os, "replace", lambda *_args, **_kwargs: pytest.fail("first write must not replace"))

    with pytest.raises(OSError) as exc_info:
        write_atomic_json(target, {"status": "passed"})

    _assert_scrubbed_atomic_error(exc_info.value, target)
    assert "publication conflict" in str(exc_info.value)
    assert len(link_calls) == 1
    assert link_calls[0][0].parent == target.parent
    assert link_calls[0][1] == target
    assert target.read_bytes() == racing_bytes
    assert _atomic_artifacts(target, "tmp") == []
    assert _atomic_artifacts(target, "bak") == []


def test_fix_round_3_atomic_first_write_fsyncs_before_link_then_syncs_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    real_fsync = os.fsync
    real_link = os.link
    events: list[str] = []
    link_paths: list[tuple[Path, Path]] = []

    def record_fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    def record_link(source: object, destination: object) -> None:
        events.append("link")
        source_path = Path(source)
        destination_path = Path(destination)
        link_paths.append((source_path, destination_path))
        real_link(source_path, destination_path)

    def record_directory_sync(_directory: Path) -> None:
        events.append("dirsync")

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "link", record_link)
    monkeypatch.setattr(os, "replace", lambda *_args, **_kwargs: pytest.fail("first write must not replace"))
    monkeypatch.setattr(reliability_validation, "_fsync_directory", record_directory_sync)

    write_atomic_json(target, {"status": "passed"})

    assert events.index("fsync") < events.index("link") < events.index("dirsync")
    assert link_paths[0][0].parent == link_paths[0][1].parent == target.parent
    assert link_paths[0][1] == target
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "passed"}
    assert _atomic_artifacts(target, "tmp") == []


def test_fix_round_3_atomic_first_write_tolerates_post_link_temp_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    real_unlink = Path.unlink

    def retain_published_temp(path: Path, missing_ok: bool = False) -> None:
        if path.suffix == ".tmp":
            raise PermissionError(f"injected-sentinel cleanup failure at {target}")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", retain_published_temp)
    monkeypatch.setattr(os, "replace", lambda *_args, **_kwargs: pytest.fail("first write must not replace"))

    write_atomic_json(target, {"status": "passed"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "passed"}
    retained = _atomic_artifacts(target, "tmp")
    assert len(retained) == 1
    assert retained[0].read_bytes() == target.read_bytes()


def test_atomic_first_write_dirsync_failure_retains_complete_target_when_removal_is_unproven(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    sync_calls = 0

    def fail_first_sync(_directory: Path) -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            raise OSError(f"injected-sentinel dirsync failure at {target}")

    monkeypatch.setattr(reliability_validation, "_fsync_directory", fail_first_sync)
    with pytest.raises(OSError) as exc_info:
        write_atomic_json(target, {"status": "passed"})
    _assert_scrubbed_atomic_error(exc_info.value, target)
    assert "durability unconfirmed" in str(exc_info.value)
    assert sync_calls == 1
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "passed"}
    assert _atomic_artifacts(target, "tmp") == []
    assert _atomic_artifacts(target, "bak") == []


def test_atomic_first_write_dirsync_failure_never_deletes_a_racing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    racing_bytes = b'{"status":"racing-writer"}\n'

    def install_racing_target_then_fail(_directory: Path) -> None:
        target.write_bytes(racing_bytes)
        raise OSError(f"injected-sentinel dirsync failure at {target}")

    monkeypatch.setattr(reliability_validation, "_fsync_directory", install_racing_target_then_fail)
    with pytest.raises(OSError) as exc_info:
        write_atomic_json(target, {"status": "passed"})
    _assert_scrubbed_atomic_error(exc_info.value, target)
    assert "durability unconfirmed" in str(exc_info.value)
    assert target.read_bytes() == racing_bytes
    assert _atomic_artifacts(target, "bak") == []
    assert _atomic_artifacts(target, "tmp") == []


def test_atomic_existing_write_fsyncs_unique_backup_before_single_target_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    original = b'{"status":"passed"}\n'
    target.write_bytes(original)
    real_fsync = os.fsync
    real_replace = os.replace
    events: list[str] = []
    replace_destinations: list[Path] = []
    live_bytes_at_publish: list[bytes] = []

    def record_fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    def record_replace(source: object, destination: object) -> None:
        events.append("replace")
        destination_path = Path(destination)
        replace_destinations.append(destination_path)
        if destination_path == target:
            live_bytes_at_publish.append(target.read_bytes())
        real_replace(source, destination)

    def record_directory_sync(_directory: Path) -> None:
        events.append("dirsync")

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "replace", record_replace)
    monkeypatch.setattr(reliability_validation, "_fsync_directory", record_directory_sync)
    write_atomic_json(target, {"status": "failed"})
    assert events.index("replace") >= 2
    assert events.index("dirsync") < events.index("replace")
    assert replace_destinations == [target]
    assert live_bytes_at_publish == [original]
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "failed"}
    assert _atomic_artifacts(target, "tmp") == []
    assert _atomic_artifacts(target, "bak") == []


def test_atomic_backup_names_are_unique_and_unrelated_files_are_never_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    initial = b'{"generation":0}\n'
    target.write_bytes(initial)
    unrelated = tmp_path / ".summary.json.unrelated.bak"
    unrelated.write_bytes(b"unrelated")
    real_unlink = Path.unlink

    def retain_backups(path: Path, missing_ok: bool = False) -> None:
        if path.suffix == ".bak" and path != unrelated:
            raise OSError("injected-sentinel retained backup")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", retain_backups)
    write_atomic_json(target, {"generation": 1})
    generation_one = target.read_bytes()
    write_atomic_json(target, {"generation": 2})

    backups = [path for path in _atomic_artifacts(target, "bak") if path != unrelated]
    assert len(backups) == 2
    assert len({path.name for path in backups}) == 2
    assert {path.read_bytes() for path in backups} == {initial, generation_one}
    assert unrelated.read_bytes() == b"unrelated"
    assert json.loads(target.read_text(encoding="utf-8")) == {"generation": 2}


def test_fix_round_1_required_case_contract_rejects_steady_recovery_and_extra_steady_case() -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    cases = _steady_cases()
    cases.append(_case("recover-cosy", "cosyvoice", phase="steady"))
    summary = finalize_run(
        fixture,
        cases,
        required_cases=[RequiredCase(case_id="recover-cosy", engine="cosyvoice", phase="recovery", expected="completed")],
    )
    assert summary.status == "failed"
    assert "required case recover-cosy has wrong phase" in summary.validation_failures

    extra = finalize_run(
        fixture,
        cases + [_case("steady-gpt-extra", "gpt-sovits", expected="failed", actual="failed", audio=None)],
        required_cases=[],
    )
    assert extra.status == "failed"
    assert "steady gpt-sovits count is 11, expected 10" in extra.validation_failures


def test_fix_round_1_raw_payload_safety_rejects_nested_private_values(tmp_path: Path) -> None:
    target = tmp_path / "summary.json"
    with pytest.raises(ValueError, match="unsafe evidence") as exc_info:
        write_atomic_json(target, {"nested": [r"C:\Users\private", {"authorization": "Bearer private-token"}]})
    assert "private-token" not in str(exc_info.value)
    assert target.exists() is False


@pytest.mark.parametrize(
    "legacy",
    [
        {"cancel-index"},
        {"cancel-index": {"phase": "fault", "expected": "cancelled"}},
        "cancel-index",
        [object()],
    ],
)
def test_fix_round_2_required_cases_reject_legacy_or_malformed_collections(legacy: object) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    with pytest.raises(ValueError, match="ordered RequiredCase sequence") as exc_info:
        finalize_run(fixture, _steady_cases(), required_cases=legacy)  # type: ignore[arg-type]
    assert not isinstance(exc_info.value, AttributeError)


def test_fix_round_2_required_case_contract_binds_engine_phase_and_outcome() -> None:
    with pytest.raises(ValidationError):
        RequiredCase(case_id="bad-recovery", engine="cosyvoice", phase="recovery", expected="failed")
    with pytest.raises(ValidationError):
        RequiredCase(case_id="bad-fault", engine="indextts", phase="fault", expected="completed")

    required = _required_cases()
    with pytest.raises(ValueError, match="duplicate required case"):
        finalize_run(
            ReliabilityFixture.model_validate(_fixture_document()),
            _steady_cases(),
            required_cases=[required[0], required[0]],
        )

    cases = _steady_cases() + [
        _case("cancel-index", "cosyvoice", phase="fault", expected="cancelled", actual="cancelled"),
        _case("restart-cosy", "cosyvoice", phase="recovery"),
    ]
    summary = finalize_run(
        ReliabilityFixture.model_validate(_fixture_document()),
        cases,
        required_cases=required,
    )
    assert summary.status == "failed"
    assert "required case cancel-index has wrong engine" in summary.validation_failures


@pytest.mark.parametrize(
    "payload",
    [
        {"token": "raw-token-sentinel"},
        {"nested": {"client_secret": "raw-client-secret-sentinel"}},
        {"nested": {"password": "raw-password-sentinel"}},
        {"nested": {"api_key": "raw-api-key-sentinel"}},
        {"nested": {"authorization": "raw-authorization-sentinel"}},
        {"nested": {"private": "raw-private-sentinel"}},
        {"nested": {"access_key": "raw-access-key-sentinel"}},
        {"note": "request Bearer raw-bearer-sentinel"},
        {"note": "request token=raw-token-sentinel"},
        {"note": "request password=raw-password-sentinel"},
        {"note": r"failure at C:\Users\private\model"},
        {"note": r"failure at \\server\share\model"},
        {"note": "failure at file:///C:/private/model"},
        {"note": "failure at /opt/private/model"},
    ],
)
def test_fix_round_2_public_evidence_rejects_contextual_secrets_and_embedded_paths(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    target = tmp_path / "summary.json"
    with pytest.raises(ValueError, match="^unsafe evidence$") as exc_info:
        write_atomic_json(target, payload)
    assert "sentinel" not in str(exc_info.value)
    assert target.exists() is False


def test_fix_round_2_public_evidence_allows_neutral_authorization_and_relative_labels(tmp_path: Path) -> None:
    target = tmp_path / "summary.json"
    payload = {
        "note": "authorization overview",
        "authorization": "",
        "relative": "fixtures/reference.wav",
        "sha256": "a" * 64,
        "private_registry_hash": "b" * 64,
    }
    write_atomic_json(target, payload)
    assert json.loads(target.read_text(encoding="utf-8")) == payload


@pytest.mark.parametrize(
    "uri",
    [
        "https://public-user:uri-secret-sentinel@example.invalid/path",
        "ssh://public-user:uri-secret-sentinel@example.invalid/repository",
        "custom+tls://public-user:uri-secret-sentinel@example.invalid/resource",
    ],
)
def test_fix_round_3_public_evidence_rejects_uri_userinfo_credentials_without_echo(
    tmp_path: Path,
    uri: str,
) -> None:
    target = tmp_path / "summary.json"
    with pytest.raises(ValueError, match="unsafe evidence") as exc_info:
        write_atomic_json(target, {"nested": [{"endpoint": uri}]})
    assert "uri-secret-sentinel" not in str(exc_info.value)
    assert uri not in str(exc_info.value)
    assert target.exists() is False


@pytest.mark.parametrize(
    "value",
    [
        "https://public-user@example.invalid/path",
        "https://public-user%3Auri-secret-sentinel@example.invalid/path",
        "https://public-user%40uri-secret-sentinel@example.invalid/path",
        "https://public-user%3Auri-secret-sentinel%40example.invalid/path",
        "https://public-user:uri%2Fsecret-sentinel@example.invalid/path",
        "//public-user:uri-secret-sentinel@example.invalid/path",
        "diagnostic: request failed at https://public-user:uri-secret-sentinel@example.invalid/path after retry",
        "diagnostic: request failed at //[public-user%3Auri-secret-sentinel%40example.invalid]/path",
        "https://public-user:uri-secret-sentinel@[2001:db8::1]/path",
    ],
)
def test_fix_round_4_public_evidence_rejects_any_uri_authority_userinfo_without_echo(
    tmp_path: Path,
    value: str,
) -> None:
    target = tmp_path / "summary.json"

    with pytest.raises(ValueError, match="^unsafe evidence$") as exc_info:
        write_atomic_json(target, {"diagnostic": value})

    assert "uri-secret-sentinel" not in str(exc_info.value)
    assert value not in str(exc_info.value)
    assert target.exists() is False


@pytest.mark.parametrize(
    "value",
    [
        "public-user@example.invalid",
        "https://example.invalid/path",
        "https://example.invalid/users/public-user@example.invalid",
        "https://example.invalid/path?email=public-user@example.invalid",
        "https://example.invalid/path//public-user@example.invalid/resource",
        "https://example.invalid/path?next=//public-user@example.invalid/resource",
        "//example.invalid/path",
        "https://[2001:db8::1]/path",
        "diagnostic: ordinary email public-user@example.invalid after retry",
    ],
)
def test_fix_round_4_public_evidence_allows_email_and_uri_without_authority_userinfo(
    tmp_path: Path,
    value: str,
) -> None:
    target = tmp_path / "summary.json"

    write_atomic_json(target, {"diagnostic": value})

    assert json.loads(target.read_text(encoding="utf-8")) == {"diagnostic": value}


@pytest.mark.parametrize("container_kind", ["dict", "list"])
def test_fix_round_4_public_evidence_rejects_self_referential_containers_without_echo(
    tmp_path: Path,
    container_kind: str,
) -> None:
    target = tmp_path / "summary.json"
    sentinel = "cycle-private-sentinel"
    if container_kind == "dict":
        payload: object = {"label": sentinel}
        payload["self"] = payload  # type: ignore[index]
    else:
        payload = [sentinel]
        payload.append(payload)  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="^unsafe evidence$") as exc_info:
        write_atomic_json(target, {"payload": payload})

    assert sentinel not in str(exc_info.value)
    assert target.exists() is False


def test_fix_round_4_public_evidence_rejects_mutually_recursive_dict_and_list_without_echo(
    tmp_path: Path,
) -> None:
    target = tmp_path / "summary.json"
    sentinel = "mutual-cycle-private-sentinel"
    mapping: dict[str, object] = {"label": sentinel}
    sequence: list[object] = [mapping]
    mapping["sequence"] = sequence

    with pytest.raises(ValueError, match="^unsafe evidence$") as exc_info:
        write_atomic_json(target, {"payload": mapping})

    assert sentinel not in str(exc_info.value)
    assert target.exists() is False


def test_fix_round_4_public_evidence_allows_repeated_shared_noncyclic_values(tmp_path: Path) -> None:
    target = tmp_path / "summary.json"
    shared = {"label": "shared-neutral-value"}
    payload = {"first": shared, "second": shared}

    write_atomic_json(target, payload)

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "first": {"label": "shared-neutral-value"},
        "second": {"label": "shared-neutral-value"},
    }


def test_fix_round_3_model_copy_secret_is_scanned_before_serializer_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    cases = _steady_cases() + [
        _case("cancel-index", "indextts", phase="fault", expected="cancelled", actual="cancelled"),
        _case("restart-cosy", "cosyvoice", phase="recovery"),
    ]
    summary = finalize_run(fixture, cases, required_cases=_required_cases())
    secret = "https://public-user:model-copy-secret-sentinel@example.invalid/path"
    poisoned_case = summary.cases[0].model_copy(update={"job_id": [secret]})
    poisoned_summary = summary.model_copy(update={"cases": [poisoned_case, *summary.cases[1:]]})
    target = tmp_path / "summary.json"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(ValueError, match="unsafe evidence") as exc_info:
            write_atomic_json(target, poisoned_summary)

    streams = capsys.readouterr()
    emitted = "\n".join(str(item.message) for item in caught)
    combined = "\n".join((str(exc_info.value), streams.out, streams.err, emitted))
    assert "model-copy-secret-sentinel" not in combined
    assert secret not in combined
    assert caught == []
    assert target.exists() is False


def test_fix_round_2_model_validation_redacts_nested_secret_values() -> None:
    document = _case("steady-gpt-01", "gpt-sovits").model_dump()
    document["boundary"]["reference_hashes"] = {"token": "model-secret-sentinel"}  # type: ignore[index]
    with pytest.raises(ValidationError, match="unsafe evidence") as exc_info:
        CaseEvidence.model_validate(document)
    assert "model-secret-sentinel" not in str(exc_info.value)


def test_fix_round_2_process_model_requires_utc_ordered_complete_observations() -> None:
    process = _case("steady-gpt-01", "gpt-sovits").processes[0].model_dump()
    process["creation_time"] = datetime(2026, 8, 1, 0, 0)
    with pytest.raises(ValidationError):
        ProcessEvidence.model_validate(process)

    process = _case("steady-gpt-01", "gpt-sovits").processes[0].model_dump()
    process["parent_pid"] = 0
    with pytest.raises(ValidationError):
        ProcessEvidence.model_validate(process)


def test_fix_round_2_validate_case_rechecks_process_identity_and_lifecycle() -> None:
    case = _case("steady-gpt-01", "gpt-sovits")
    process = case.processes[0]
    invalid_processes = [
        [process.model_copy(update={"started": False})],
        [process.model_copy(update={"parent_creation_time": process.creation_time + timedelta(seconds=1)})],
        [process.model_copy(update={"stopped_at": process.creation_time - timedelta(seconds=1)})],
        [process.model_copy(update={"parent_pid": 0})],
        [process.model_copy(update={"executable_name": r"C:\private\python.exe"})],
        [process.model_copy(update={"executable_hash": "not-a-hash"})],
        [process.model_copy(update={"ownership_hash": "not-a-hash"})],
        [process.model_copy(update={"ownership": "pre-existing"})],
        [process, process],
    ]
    for processes in invalid_processes:
        result = validate_case(case.model_copy(update={"processes": processes}))
        assert result.valid is False
        assert "process identity/lifecycle proof is incomplete" in result.diagnostics


def test_fix_round_2_validate_case_requires_exact_queue_history_observation() -> None:
    case = _case("steady-gpt-01", "gpt-sovits")
    prompt_id = case.prompt_id
    invalid_queues = [
        case.comfyui.model_copy(update={"queue_before_prompt_ids": []}),
        case.comfyui.model_copy(update={"queue_after_prompt_ids": [prompt_id]}),
        case.comfyui.model_copy(update={"history_prompt_ids": [prompt_id, prompt_id]}),
        case.comfyui.model_copy(update={"history_prompt_ids": []}),
        case.comfyui.model_copy(update={"terminal_history_status": "failed"}),
    ]
    for queue in invalid_queues:
        result = validate_case(case.model_copy(update={"comfyui": queue}))
        assert result.valid is False
        assert "ComfyUI queue/history proof is incomplete" in result.diagnostics


def test_fix_round_3_queue_empty_requires_globally_empty_after_snapshot() -> None:
    case = _case("steady-gpt-01", "gpt-sovits")
    queue_document = case.comfyui.model_dump()
    queue_document["queue_after_prompt_ids"] = ["unrelated-prompt"]
    with pytest.raises(ValidationError, match="queue/history proof"):
        ComfyQueueEvidence.model_validate(queue_document)

    contradictory = case.comfyui.model_copy(update={"queue_after_prompt_ids": ["unrelated-prompt"]})
    result = validate_case(case.model_copy(update={"comfyui": contradictory}))
    assert result.valid is False
    assert "ComfyUI queue/history proof is incomplete" in result.diagnostics


@pytest.mark.parametrize("field", ["queue_before_prompt_ids", "history_prompt_ids"])
def test_fix_round_4_queue_history_rejects_duplicate_unrelated_prompt_ids(field: str) -> None:
    case = _case("steady-gpt-01", "gpt-sovits")
    queue_document = case.comfyui.model_dump()
    queue_document[field] = [case.prompt_id, "unrelated-prompt", "unrelated-prompt"]

    with pytest.raises(ValidationError, match="queue/history proof"):
        ComfyQueueEvidence.model_validate(queue_document)

    duplicated = case.comfyui.model_copy(update={field: queue_document[field]})
    result = validate_case(case.model_copy(update={"comfyui": duplicated}))
    assert result.valid is False
    assert result.diagnostics == ["ComfyUI queue/history proof is incomplete"]


@pytest.mark.parametrize("field", ["queue_before_prompt_ids", "history_prompt_ids"])
def test_fix_round_4_queue_history_rejects_duplicate_target_prompt_ids(field: str) -> None:
    case = _case("steady-gpt-01", "gpt-sovits")
    queue_document = case.comfyui.model_dump()
    queue_document[field] = [case.prompt_id, case.prompt_id]

    with pytest.raises(ValidationError, match="queue/history proof"):
        ComfyQueueEvidence.model_validate(queue_document)

    duplicated = case.comfyui.model_copy(update={field: queue_document[field]})
    result = validate_case(case.model_copy(update={"comfyui": duplicated}))
    assert result.valid is False
    assert result.diagnostics == ["ComfyUI queue/history proof is incomplete"]


def test_fix_round_3_process_rejects_self_parent_identity() -> None:
    case = _case("steady-gpt-01", "gpt-sovits")
    process_document = case.processes[0].model_dump()
    process_document["parent_pid"] = process_document["pid"]
    with pytest.raises(ValidationError, match="parent"):
        ProcessEvidence.model_validate(process_document)

    self_parent = case.processes[0].model_copy(update={"parent_pid": case.processes[0].pid})
    result = validate_case(case.model_copy(update={"processes": [self_parent]}))
    assert result.valid is False
    assert "process identity/lifecycle proof is incomplete" in result.diagnostics


@pytest.mark.parametrize("second_created_seconds", [3, 4])
def test_fix_round_3_same_pid_reuse_rejects_overlapping_or_touching_lifetimes(
    second_created_seconds: int,
) -> None:
    case = _case("steady-gpt-01", "gpt-sovits")
    first = case.processes[0].model_dump()
    first["stopped_at"] = case.started_at + timedelta(seconds=4)
    second = {
        **first,
        "creation_time": case.started_at + timedelta(seconds=second_created_seconds),
        "stopped_at": case.started_at + timedelta(seconds=8),
    }
    document = case.model_dump()
    document["processes"] = [first, second]
    with pytest.raises(ValidationError, match="PID lifetimes"):
        CaseEvidence.model_validate(document)


def test_fix_round_3_validate_case_rechecks_same_pid_lifetime_overlap() -> None:
    case = _case("steady-gpt-01", "gpt-sovits")
    first = case.processes[0].model_copy(update={"stopped_at": case.started_at + timedelta(seconds=6)})
    second = case.processes[0].model_copy(
        update={
            "creation_time": case.started_at + timedelta(seconds=5),
            "stopped_at": case.started_at + timedelta(seconds=8),
        }
    )
    result = validate_case(case.model_copy(update={"processes": [first, second]}))
    assert result.valid is False
    assert "process identity/lifecycle proof is incomplete" in result.diagnostics


def test_fix_round_3_same_pid_reuse_allows_strictly_separated_lifetimes() -> None:
    case = _case("steady-gpt-01", "gpt-sovits")
    first = case.processes[0].model_dump()
    first["stopped_at"] = case.started_at + timedelta(seconds=4)
    second = {
        **first,
        "creation_time": case.started_at + timedelta(seconds=5),
        "stopped_at": case.started_at + timedelta(seconds=8),
    }
    document = case.model_dump()
    document["processes"] = [first, second]
    evidence = CaseEvidence.model_validate(document)
    assert validate_case(evidence).valid is True


@pytest.mark.parametrize(
    ("nested_field", "invalid_value"),
    [
        ("audio_peak", float("nan")),
        ("audio_peak", float("inf")),
        ("audio_size_bytes", "1600"),
        ("cleanup_ok", 1),
    ],
)
def test_fix_round_3_validate_case_revalidates_nested_model_copy_values(
    nested_field: str,
    invalid_value: object,
) -> None:
    case = _case("steady-gpt-01", "gpt-sovits")
    if nested_field == "cleanup_ok":
        poisoned = case.model_copy(update={"cleanup": case.cleanup.model_copy(update={"ok": invalid_value})})
    else:
        assert case.audio is not None
        audio_field = "peak" if nested_field == "audio_peak" else "size_bytes"
        poisoned = case.model_copy(
            update={"audio": case.audio.model_copy(update={audio_field: invalid_value})},
        )

    result = validate_case(poisoned)

    assert result.valid is False


@pytest.mark.parametrize("peak", [float("nan"), float("inf"), float("-inf")])
def test_fix_round_3_audio_proof_rejects_nonfinite_peak(peak: float) -> None:
    with pytest.raises(ValidationError):
        AudioProof(
            sha256="e" * 64,
            size_bytes=1600,
            sample_rate=16000,
            frames=800,
            peak=peak,
        )


def test_fix_round_3_finalize_revalidates_poisoned_nested_model_copy() -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    cases = _complete_cases()
    poisoned_cleanup = cases[0].cleanup.model_copy(update={"ok": 1})
    cases[0] = cases[0].model_copy(update={"cleanup": poisoned_cleanup})

    with pytest.raises(ValueError, match="invalid case evidence"):
        finalize_run(fixture, cases, required_cases=_required_cases())


def test_fix_round_3_write_revalidates_nested_model_copy_before_serializer_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    summary = finalize_run(fixture, _complete_cases(), required_cases=_required_cases())
    sentinel = "nested-invalid-type-sentinel"
    case = next(item for item in summary.cases if item.audio is not None)
    assert case.audio is not None
    poisoned_audio = case.audio.model_copy(update={"size_bytes": sentinel})
    poisoned_case = case.model_copy(update={"audio": poisoned_audio})
    poisoned_summary = summary.model_copy(
        update={"cases": [poisoned_case if item is case else item for item in summary.cases]},
    )
    target = tmp_path / "summary.json"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(ValueError) as exc_info:
            write_atomic_json(target, poisoned_summary)

    streams = capsys.readouterr()
    emitted = "\n".join(str(item.message) for item in caught)
    combined = "\n".join((str(exc_info.value), streams.out, streams.err, emitted))
    assert sentinel not in combined
    assert caught == []
    assert target.exists() is False


def test_fix_round_3_raw_json_rejects_nonfinite_numbers(tmp_path: Path) -> None:
    target = tmp_path / "summary.json"

    with pytest.raises(OSError, match="atomic evidence preparation failed"):
        write_atomic_json(target, {"peak": float("nan")})

    assert target.exists() is False
    assert _atomic_artifacts(target, "tmp") == []


def test_fix_round_3_passed_summary_requires_matching_required_case_specs() -> None:
    cases = sorted(
        _complete_cases(),
        key=lambda case: (case.case_id, case.engine, case.phase, case.expected, case.actual),
    )
    summary = ReliabilityRunSummary(
        status="passed",
        fixture_version=1,
        rounds=10,
        required_cases=list(reversed(_required_cases())),
        cases=cases,
        missing_cases=[],
        duplicate_case_ids=[],
        cleanup_failures=[],
        validation_failures=[],
        boundary_failures=[],
        steady_counts={engine: 10 for engine in ("gpt-sovits", "indextts", "cosyvoice")},
    )

    assert [required.case_id for required in summary.required_cases] == ["cancel-index", "restart-cosy"]


def test_fix_round_3_direct_steady_only_passed_summary_is_rejected() -> None:
    cases = sorted(
        _steady_cases(),
        key=lambda case: (case.case_id, case.engine, case.phase, case.expected, case.actual),
    )
    with pytest.raises(ValidationError, match="passed summary contains failed evidence"):
        ReliabilityRunSummary(
            status="passed",
            fixture_version=1,
            rounds=10,
            required_cases=[],
            cases=cases,
            missing_cases=[],
            duplicate_case_ids=[],
            cleanup_failures=[],
            validation_failures=[],
            boundary_failures=[],
            steady_counts={engine: 10 for engine in ("gpt-sovits", "indextts", "cosyvoice")},
        )


def test_fix_round_3_passed_summary_rejects_duplicate_required_specs() -> None:
    cases = sorted(
        _complete_cases(),
        key=lambda case: (case.case_id, case.engine, case.phase, case.expected, case.actual),
    )
    required = _required_cases()
    with pytest.raises(ValidationError, match="required case specifications"):
        ReliabilityRunSummary(
            status="passed",
            fixture_version=1,
            rounds=10,
            required_cases=[required[0], required[0], required[1]],
            cases=cases,
            missing_cases=[],
            duplicate_case_ids=[],
            cleanup_failures=[],
            validation_failures=[],
            boundary_failures=[],
            steady_counts={engine: 10 for engine in ("gpt-sovits", "indextts", "cosyvoice")},
        )


def test_fix_round_3_evidence_models_are_frozen() -> None:
    case = _case("steady-gpt-01", "gpt-sovits")

    with pytest.raises(ValidationError):
        case.case_id = "changed"  # type: ignore[misc]


def test_fix_round_2_validate_case_rechecks_gpu_observation_and_recovery() -> None:
    case = _case("steady-gpt-01", "gpt-sovits")
    invalid_snapshots = [
        {"gpu_before": case.gpu_before.model_copy(update={"used_mib": -1})},
        {"gpu_peak": case.gpu_peak.model_copy(update={"used_mib": 0})},
        {"gpu_after": case.gpu_after.model_copy(update={"used_mib": case.gpu_before.used_mib + 1025})},
    ]
    for update in invalid_snapshots:
        result = validate_case(case.model_copy(update=update))
        assert result.valid is False
        assert "GPU memory observation/recovery proof is incomplete" in result.diagnostics


def test_fix_round_2_boundary_and_summary_outputs_are_deterministic() -> None:
    case = _case("steady-gpt-01", "gpt-sovits")
    boundary = BoundaryEvidence.model_validate(
        {
            **case.boundary.model_dump(),
            "repositories_before": list(reversed(case.boundary.repositories_before)),
            "repositories_after": list(reversed(case.boundary.repositories_after)),
            "reference_hashes": {"z": "a" * 64, "a": "b" * 64},
            "reference_hashes_before": {"z": "a" * 64, "a": "b" * 64},
            "reference_hashes_after": {"z": "a" * 64, "a": "b" * 64},
        }
    )
    assert [item.label for item in boundary.repositories_before] == sorted(REPOSITORY_LABELS)
    assert list(boundary.reference_hashes) == ["a", "z"]

    fixture = ReliabilityFixture.model_validate(_fixture_document())
    cases = _steady_cases() + [
        _case("cancel-index", "indextts", phase="fault", expected="cancelled", actual="cancelled"),
        _case("restart-cosy", "cosyvoice", phase="recovery"),
    ]
    summary = finalize_run(fixture, list(reversed(cases)), required_cases=list(reversed(_required_cases())))
    assert summary.status == "passed"
    assert [item.case_id for item in summary.cases] == sorted(item.case_id for item in cases)


def test_fix_round_2_direct_passed_summary_cannot_bypass_validation() -> None:
    with pytest.raises(ValidationError, match="passed summary contains failed evidence"):
        ReliabilityRunSummary(
            status="passed",
            fixture_version=1,
            rounds=10,
            required_cases=[],
            cases=[],
            missing_cases=[],
            duplicate_case_ids=[],
            cleanup_failures=[],
            validation_failures=[],
            boundary_failures=[],
            steady_counts={},
        )


def test_task_10_scenario_plan_has_exact_order_ids_phases_and_deadlines() -> None:
    plan = reliability_validation.build_case_plan(rounds=10)
    steady = [case for case in plan if case.phase == "steady"]
    nonsteady = [case for case in plan if case.phase != "steady"]

    assert all(isinstance(case, reliability_validation.CasePlan) for case in plan)
    assert len(plan) == 47
    assert len({case.case_id for case in plan}) == 47
    assert [case.engine for case in steady] == ["gpt-sovits", "indextts", "cosyvoice"] * 10
    assert [case.case_id for case in nonsteady] == [
        "cancel-queued",
        "cancel-running-gpt-sovits",
        "recover-cancel-gpt-sovits",
        "cancel-running-indextts",
        "recover-cancel-indextts",
        "cancel-running-cosyvoice",
        "recover-cancel-cosyvoice",
        "timeout-gpt-sovits",
        "recover-timeout-gpt-sovits",
        "timeout-indextts",
        "recover-timeout-indextts",
        "timeout-cosyvoice",
        "recover-timeout-cosyvoice",
        "terminate-comfyui-indextts",
        "restart-gpt-sovits",
        "restart-indextts",
        "restart-cosyvoice",
    ]
    assert [
        (case.case_id, case.phase, case.expected)
        for case in nonsteady
        if case.case_id.startswith(("recover-", "restart-"))
    ] == [
        ("recover-cancel-gpt-sovits", "recovery", "completed"),
        ("recover-cancel-indextts", "recovery", "completed"),
        ("recover-cancel-cosyvoice", "recovery", "completed"),
        ("recover-timeout-gpt-sovits", "recovery", "completed"),
        ("recover-timeout-indextts", "recovery", "completed"),
        ("recover-timeout-cosyvoice", "recovery", "completed"),
        ("restart-gpt-sovits", "recovery", "completed"),
        ("restart-indextts", "recovery", "completed"),
        ("restart-cosyvoice", "recovery", "completed"),
    ]
    assert {case.convergence_seconds for case in steady} == {30.0}
    assert {
        case.request_timeout_seconds
        for case in plan
        if case.case_id.startswith("timeout-")
    } == {1.0}
    assert {
        case.convergence_seconds
        for case in nonsteady
        if case.phase == "recovery"
    } == {180.0}


def test_task_10_scenario_plan_builds_unique_required_case_specs() -> None:
    plan = reliability_validation.build_case_plan(rounds=10)

    required = reliability_validation.required_case_specs(plan)

    assert len(required) == 17
    assert len({case.case_id for case in required}) == 17
    assert all(isinstance(case, RequiredCase) for case in required)
    assert {
        (case.case_id, case.engine, case.phase, case.expected)
        for case in required
    } == {
        (case.case_id, case.engine, case.phase, case.expected)
        for case in plan
        if case.phase != "steady"
    }


def test_task_10_queued_cancel_uses_tts_terminal_proof_without_fabricated_prompt() -> None:
    document = _case(
        "queued-cancel-fixture",
        "gpt-sovits",
        phase="fault",
        expected="cancelled",
        actual="cancelled",
    ).model_dump()
    document.update(
        {
            "case_id": "cancel-queued",
            "prompt_submitted": False,
            "prompt_id": None,
            "version_id": None,
            "comfyui": None,
            "tts_more": {
                "job_status": "cancelled",
                "item_status": "cancelled",
                "version_status": None,
                "manifest_version_absent": True,
                "version_audio_absent": True,
                "control": None,
            },
        }
    )

    evidence = CaseEvidence.model_validate(document)
    validation = validate_case(evidence)

    assert validation.valid is True
    serialized = validation.evidence.model_dump(mode="json")
    assert serialized["prompt_submitted"] is False
    assert serialized["prompt_id"] is None
    assert serialized["comfyui"] is None


def test_task_10_queued_cancel_rejects_missing_tts_version_or_any_prompt_claim() -> None:
    base = _case(
        "queued-cancel-fixture",
        "gpt-sovits",
        phase="fault",
        expected="cancelled",
        actual="cancelled",
    ).model_dump()
    base["case_id"] = "cancel-queued"
    for update in (
        {
            "prompt_submitted": False,
            "prompt_id": None,
            "version_id": None,
            "comfyui": None,
            "tts_more": {"job_status": "cancelled", "item_status": "cancelled", "version_status": "failed", "manifest_version_absent": False, "version_audio_absent": True, "control": None},
        },
        {
            "prompt_submitted": True,
            "tts_more": {"job_status": "cancelled", "item_status": "cancelled", "version_status": None, "manifest_version_absent": True, "version_audio_absent": True, "control": None},
        },
    ):
        with pytest.raises(ValidationError, match="queue/history proof"):
            CaseEvidence.model_validate({**base, **update})


class _ExecutorHttpProbe:
    def __init__(self, *, preflight_mode: str = "ready") -> None:
        self.preflight_mode = preflight_mode
        self.executed: list[tuple[str, float, float]] = []
        self.released = False

    def preflight(
        self,
        fixture: ReliabilityFixture,
    ) -> reliability_validation.HttpPreflightObservation:
        resources = [
            reliability_validation.ReadyResource(
                engine=engine,
                resource_id=fixture.resources[engine].resource_id,
                ready=True,
            )
            for engine in ("gpt-sovits", "indextts", "cosyvoice")
        ]
        queue = reliability_validation.QueueSnapshot(
            tts_queued=0,
            tts_running=0,
            comfy_pending_prompt_ids=[],
            comfy_running_prompt_ids=[],
        )
        if self.preflight_mode == "missing-resource":
            resources.pop()
        elif self.preflight_mode == "busy-queue":
            queue = queue.model_copy(update={"tts_running": 1})
        return reliability_validation.HttpPreflightObservation(resources=resources, queue=queue)

    def execute_case(
        self,
        case: reliability_validation.CasePlan,
        fixture: ReliabilityFixture,
        output_directory: Path,
        *,
        action_hook: object | None = None,
    ) -> reliability_validation.HttpCaseObservation:
        del fixture
        self.executed.append(
            (case.case_id, case.request_timeout_seconds, case.convergence_seconds),
        )
        if callable(action_hook):
            action_hook()
        wav_path: Path | None = None
        if case.expected == "completed":
            wav_path = output_directory / f"{case.case_id}.wav"
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            _write_voiced_wav(wav_path)
        if case.action == "cancel-queued":
            return reliability_validation.HttpCaseObservation(
                actual="cancelled",
                job_id=f"job-{case.case_id}",
                prompt_id=None,
                version_id=None,
                wav_path=None,
                comfyui=None,
                prompt_submitted=False,
                tts_more=reliability_validation.TtsTerminalEvidence(
                    job_status="cancelled",
                    item_status="cancelled",
                    version_status=None,
                    manifest_version_absent=True,
                    version_audio_absent=True,
                ),
            )
        if case.action == "terminate-comfyui":
            prompt_id = f"prompt-{case.case_id}"
            return reliability_validation.HttpCaseObservation(
                actual="failed",
                job_id=f"job-{case.case_id}",
                prompt_id=prompt_id,
                version_id=f"version-{case.case_id}",
                wav_path=None,
                comfyui=None,
                tts_more=reliability_validation.TtsTerminalEvidence(
                    job_status="failed",
                    item_status="failed",
                    version_status="failed",
                    manifest_version_absent=False,
                    version_audio_absent=True,
                ),
                termination=reliability_validation.TerminationEvidence(
                    endpoint_unavailable=True,
                    prompt_id=prompt_id,
                    queue_before_prompt_ids=[prompt_id],
                    manifest_audio_absent=True,
                ),
            )
        prompt_id = f"prompt-{case.case_id}"
        tts_more = None
        if case.action in {"cancel-running", "timeout"}:
            terminal_status = "cancelled" if case.action == "cancel-running" else "failed"
            control_code = "cancelled" if case.action == "cancel-running" else "timeout"
            tts_more = reliability_validation.TtsTerminalEvidence(
                job_status=terminal_status,
                item_status=terminal_status,
                version_status=terminal_status,
                manifest_version_absent=False,
                version_audio_absent=True,
                control=reliability_validation.FaultControlEvidence(
                    control_code=control_code,
                    failure_stage=None if case.action == "cancel-running" else "timeout",
                    prompt_id=prompt_id,
                    initial_state="running",
                    final_state="interrupted",
                    actions=["interrupt"],
                    duration_seconds=0.5,
                    converged=True,
                ),
            )
        return reliability_validation.HttpCaseObservation(
            actual=case.expected,
            job_id=f"job-{case.case_id}",
            prompt_id=prompt_id,
            version_id=f"version-{case.case_id}",
            wav_path=wav_path,
            comfyui=ComfyQueueEvidence(
                queue_empty=True,
                history_present=True,
                prompt_id=prompt_id,
                queue_before_prompt_ids=[prompt_id],
                queue_after_prompt_ids=[],
                history_prompt_ids=[prompt_id],
                terminal_history_status=case.expected,
            ),
            tts_more=tts_more,
        )

    def release(self) -> None:
        self.released = True

    def final_state(self) -> reliability_validation.HttpFinalObservation:
        return reliability_validation.HttpFinalObservation(
            queue=reliability_validation.QueueSnapshot(
                tts_queued=0,
                tts_running=0,
                comfy_pending_prompt_ids=[],
                comfy_running_prompt_ids=[],
            ),
            runtime_released=self.released,
        )


class _ExecutorHostProbe:
    def __init__(self, *, preflight_mode: str = "ready") -> None:
        self.preflight_mode = preflight_mode
        self.terminated = 0
        self.restarted = 0
        self.case_number = 0
        self.finalized = False
        self.boundary = reliability_validation.BoundarySnapshot(
            aggregate_hash="b" * 64,
            private_registry_hash="c" * 64,
            reference_hashes={"reference": "d" * 64},
            repositories=[
                RepositorySnapshot(
                    label=label,
                    head="a" * 40,
                    branch="feature",
                    porcelain_hash="f" * 64,
                )
                for label in REPOSITORY_LABELS
            ],
        )
        created = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
        self.owned_processes = {
            "tts-more": reliability_validation.OwnedProcessIdentity(
                pid=8000,
                creation_time=created,
                executable_name="python.exe",
                ownership_hash="1" * 64,
            ),
            "comfyui": reliability_validation.OwnedProcessIdentity(
                pid=8188,
                creation_time=created,
                executable_name="python.exe",
                ownership_hash="2" * 64,
            ),
        }

    def preflight(
        self,
        fixture: ReliabilityFixture,
    ) -> reliability_validation.HostPreflightObservation:
        del fixture
        port_owners = {
            8000: self.owned_processes["tts-more"],
            8188: self.owned_processes["comfyui"],
        }
        if self.preflight_mode == "foreign-owner":
            port_owners[8188] = port_owners[8188].model_copy(update={"pid": 9999})
        return reliability_validation.HostPreflightObservation(
            port_owners=port_owners,
            boundary=self.boundary,
            gpu_idle_baseline=GpuSnapshot(used_mib=100, free_mib=8000),
        )

    def begin_case(self, case: reliability_validation.CasePlan) -> datetime:
        del case
        self.case_number += 1
        return datetime(2026, 8, 1, 0, self.case_number, tzinfo=timezone.utc)

    def finish_case(
        self,
        case: reliability_validation.CasePlan,
        started_at: datetime,
    ) -> reliability_validation.HostCaseObservation:
        finished_at = started_at + timedelta(seconds=10)
        process_started = started_at + timedelta(seconds=2)
        return reliability_validation.HostCaseObservation(
            started_at=started_at,
            finished_at=finished_at,
            cleanup=CleanupEvidence(
                ok=True,
                owned_processes_stopped=True,
                temp_paths_removed=True,
            ),
            processes=[
                ProcessEvidence(
                    pid=10_000 + self.case_number,
                    ownership="validator-owned",
                    command_hash="a" * 64,
                    creation_time=process_started,
                    parent_pid=8188,
                    parent_creation_time=started_at + timedelta(seconds=1),
                    stopped_at=finished_at - timedelta(seconds=1),
                    executable_name="python.exe",
                    executable_hash="a" * 64,
                    ownership_hash="b" * 64,
                    started=True,
                    stopped=True,
                    descendants_stopped=True,
                    alive_after=False,
                ),
            ],
            gpu_before=GpuSnapshot(used_mib=100, free_mib=8000),
            gpu_peak=GpuSnapshot(used_mib=300, free_mib=7800),
            gpu_after=GpuSnapshot(used_mib=100, free_mib=8000),
        )

    def terminate_comfyui(self) -> None:
        self.terminated += 1

    def restart_comfyui(self) -> None:
        self.restarted += 1

    def final_state(self) -> reliability_validation.HostFinalObservation:
        self.finalized = True
        return reliability_validation.HostFinalObservation(
            boundary=self.boundary,
            owned_processes_stopped=True,
            temp_paths_removed=True,
            gpu_after_release=GpuSnapshot(used_mib=100, free_mib=8000),
        )


def test_task_10_injected_executor_runs_exact_matrix_and_writes_public_evidence(
    tmp_path: Path,
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    http_probe = _ExecutorHttpProbe()
    host_probe = _ExecutorHostProbe()

    summary = reliability_validation.execute_reliability_validation(
        fixture,
        output_root=tmp_path / "evidence",
        http_probe=http_probe,
        host_probe=host_probe,
        owned_processes=host_probe.owned_processes,
    )

    assert summary.status == "passed"
    assert len(summary.cases) == 47
    assert len(http_probe.executed) == 47
    assert http_probe.executed[0][0] == "steady-01-gpt-sovits"
    assert http_probe.executed[-1] == ("restart-cosyvoice", 180.0, 180.0)
    assert host_probe.terminated == 1
    assert host_probe.restarted == 3
    assert http_probe.released is True
    assert host_probe.finalized is True
    evidence = (tmp_path / "evidence" / "reliability-summary.json").read_text(encoding="utf-8")
    assert json.loads(evidence)["status"] == "passed"
    assert str(tmp_path) not in evidence


def test_fix_round_1_final_gpu_compares_run_idle_baseline_after_runtime_release(
    tmp_path: Path,
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    events: list[str] = []

    class TrackingHttpProbe(_ExecutorHttpProbe):
        def release(self) -> None:
            events.append("runtime-release")
            super().release()

    class CumulativeLeakHostProbe(_ExecutorHostProbe):
        def preflight(
            self,
            fixture: ReliabilityFixture,
        ) -> reliability_validation.HostPreflightObservation:
            observation = super().preflight(fixture)
            return reliability_validation.HostPreflightObservation(
                port_owners=observation.port_owners,
                boundary=observation.boundary,
                gpu_idle_baseline=GpuSnapshot(used_mib=100, free_mib=8000),
            )

        def finish_case(
            self,
            case: reliability_validation.CasePlan,
            started_at: datetime,
        ) -> reliability_validation.HostCaseObservation:
            observation = super().finish_case(case, started_at)
            used_mib = 100 + self.case_number * 20
            return observation.model_copy(
                update={
                    "gpu_before": GpuSnapshot(used_mib=used_mib, free_mib=8100 - used_mib),
                    "gpu_peak": GpuSnapshot(used_mib=used_mib + 100, free_mib=8000 - used_mib),
                    "gpu_after": GpuSnapshot(used_mib=used_mib + 20, free_mib=8080 - used_mib),
                }
            )

        def final_state(self) -> reliability_validation.HostFinalObservation:
            events.append("gpu-after-release")
            observation = super().final_state()
            return reliability_validation.HostFinalObservation(
                boundary=observation.boundary,
                owned_processes_stopped=observation.owned_processes_stopped,
                temp_paths_removed=observation.temp_paths_removed,
                gpu_after_release=GpuSnapshot(used_mib=1500, free_mib=6600),
            )

    http_probe = TrackingHttpProbe()
    host_probe = CumulativeLeakHostProbe()
    output_root = tmp_path / "evidence"

    with pytest.raises(reliability_validation.LiveValidationError) as exc_info:
        reliability_validation.execute_reliability_validation(
            fixture,
            output_root=output_root,
            http_probe=http_probe,
            host_probe=host_probe,
            owned_processes=host_probe.owned_processes,
        )

    assert exc_info.value.code == "final-gpu-not-recovered"
    assert events == ["runtime-release", "gpu-after-release"]
    assert json.loads((output_root / "failure.json").read_text(encoding="utf-8")) == {
        "code": "final-gpu-not-recovered",
        "stage": "finalize",
    }


def test_task_10_passed_summary_is_published_only_after_all_case_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    http_probe = _ExecutorHttpProbe()
    host_probe = _ExecutorHostProbe()
    output_root = tmp_path / "evidence"
    original_write = reliability_validation.write_atomic_json

    def fail_case_write(path: Path, payload: object) -> None:
        if path.parent.name == "cases":
            raise OSError("injected case evidence failure")
        original_write(path, payload)

    monkeypatch.setattr(reliability_validation, "write_atomic_json", fail_case_write)

    with pytest.raises(OSError, match="injected case evidence failure"):
        reliability_validation.execute_reliability_validation(
            fixture,
            output_root=output_root,
            http_probe=http_probe,
            host_probe=host_probe,
            owned_processes=host_probe.owned_processes,
        )

    assert (output_root / "reliability-summary.json").exists() is False


def test_task_10_controller_always_finishes_active_host_case_after_http_failure(
    tmp_path: Path,
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())

    class FailingHttpProbe(_ExecutorHttpProbe):
        def execute_case(self, *args: object, **kwargs: object) -> reliability_validation.HttpCaseObservation:
            del args, kwargs
            raise httpx.ReadTimeout("injected case failure")

    class TrackingHostProbe(_ExecutorHostProbe):
        def __init__(self) -> None:
            super().__init__()
            self.finished_cases = 0

        def finish_case(
            self,
            case: reliability_validation.CasePlan,
            started_at: datetime,
        ) -> reliability_validation.HostCaseObservation:
            self.finished_cases += 1
            return super().finish_case(case, started_at)

    http_probe = FailingHttpProbe()
    host_probe = TrackingHostProbe()

    with pytest.raises(reliability_validation.LiveValidationError) as exc_info:
        reliability_validation.execute_reliability_validation(
            fixture,
            output_root=tmp_path / "evidence",
            http_probe=http_probe,
            host_probe=host_probe,
            owned_processes=host_probe.owned_processes,
        )

    assert exc_info.value.code == "case-execution-failed"
    assert host_probe.finished_cases == 1
    assert http_probe.released is True


def test_fix_round_1_case_failure_writes_current_scrubbed_case_before_failed_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    private_sentinel = f"token=case-secret {tmp_path}"

    class SecondCaseFailsHttpProbe(_ExecutorHttpProbe):
        def execute_case(
            self,
            case: reliability_validation.CasePlan,
            fixture: ReliabilityFixture,
            output_directory: Path,
            *,
            action_hook: object | None = None,
        ) -> reliability_validation.HttpCaseObservation:
            if len(self.executed) == 1:
                raise httpx.ReadTimeout(private_sentinel)
            return super().execute_case(
                case,
                fixture,
                output_directory,
                action_hook=action_hook,
            )

    writes: list[str] = []
    original_write = reliability_validation.write_atomic_json

    def track_write(path: Path, payload: object) -> None:
        writes.append(path.relative_to(output_root).as_posix())
        original_write(path, payload)

    http_probe = SecondCaseFailsHttpProbe()
    host_probe = _ExecutorHostProbe()
    output_root = tmp_path / "evidence"
    monkeypatch.setattr(reliability_validation, "write_atomic_json", track_write)

    with pytest.raises(reliability_validation.LiveValidationError) as exc_info:
        reliability_validation.execute_reliability_validation(
            fixture,
            output_root=output_root,
            http_probe=http_probe,
            host_probe=host_probe,
            owned_processes=host_probe.owned_processes,
        )

    assert exc_info.value.code == "case-execution-failed"
    first_case = json.loads(
        (output_root / "cases" / "steady-01-gpt-sovits.json").read_text(encoding="utf-8")
    )
    failed_case = json.loads(
        (output_root / "cases" / "steady-01-indextts.json").read_text(encoding="utf-8")
    )
    assert first_case["actual"] == "completed"
    assert failed_case["status"] == "failed"
    assert failed_case["case_id"] == "steady-01-indextts"
    assert failed_case["engine"] == "indextts"
    assert failed_case["expected"] == "completed"
    assert failed_case["failure"] == {"code": "case-execution-failed", "stage": "case"}
    assert failed_case["host"]["cleanup"] == {
        "ok": True,
        "owned_processes_stopped": True,
        "temp_paths_removed": True,
    }
    rendered = json.dumps(failed_case)
    assert "case-secret" not in rendered
    assert str(tmp_path) not in rendered
    assert writes[-3:] == [
        "cases/steady-01-indextts.json",
        "failure.json",
        "reliability-summary.json",
    ]


@pytest.mark.parametrize(
    ("fixture_update", "http_mode", "host_mode", "error_code"),
    [
        (
            {"base_urls": {"tts_more": "http://192.168.2.10:8000", "comfyui": "http://127.0.0.1:8188"}},
            "ready",
            "ready",
            "non-loopback-endpoint",
        ),
        ({}, "missing-resource", "ready", "resource-readiness"),
        ({}, "busy-queue", "ready", "initial-queue-not-idle"),
        ({}, "ready", "foreign-owner", "port-owner-mismatch"),
    ],
)
def test_task_10_preflight_fails_closed_and_persists_failed_summary(
    tmp_path: Path,
    fixture_update: dict[str, object],
    http_mode: str,
    host_mode: str,
    error_code: str,
) -> None:
    fixture_document = {**_fixture_document(), **fixture_update}
    fixture = ReliabilityFixture.model_validate(fixture_document)
    http_probe = _ExecutorHttpProbe(preflight_mode=http_mode)
    host_probe = _ExecutorHostProbe(preflight_mode=host_mode)
    output_root = tmp_path / "evidence"

    with pytest.raises(reliability_validation.LiveValidationError) as exc_info:
        reliability_validation.execute_reliability_validation(
            fixture,
            output_root=output_root,
            http_probe=http_probe,
            host_probe=host_probe,
            owned_processes=host_probe.owned_processes,
        )

    assert exc_info.value.code == error_code
    summary = json.loads((output_root / "reliability-summary.json").read_text(encoding="utf-8"))
    failure = json.loads((output_root / "failure.json").read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert failure == {"code": error_code, "stage": "preflight"}
    assert str(tmp_path) not in json.dumps({"summary": summary, "failure": failure})


def test_task_10_allow_lan_is_explicit_and_does_not_relax_other_preflight_gates(
    tmp_path: Path,
) -> None:
    document = _fixture_document()
    document["base_urls"] = {
        "tts_more": "http://192.168.2.10:8000",
        "comfyui": "http://192.168.2.11:8188",
    }
    fixture = ReliabilityFixture.model_validate(document)
    http_probe = _ExecutorHttpProbe(preflight_mode="busy-queue")
    host_probe = _ExecutorHostProbe()

    with pytest.raises(reliability_validation.LiveValidationError) as exc_info:
        reliability_validation.execute_reliability_validation(
            fixture,
            output_root=tmp_path / "evidence",
            http_probe=http_probe,
            host_probe=host_probe,
            owned_processes=host_probe.owned_processes,
            allow_lan=True,
        )

    assert exc_info.value.code == "initial-queue-not-idle"


def test_fix5_registered_services_bind_unique_runtime_ids_into_all_generation_payloads() -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    service_ids = {
        "gpt-sovits": "runtime-selected-gpt-v2",
        "indextts": "runtime-selected-index-v2",
        "cosyvoice": "runtime-selected-cosy-v2",
    }
    services = [
        _registered_service_document(fixture, engine, service_id=service_ids[engine])
        for engine in reliability_validation.ENGINE_ORDER
    ]
    calls: list[tuple[str, str, object | None]] = []
    probe = reliability_validation.HttpReliabilityProbe(
        transport=_registered_service_transport(fixture, services, calls),
        reference_root=Path("fixtures"),
    )

    probe.preflight(fixture)

    preflight_payloads = [
        body
        for method, path, body in calls
        if method == "POST" and path == "/api/generation/preflight"
    ]
    assert [payload["tasks"][0]["service_id"] for payload in preflight_payloads] == [
        service_ids[engine] for engine in reliability_validation.ENGINE_ORDER
    ]
    later_case = reliability_validation.CasePlan(
        case_id="later-cosy",
        phase="steady",
        engine="cosyvoice",
        expected="completed",
        action="synthesize",
        request_timeout_seconds=30.0,
        convergence_seconds=30.0,
    )
    assert probe._generation_payload(later_case, fixture)["tasks"][0]["service_id"] == service_ids["cosyvoice"]
    assert ("GET", "/api/services") in [(method, path) for method, path, _body in calls]


def test_fix5_generation_payload_fails_closed_before_registered_services_are_bound() -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    probe = reliability_validation.HttpReliabilityProbe(reference_root=Path("fixtures"))
    case = reliability_validation.CasePlan(
        case_id="unbound-gpt",
        phase="steady",
        engine="gpt-sovits",
        expected="completed",
        action="synthesize",
        request_timeout_seconds=30.0,
        convergence_seconds=30.0,
    )

    with pytest.raises(reliability_validation.LiveValidationError) as exc_info:
        probe._generation_payload(case, fixture)

    assert (exc_info.value.code, exc_info.value.stage) == (
        "registered-service-binding",
        "preflight",
    )


@pytest.mark.parametrize(
    "defect",
    [
        "zero-match",
        "multiple-matches",
        "disabled",
        "not-ready",
        "wrong-contract",
        "wrong-engine",
        "wrong-provider",
        "wrong-resource",
        "wrong-base-url",
        "missing-capability",
        "wrong-capacity",
        "inconsistent-resource-group",
    ],
)
def test_fix5_registered_service_binding_fails_closed_for_nonunique_or_invalid_runtime_contract(
    defect: str,
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    services = [
        _registered_service_document(fixture, engine)
        for engine in reliability_validation.ENGINE_ORDER
    ]
    target = services[0]
    if defect == "zero-match":
        services.pop(0)
    elif defect == "multiple-matches":
        duplicate = json.loads(json.dumps(target))
        duplicate["service_id"] = "second-valid-gpt-service"
        services.append(duplicate)
    elif defect == "disabled":
        target["enabled"] = False
    elif defect == "not-ready":
        target["ready"] = False
    elif defect == "wrong-contract":
        target["api_contract"] = "tts-more-v1"
    elif defect == "wrong-engine":
        target["engine"] = "indextts"
    elif defect == "wrong-provider":
        target["provider_type"] = "indextts"
    elif defect == "wrong-resource":
        target["default_params"] = {"resource_id": "wrong-gpt-resource"}
    elif defect == "wrong-base-url":
        target["base_url"] = "http://127.0.0.1:8199"
    elif defect == "missing-capability":
        target["capabilities"] = [
            capability
            for capability in _REGISTERED_SERVICE_CAPABILITIES
            if capability != "tts-audio-suite"
        ]
    elif defect == "wrong-capacity":
        target["capacity"] = 2
    elif defect == "inconsistent-resource-group":
        target["resource_group"] = "different-gpu"
    else:
        raise AssertionError(defect)
    calls: list[tuple[str, str, object | None]] = []
    probe = reliability_validation.HttpReliabilityProbe(
        transport=_registered_service_transport(fixture, services, calls),
        reference_root=Path("fixtures"),
    )

    with pytest.raises(reliability_validation.LiveValidationError) as exc_info:
        probe.preflight(fixture)

    assert (exc_info.value.code, exc_info.value.stage) == (
        "registered-service-binding",
        "preflight",
    )
    assert not any(path == "/api/generation/preflight" for _method, path, _body in calls)


def test_fix5_derived_gpt_service_binding_passes_real_tts_more_preflight_without_raw_weights(
    tmp_path: Path,
) -> None:
    from app.main import GenerateRequest, _preflight_task
    from app.models import TTSServiceEndpoint
    from app.queue import ServiceGenerationQueue
    from app.services import MockServiceClient, ServiceRegistry, ServiceRouter
    from app.supervisor import ServiceSupervisor

    fixture = ReliabilityFixture.model_validate(_fixture_document())
    service_id = "runtime-derived-gpt-not-hard-coded"
    service_document = _registered_service_document(
        fixture,
        "gpt-sovits",
        service_id=service_id,
    )
    calls: list[tuple[str, str, object | None]] = []
    probe = reliability_validation.HttpReliabilityProbe(
        transport=_registered_service_transport(
            fixture,
            [
                service_document,
                _registered_service_document(fixture, "indextts"),
                _registered_service_document(fixture, "cosyvoice"),
            ],
            calls,
        ),
        reference_root=Path("fixtures"),
    )
    probe.preflight(fixture)
    case = reliability_validation.CasePlan(
        case_id="real-gpt-preflight",
        phase="steady",
        engine="gpt-sovits",
        expected="completed",
        action="synthesize",
        request_timeout_seconds=30.0,
        convergence_seconds=30.0,
    )
    payload = probe._generation_payload(case, fixture)
    request = GenerateRequest.model_validate(payload)
    endpoint = TTSServiceEndpoint.model_validate(service_document)
    registry = ServiceRegistry([endpoint])
    router = ServiceRouter(
        registry,
        clients={service_id: MockServiceClient(endpoint)},
    )
    queue = ServiceGenerationQueue(router)
    supervisor = ServiceSupervisor(project_root=tmp_path, runtime_root=tmp_path / "runtime")

    result = _preflight_task(router, supervisor, queue, request.tasks[0])

    assert result["status"] == "ready"
    assert result["selected_service_id"] == service_id
    assert payload["tasks"][0]["parameters"].get("gpt_weights_path") is None
    assert payload["tasks"][0]["parameters"].get("sovits_weights_path") is None


def test_task_10_http_probe_uses_exact_preflight_queue_and_release_routes() -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    calls: list[tuple[str, str, object | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {"cuda": True}})
        if request.url.path == "/object_info":
            return httpx.Response(200, json={"TTSExternalEngine": {}})
        if request.url.path == "/api/tts-audio-suite/v1/capabilities":
            bridge_engines = {
                "gpt-sovits": "gpt_sovits",
                "indextts": "index_tts",
                "cosyvoice": "cosyvoice",
            }
            return httpx.Response(
                200,
                json={
                    "protocol_version": 1,
                    "resources": [
                        {
                            "engine": bridge_engines[engine],
                            "resource_id": resource.resource_id,
                            "ready": True,
                        }
                        for engine, resource in fixture.resources.items()
                    ],
                },
            )
        if request.url.path == "/api/services":
            return httpx.Response(
                200,
                json={
                    "services": [
                        _registered_service_document(fixture, engine)
                        for engine in reliability_validation.ENGINE_ORDER
                    ]
                },
            )
        if request.url.path == "/api/generation/preflight":
            return httpx.Response(200, json={"status": "ready", "items": [{"status": "ready"}]})
        if request.url.path == "/api/queue/status":
            return httpx.Response(200, json={"jobs": [], "queued": 0, "running": 0})
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
        if request.url.path == "/api/tts-audio-suite/v1/runtime/release":
            return httpx.Response(200, json={"status": "released"})
        if request.url.path == "/free":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    probe = reliability_validation.HttpReliabilityProbe(
        transport=httpx.MockTransport(handler),
        reference_root=Path("fixtures"),
    )

    observation = probe.preflight(fixture)
    probe.release()
    final = probe.final_state()

    assert {(item.engine, item.resource_id, item.ready) for item in observation.resources} == {
        (engine, resource.resource_id, True)
        for engine, resource in fixture.resources.items()
    }
    assert final.runtime_released is True
    assert [path for method, path, _body in calls if method == "POST"].count(
        "/api/generation/preflight"
    ) == 3
    assert ("GET", "/system_stats") in [(method, path) for method, path, _body in calls]
    assert ("GET", "/object_info") in [(method, path) for method, path, _body in calls]
    assert ("GET", "/api/tts-audio-suite/v1/capabilities") in [
        (method, path) for method, path, _body in calls
    ]
    assert ("GET", "/api/services") in [
        (method, path) for method, path, _body in calls
    ]
    assert ("POST", "/api/tts-audio-suite/v1/runtime/release", {"all": True}) in calls
    assert ("POST", "/free", {"unload_models": True, "free_memory": True}) in calls


def _fault_case(
    action: reliability_validation.CaseAction,
    *,
    expected: reliability_validation.Outcome,
) -> reliability_validation.CasePlan:
    return reliability_validation.CasePlan(
        case_id=f"route-{action}",
        phase="recovery" if action == "restart-readiness" else "fault",
        engine="indextts" if action == "terminate-comfyui" else "gpt-sovits",
        expected=expected,
        action=action,
        request_timeout_seconds=1.0 if action == "timeout" else 30.0,
        convergence_seconds=30.0,
    )


def test_task_10_manifest_version_identity_is_line_scoped_and_publicly_unique() -> None:
    manifest = {
        "lines": {
            "other-line": {
                "line_id": "other-line",
                "versions": [{"version_id": "v001", "audio_path": "other.wav"}],
            },
            "target-line": {
                "line_id": "target-line",
                "versions": [{"version_id": "v001", "audio_path": "target.wav"}],
            },
        }
    }

    selected = reliability_validation._find_manifest_version(manifest, "target-line", "v001")
    target_public_id = reliability_validation._public_manifest_version_id("target-line", "v001")
    other_public_id = reliability_validation._public_manifest_version_id("other-line", "v001")

    assert selected["audio_path"] == "target.wav"
    assert target_public_id != other_public_id
    assert len(target_public_id) == 64
    assert set(target_public_id) <= set("0123456789abcdef")


@pytest.mark.parametrize("outcome", ["completed", "cancelled", "timeout", "failed"])
def test_task_10_key_only_history_is_not_terminal_evidence(outcome: str) -> None:
    with pytest.raises(RuntimeError, match="history"):
        reliability_validation._terminal_comfy_history_status({}, expected=outcome)


@pytest.mark.parametrize(
    ("action", "expected", "terminal_status"),
    [
        ("cancel-running", "cancelled", "cancelled"),
        ("timeout", "timeout", "failed"),
        ("restart-readiness", "completed", "completed"),
    ],
)
def test_task_10_http_probe_executes_concrete_running_fault_and_restart_actions(
    tmp_path: Path,
    action: reliability_validation.CaseAction,
    expected: reliability_validation.Outcome,
    terminal_status: str,
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    case = _fault_case(action, expected=expected)
    prompt_id = f"prompt-{action}"
    version_id = f"version-{action}"
    wav_path = tmp_path / f"{action}.wav"
    if expected == "completed":
        _write_voiced_wav(wav_path)
    calls: list[tuple[str, str, object | None]] = []
    job_reads = 0
    cancelled = False

    def job(status: str) -> dict[str, object]:
        error = (
            "ComfyUI prompt prompt-timeout did not complete within 1.0s"
            if action == "timeout" and status == "failed"
            else None
        )
        return {
            "job_id": f"job-{action}",
            "status": status,
            "error": error,
            "items": [
                {
                    "status": status,
                    "external_job_id": prompt_id,
                    "version_id": version_id if status != "running" else None,
                    "error": error,
                }
            ],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal job_reads, cancelled
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path == "/api/generation/preflight":
            return httpx.Response(200, json={"status": "ready", "items": [{"status": "ready"}]})
        if request.url.path == "/api/jobs/generation":
            return httpx.Response(200, json=job("queued"))
        if request.url.path == f"/api/jobs/job-{action}/cancel":
            cancelled = True
            return httpx.Response(200, json=job("cancelled"))
        if request.url.path == f"/api/jobs/job-{action}":
            job_reads += 1
            terminal = (
                (action == "cancel-running" and cancelled)
                or (action in {"timeout", "restart-readiness"} and job_reads >= 2)
            )
            return httpx.Response(200, json=job(terminal_status if terminal else "running"))
        if request.url.path == "/queue":
            running = job_reads == 1
            return httpx.Response(
                200,
                json={
                    "queue_running": [[0, prompt_id, {}, {}, []]] if running else [],
                    "queue_pending": [],
                },
            )
        if request.url.path.endswith("/manifest"):
            metadata: dict[str, object] = {}
            if action in {"cancel-running", "timeout"}:
                metadata = {
                    "control_code": "cancelled" if action == "cancel-running" else "timeout",
                    "control_details": {
                        "prompt_id": prompt_id,
                        "cancellation": {
                            "prompt_id": prompt_id,
                            "initial_state": "running",
                            "final_state": "interrupted",
                            "actions": ["interrupt"],
                            "duration_seconds": 0.5,
                            "converged": True,
                        },
                    },
                }
                if action == "timeout":
                    metadata["failure_stage"] = "timeout"
            return httpx.Response(
                200,
                json={
                    "lines": {
                        case.case_id: {
                            "line_id": case.case_id,
                            "versions": [
                                {
                                    "version_id": version_id,
                                    "status": terminal_status,
                                    "audio_path": str(wav_path) if expected == "completed" else None,
                                    "metadata": metadata,
                                }
                            ],
                        }
                    }
                },
            )
        if request.url.path == f"/history/{prompt_id}":
            return httpx.Response(200, json={prompt_id: _comfy_terminal_entry(expected)})
        return httpx.Response(404)

    probe = reliability_validation.HttpReliabilityProbe(
        transport=httpx.MockTransport(handler),
        reference_root=Path("fixtures"),
        poll_interval_seconds=0.001,
        sleep=lambda _seconds: None,
    )
    _preflight_http_probe_for_case(probe, fixture)
    hook_calls = 0

    def hook() -> None:
        nonlocal hook_calls
        hook_calls += 1

    observation = probe.execute_case(case, fixture, tmp_path, action_hook=hook)

    assert observation.actual == expected
    assert hook_calls == (1 if action == "restart-readiness" else 0)
    assert any(path == "/api/jobs/generation" for _method, path, _body in calls)
    assert any(path == f"/history/{prompt_id}" for _method, path, _body in calls)
    if action == "cancel-running":
        assert ("POST", f"/api/jobs/job-{action}/cancel", None) in calls
    if action == "timeout":
        submitted = next(body for method, path, body in calls if method == "POST" and path == "/api/jobs/generation")
        assert isinstance(submitted, dict)
        assert submitted["tasks"][0]["parameters"]["timeout_seconds"] == 1.0


def test_task_10_http_probe_queued_cancel_never_fabricates_comfy_prompt_or_version() -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    case = next(case for case in reliability_validation.build_case_plan() if case.action == "cancel-queued")
    calls: list[tuple[str, str]] = []
    created_jobs = 0
    target_cancelled = False
    blocker_cancelled = False

    def target_document(*, settled: bool) -> dict[str, object]:
        return {
            "job_id": "job-target",
            "status": "cancelled" if target_cancelled else "running",
            "progress": 1.0 if target_cancelled else 0.0,
            "error": None,
            "updated_at": "2026-08-01T00:00:03Z" if settled else "2026-08-01T00:00:02Z",
            "items": [
                {
                    "status": "cancelled" if target_cancelled else "queued",
                    "progress": 1.0 if target_cancelled else 0.0,
                    "external_job_id": None,
                    "external_status": None,
                    "error": None,
                    "version_id": None,
                }
            ],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal created_jobs, target_cancelled, blocker_cancelled
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/generation/preflight":
            return httpx.Response(200, json={"status": "ready", "items": [{"status": "ready"}]})
        if request.url.path.endswith("/manifest"):
            return httpx.Response(200, json={"lines": {}})
        if request.url.path == "/api/jobs/generation":
            created_jobs += 1
            return httpx.Response(200, json={"job_id": "job-blocker" if created_jobs == 1 else "job-target"})
        if request.url.path == "/api/jobs/job-blocker" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "job_id": "job-blocker",
                    "status": "cancelled" if blocker_cancelled else "running",
                    "items": [{"external_job_id": "prompt-blocker"}],
                },
            )
        if request.url.path == "/api/jobs/job-target" and request.method == "GET":
            return httpx.Response(200, json=target_document(settled=blocker_cancelled))
        if request.url.path == "/api/jobs/job-target/cancel":
            target_cancelled = True
            return httpx.Response(200, json=target_document(settled=False))
        if request.url.path == "/api/jobs/job-blocker/cancel":
            blocker_cancelled = True
            return httpx.Response(200, json={"job_id": "job-blocker", "status": "cancelled", "items": []})
        if request.url.path == "/queue":
            return httpx.Response(
                200,
                json={
                    "queue_running": [] if blocker_cancelled else [[0, "prompt-blocker", {}, {}, []]],
                    "queue_pending": [],
                },
            )
        return httpx.Response(404)

    probe = reliability_validation.HttpReliabilityProbe(
        transport=httpx.MockTransport(handler),
        reference_root=Path("fixtures"),
        poll_interval_seconds=0.001,
        sleep=lambda _seconds: None,
    )
    _preflight_http_probe_for_case(probe, fixture)

    observation = probe.execute_case(case, fixture, Path("unused"))

    assert observation.actual == "cancelled"
    assert observation.prompt_submitted is False
    assert observation.prompt_id is None
    assert observation.version_id is None
    assert observation.comfyui is None
    assert not any(path.startswith("/history/") for _method, path in calls)


def test_task_10_http_probe_termination_proves_endpoint_absence_without_fake_history() -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    case = _fault_case("terminate-comfyui", expected="failed")
    prompt_id = "prompt-terminate"
    comfy_dead = False
    job_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal job_reads
        if request.url.port == 8188 and comfy_dead:
            raise httpx.ConnectError("owned ComfyUI is stopped", request=request)
        if request.url.path == "/api/generation/preflight":
            return httpx.Response(200, json={"status": "ready", "items": [{"status": "ready"}]})
        if request.url.path == "/api/jobs/generation":
            return httpx.Response(200, json={"job_id": "job-terminate"})
        if request.url.path == "/api/jobs/job-terminate":
            job_reads += 1
            terminal = job_reads >= 2
            return httpx.Response(
                200,
                json={
                    "job_id": "job-terminate",
                    "status": "failed" if terminal else "running",
                    "error": "ComfyUI connection stopped" if terminal else None,
                    "items": [
                        {
                            "status": "failed" if terminal else "running",
                            "external_job_id": prompt_id,
                            "external_status": "running" if not terminal else None,
                            "error": "ComfyUI connection stopped" if terminal else None,
                            "version_id": "version-terminate" if terminal else None,
                        }
                    ],
                },
            )
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [[0, prompt_id, {}, {}, []]], "queue_pending": []})
        if request.url.path.endswith("/manifest"):
            return httpx.Response(
                200,
                json={
                    "lines": {
                        case.case_id: {
                            "line_id": case.case_id,
                            "versions": [
                                {
                                    "version_id": "version-terminate",
                                    "status": "failed",
                                    "audio_path": None,
                                }
                            ],
                        }
                    }
                },
            )
        return httpx.Response(404)

    probe = reliability_validation.HttpReliabilityProbe(
        transport=httpx.MockTransport(handler),
        reference_root=Path("fixtures"),
        poll_interval_seconds=0.001,
        sleep=lambda _seconds: None,
    )
    _preflight_http_probe_for_case(probe, fixture)

    def terminate() -> None:
        nonlocal comfy_dead
        comfy_dead = True

    observation = probe.execute_case(case, fixture, Path("unused"), action_hook=terminate)

    assert observation.actual == "failed"
    assert observation.comfyui is None
    assert observation.termination is not None
    assert observation.termination.endpoint_unavailable is True
    assert observation.termination.queue_before_prompt_ids == [prompt_id]
    assert observation.tts_more is not None
    assert observation.tts_more.version_status == "failed"


@pytest.mark.parametrize("mode", ["status", "timeout"])
def test_task_10_http_probe_fails_closed_on_http_error_or_timeout(mode: str) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    case = _fault_case("cancel-running", expected="cancelled")

    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "timeout":
            raise httpx.ReadTimeout("injected timeout", request=request)
        return httpx.Response(503, json={"detail": "unavailable"})

    probe = reliability_validation.HttpReliabilityProbe(
        transport=httpx.MockTransport(handler),
        reference_root=Path("fixtures"),
    )
    _preflight_http_probe_for_case(probe, fixture)

    expected_error = httpx.ReadTimeout if mode == "timeout" else httpx.HTTPStatusError
    with pytest.raises(expected_error):
        probe.execute_case(case, fixture, Path("unused"))


def _task_10_plan(case_id: str) -> reliability_validation.CasePlan:
    return next(case for case in reliability_validation.build_case_plan() if case.case_id == case_id)


def _task_10_job_document(
    *,
    status: str,
    prompt_id: str | None = "prompt-main",
    version_id: str | None = "version-main",
    error: str | None = None,
    updated_at: str = "2026-08-01T00:00:02Z",
) -> dict[str, object]:
    progress = 1.0 if status in {"completed", "cancelled", "failed"} else 0.5
    return {
        "job_id": "job-main",
        "project_id": "windows-reliability-validation",
        "status": status,
        "progress": progress,
        "error": error,
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": updated_at,
        "items": [
            {
                "line_id": "line-main",
                "status": status,
                "progress": progress,
                "external_job_id": prompt_id,
                "external_status": status,
                "error": error,
                "version_id": version_id,
            }
        ],
    }


class _Task10FaultRouteScenario:
    def __init__(self, case: reliability_validation.CasePlan, wav_path: Path) -> None:
        self.case = case
        self.wav_path = wav_path
        self.calls: list[tuple[str, str, object | None]] = []
        self.job_reads = 0
        self.queue_reads = 0
        self.cancelled = False
        self.terminated = False
        self.restarted = False
        self.version_overrides: dict[str, object] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.calls.append((request.method, path, body))
        if self.terminated and (path == "/queue" or path.startswith("/history/")):
            raise httpx.ConnectError("ComfyUI is intentionally offline", request=request)
        if path == "/api/generation/preflight":
            if self.case.action == "restart-readiness" and not self.restarted:
                return httpx.Response(503, json={"status": "offline"})
            return httpx.Response(200, json={"status": "ready", "items": [{"status": "ready"}]})
        if path == "/api/jobs/generation":
            return httpx.Response(200, json={"job_id": "job-main"})
        if path == "/api/jobs/job-main/cancel":
            self.cancelled = True
            return httpx.Response(
                200,
                json=_task_10_job_document(status="cancelling", version_id=None),
            )
        if path == "/api/jobs/job-main":
            self.job_reads += 1
            if self.case.action == "cancel-running" and self.cancelled:
                return httpx.Response(200, json=_task_10_job_document(status="cancelled"))
            if self.case.action == "timeout" and self.job_reads > 1:
                return httpx.Response(
                    200,
                    json=_task_10_job_document(
                        status="failed",
                        error="ComfyUI prompt prompt-main did not complete within 1.0s",
                    ),
                )
            if self.case.action == "terminate-comfyui" and self.terminated:
                return httpx.Response(
                    200,
                    json=_task_10_job_document(status="failed", error="ComfyUI connection lost"),
                )
            if self.case.action == "restart-readiness":
                return httpx.Response(200, json=_task_10_job_document(status="completed"))
            return httpx.Response(
                200,
                json=_task_10_job_document(status="running", version_id=None),
            )
        if path == "/queue":
            self.queue_reads += 1
            running = [[1, "prompt-main", {}, {}, []]] if self.queue_reads == 1 else []
            return httpx.Response(200, json={"queue_running": running, "queue_pending": []})
        if path == "/api/projects/windows-reliability-validation/manifest":
            persisted_status = "failed" if self.case.expected in {"failed", "timeout"} else self.case.expected
            metadata: dict[str, object] = {}
            if self.case.action in {"cancel-running", "timeout"}:
                metadata = {
                    "control_code": "cancelled" if self.case.action == "cancel-running" else "timeout",
                    "control_details": {
                        "prompt_id": "prompt-main",
                        "cancellation": {
                            "prompt_id": "prompt-main",
                            "initial_state": "running",
                            "final_state": "interrupted",
                            "actions": ["interrupt"],
                            "duration_seconds": 0.5,
                            "converged": True,
                        },
                    },
                }
                if self.case.action == "timeout":
                    metadata["failure_stage"] = "timeout"
            version = {
                "version_id": "version-main",
                "status": persisted_status,
                "audio_path": str(self.wav_path) if self.case.expected == "completed" else None,
                "metadata": metadata,
            }
            version.update(self.version_overrides)
            return httpx.Response(
                200,
                json={
                    "project_id": "windows-reliability-validation",
                    "lines": {
                        self.case.case_id: {
                            "line_id": self.case.case_id,
                            "versions": [version],
                        }
                    },
                },
            )
        if path == "/history/prompt-main":
            return httpx.Response(
                200,
                json={"prompt-main": _comfy_terminal_entry(self.case.expected)},
            )
        return httpx.Response(404, json={"detail": "unexpected route"})


@pytest.mark.parametrize(
    ("case_id", "actual"),
    [
        ("cancel-running-gpt-sovits", "cancelled"),
        ("timeout-indextts", "timeout"),
        ("restart-cosyvoice", "completed"),
    ],
)
def test_task_10_http_probe_executes_fault_routes_and_preserves_terminal_evidence(
    tmp_path: Path,
    case_id: str,
    actual: str,
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    case = _task_10_plan(case_id)
    wav_path = tmp_path / "terminal.wav"
    if actual == "completed":
        _write_voiced_wav(wav_path)
    scenario = _Task10FaultRouteScenario(case, wav_path)
    probe = reliability_validation.HttpReliabilityProbe(
        transport=httpx.MockTransport(scenario.handler),
        reference_root=tmp_path,
        poll_interval_seconds=0,
        sleep=lambda _seconds: None,
    )
    _preflight_http_probe_for_case(probe, fixture)

    hook = None
    if case.action == "restart-readiness":
        hook = lambda: setattr(scenario, "restarted", True)
    observation = probe.execute_case(case, fixture, tmp_path, action_hook=hook)

    assert observation.actual == actual
    assert observation.prompt_id == "prompt-main"
    assert observation.version_id == reliability_validation._public_manifest_version_id(
        case.case_id,
        "version-main",
    )
    assert observation.comfyui is not None and observation.comfyui.queue_empty is True
    create_call = next(call for call in scenario.calls if call[:2] == ("POST", "/api/jobs/generation"))
    assert create_call[2]["tasks"][0]["parameters"]["timeout_seconds"] == case.request_timeout_seconds
    cancel_paths = [path for method, path, _body in scenario.calls if method == "POST" and path.endswith("/cancel")]
    assert cancel_paths == (["/api/jobs/job-main/cancel"] if case.action == "cancel-running" else [])

    if case.action in {"cancel-running", "timeout"}:
        assert observation.tts_more is not None
        assert observation.tts_more.job_status == ("cancelled" if case.action == "cancel-running" else "failed")
        assert observation.tts_more.item_status == observation.tts_more.job_status
        assert observation.tts_more.version_status == observation.tts_more.job_status
        assert observation.tts_more.manifest_version_absent is False
        assert observation.tts_more.version_audio_absent is True
        assert observation.tts_more.control is not None
        assert observation.tts_more.control.control_code == (
            "cancelled" if case.action == "cancel-running" else "timeout"
        )
        assert observation.tts_more.control.failure_stage == (
            None if case.action == "cancel-running" else "timeout"
        )
        assert observation.tts_more.control.prompt_id == "prompt-main"
        assert observation.tts_more.control.initial_state == "running"
        assert observation.tts_more.control.final_state == "interrupted"
        assert observation.tts_more.control.actions == ["interrupt"]
        assert observation.tts_more.control.converged is True


@pytest.mark.parametrize(
    ("case_id", "version_overrides"),
    [
        (
            "cancel-running-gpt-sovits",
            {"status": "completed", "audio_path": "forbidden-success.wav"},
        ),
        ("cancel-running-gpt-sovits", {"metadata": {}}),
        (
            "timeout-indextts",
            {"status": "completed", "audio_path": "forbidden-success.wav"},
        ),
        (
            "timeout-indextts",
            {
                "metadata": {
                    "failure_stage": "timeout",
                    "control_code": "timeout",
                    "control_details": {
                        "prompt_id": "prompt-main",
                        "cancellation": {
                            "prompt_id": "prompt-main",
                            "initial_state": "running",
                            "final_state": "running",
                            "actions": ["interrupt"],
                            "duration_seconds": 30.0,
                            "converged": False,
                        },
                    },
                }
            },
        ),
    ],
)
def test_fix_round_1_fault_cases_reject_false_terminal_manifest_evidence(
    tmp_path: Path,
    case_id: str,
    version_overrides: dict[str, object],
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    case = _task_10_plan(case_id)
    scenario = _Task10FaultRouteScenario(case, tmp_path / "unused.wav")
    scenario.version_overrides.update(version_overrides)
    probe = reliability_validation.HttpReliabilityProbe(
        transport=httpx.MockTransport(scenario.handler),
        reference_root=tmp_path,
        poll_interval_seconds=0,
        sleep=lambda _seconds: None,
    )
    _preflight_http_probe_for_case(probe, fixture)

    with pytest.raises(RuntimeError, match="fault terminal evidence"):
        probe.execute_case(case, fixture, tmp_path)


def test_task_10_terminate_case_does_not_probe_comfyui_after_owned_termination(
    tmp_path: Path,
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    case = _task_10_plan("terminate-comfyui-indextts")
    scenario = _Task10FaultRouteScenario(case, tmp_path / "unused.wav")
    probe = reliability_validation.HttpReliabilityProbe(
        transport=httpx.MockTransport(scenario.handler),
        reference_root=tmp_path,
        poll_interval_seconds=0,
        sleep=lambda _seconds: None,
    )
    _preflight_http_probe_for_case(probe, fixture)

    observation = probe.execute_case(
        case,
        fixture,
        tmp_path,
        action_hook=lambda: setattr(scenario, "terminated", True),
    )

    assert observation.actual == "failed"
    assert observation.prompt_id == "prompt-main"
    assert observation.version_id == reliability_validation._public_manifest_version_id(
        case.case_id,
        "version-main",
    )


def test_task_10_http_probe_cancels_queued_target_before_comfyui_dispatch(tmp_path: Path) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    case = _task_10_plan("cancel-queued")
    calls: list[tuple[str, str, object | None]] = []
    blocker_cancelled = False
    target_cancelled = False
    target_settled = False

    def queued_job(*, job_id: str, status: str, prompt_id: str | None, updated_at: str) -> dict[str, object]:
        progress = 1.0 if status == "cancelled" else (0.5 if prompt_id else 0.0)
        return {
            "job_id": job_id,
            "project_id": "windows-reliability-validation",
            "status": status,
            "progress": progress,
            "error": None,
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": updated_at,
            "items": [
                {
                    "line_id": "blocker" if prompt_id else "cancel-queued",
                    "status": status if prompt_id else ("cancelled" if status == "cancelled" else "queued"),
                    "progress": progress,
                    "external_job_id": prompt_id,
                    "external_status": status if prompt_id else None,
                    "error": None,
                    "version_id": "version-blocker" if prompt_id and status == "cancelled" else None,
                }
            ],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal blocker_cancelled, target_cancelled, target_settled
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        calls.append((request.method, path, body))
        if path == "/api/generation/preflight":
            return httpx.Response(200, json={"status": "ready", "items": [{"status": "ready"}]})
        if path == "/api/projects/windows-reliability-validation/manifest":
            return httpx.Response(200, json={"project_id": "windows-reliability-validation", "lines": {}})
        if path == "/api/jobs/generation":
            line_id = body["tasks"][0]["line"]["id"]
            return httpx.Response(200, json={"job_id": "job-target" if line_id == "cancel-queued" else "job-blocker"})
        if path == "/api/jobs/job-blocker":
            status = "cancelled" if blocker_cancelled else "running"
            return httpx.Response(200, json=queued_job(job_id="job-blocker", status=status, prompt_id="prompt-blocker", updated_at="2026-08-01T00:00:04Z"))
        if path == "/api/jobs/job-target":
            if target_cancelled:
                target_settled = blocker_cancelled
                updated = "2026-08-01T00:00:05Z" if target_settled else "2026-08-01T00:00:03Z"
                return httpx.Response(200, json=queued_job(job_id="job-target", status="cancelled", prompt_id=None, updated_at=updated))
            return httpx.Response(200, json=queued_job(job_id="job-target", status="running", prompt_id=None, updated_at="2026-08-01T00:00:02Z"))
        if path == "/api/jobs/job-target/cancel":
            target_cancelled = True
            return httpx.Response(200, json=queued_job(job_id="job-target", status="cancelled", prompt_id=None, updated_at="2026-08-01T00:00:03Z"))
        if path == "/api/jobs/job-blocker/cancel":
            blocker_cancelled = True
            return httpx.Response(200, json=queued_job(job_id="job-blocker", status="cancelled", prompt_id="prompt-blocker", updated_at="2026-08-01T00:00:04Z"))
        if path == "/queue":
            running = [] if blocker_cancelled else [[1, "prompt-blocker", {}, {}, []]]
            return httpx.Response(200, json={"queue_running": running, "queue_pending": []})
        return httpx.Response(404, json={"detail": "unexpected route"})

    probe = reliability_validation.HttpReliabilityProbe(
        transport=httpx.MockTransport(handler),
        reference_root=tmp_path,
        poll_interval_seconds=0,
        sleep=lambda _seconds: None,
    )
    _preflight_http_probe_for_case(probe, fixture)

    observation = probe.execute_case(case, fixture, tmp_path)

    assert observation.actual == "cancelled"
    assert observation.prompt_submitted is False
    assert observation.prompt_id is None
    assert observation.version_id is None
    assert observation.comfyui is None
    assert observation.tts_more is not None and observation.tts_more.manifest_version_absent is True
    assert target_settled is True
    assert [path for method, path, _body in calls if method == "POST" and path.endswith("/cancel")] == [
        "/api/jobs/job-target/cancel",
        "/api/jobs/job-blocker/cancel",
    ]
    assert not any(path.startswith("/history/") for _method, path, _body in calls)


@pytest.mark.parametrize("failure", ["http-status", "read-timeout"])
def test_task_10_http_probe_fails_closed_on_transport_errors(tmp_path: Path, failure: str) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    case = _task_10_plan("timeout-gpt-sovits")

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "read-timeout":
            raise httpx.ReadTimeout("injected read timeout", request=request)
        return httpx.Response(503, json={"detail": "injected failure"})

    probe = reliability_validation.HttpReliabilityProbe(
        transport=httpx.MockTransport(handler),
        reference_root=tmp_path,
    )
    _preflight_http_probe_for_case(probe, fixture)

    expected = httpx.HTTPStatusError if failure == "http-status" else httpx.ReadTimeout
    with pytest.raises(expected):
        probe.execute_case(case, fixture, tmp_path)


def test_task_10_cli_uses_injected_probes_and_returns_nonzero_for_failed_gate(
    tmp_path: Path,
) -> None:
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_fixture_document()), encoding="utf-8")
    success_http = _ExecutorHttpProbe()
    success_host = _ExecutorHostProbe()

    success = reliability_validation.main(
        [
            "--fixture",
            str(fixture_path),
            "--output-root",
            str(tmp_path / "success"),
            "--comfyui-pid",
            "8188",
            "--tts-more-pid",
            "8000",
        ],
        probe_factory=lambda _fixture, _args: (
            success_http,
            success_host,
            success_host.owned_processes,
        ),
    )

    failed_http = _ExecutorHttpProbe(preflight_mode="busy-queue")
    failed_host = _ExecutorHostProbe()
    failed = reliability_validation.main(
        [
            "--fixture",
            str(fixture_path),
            "--output-root",
            str(tmp_path / "failed"),
            "--comfyui-pid",
            "8188",
            "--tts-more-pid",
            "8000",
        ],
        probe_factory=lambda _fixture, _args: (
            failed_http,
            failed_host,
            failed_host.owned_processes,
        ),
    )

    assert success == 0
    assert failed == 1
    assert json.loads((tmp_path / "success" / "reliability-summary.json").read_text())["status"] == "passed"
    assert json.loads((tmp_path / "failed" / "reliability-summary.json").read_text())["status"] == "failed"


def test_task_10_cli_preflight_only_writes_evidence_without_running_cases(
    tmp_path: Path,
) -> None:
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_fixture_document()), encoding="utf-8")
    http_probe = _ExecutorHttpProbe()
    host_probe = _ExecutorHostProbe()

    result = reliability_validation.main(
        [
            "--fixture",
            str(fixture_path),
            "--output-root",
            str(tmp_path / "preflight"),
            "--comfyui-pid",
            "8188",
            "--tts-more-pid",
            "8000",
            "--preflight-only",
        ],
        probe_factory=lambda _fixture, _args: (
            http_probe,
            host_probe,
            host_probe.owned_processes,
        ),
    )

    assert result == 0
    assert http_probe.executed == []
    evidence = json.loads((tmp_path / "preflight" / "preflight.json").read_text())
    assert evidence["status"] == "passed"
    assert not (tmp_path / "preflight" / "failure.json").exists()
    assert [item["engine"] for item in evidence["resources"]] == [
        "cosyvoice",
        "gpt-sovits",
        "indextts",
    ]
    assert all("resource_id" not in item for item in evidence["resources"])
    assert {
        (item["engine"], item["resource_id_hash"])
        for item in evidence["resources"]
    } == {
        (engine, hashlib.sha256(resource.resource_id.encode("utf-8")).hexdigest())
        for engine, resource in ReliabilityFixture.model_validate(_fixture_document()).resources.items()
    }
    assert not any(
        resource.resource_id in json.dumps(evidence)
        for resource in ReliabilityFixture.model_validate(_fixture_document()).resources.values()
    )


def test_fix5_preflight_only_persists_typed_primary_failure_and_real_cli_returns_one(
    tmp_path: Path,
) -> None:
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_fixture_document()), encoding="utf-8")
    host_probe = _ExecutorHostProbe()

    class TypedFailureProbe(_ExecutorHttpProbe):
        def preflight(
            self,
            fixture: ReliabilityFixture,
        ) -> reliability_validation.HttpPreflightObservation:
            del fixture
            raise reliability_validation.LiveValidationError(
                "registered-service-binding",
                stage="preflight",
            )

    output_root = tmp_path / "typed-failure"
    result = reliability_validation.main(
        [
            "--fixture",
            str(fixture_path),
            "--output-root",
            str(output_root),
            "--comfyui-pid",
            "8188",
            "--tts-more-pid",
            "8000",
            "--preflight-only",
        ],
        probe_factory=lambda _fixture, _args: (
            TypedFailureProbe(),
            host_probe,
            host_probe.owned_processes,
        ),
    )

    assert result == 1
    assert json.loads((output_root / "failure.json").read_text(encoding="utf-8")) == {
        "code": "registered-service-binding",
        "stage": "preflight",
    }
    assert not (output_root / "preflight.json").exists()


def test_fix5_real_blocked_generation_preflight_persists_stable_sanitized_failure(
    tmp_path: Path,
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_fixture_document()), encoding="utf-8")
    private_reason = f"token=private-value model=C:\\private\\voice {tmp_path}"
    calls: list[tuple[str, str, object | None]] = []
    http_probe = reliability_validation.HttpReliabilityProbe(
        transport=_registered_service_transport(
            fixture,
            [
                _registered_service_document(fixture, engine)
                for engine in reliability_validation.ENGINE_ORDER
            ],
            calls,
            generation_preflight_document={
                "status": "blocked",
                "items": [{"status": "blocked", "reason": private_reason}],
            },
        ),
        reference_root=tmp_path,
    )
    host_probe = _ExecutorHostProbe()
    output_root = tmp_path / "blocked-preflight"

    result = reliability_validation.main(
        [
            "--fixture",
            str(fixture_path),
            "--output-root",
            str(output_root),
            "--comfyui-pid",
            "8188",
            "--tts-more-pid",
            "8000",
            "--preflight-only",
        ],
        probe_factory=lambda _fixture, _args: (
            http_probe,
            host_probe,
            host_probe.owned_processes,
        ),
    )

    persisted = (output_root / "failure.json").read_text(encoding="utf-8")
    assert result == 1
    assert json.loads(persisted) == {
        "code": "tts-more-preflight-not-ready",
        "stage": "preflight",
    }
    assert private_reason not in persisted
    assert str(tmp_path) not in persisted
    assert not (output_root / "preflight.json").exists()


def test_fix5_preflight_only_maps_and_scrubs_raw_observation_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_sentinel = f"token=private-value Authorization=Bearer-private {tmp_path}"
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_fixture_document()), encoding="utf-8")
    host_probe = _ExecutorHostProbe()

    class RawFailureProbe(_ExecutorHttpProbe):
        def preflight(
            self,
            fixture: ReliabilityFixture,
        ) -> reliability_validation.HttpPreflightObservation:
            del fixture
            raise httpx.ReadTimeout(private_sentinel)

    output_root = tmp_path / "raw-failure"
    result = reliability_validation.main(
        [
            "--fixture",
            str(fixture_path),
            "--output-root",
            str(output_root),
            "--comfyui-pid",
            "8188",
            "--tts-more-pid",
            "8000",
            "--preflight-only",
        ],
        probe_factory=lambda _fixture, _args: (
            RawFailureProbe(),
            host_probe,
            host_probe.owned_processes,
        ),
    )

    captured = capsys.readouterr()
    persisted = (output_root / "failure.json").read_text(encoding="utf-8")
    assert result == 1
    assert json.loads(persisted) == {
        "code": "preflight-observation-failed",
        "stage": "preflight",
    }
    assert private_sentinel not in persisted
    assert "private-value" not in captured.out + captured.err
    assert str(tmp_path) not in captured.out + captured.err
    assert not (output_root / "preflight.json").exists()


def test_fix5_preflight_failure_evidence_write_failure_preserves_nonzero_without_partial_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_fixture_document()), encoding="utf-8")
    host_probe = _ExecutorHostProbe()
    writes: list[Path] = []

    class TypedFailureProbe(_ExecutorHttpProbe):
        def preflight(
            self,
            fixture: ReliabilityFixture,
        ) -> reliability_validation.HttpPreflightObservation:
            del fixture
            raise reliability_validation.LiveValidationError(
                "registered-service-binding",
                stage="preflight",
            )

    def fail_atomic_write(path: Path, payload: object) -> None:
        del payload
        writes.append(Path(path))
        partial = Path(path).with_name(f".{Path(path).name}.injected.partial")
        partial.write_text("incomplete", encoding="utf-8")
        partial.unlink()
        raise RuntimeError("token=writer-secret C:\\private\\model")

    monkeypatch.setattr(reliability_validation, "write_atomic_json", fail_atomic_write)
    output_root = tmp_path / "write-failure"
    result = reliability_validation.main(
        [
            "--fixture",
            str(fixture_path),
            "--output-root",
            str(output_root),
            "--comfyui-pid",
            "8188",
            "--tts-more-pid",
            "8000",
            "--preflight-only",
        ],
        probe_factory=lambda _fixture, _args: (
            TypedFailureProbe(),
            host_probe,
            host_probe.owned_processes,
        ),
    )

    assert result == 1
    assert writes == [output_root / "failure.json", output_root / "failure.json"]
    assert not (output_root / "failure.json").exists()
    assert not list(output_root.glob(".failure.json.*"))
    assert not (output_root / "preflight.json").exists()


def test_fix5_full_validator_secondary_failure_persistence_never_overrides_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    http_probe = _ExecutorHttpProbe(preflight_mode="busy-queue")
    host_probe = _ExecutorHostProbe()
    output_root = tmp_path / "matrix-primary"
    original_write = reliability_validation.write_atomic_json

    def fail_only_failure_marker(path: Path, payload: object) -> None:
        if Path(path).name == "failure.json":
            raise RuntimeError("token=secondary-writer-secret C:\\private\\model")
        original_write(path, payload)

    monkeypatch.setattr(
        reliability_validation,
        "write_atomic_json",
        fail_only_failure_marker,
    )

    with pytest.raises(reliability_validation.LiveValidationError) as exc_info:
        reliability_validation.execute_reliability_validation(
            fixture,
            output_root=output_root,
            http_probe=http_probe,
            host_probe=host_probe,
            owned_processes=host_probe.owned_processes,
        )

    assert (exc_info.value.code, exc_info.value.stage) == (
        "initial-queue-not-idle",
        "preflight",
    )
    assert not (output_root / "failure.json").exists()
    assert not list(output_root.glob(".failure.json.*"))


@pytest.mark.parametrize(
    ("failure_mode", "expected_code"),
    [
        ("invalid-fixture", "preflight-observation-failed"),
        ("typed", "registered-service-binding"),
        ("raw", "preflight-observation-failed"),
    ],
)
def test_fix5_reused_output_root_archives_stale_failure_and_publishes_current_primary(
    tmp_path: Path,
    failure_mode: str,
    expected_code: str,
) -> None:
    output_root = tmp_path / "reused-root"
    output_root.mkdir()
    stale_failure = {"code": "stale-prior-failure", "stage": "case"}
    (output_root / "failure.json").write_text(
        json.dumps(stale_failure),
        encoding="utf-8",
    )
    fixture_document = _fixture_document()
    if failure_mode == "invalid-fixture":
        fixture_document["rounds"] = 9
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture_document), encoding="utf-8")
    host_probe = _ExecutorHostProbe()

    class CurrentFailureProbe(_ExecutorHttpProbe):
        def preflight(
            self,
            fixture: ReliabilityFixture,
        ) -> reliability_validation.HttpPreflightObservation:
            del fixture
            if failure_mode == "typed":
                raise reliability_validation.LiveValidationError(
                    "registered-service-binding",
                    stage="preflight",
                )
            if failure_mode == "raw":
                raise httpx.ReadTimeout(
                    f"token=current-private {tmp_path}",
                )
            raise AssertionError("invalid fixture reached the probe")

    result = reliability_validation.main(
        [
            "--fixture",
            str(fixture_path),
            "--output-root",
            str(output_root),
            "--comfyui-pid",
            "8188",
            "--tts-more-pid",
            "8000",
            "--preflight-only",
        ],
        probe_factory=lambda _fixture, _args: (
            CurrentFailureProbe(),
            host_probe,
            host_probe.owned_processes,
        ),
    )

    assert result == 1
    assert json.loads((output_root / "failure.json").read_text(encoding="utf-8")) == {
        "code": expected_code,
        "stage": "preflight",
    }
    assert not (output_root / "preflight.json").exists()
    archives = list((output_root / "history" / "terminal-markers").glob("failure-*.json"))
    assert len(archives) == 1
    archived = json.loads(archives[0].read_text(encoding="utf-8"))
    assert archived["kind"] == "failure"
    assert archived["document"] == stale_failure
    assert len(archived["sha256"]) == 64


def test_fix5_reused_output_root_archives_stale_failure_before_current_success(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "reused-success"
    output_root.mkdir()
    stale_failure = {"code": "stale-prior-failure", "stage": "case"}
    (output_root / "failure.json").write_text(
        json.dumps(stale_failure),
        encoding="utf-8",
    )
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_fixture_document()), encoding="utf-8")
    http_probe = _ExecutorHttpProbe()
    host_probe = _ExecutorHostProbe()

    result = reliability_validation.main(
        [
            "--fixture",
            str(fixture_path),
            "--output-root",
            str(output_root),
            "--comfyui-pid",
            "8188",
            "--tts-more-pid",
            "8000",
            "--preflight-only",
        ],
        probe_factory=lambda _fixture, _args: (
            http_probe,
            host_probe,
            host_probe.owned_processes,
        ),
    )

    assert result == 0
    assert json.loads((output_root / "preflight.json").read_text(encoding="utf-8"))[
        "status"
    ] == "passed"
    assert not (output_root / "failure.json").exists()
    archives = list((output_root / "history" / "terminal-markers").glob("failure-*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8"))["document"] == stale_failure


def test_fix5_marker_archive_redacts_unsafe_prior_document(tmp_path: Path) -> None:
    output_root = tmp_path / "unsafe-prior"
    output_root.mkdir()
    secret = "token=stale-private-value"
    (output_root / "failure.json").write_text(
        json.dumps(
            {
                "code": "stale-prior-failure",
                "stage": "case",
                "detail": secret,
            }
        ),
        encoding="utf-8",
    )
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_fixture_document()), encoding="utf-8")
    host_probe = _ExecutorHostProbe()

    class TypedFailureProbe(_ExecutorHttpProbe):
        def preflight(
            self,
            fixture: ReliabilityFixture,
        ) -> reliability_validation.HttpPreflightObservation:
            del fixture
            raise reliability_validation.LiveValidationError(
                "registered-service-binding",
                stage="preflight",
            )

    result = reliability_validation.main(
        [
            "--fixture",
            str(fixture_path),
            "--output-root",
            str(output_root),
            "--comfyui-pid",
            "8188",
            "--tts-more-pid",
            "8000",
            "--preflight-only",
        ],
        probe_factory=lambda _fixture, _args: (
            TypedFailureProbe(),
            host_probe,
            host_probe.owned_processes,
        ),
    )

    assert result == 1
    assert json.loads((output_root / "failure.json").read_text(encoding="utf-8")) == {
        "code": "registered-service-binding",
        "stage": "preflight",
    }
    archives = list((output_root / "history" / "terminal-markers").glob("failure-*.json"))
    assert len(archives) == 1
    archived = json.loads(archives[0].read_text(encoding="utf-8"))
    assert archived["document_status"] == "redacted"
    assert "document" not in archived
    assert secret not in archives[0].name
    assert secret not in archives[0].read_text(encoding="utf-8")


def test_fix5_success_fails_closed_when_prior_marker_cannot_be_archived(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "archive-write-failure"
    output_root.mkdir()
    stale_failure = {"code": "stale-prior-failure", "stage": "case"}
    (output_root / "failure.json").write_text(
        json.dumps(stale_failure),
        encoding="utf-8",
    )
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_fixture_document()), encoding="utf-8")
    http_probe = _ExecutorHttpProbe()
    host_probe = _ExecutorHostProbe()
    original_write = reliability_validation.write_atomic_json

    def fail_archive_write(path: Path, payload: object, **kwargs: object) -> None:
        if Path(path).parent.name == "terminal-markers":
            raise OSError("token=secondary-archive-secret C:\\private\\marker")
        original_write(path, payload, **kwargs)

    monkeypatch.setattr(reliability_validation, "write_atomic_json", fail_archive_write)

    result = reliability_validation.main(
        [
            "--fixture",
            str(fixture_path),
            "--output-root",
            str(output_root),
            "--comfyui-pid",
            "8188",
            "--tts-more-pid",
            "8000",
            "--preflight-only",
        ],
        probe_factory=lambda _fixture, _args: (
            http_probe,
            host_probe,
            host_probe.owned_processes,
        ),
    )

    assert result == 1
    assert json.loads((output_root / "failure.json").read_text(encoding="utf-8")) == stale_failure
    assert not (output_root / "preflight.json").exists()
    assert not list((output_root / "history" / "terminal-markers").glob("*.json"))


def test_fix5_archive_failure_never_overrides_current_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    http_probe = _ExecutorHttpProbe(preflight_mode="busy-queue")
    host_probe = _ExecutorHostProbe()
    output_root = tmp_path / "archive-primary"
    output_root.mkdir()
    stale_failure = {"code": "stale-prior-failure", "stage": "case"}
    (output_root / "failure.json").write_text(
        json.dumps(stale_failure),
        encoding="utf-8",
    )
    original_write = reliability_validation.write_atomic_json

    def fail_archive_write(path: Path, payload: object, **kwargs: object) -> None:
        if Path(path).parent.name == "terminal-markers":
            raise OSError("token=secondary-archive-secret C:\\private\\marker")
        original_write(path, payload, **kwargs)

    monkeypatch.setattr(reliability_validation, "write_atomic_json", fail_archive_write)

    with pytest.raises(reliability_validation.LiveValidationError) as exc_info:
        reliability_validation.execute_reliability_validation(
            fixture,
            output_root=output_root,
            http_probe=http_probe,
            host_probe=host_probe,
            owned_processes=host_probe.owned_processes,
        )

    assert (exc_info.value.code, exc_info.value.stage) == (
        "initial-queue-not-idle",
        "preflight",
    )
    assert json.loads((output_root / "failure.json").read_text(encoding="utf-8")) == stale_failure
    assert not (output_root / "preflight.json").exists()


def test_fix5_marker_archive_collision_never_overwrites_prior_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "archive-collision"
    output_root.mkdir()
    stale_failure = {"code": "stale-prior-failure", "stage": "case"}
    stale_bytes = json.dumps(stale_failure).encode("utf-8")
    (output_root / "failure.json").write_bytes(stale_bytes)
    digest = hashlib.sha256(stale_bytes).hexdigest()
    archive_root = output_root / "history" / "terminal-markers"
    archive_root.mkdir(parents=True)
    collision = archive_root / f"failure-{digest[:16]}-{'a' * 32}.json"
    prior_history = {
        "kind": "failure",
        "sha256": "b" * 64,
        "document_status": "redacted",
    }
    collision.write_text(json.dumps(prior_history), encoding="utf-8")
    monkeypatch.setattr(reliability_validation.secrets, "token_hex", lambda _size: "a" * 32)
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_fixture_document()), encoding="utf-8")
    http_probe = _ExecutorHttpProbe()
    host_probe = _ExecutorHostProbe()

    result = reliability_validation.main(
        [
            "--fixture",
            str(fixture_path),
            "--output-root",
            str(output_root),
            "--comfyui-pid",
            "8188",
            "--tts-more-pid",
            "8000",
            "--preflight-only",
        ],
        probe_factory=lambda _fixture, _args: (
            http_probe,
            host_probe,
            host_probe.owned_processes,
        ),
    )

    assert result == 0
    assert json.loads(collision.read_text(encoding="utf-8")) == prior_history
    archives = sorted(archive_root.glob("failure-*.json"))
    assert len(archives) == 2
    current_archives = [path for path in archives if path != collision]
    assert len(current_archives) == 1
    assert json.loads(current_archives[0].read_text(encoding="utf-8"))["document"] == stale_failure


@pytest.mark.parametrize("business_path", ["primary-failure", "otherwise-success"])
@pytest.mark.parametrize(
    "fault_point",
    ["archive-first", "archive-second", "unlink", "publish-current"],
)
def test_fix5_two_marker_transition_reconciles_every_single_io_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    business_path: str,
    fault_point: str,
) -> None:
    output_root = tmp_path / "r"
    output_root.mkdir()
    stale_failure = {"code": "stale-prior-failure", "stage": "case"}
    stale_preflight = {"status": "passed"}
    (output_root / "failure.json").write_text(
        json.dumps(stale_failure),
        encoding="utf-8",
    )
    (output_root / "preflight.json").write_text(
        json.dumps(stale_preflight),
        encoding="utf-8",
    )
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_fixture_document()), encoding="utf-8")
    host_probe = _ExecutorHostProbe()

    class TypedFailureProbe(_ExecutorHttpProbe):
        def preflight(
            self,
            fixture: ReliabilityFixture,
        ) -> reliability_validation.HttpPreflightObservation:
            del fixture
            raise reliability_validation.LiveValidationError(
                "registered-service-binding",
                stage="preflight",
            )

    http_probe: _ExecutorHttpProbe
    if business_path == "primary-failure":
        http_probe = TypedFailureProbe()
        expected_failure = {
            "code": "registered-service-binding",
            "stage": "preflight",
        }
        current_marker_name = "failure.json"
    else:
        http_probe = _ExecutorHttpProbe()
        expected_failure = {
            "code": "public-marker-transition-failed",
            "stage": "preflight",
        }
        current_marker_name = "preflight.json"

    original_write = reliability_validation.write_atomic_json
    archive_write_count = 0
    fault_raised = False

    def injected_write(
        path: Path,
        payload: object,
        **kwargs: object,
    ) -> None:
        nonlocal archive_write_count, fault_raised
        path = Path(path)
        if path.parent.name == "terminal-markers":
            archive_write_count += 1
            archive_target = {
                "archive-first": 1,
                "archive-second": 2,
            }.get(fault_point)
            if archive_target == archive_write_count and not fault_raised:
                fault_raised = True
                raise OSError("token=archive-private C:\\private\\history")
        if (
            fault_point == "publish-current"
            and path.parent == output_root
            and path.name == current_marker_name
            and not fault_raised
        ):
            fault_raised = True
            raise OSError("token=publish-private C:\\private\\current")
        original_write(path, payload, **kwargs)

    monkeypatch.setattr(reliability_validation, "write_atomic_json", injected_write)
    original_unlink = Path.unlink

    def injected_unlink(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal fault_raised
        marker = Path(path)
        if (
            fault_point == "unlink"
            and marker.parent == output_root
            and marker.name in {"failure.json", "preflight.json"}
            and not fault_raised
        ):
            fault_raised = True
            raise OSError("token=unlink-private C:\\private\\current")
        original_unlink(marker, *args, **kwargs)

    if fault_point == "unlink":
        monkeypatch.setattr(Path, "unlink", injected_unlink)

    result = reliability_validation.main(
        [
            "--fixture",
            str(fixture_path),
            "--output-root",
            str(output_root),
            "--comfyui-pid",
            "8188",
            "--tts-more-pid",
            "8000",
            "--preflight-only",
        ],
        probe_factory=lambda _fixture, _args: (
            http_probe,
            host_probe,
            host_probe.owned_processes,
        ),
    )

    assert fault_raised is True, archive_write_count
    assert result == 1
    assert json.loads((output_root / "failure.json").read_text(encoding="utf-8")) == (
        expected_failure
    )
    assert not (output_root / "preflight.json").exists()
    archives = list((output_root / "history" / "terminal-markers").glob("*.json"))
    archived_documents = [
        json.loads(path.read_text(encoding="utf-8")).get("document")
        for path in archives
    ]
    assert stale_failure in archived_documents
    assert stale_preflight in archived_documents
    assert len(archives) == len({path.name for path in archives})
    assert all("token=" not in path.read_text(encoding="utf-8") for path in archives)


def _host_manifest_document(tmp_path: Path) -> dict[str, object]:
    created = datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat()
    parent_created = datetime(2026, 7, 31, 23, 59, tzinfo=timezone.utc).isoformat()
    repositories = {label: str((tmp_path / label).resolve()) for label in REPOSITORY_LABELS}
    for root in repositories.values():
        Path(root).mkdir(parents=True)
    registry = (tmp_path / "resources.yaml").resolve()
    resource_documents: dict[str, dict[str, str]] = {}
    resource_by_engine = {
        "gpt-sovits": ("gpt-main", "gpt_sovits", "gpt_sovits"),
        "indextts": ("index-main", "index_tts", "index_tts"),
        "cosyvoice": ("cosy-main", "cosyvoice", "cosyvoice"),
    }
    suite_root = Path(repositories["tts-audio-suite"])
    for engine, (resource_id, registry_engine, suite_engine) in resource_by_engine.items():
        source_root = Path(repositories[engine])
        interpreter = source_root / ".venv" / "Scripts" / "python.exe"
        interpreter.parent.mkdir(parents=True, exist_ok=True)
        interpreter.write_bytes(b"python")
        entrypoint = suite_root / "engines" / suite_engine / "external_subprocess_runner.py"
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text("# runner\n", encoding="utf-8")
        resource_documents[resource_id] = {
            "engine": registry_engine,
            "source_root": str(source_root),
        }
    registry.write_text(
        json.dumps({"version": 1, "resources": resource_documents}),
        encoding="utf-8",
    )
    reference = (tmp_path / "reference.wav").resolve()
    _write_voiced_wav(reference)
    python = (tmp_path / "python.exe").resolve()
    python.write_bytes(b"python")
    run_id = "a" * 32
    temp_root = (tmp_path / f"reliability-temp-{run_id}").resolve()
    runner_temp_root = temp_root / "runner"
    comfy_temp_root = temp_root / "comfyui" / "temp"
    runner_temp_root.mkdir(parents=True)
    comfy_temp_root.mkdir(parents=True)
    (tmp_path / f".request-temp-{run_id}.owner.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "temp_root": str(temp_root),
                "runner_temp_root": str(runner_temp_root),
                "comfy_temp_root": str(comfy_temp_root),
            }
        ),
        encoding="utf-8",
    )
    document = {
        "version": 1,
        "run_id": run_id,
        "owned_processes": {
            "tts-more": {
                "pid": 8000,
                "creation_time": created,
                "executable_path": str(python),
                "command_line": "python -m uvicorn app.main:app",
                "parent_pid": 7000,
                "parent_creation_time": parent_created,
            },
            "comfyui": {
                "pid": 8188,
                "creation_time": created,
                "executable_path": str(python),
                "command_line": "python main.py --listen 127.0.0.1 --port 8188",
                "parent_pid": 7000,
                "parent_creation_time": parent_created,
            },
        },
        "launch": {
            "comfyui": {
                "executable_path": str(python),
                "arguments": ["main.py", "--listen", "127.0.0.1", "--port", "8188"],
                "working_directory": str(tmp_path.resolve()),
                "port": 8188,
                "temp_root": str(runner_temp_root),
            }
        },
        "boundary": {
            "repositories": repositories,
            "private_registry": str(registry),
            "references": {"reference": str(reference)},
        },
        "temp_roots": [str(runner_temp_root), str(comfy_temp_root)],
    }
    document["launch_roots"] = json.loads(json.dumps(document["owned_processes"]))
    return document


class _FakeWindowsHostSystem:
    def __init__(
        self,
        manifest: dict[str, object],
        *,
        mismatch: bool = False,
        runner_inventories: list[tuple[reliability_validation.RecordedProcessIdentity, ...]] | None = None,
    ) -> None:
        owned = manifest["owned_processes"]
        assert isinstance(owned, dict)
        self.records = {
            label: reliability_validation.RecordedProcessIdentity.from_document(document)
            for label, document in owned.items()
        }
        self.mismatch = mismatch
        self.stopped: list[int] = []
        self.restarted = 0
        self.boundary = _ExecutorHostProbe().boundary
        self.case_number = 0
        self.runner_inventories = list(runner_inventories or [])

    def inspect_process(self, pid: int) -> reliability_validation.RecordedProcessIdentity:
        record = next(item for item in self.records.values() if item.pid == pid)
        if self.mismatch and pid == 8188:
            return reliability_validation.RecordedProcessIdentity(
                **{**record.__dict__, "creation_time": record.creation_time + timedelta(seconds=1)}
            )
        return record

    def port_owner(self, port: int) -> reliability_validation.RecordedProcessIdentity | None:
        label = "tts-more" if port == 8000 else "comfyui"
        return self.inspect_process(self.records[label].pid)

    def capture_boundary(
        self,
        specification: reliability_validation.PrivateBoundarySpecification,
    ) -> reliability_validation.BoundarySnapshot:
        del specification
        return self.boundary

    def gpu_snapshot(self) -> GpuSnapshot:
        return GpuSnapshot(used_mib=100, free_mib=8000)

    def matching_runners(
        self,
        specifications: tuple[object, ...],
    ) -> tuple[reliability_validation.RecordedProcessIdentity, ...]:
        assert len(specifications) == 3
        return self.runner_inventories.pop(0) if self.runner_inventories else ()

    def begin_case(
        self,
        case: reliability_validation.CasePlan,
        roots: tuple[reliability_validation.RecordedProcessIdentity, ...],
        temp_roots: tuple[Path, ...],
    ) -> object:
        del case, roots, temp_roots
        self.case_number += 1
        return datetime(2026, 8, 1, 1, self.case_number, tzinfo=timezone.utc)

    def finish_case(
        self,
        token: object,
        convergence_seconds: float,
    ) -> reliability_validation.HostCaseObservation:
        del convergence_seconds
        assert isinstance(token, datetime)
        return _ExecutorHostProbe().finish_case(
            reliability_validation.build_case_plan()[0],
            token,
        )

    def stop_owned(self, identity: reliability_validation.RecordedProcessIdentity) -> None:
        self.stopped.append(identity.pid)

    def restart_owned(
        self,
        identity: reliability_validation.RecordedProcessIdentity,
        launch: reliability_validation.PrivateLaunchSpecification,
        convergence_seconds: float,
        *,
        run_id: str,
        lifecycle: reliability_validation.PrivateRestartLifecycle,
    ) -> reliability_validation.RecordedProcessIdentity:
        del convergence_seconds
        self.restarted += 1
        marker = f"tts_more_reliability_run={run_id}-comfyui-restart-{'c' * 32}"
        arguments = ("-X", marker, *launch.arguments)
        started_after = identity.creation_time + timedelta(minutes=self.restarted)
        intent = reliability_validation.PrivateRestartLaunchIntent(
            marker=marker,
            executable_path=launch.executable_path,
            arguments=arguments,
            working_directory=launch.working_directory,
            child_temp_root=launch.temp_root,
            parent_pid=identity.parent_pid,
            parent_creation_time=identity.parent_creation_time,
            started_after=started_after,
        )
        lifecycle.persist_launch_intent(intent)
        provisional = reliability_validation.PrivateRestartProvisionalProcess(
            pid=identity.pid + self.restarted * 10_000,
            executable_path=launch.executable_path,
            parent_pid=identity.parent_pid,
            parent_creation_time=identity.parent_creation_time,
            started_after=started_after,
            started_before=started_after + timedelta(seconds=1),
        )
        lifecycle.persist_provisional(provisional)
        replacement = reliability_validation.RecordedProcessIdentity(
            **{
                **identity.__dict__,
                "pid": provisional.pid,
                "creation_time": started_after,
                "executable_path": launch.executable_path,
                "command_line": subprocess.list2cmdline(
                    [str(launch.executable_path), *arguments]
                ),
            }
        )
        self.records["comfyui"] = replacement
        lifecycle.promote(replacement)
        return replacement

    def final_cleanup_state(self, temp_roots: tuple[Path, ...]) -> tuple[bool, bool]:
        del temp_roots
        return True, True


class _RestartLifecycleWindowsHostSystem(_FakeWindowsHostSystem):
    def __init__(self, manifest: dict[str, object], control_state_path: Path, *, interrupt_at: str | None = None) -> None:
        super().__init__(manifest)
        self.control_state_path = control_state_path
        self.interrupt_at = interrupt_at
        self.control_snapshots: list[dict[str, object]] = []

    def _capture_control(self) -> None:
        self.control_snapshots.append(
            json.loads(self.control_state_path.read_text(encoding="utf-8"))
        )

    def restart_owned(
        self,
        identity: reliability_validation.RecordedProcessIdentity,
        launch: reliability_validation.PrivateLaunchSpecification,
        convergence_seconds: float,
        *,
        run_id: str,
        lifecycle: reliability_validation.PrivateRestartLifecycle,
    ) -> reliability_validation.RecordedProcessIdentity:
        del convergence_seconds
        marker = f"tts_more_reliability_run={run_id}-comfyui-restart-{'b' * 32}"
        arguments = ("-X", marker, *launch.arguments)
        started_after = identity.creation_time + timedelta(minutes=1)
        intent = reliability_validation.PrivateRestartLaunchIntent(
            marker=marker,
            executable_path=launch.executable_path,
            arguments=arguments,
            working_directory=launch.working_directory,
            child_temp_root=launch.temp_root,
            parent_pid=identity.parent_pid,
            parent_creation_time=identity.parent_creation_time,
            started_after=started_after,
        )
        lifecycle.persist_launch_intent(intent)
        self._capture_control()
        if self.interrupt_at == "intent":
            raise reliability_validation.RestartLifecycleError(
                "injected interruption after launch intent",
                cleanup_proven=False,
            )

        provisional = reliability_validation.PrivateRestartProvisionalProcess(
            pid=18_188,
            executable_path=launch.executable_path,
            parent_pid=identity.parent_pid,
            parent_creation_time=identity.parent_creation_time,
            started_after=started_after,
            started_before=started_after + timedelta(seconds=1),
        )
        lifecycle.persist_provisional(provisional)
        self._capture_control()
        if self.interrupt_at == "provisional":
            raise reliability_validation.RestartLifecycleError(
                "injected interruption after provisional identity",
                cleanup_proven=False,
            )

        replacement = reliability_validation.RecordedProcessIdentity(
            pid=provisional.pid,
            creation_time=started_after + timedelta(milliseconds=500),
            executable_path=launch.executable_path,
            command_line=subprocess.list2cmdline(
                [str(launch.executable_path), *arguments]
            ),
            parent_pid=provisional.parent_pid,
            parent_creation_time=provisional.parent_creation_time,
        )
        self.records["comfyui"] = replacement
        lifecycle.promote(replacement)
        self._capture_control()
        self.restarted += 1
        return replacement


def _recorded_runner_identity(
    *,
    executable: Path,
    argv: list[str],
    pid: int = 19001,
) -> reliability_validation.RecordedProcessIdentity:
    created = datetime(2026, 8, 1, 2, 0, tzinfo=timezone.utc)
    return reliability_validation.RecordedProcessIdentity(
        pid=pid,
        creation_time=created,
        executable_path=executable.resolve(),
        command_line=subprocess.list2cmdline(argv),
        parent_pid=8188,
        parent_creation_time=created - timedelta(seconds=1),
    )


def test_fix_round_1_runner_fingerprint_is_exact_and_accepts_verified_prior_run_root(
    tmp_path: Path,
) -> None:
    executable = (tmp_path / "gpt" / ".venv" / "Scripts" / "python.exe").resolve()
    entrypoint = (
        tmp_path
        / "suite"
        / "engines"
        / "gpt_sovits"
        / "external_subprocess_runner.py"
    ).resolve()
    current_root = (tmp_path / "reliability-temp-current" / "runner").resolve()
    prior_root = (tmp_path / "reliability-temp-prior" / "runner").resolve()
    request = prior_root / "tts-audio-suite-gptsovits-123" / "request.json"
    specification = reliability_validation.PrivateRunnerSpecification(
        engine="gpt-sovits",
        executable_path=executable,
        entrypoint_path=entrypoint,
        temp_prefix="tts-audio-suite-gptsovits-",
        request_roots=(current_root, prior_root),
    )
    exact = _recorded_runner_identity(
        executable=executable,
        argv=[str(executable), str(entrypoint), str(request)],
    )
    assert reliability_validation._process_matches_runner_specification(exact, specification) is True

    wrong_executable = (tmp_path / "other" / "python.exe").resolve()
    wrong_entrypoint = (tmp_path / "elsewhere" / "external_subprocess_runner.py").resolve()
    wrong_engine = (
        tmp_path
        / "suite"
        / "engines"
        / "index_tts"
        / "external_subprocess_runner.py"
    ).resolve()
    outside_request = (
        tmp_path / "outside" / "tts-audio-suite-gptsovits-123" / "request.json"
    ).resolve()
    near_misses = [
        _recorded_runner_identity(
            executable=wrong_executable,
            argv=[str(wrong_executable), str(entrypoint), str(request)],
            pid=19002,
        ),
        _recorded_runner_identity(
            executable=executable,
            argv=[str(executable), str(wrong_entrypoint), str(request)],
            pid=19003,
        ),
        _recorded_runner_identity(
            executable=executable,
            argv=[str(executable), str(wrong_engine), str(request)],
            pid=19004,
        ),
        _recorded_runner_identity(
            executable=executable,
            argv=[str(executable), str(entrypoint), str(outside_request)],
            pid=19005,
        ),
        _recorded_runner_identity(
            executable=executable,
            argv=[str(executable), "-c", "print('not a runner')"],
            pid=19006,
        ),
        _recorded_runner_identity(
            executable=wrong_executable,
            argv=["whoami.exe"],
            pid=19007,
        ),
        _recorded_runner_identity(
            executable=executable,
            argv=[str(executable), str(entrypoint), str(request), "extra"],
            pid=19008,
        ),
    ]
    assert all(
        not reliability_validation._process_matches_runner_specification(identity, specification)
        for identity in near_misses
    )


def test_fix_round_2_native_runner_inventory_detects_prior_run_orphan_without_live_parent(
    tmp_path: Path,
) -> None:
    executable = (tmp_path / "engine" / ".venv" / "Scripts" / "python.exe").resolve()
    entrypoint = (
        tmp_path
        / "suite"
        / "engines"
        / "gpt_sovits"
        / "external_subprocess_runner.py"
    ).resolve()
    request_root = (tmp_path / "reliability-temp-prior" / "runner").resolve()
    request = request_root / "tts-audio-suite-gptsovits-orphan" / "request.json"
    specification = reliability_validation.PrivateRunnerSpecification(
        engine="gpt-sovits",
        executable_path=executable,
        entrypoint_path=entrypoint,
        temp_prefix="tts-audio-suite-gptsovits-",
        request_roots=(request_root,),
    )
    orphan = {
        "pid": 19009,
        "creation_time": "2026-08-01T01:00:00.0000000Z",
        "name": executable.name,
        "executable_path": str(executable),
        "command_line": subprocess.list2cmdline(
            [str(executable), str(entrypoint), str(request)]
        ),
        "parent_pid": 18888,
    }
    system = object.__new__(reliability_validation.NativeWindowsHostSystem)
    system._started_identities = {}
    system._powershell_document = lambda *_args, **_kwargs: [orphan]

    assert system.matching_runners((specification,)) == (19009,)


@pytest.mark.parametrize(
    "mutation",
    [
        {"command_line": ""},
        {"executable_path": ""},
        {"executable_path": "", "command_line": ""},
        {"command_line": '"unterminated'},
    ],
)
def test_fix_round_2_native_runner_inventory_fails_closed_for_incomplete_candidate(
    tmp_path: Path,
    mutation: dict[str, str],
) -> None:
    executable = (tmp_path / "engine" / ".venv" / "Scripts" / "python.exe").resolve()
    entrypoint = (
        tmp_path
        / "suite"
        / "engines"
        / "gpt_sovits"
        / "external_subprocess_runner.py"
    ).resolve()
    request_root = (tmp_path / "reliability-temp-prior" / "runner").resolve()
    request = request_root / "tts-audio-suite-gptsovits-orphan" / "request.json"
    specification = reliability_validation.PrivateRunnerSpecification(
        engine="gpt-sovits",
        executable_path=executable,
        entrypoint_path=entrypoint,
        temp_prefix="tts-audio-suite-gptsovits-",
        request_roots=(request_root,),
    )
    candidate = {
        "pid": 19010,
        "creation_time": "2026-08-01T01:00:00.0000000Z",
        "name": executable.name,
        "executable_path": str(executable),
        "command_line": subprocess.list2cmdline(
            [str(executable), str(entrypoint), str(request)]
        ),
        "parent_pid": 18888,
        **mutation,
    }
    system = object.__new__(reliability_validation.NativeWindowsHostSystem)
    system._started_identities = {}
    system._powershell_document = lambda *_args, **_kwargs: [candidate]

    with pytest.raises(RuntimeError, match="runner inventory"):
        system.matching_runners((specification,))


@pytest.mark.parametrize(
    ("phase", "expected_code"),
    [
        ("preflight", "pre-existing-external-runner"),
        ("final", "final-external-runner-present"),
    ],
)
def test_fix_round_1_runner_inventory_fails_gate_without_stopping_process(
    tmp_path: Path,
    phase: str,
    expected_code: str,
) -> None:
    document = _host_manifest_document(tmp_path)
    owned = document["owned_processes"]
    assert isinstance(owned, dict)
    observed = reliability_validation.RecordedProcessIdentity.from_document(owned["comfyui"])
    inventories = [(observed,)] if phase == "preflight" else [(), (observed,)]
    system = _FakeWindowsHostSystem(document, runner_inventories=inventories)
    manifest_path = tmp_path / ".host-manifest-private.json"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    probe = reliability_validation.WindowsReliabilityHostProbe.from_manifest(
        manifest_path,
        system=system,
    )
    fixture = ReliabilityFixture.model_validate(_fixture_document())

    with pytest.raises(reliability_validation.LiveValidationError) as exc_info:
        if phase == "preflight":
            probe.preflight(fixture)
        else:
            probe.preflight(fixture)
            probe.final_state()

    assert exc_info.value.code == expected_code
    assert system.stopped == []


def test_task_10_native_final_cleanup_detects_residue_in_configured_runner_temp_root(
    tmp_path: Path,
) -> None:
    temp_root = tmp_path / "owned-temp" / "system"
    request_root = temp_root / "tts-audio-suite-gptsovits-residue"
    request_root.mkdir(parents=True)
    (request_root / "request.json").write_text("{}", encoding="utf-8")
    system = object.__new__(reliability_validation.NativeWindowsHostSystem)
    system._active_tokens = []

    assert system.final_cleanup_state((temp_root,)) == (True, False)

    (request_root / "request.json").unlink()
    request_root.rmdir()
    assert system.final_cleanup_state((temp_root,)) == (True, True)


def test_task_12_powershell_process_helpers_accept_current_process_identity() -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "run-windows-comfyui-reliability.ps1"
    )
    command = r"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @(
    'Get-UtcTicks',
    'Get-PortOwnerPid',
    'Get-ProcessRecord',
    'Test-ProcessAbsent',
    'Wait-ProcessRecord',
    'Wait-ExactPortOwner'
)) {
    $function = $ast.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    if ($null -eq $function) { throw "$name is missing" }
    Invoke-Expression $function.Extent.Text
}

$record = Get-ProcessRecord $PID
if ([int] $record.pid -ne $PID) {
    throw 'Get-ProcessRecord did not return the current process identity'
}
if (Test-ProcessAbsent $PID) {
    throw 'Test-ProcessAbsent reported the current process absent'
}
$waited = Wait-ProcessRecord $PID 2
if ([int] $waited.pid -ne $PID) {
    throw 'Wait-ProcessRecord did not return the current process identity'
}

$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
try {
    $port = [int] ([Net.IPEndPoint] $listener.LocalEndpoint).Port
    Wait-ExactPortOwner $port $PID 10
    if ((Get-PortOwnerPid $port) -ne $PID) {
        throw 'Wait-ExactPortOwner did not preserve exact current-process ownership'
    }
} finally {
    $listener.Stop()
}
Write-Output 'PROCESS_HELPER_CURRENT_PID_OK'
"""
    environment = os.environ.copy()
    environment["TTS_MORE_RELIABILITY_SCRIPT"] = str(script_path)

    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "PROCESS_HELPER_CURRENT_PID_OK" in completed.stdout


def test_task_12_wrapper_cleans_owned_empty_temp_after_launcher_identity_failure(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    repository_root = Path(__file__).resolve().parents[2]
    script_path = repository_root / "scripts" / "run-windows-comfyui-reliability.ps1"
    output_root = tmp_path / "validation output"
    output_root.mkdir()
    sentinel_directory = output_root / "unrelated sentinel directory"
    sentinel_directory.mkdir()
    sentinel_file = output_root / "unrelated-sentinel.txt"
    sentinel_file.write_text("retain-exactly", encoding="utf-8")

    fixture_root = tmp_path / "fixture"
    reference_root = fixture_root / "references"
    reference_root.mkdir(parents=True)
    resources: dict[str, dict[str, str]] = {}
    for engine in ("gpt-sovits", "indextts", "cosyvoice"):
        reference = reference_root / f"{engine}.wav"
        reference.write_bytes(b"reference")
        resources[engine] = {
            "reference_audio": f"references/{engine}.wav",
        }
    fixture_path = fixture_root / "fixture.json"
    fixture_path.write_text(json.dumps({"resources": resources}), encoding="utf-8")

    comfy_root = tmp_path / "ComfyUI"
    (comfy_root / "custom_nodes" / "TTS-Audio-Suite").mkdir(parents=True)
    fake_comfy_python = tmp_path / "never-started-comfy-python.exe"
    fake_comfy_python.write_bytes(b"not executable")
    registry_path = tmp_path / "resources.yaml"
    registry_path.write_text("resources: {}\n", encoding="utf-8")
    engine_roots: dict[str, Path] = {}
    for engine in ("gpt-sovits", "indextts", "cosyvoice"):
        engine_root = tmp_path / f"{engine}-root"
        engine_root.mkdir()
        engine_roots[engine] = engine_root

    command = r"""
function Get-PortOwners {
    param([int] $Port)
    return @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique |
            Sort-Object
    )
}
function Get-MatchingProcessIdentities {
    return @(
        CimCmdlets\Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                $_.ExecutablePath -and
                [IO.Path]::GetFullPath([string] $_.ExecutablePath).Equals(
                    [IO.Path]::GetFullPath($env:TTS_MORE_TEST_COMFY_PYTHON),
                    [StringComparison]::OrdinalIgnoreCase
                )
            } |
            ForEach-Object {
                '{0}:{1}' -f $_.ProcessId, $_.CreationDate.ToUniversalTime().Ticks
            } |
            Sort-Object
    )
}

$port8000Before = @(Get-PortOwners 8000)
$port8188Before = @(Get-PortOwners 8188)
$matchingBefore = @(Get-MatchingProcessIdentities)
function Get-CimInstance {
    param([string] $ClassName, [string] $Filter, [object] $ErrorAction)
    $ownedRoots = @(
        Get-ChildItem -LiteralPath $env:TTS_MORE_TEST_OUTPUT -Directory `
            -Filter 'reliability-temp-*'
    )
    $ownerMarkers = @(
        Get-ChildItem -LiteralPath $env:TTS_MORE_TEST_OUTPUT -File `
            -Filter '.request-temp-*.owner.json'
    )
    if ($ownedRoots.Count -ne 1 -or $ownerMarkers.Count -ne 1) {
        throw 'launcher identity failure was not injected after owned artifacts existed'
    }
    $owner = Get-Content -LiteralPath $ownerMarkers[0].FullName -Raw |
        ConvertFrom-Json
    if (
        $owner.temp_root -ne $ownedRoots[0].FullName -or
        @(Get-ChildItem -LiteralPath $ownedRoots[0].FullName -Recurse -File).Count -ne 0
    ) { throw 'launcher identity failure did not observe the exact empty owned temp tree' }
    throw 'injected launcher identity failure'
}

$caught = $null
try {
    & $env:TTS_MORE_RELIABILITY_SCRIPT `
        -Fixture $env:TTS_MORE_TEST_FIXTURE `
        -OutputRoot $env:TTS_MORE_TEST_OUTPUT `
        -ComfyUiRoot $env:TTS_MORE_TEST_COMFY_ROOT `
        -ComfyPython $env:TTS_MORE_TEST_COMFY_PYTHON `
        -TtsMoreRoot $env:TTS_MORE_TEST_TTS_ROOT `
        -PreflightOnly
} catch {
    $caught = $_
}

if ($null -eq $caught -or $caught.Exception.Message -ne 'injected launcher identity failure') {
    throw ('Expected injected launcher identity failure, got: {0}' -f $caught)
}
if (@(Get-ChildItem -LiteralPath $env:TTS_MORE_TEST_OUTPUT -Directory -Filter 'reliability-temp-*').Count -ne 0) {
    throw 'run-owned temp root survived early launcher identity failure'
}
if (@(Get-ChildItem -LiteralPath $env:TTS_MORE_TEST_OUTPUT -File -Filter '.request-temp-*.owner.json').Count -ne 0) {
    throw 'run-owned temp marker survived early launcher identity failure'
}
if (
    -not (Test-Path -LiteralPath $env:TTS_MORE_TEST_SENTINEL_DIRECTORY -PathType Container) -or
    (Get-Content -LiteralPath $env:TTS_MORE_TEST_SENTINEL_FILE -Raw) -ne 'retain-exactly'
) { throw 'unrelated output-root sentinels changed' }
if (
    (@(Get-PortOwners 8000) -join ',') -ne ($port8000Before -join ',') -or
    (@(Get-PortOwners 8188) -join ',') -ne ($port8188Before -join ',')
) { throw 'launcher identity failure changed port ownership' }
if ((@(Get-MatchingProcessIdentities) -join ',') -ne ($matchingBefore -join ',')) {
    throw 'launcher identity failure changed the configured child-process set'
}
Write-Output 'EARLY_LAUNCHER_FAILURE_CLEANUP_OK'
"""
    environment = os.environ.copy()
    environment.update(
        {
            "TTS_MORE_RELIABILITY_SCRIPT": str(script_path),
            "TTS_MORE_TEST_FIXTURE": str(fixture_path),
            "TTS_MORE_TEST_OUTPUT": str(output_root),
            "TTS_MORE_TEST_COMFY_ROOT": str(comfy_root),
            "TTS_MORE_TEST_COMFY_PYTHON": str(fake_comfy_python),
            "TTS_MORE_TEST_TTS_ROOT": str(repository_root),
            "TTS_MORE_TEST_SENTINEL_DIRECTORY": str(sentinel_directory),
            "TTS_MORE_TEST_SENTINEL_FILE": str(sentinel_file),
            "TTS_MORE_RELIABILITY_GPT_SOVITS_ROOT": str(
                engine_roots["gpt-sovits"]
            ),
            "TTS_MORE_RELIABILITY_INDEXTTS_ROOT": str(engine_roots["indextts"]),
            "TTS_MORE_RELIABILITY_COSYVOICE_ROOT": str(engine_roots["cosyvoice"]),
            "TTS_AUDIO_SUITE_RESOURCES": str(registry_path),
        }
    )

    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "EARLY_LAUNCHER_FAILURE_CLEANUP_OK" in completed.stdout


def test_task_12_powershell_absent_cim_result_is_not_a_query_failure() -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "run-windows-comfyui-reliability.ps1"
    )
    command = r"""
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @(
    'Get-UtcTicks',
    'Get-ProcessRecord',
    'Test-RecordedIdentity',
    'Test-ProcessAbsent',
    'Stop-ProvisionalStartedProcess',
    'Stop-RecordedTree'
)) {
    $function = $ast.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    if ($null -eq $function) { throw "$name is missing" }
    Invoke-Expression $function.Extent.Text
}

$script:cimMode = 'absent'
$script:cimFilters = New-Object 'System.Collections.Generic.List[string]'
function Get-CimInstance {
    param([string] $ClassName, [string] $Filter, [object] $ErrorAction)
    if ($Filter) { $script:cimFilters.Add($Filter) }
    if ($script:cimMode -eq 'error') {
        throw 'injected exact-filter CIM query failure'
    }
    if ($script:cimMode -eq 'incomplete' -and $Filter -eq 'ProcessId = 4242') {
        return [pscustomobject]@{ ProcessId = 4242 }
    }
    if ($script:cimMode -eq 'parent-absent' -and $Filter -eq 'ProcessId = 4242') {
        return [pscustomobject]@{
            ProcessId = 4242
            CreationDate = [DateTime]::Parse('2026-08-01T00:00:00Z').ToUniversalTime()
            ExecutablePath = 'C:\controlled\python.exe'
            CommandLine = 'python.exe controlled.py'
            ParentProcessId = 111
        }
    }
    return $null
}
function Stop-Process { throw 'an absent PID must never be stopped' }

$record = [pscustomobject]@{
    pid = 4242
    creation_time = '2026-08-01T00:00:00Z'
    executable_path = 'C:\controlled\python.exe'
    command_line = 'python.exe controlled.py'
    parent_pid = 111
    parent_creation_time = '2026-07-31T23:59:00Z'
}
$token = [pscustomobject]@{
    pid = 4242
    executable_path = 'C:\controlled\python.exe'
    parent_pid = 111
    parent_creation_time = '2026-07-31T23:59:00Z'
    started_after = '2026-07-31T23:59:59Z'
    started_before = '2026-08-01T00:00:01Z'
}
$failures = New-Object 'System.Collections.Generic.List[string]'

try {
    if (-not (Test-ProcessAbsent -ProcessId 4242)) {
        $failures.Add('successful null exact-filter result was not absent')
    }
} catch { $failures.Add('absent predicate accessed a null property: ' + $_.Exception.Message) }
try {
    if (Test-RecordedIdentity -Record $record) {
        $failures.Add('missing process matched a recorded identity')
    }
} catch { $failures.Add('record matcher accessed a null property: ' + $_.Exception.Message) }
try {
    $null = Get-ProcessRecord -ProcessId 4242
    $failures.Add('missing process unexpectedly produced a full record')
} catch {
    if ($_.Exception.Message -ne 'Process identity is absent') {
        $failures.Add('missing process did not produce the bounded absent error: ' + $_.Exception.Message)
    }
}
try {
    if (-not (Stop-ProvisionalStartedProcess -Token $token)) {
        $failures.Add('already-absent provisional identity was not cleanup-proven')
    }
} catch { $failures.Add('provisional cleanup accessed a null property: ' + $_.Exception.Message) }
try {
    if (-not (Stop-RecordedTree -Record $record)) {
        $failures.Add('already-absent full identity was not cleanup-proven')
    }
} catch { $failures.Add('full cleanup accessed a null property: ' + $_.Exception.Message) }

$script:cimMode = 'incomplete'
try {
    if (Test-RecordedIdentity -Record $record) {
        $failures.Add('incomplete live process matched a recorded identity')
    }
} catch { $failures.Add('record matcher accessed a missing property: ' + $_.Exception.Message) }
try {
    $null = Get-ProcessRecord -ProcessId 4242
    $failures.Add('incomplete live process unexpectedly produced a full record')
} catch {
    if ($_.Exception.Message -ne 'Process identity is incomplete') {
        $failures.Add('incomplete process did not produce the bounded error: ' + $_.Exception.Message)
    }
}

$script:cimMode = 'parent-absent'
try {
    $null = Get-ProcessRecord -ProcessId 4242
    $failures.Add('missing parent unexpectedly produced a full record')
} catch {
    if ($_.Exception.Message -ne 'Parent process identity is absent') {
        $failures.Add('missing parent did not produce the bounded absent error: ' + $_.Exception.Message)
    }
}

$script:cimMode = 'error'
$queryError = $null
try { $null = Test-ProcessAbsent -ProcessId 4242 } catch { $queryError = $_ }
if (
    $null -eq $queryError -or
    $queryError.Exception.Message -ne 'injected exact-filter CIM query failure'
) { $failures.Add('CIM query error was incorrectly converted to absence') }

if (@($script:cimFilters | Where-Object { $_ -notmatch '^ProcessId = \d+$' }).Count -ne 0) {
    $failures.Add('process helper issued a non-exact CIM filter')
}
if ($failures.Count -ne 0) { throw ($failures -join '; ') }
Write-Output 'NULL_CIM_ABSENCE_SEMANTICS_OK'
"""
    environment = os.environ.copy()
    environment["TTS_MORE_RELIABILITY_SCRIPT"] = str(script_path)

    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "NULL_CIM_ABSENCE_SEMANTICS_OK" in completed.stdout


def test_task_12_port_readiness_rejects_reused_pid_after_tracked_child_exit() -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "run-windows-comfyui-reliability.ps1"
    )
    command = r"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
$function = $ast.Find({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Wait-ExactPortOwner'
}, $true)
if ($null -eq $function) { throw 'Wait-ExactPortOwner is missing' }
Invoke-Expression $function.Extent.Text

function Get-PortOwnerPid { param([int] $Port) return 4242 }
$script:waitCalls = 0
$exited = [pscustomobject]@{ HasExited = $true }
$exited | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {
    $script:waitCalls += 1
}
$caught = $null
try {
    Wait-ExactPortOwner -Port 8188 -ProcessId 4242 -TimeoutSeconds 1 `
        -Process $exited
} catch { $caught = $_ }
if (
    $null -eq $caught -or
    $caught.Exception.Message -notlike 'Owned process exited before acquiring port 8188*'
) { throw 'reused numeric port-owner PID was accepted after the tracked child exited' }
if ($script:waitCalls -ne 1) {
    throw 'tracked exited process was not reaped before readiness failed'
}
Write-Output 'EXITED_PROCESS_PID_REUSE_REJECTED_OK'
"""
    environment = os.environ.copy()
    environment["TTS_MORE_RELIABILITY_SCRIPT"] = str(script_path)

    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "EXITED_PROCESS_PID_REUSE_REJECTED_OK" in completed.stdout


def test_task_12_wrapper_preserves_exited_child_startup_logs_and_primary_error(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    repository_root = Path(__file__).resolve().parents[2]
    script_path = repository_root / "scripts" / "run-windows-comfyui-reliability.ps1"
    output_root = tmp_path / "private evidence"
    output_root.mkdir()
    fixture_root = tmp_path / "fixture"
    reference_root = fixture_root / "references"
    reference_root.mkdir(parents=True)
    resources: dict[str, dict[str, str]] = {}
    for engine in ("gpt-sovits", "indextts", "cosyvoice"):
        reference = reference_root / f"{engine}.wav"
        reference.write_bytes(b"reference")
        resources[engine] = {"reference_audio": f"references/{engine}.wav"}
    fixture_path = fixture_root / "fixture.json"
    fixture_path.write_text(json.dumps({"resources": resources}), encoding="utf-8")

    comfy_root = tmp_path / "controlled ComfyUI"
    (comfy_root / "custom_nodes" / "TTS-Audio-Suite").mkdir(parents=True)
    argv_capture = tmp_path / "controlled-child-argv.json"
    (comfy_root / "main.py").write_text(
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['TTS_MORE_CONTROLLED_ARGV']).write_text(\n"
        "    json.dumps(sys.argv[1:]), encoding='utf-8'\n"
        ")\n"
        "sys.stdout.write('CONTROLLED_COMFY_STDOUT')\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('CONTROLLED_COMFY_STDERR')\n"
        "sys.stderr.flush()\n"
        "time.sleep(1.0)\n"
        "raise SystemExit(23)\n",
        encoding="utf-8",
    )
    registry_path = tmp_path / "resources.yaml"
    registry_path.write_text("resources: {}\n", encoding="utf-8")
    engine_roots: dict[str, Path] = {}
    for engine in ("gpt-sovits", "indextts", "cosyvoice"):
        engine_root = tmp_path / f"{engine}-root"
        engine_root.mkdir()
        engine_roots[engine] = engine_root

    environment = os.environ.copy()
    environment.update(
        {
            "TTS_MORE_CONTROLLED_ARGV": str(argv_capture),
            "TTS_MORE_RELIABILITY_GPT_SOVITS_ROOT": str(
                engine_roots["gpt-sovits"]
            ),
            "TTS_MORE_RELIABILITY_INDEXTTS_ROOT": str(engine_roots["indextts"]),
            "TTS_MORE_RELIABILITY_COSYVOICE_ROOT": str(engine_roots["cosyvoice"]),
            "TTS_AUDIO_SUITE_RESOURCES": str(registry_path),
        }
    )
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script_path),
            "-Fixture",
            str(fixture_path),
            "-OutputRoot",
            str(output_root),
            "-ComfyUiRoot",
            str(comfy_root),
            "-ComfyPython",
            str(Path(os.sys.executable).resolve()),
            "-TtsMoreRoot",
            str(repository_root),
            "-PreflightOnly",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
        timeout=15,
    )

    assert completed.returncode != 0
    combined_output = completed.stdout + completed.stderr
    assert "Owned process exited before acquiring port 8188" in combined_output
    assert "property 'ProcessId'" not in combined_output
    assert "Provisional process could not be proven owned" not in combined_output

    sidecars = list(output_root.glob(".*-*.log"))
    assert len(sidecars) == 4
    comfy_stdout = next(output_root.glob(".comfyui-*.stdout.log"))
    comfy_stderr = next(output_root.glob(".comfyui-*.stderr.log"))
    backend_stdout = next(output_root.glob(".tts-more-*.stdout.log"))
    backend_stderr = next(output_root.glob(".tts-more-*.stderr.log"))
    run_ids = {
        path.name.removeprefix(".comfyui-")
        .removeprefix(".tts-more-")
        .split(".", 1)[0]
        for path in sidecars
    }
    assert len(run_ids) == 1
    (run_id,) = run_ids
    assert len(run_id) == 32 and all(character in "0123456789abcdef" for character in run_id)
    assert comfy_stdout.read_bytes() == b"CONTROLLED_COMFY_STDOUT"
    assert comfy_stderr.read_bytes() == b"CONTROLLED_COMFY_STDERR"
    assert backend_stdout.read_bytes() == b""
    assert backend_stderr.read_bytes() == b""

    semantic_argv = json.loads(argv_capture.read_text(encoding="utf-8"))
    assert semantic_argv == [
        "--listen",
        "127.0.0.1",
        "--port",
        "8188",
        "--temp-directory",
        str(output_root / f"reliability-temp-{run_id}" / "comfyui"),
    ]
    assert all(str(path) not in semantic_argv for path in sidecars)
    assert not list(output_root.glob("reliability-temp-*"))
    assert not list(output_root.glob(".request-temp-*.owner.json"))
    assert not list(output_root.glob(".host-manifest-*.private.json*"))

    inventory = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "@(CimCmdlets\\Get-CimInstance Win32_Process | Where-Object { "
            "$_.CommandLine -and $_.CommandLine.Contains($env:TTS_MORE_TEST_RUN_ID) "
            "}).Count",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**environment, "TTS_MORE_TEST_RUN_ID": run_id},
        check=False,
        timeout=30,
    )
    assert inventory.returncode == 0, inventory.stderr
    assert inventory.stdout.strip() == "0"


def test_task_12_launcher_failure_arbitration_prefers_primary_and_fails_cleanup_only() -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "run-windows-comfyui-reliability.ps1"
    )
    command = r"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
$function = $ast.Find({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Complete-LauncherFailureState'
}, $true)
if ($null -eq $function) { throw 'Complete-LauncherFailureState is missing' }
Invoke-Expression $function.Extent.Text

$primary = $null
$cleanup = $null
try { throw 'primary controlled startup failure' } catch { $primary = $_ }
try { throw 'secondary injected cleanup query failure' } catch { $cleanup = $_ }

$caught = $null
try {
    Complete-LauncherFailureState -PrimaryFailure $primary -CleanupFailure $cleanup
} catch { $caught = $_ }
if ($null -eq $caught -or $caught.Exception.Message -ne 'primary controlled startup failure') {
    throw 'secondary cleanup failure replaced the primary startup failure'
}

$caught = $null
try {
    Complete-LauncherFailureState -PrimaryFailure $null -CleanupFailure $cleanup
} catch { $caught = $_ }
if (
    $null -eq $caught -or
    $caught.Exception.Message -ne 'Windows reliability cleanup verification failed'
) { throw 'cleanup-only validation error incorrectly passed' }

Complete-LauncherFailureState -PrimaryFailure $null -CleanupFailure $null
Write-Output 'LAUNCHER_FAILURE_ARBITRATION_OK'
"""
    environment = os.environ.copy()
    environment["TTS_MORE_RELIABILITY_SCRIPT"] = str(script_path)

    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "LAUNCHER_FAILURE_ARBITRATION_OK" in completed.stdout


def test_task_12_startup_stage_cleanup_preserves_primary_and_marks_unproved() -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "run-windows-comfyui-reliability.ps1"
    )
    command = r"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
$function = $ast.Find({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Complete-ProvisionalStartupFailure'
}, $true)
if ($null -eq $function) { throw 'Complete-ProvisionalStartupFailure is missing' }
Invoke-Expression $function.Extent.Text

function Stop-ProvisionalStartedProcess {
    param([object] $Token)
    if ($script:stopMode -eq 'error') { throw 'raw injected cleanup query error' }
    return ($script:stopMode -eq 'success')
}
function Write-Warning {
    param([object] $Message)
    $script:warningMessages.Add([string] $Message)
}

foreach ($case in @(
    [pscustomobject]@{ mode = 'error'; failed = $true; warning =
        'Provisional process cleanup verification failed; preserving startup evidence' },
    [pscustomobject]@{ mode = 'unproved'; failed = $true; warning =
        'controlled cleanup did not converge' },
    [pscustomobject]@{ mode = 'success'; failed = $false; warning = $null }
)) {
    $script:stopMode = $case.mode
    $primary = $null
    try { throw 'primary process-record startup failure' } catch { $primary = $_ }
    $cleanupFailed = $false
    $script:warningMessages = [System.Collections.Generic.List[string]]::new()
    $caught = $null
    try {
        Complete-ProvisionalStartupFailure `
            -PrimaryFailure $primary -Token ([pscustomobject]@{ pid = 4242 }) `
            -CleanupFailed ([ref]$cleanupFailed) `
            -UnprovedWarning 'controlled cleanup did not converge'
    } catch { $caught = $_ }
    if (
        $null -eq $caught -or
        $caught.Exception.Message -ne 'primary process-record startup failure'
    ) { throw "startup primary was replaced for $($case.mode) cleanup" }
    if ($cleanupFailed -ne $case.failed) {
        throw "cleanup proof state was wrong for $($case.mode) cleanup"
    }
    $warningText = @($script:warningMessages) -join "`n"
    if ($null -eq $case.warning) {
        if ($warningText) { throw 'successful inner cleanup emitted a warning' }
    } elseif ($warningText -notlike "*$($case.warning)*") {
        throw "missing neutral inner cleanup warning for $($case.mode) cleanup"
    }
    if ($warningText -like '*raw injected cleanup query error*') {
        throw 'raw inner cleanup error leaked into diagnostics'
    }
}
Write-Output 'STARTUP_STAGE_PRIMARY_ARBITRATION_OK'
"""
    environment = os.environ.copy()
    environment["TTS_MORE_RELIABILITY_SCRIPT"] = str(script_path)

    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "STARTUP_STAGE_PRIMARY_ARBITRATION_OK" in completed.stdout
    assert "raw injected cleanup query error" not in (
        completed.stdout + completed.stderr
    )


def test_task_12_wrapper_preserves_primary_error_when_cleanup_cim_query_fails(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    repository_root = Path(__file__).resolve().parents[2]
    script_path = repository_root / "scripts" / "run-windows-comfyui-reliability.ps1"
    output_root = tmp_path / "private failure evidence"
    output_root.mkdir()
    fixture_root = tmp_path / "fixture"
    reference_root = fixture_root / "references"
    reference_root.mkdir(parents=True)
    resources: dict[str, dict[str, str]] = {}
    for engine in ("gpt-sovits", "indextts", "cosyvoice"):
        reference = reference_root / f"{engine}.wav"
        reference.write_bytes(b"reference")
        resources[engine] = {"reference_audio": f"references/{engine}.wav"}
    fixture_path = fixture_root / "fixture.json"
    fixture_path.write_text(json.dumps({"resources": resources}), encoding="utf-8")

    comfy_root = tmp_path / "controlled ComfyUI cleanup query error"
    (comfy_root / "custom_nodes" / "TTS-Audio-Suite").mkdir(parents=True)
    (comfy_root / "main.py").write_text(
        "import sys, time\n"
        "sys.stdout.write('PRIMARY_ERROR_STDOUT')\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('PRIMARY_ERROR_STDERR')\n"
        "sys.stderr.flush()\n"
        "time.sleep(1.0)\n"
        "raise SystemExit(31)\n",
        encoding="utf-8",
    )
    registry_path = tmp_path / "resources.yaml"
    registry_path.write_text("resources: {}\n", encoding="utf-8")
    engine_roots: dict[str, Path] = {}
    for engine in ("gpt-sovits", "indextts", "cosyvoice"):
        engine_root = tmp_path / f"{engine}-root"
        engine_root.mkdir()
        engine_roots[engine] = engine_root

    command = r"""
$global:TTSMoreCleanupQueryErrors = 0
function Get-CimInstance {
    param([string] $ClassName, [string] $Filter, [object] $ErrorAction)
    if ($Filter) {
        $result = CimCmdlets\Get-CimInstance -ClassName $ClassName `
            -Filter $Filter -ErrorAction Stop
        if ($null -eq $result -and $Filter -match '^ProcessId = \d+$') {
            $global:TTSMoreCleanupQueryErrors += 1
            throw 'injected exact cleanup CIM query failure'
        }
        return $result
    }
    return @(CimCmdlets\Get-CimInstance -ClassName $ClassName -ErrorAction Stop)
}

$caught = $null
try {
    & $env:TTS_MORE_RELIABILITY_SCRIPT `
        -Fixture $env:TTS_MORE_TEST_FIXTURE `
        -OutputRoot $env:TTS_MORE_TEST_OUTPUT `
        -ComfyUiRoot $env:TTS_MORE_TEST_COMFY_ROOT `
        -ComfyPython $env:TTS_MORE_TEST_COMFY_PYTHON `
        -TtsMoreRoot $env:TTS_MORE_TEST_TTS_ROOT `
        -PreflightOnly
} catch { $caught = $_ }
if (
    $null -eq $caught -or
    $caught.Exception.Message -notlike 'Owned process exited before acquiring port 8188*'
) { throw ('primary startup failure was not preserved: {0}' -f $caught) }
if ($global:TTSMoreCleanupQueryErrors -ne 1) {
    throw 'controlled cleanup CIM query error was not injected exactly once'
}
Write-Output 'PRIMARY_ERROR_SURVIVED_CLEANUP_QUERY_ERROR_OK'
"""
    environment = os.environ.copy()
    environment.update(
        {
            "TTS_MORE_RELIABILITY_SCRIPT": str(script_path),
            "TTS_MORE_TEST_FIXTURE": str(fixture_path),
            "TTS_MORE_TEST_OUTPUT": str(output_root),
            "TTS_MORE_TEST_COMFY_ROOT": str(comfy_root),
            "TTS_MORE_TEST_COMFY_PYTHON": str(Path(os.sys.executable).resolve()),
            "TTS_MORE_TEST_TTS_ROOT": str(repository_root),
            "TTS_MORE_RELIABILITY_GPT_SOVITS_ROOT": str(
                engine_roots["gpt-sovits"]
            ),
            "TTS_MORE_RELIABILITY_INDEXTTS_ROOT": str(engine_roots["indextts"]),
            "TTS_MORE_RELIABILITY_COSYVOICE_ROOT": str(engine_roots["cosyvoice"]),
            "TTS_AUDIO_SUITE_RESOURCES": str(registry_path),
        }
    )
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    combined_output = completed.stdout + completed.stderr
    assert "PRIMARY_ERROR_SURVIVED_CLEANUP_QUERY_ERROR_OK" in completed.stdout
    assert (
        "WARNING: Process cleanup verification failed; preserving private "
        "process, temp, and control evidence"
    ) in combined_output
    assert "injected exact cleanup CIM query failure" not in combined_output

    sidecars = list(output_root.glob(".*-*.log"))
    assert len(sidecars) == 4
    comfy_stdout = next(output_root.glob(".comfyui-*.stdout.log"))
    comfy_stderr = next(output_root.glob(".comfyui-*.stderr.log"))
    backend_stdout = next(output_root.glob(".tts-more-*.stdout.log"))
    backend_stderr = next(output_root.glob(".tts-more-*.stderr.log"))
    assert comfy_stdout.read_bytes() == b"PRIMARY_ERROR_STDOUT"
    assert comfy_stderr.read_bytes() == b"PRIMARY_ERROR_STDERR"
    assert backend_stdout.read_bytes() == b""
    assert backend_stderr.read_bytes() == b""
    run_ids = {
        path.name.removeprefix(".comfyui-")
        .removeprefix(".tts-more-")
        .split(".", 1)[0]
        for path in sidecars
    }
    assert len(run_ids) == 1
    (run_id,) = run_ids
    assert list(output_root.glob(f"reliability-temp-{run_id}"))
    assert list(output_root.glob(f".request-temp-{run_id}.owner.json"))
    assert list(
        output_root.glob(f".host-manifest-{run_id}.private.json.current.json")
    )
    assert not list(output_root.glob(f".host-manifest-{run_id}.private.json"))

    inventory = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "@(CimCmdlets\\Get-CimInstance Win32_Process | Where-Object { "
            "$_.CommandLine -and $_.CommandLine.Contains($env:TTS_MORE_TEST_RUN_ID) "
            "}).Count",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**environment, "TTS_MORE_TEST_RUN_ID": run_id},
        check=False,
        timeout=30,
    )
    assert inventory.returncode == 0, inventory.stderr
    assert inventory.stdout.strip() == "0"


def test_task_10_powershell_identity_timestamps_compare_utc_instants_not_spelling() -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "run-windows-comfyui-reliability.ps1"
    command = r"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
$function = $ast.Find({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Get-UtcTicks'
}, $true)
if ($null -eq $function) { throw 'Get-UtcTicks is missing' }
Invoke-Expression $function.Extent.Text
if ((Get-UtcTicks '2026-08-01T00:00:00Z') -ne (Get-UtcTicks '2026-08-01T00:00:00+00:00')) {
    throw 'equivalent UTC timestamps did not compare equal'
}
Write-Output 'UTC_TIMESTAMP_IDENTITY_OK'
"""
    environment = os.environ.copy()
    environment["TTS_MORE_RELIABILITY_SCRIPT"] = str(script_path)

    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "UTC_TIMESTAMP_IDENTITY_OK" in completed.stdout


def test_fix_round_1_powershell_preserves_private_identity_until_cleanup_is_proven(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "run-windows-comfyui-reliability.ps1"
    command = r"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
foreach ($name in @('Test-PrivateIdentityRecordsCanBeRemoved', 'Remove-PrivateIdentityRecordsIfSafe')) {
    $function = $ast.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    if ($null -eq $function) { throw "$name is missing" }
    Invoke-Expression $function.Extent.Text
}
$hostRecord = Join-Path $env:TTS_MORE_PRIVATE_TEST_ROOT '.host-manifest.private.json'
$currentRecord = "$hostRecord.current.json"

function Reset-Records {
    Set-Content -LiteralPath $hostRecord -Value '{"private":true}' -Encoding UTF8
    Set-Content -LiteralPath $currentRecord -Value '{"private":true}' -Encoding UTF8
}
function Assert-RecordsPresent {
    if (-not (Test-Path -LiteralPath $hostRecord -PathType Leaf)) { throw 'host record was removed' }
    if (-not (Test-Path -LiteralPath $currentRecord -PathType Leaf)) { throw 'current record was removed' }
}
function Assert-RecordsAbsent {
    if (Test-Path -LiteralPath $hostRecord) { throw 'host record remains' }
    if (Test-Path -LiteralPath $currentRecord) { throw 'current record remains' }
}

Reset-Records
if (Remove-PrivateIdentityRecordsIfSafe -HostManifestPath $hostRecord -ControlStatePath $currentRecord `
        -ProcessCleanupProven $false -TempCleanupProven $false -OwnedProcessCount 2) {
    throw 'identity mismatch incorrectly allowed record removal'
}
Assert-RecordsPresent

if (Remove-PrivateIdentityRecordsIfSafe -HostManifestPath $hostRecord -ControlStatePath $currentRecord `
        -ProcessCleanupProven $true -TempCleanupProven $false -OwnedProcessCount 2) {
    throw 'unproved temp cleanup incorrectly allowed record removal'
}
Assert-RecordsPresent

if (-not (Remove-PrivateIdentityRecordsIfSafe -HostManifestPath $hostRecord -ControlStatePath $currentRecord `
        -ProcessCleanupProven $true -TempCleanupProven $true -OwnedProcessCount 2)) {
    throw 'proved cleanup did not remove private records'
}
Assert-RecordsAbsent

Reset-Records
if (-not (Remove-PrivateIdentityRecordsIfSafe -HostManifestPath $hostRecord -ControlStatePath $currentRecord `
        -ProcessCleanupProven $false -TempCleanupProven $false -OwnedProcessCount 0)) {
    throw 'known empty ownership did not remove empty private records'
}
Assert-RecordsAbsent
Write-Output 'PRIVATE_IDENTITY_RETENTION_OK'
"""
    environment = os.environ.copy()
    environment["TTS_MORE_RELIABILITY_SCRIPT"] = str(script_path)
    environment["TTS_MORE_PRIVATE_TEST_ROOT"] = str(tmp_path)

    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "PRIVATE_IDENTITY_RETENTION_OK" in completed.stdout


def test_fix_round_2_powershell_persists_provisional_identity_before_full_upgrade(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "run-windows-comfyui-reliability.ps1"
    command = r"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @(
    'Get-UtcTicks',
    'Test-RecordDocumentMatches',
    'Test-CommandLineArgument',
    'ConvertTo-WindowsCommandLineArgument',
    'Test-FullRecordPromotesProvisional',
    'Write-PrivateJsonAtomic',
    'Write-LaunchIntentRunControlState',
    'Write-ProvisionalRunControlState',
    'Complete-ProvisionalStartupFailure',
    'Start-ProvisionallyTrackedProcess',
    'Write-RunControlState'
)) {
    $function = $ast.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    if ($null -eq $function) { throw "$name is missing" }
    Invoke-Expression $function.Extent.Text
}
function Start-Process {
    param(
        [string] $FilePath,
        [string[]] $ArgumentList,
        [string] $WorkingDirectory,
        [string] $WindowStyle,
        [switch] $PassThru
    )
    $intentAtLaunch = Get-Content -LiteralPath $script:statePath -Raw | ConvertFrom-Json
    $script:startSawIntent = (
        $intentAtLaunch.version -eq 2 -and
        $intentAtLaunch.launch_intents.comfyui.marker -eq $script:launchMarker -and
        $null -eq $intentAtLaunch.provisional_processes.comfyui
    )
    return [pscustomobject]@{ Id = 4242 }
}
function Test-RecordedIdentity { param([object] $Record) return $true }

$statePath = Join-Path $env:TTS_MORE_PRIVATE_TEST_ROOT 'control.json'
$launcher = [pscustomobject]@{
    pid = 111
    creation_time = '2026-08-01T00:00:00Z'
}
$started = New-Object 'System.Collections.Generic.List[object]'
$executable = Join-Path $env:TTS_MORE_PRIVATE_TEST_ROOT 'python.exe'
$workingDirectory = Join-Path $env:TTS_MORE_PRIVATE_TEST_ROOT 'working directory'
$childTemp = Join-Path $env:TTS_MORE_PRIVATE_TEST_ROOT 'child temp'
$runId = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
$marker = "tts_more_reliability_run=$runId-comfyui"
$arguments = @('-X', $marker, 'main.py', '--port', '8188')
$script:statePath = $statePath
$script:launchMarker = $marker
$script:startSawIntent = $false
$result = Start-ProvisionallyTrackedProcess -FilePath $executable `
    -ArgumentList $arguments -WorkingDirectory $workingDirectory `
    -ChildTempRoot $childTemp -LauncherRecord $launcher `
    -StartedProcesses $started -ControlStatePath $statePath -RunId $runId `
    -ProcessLabel 'comfyui' -LaunchMarker $marker `
    -BackendRecord $null -ComfyRecord $null
if ($result.process.Id -ne 4242 -or $started.Count -ne 1 -or -not $script:startSawIntent) {
    throw 'launch did not occur strictly after durable intent'
}
if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
    throw 'provisional recovery state was not persisted before return'
}
$provisional = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
if (
    $provisional.version -ne 2 -or
    $provisional.run_id -ne $runId -or
    $provisional.launch_intents.comfyui.marker -ne $marker -or
    $provisional.launch_intents.comfyui.executable_path -ne [IO.Path]::GetFullPath($executable) -or
    $provisional.launch_intents.comfyui.working_directory -ne [IO.Path]::GetFullPath($workingDirectory) -or
    $provisional.launch_intents.comfyui.child_temp_root -ne [IO.Path]::GetFullPath($childTemp) -or
    @($provisional.launch_intents.comfyui.arguments).Count -ne 5 -or
    @($provisional.launch_intents.comfyui.arguments)[1] -ne $marker -or
    [int] $provisional.provisional_processes.comfyui.pid -ne 4242 -or
    $provisional.provisional_processes.comfyui.executable_path -ne [IO.Path]::GetFullPath($executable) -or
    [int] $provisional.provisional_processes.comfyui.parent_pid -ne 111 -or
    $provisional.provisional_processes.comfyui.parent_creation_time -ne '2026-08-01T00:00:00Z' -or
    (Get-UtcTicks $provisional.provisional_processes.comfyui.started_after) -gt
        (Get-UtcTicks $provisional.provisional_processes.comfyui.started_before)
) { throw 'provisional recovery state is incomplete' }
if (@(Get-ChildItem -LiteralPath $env:TTS_MORE_PRIVATE_TEST_ROOT -File | Where-Object {
        $_.Name -match '\.(tmp|previous)$'
    }).Count -ne 0) { throw 'provisional publication residue remains' }

$full = [pscustomobject]@{
    pid = 4242
    creation_time = [string] $provisional.provisional_processes.comfyui.started_after
    executable_path = [IO.Path]::GetFullPath($executable)
    command_line = ('python.exe -X {0} main.py --port 8188' -f $marker)
    parent_pid = 111
    parent_creation_time = '2026-08-01T00:00:00Z'
}
Write-RunControlState -Path $statePath -RunId $runId -BackendRecord $null -ComfyRecord $full
$upgraded = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
if (
    $upgraded.version -ne 2 -or
    $upgraded.run_id -ne $runId -or
    [int] $upgraded.owned_processes.comfyui.pid -ne 4242 -or
    $null -ne $upgraded.provisional_processes.comfyui -or
    $null -ne $upgraded.launch_intents.comfyui
) { throw 'full process identity did not atomically replace provisional state' }
Write-Output 'PROVISIONAL_IDENTITY_UPGRADE_OK'
"""
    environment = os.environ.copy()
    environment["TTS_MORE_RELIABILITY_SCRIPT"] = str(script_path)
    environment["TTS_MORE_PRIVATE_TEST_ROOT"] = str(tmp_path)

    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "PROVISIONAL_IDENTITY_UPGRADE_OK" in completed.stdout


def test_fix_round_2_powershell_provisional_persistence_failure_converges_or_retries(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "run-windows-comfyui-reliability.ps1"
    command = r"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @(
    'Get-UtcTicks',
    'Test-RecordDocumentMatches',
    'ConvertTo-WindowsCommandLineArgument',
    'Write-PrivateJsonAtomic',
    'Write-LaunchIntentRunControlState',
    'Write-ProvisionalRunControlState',
    'Complete-ProvisionalStartupFailure',
    'Start-ProvisionallyTrackedProcess'
)) {
    $function = $ast.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    if ($null -eq $function) { throw "$name is missing" }
    Invoke-Expression $function.Extent.Text
}
function Start-Process {
    param(
        [string] $FilePath,
        [string[]] $ArgumentList,
        [string] $WorkingDirectory,
        [string] $WindowStyle,
        [switch] $PassThru
    )
    $script:startCalls += 1
    return [pscustomobject]@{ Id = 4343 }
}

$script:realAtomicWriter = (Get-Command Write-PrivateJsonAtomic).ScriptBlock
$script:atomicCalls = 0
function Write-PrivateJsonAtomic {
    param([string] $Path, [object] $Document)
    $script:atomicCalls += 1
    if ($script:atomicCalls -eq 2) { throw 'injected provisional persistence failure' }
    & $script:realAtomicWriter -Path $Path -Document $Document
}
$script:startCalls = 0
$script:stopCalls = 0
function Stop-ProvisionalStartedProcess {
    param([object] $Token)
    $script:stopCalls += 1
    throw 'raw injected persistence cleanup query error'
}
$statePath = Join-Path $env:TTS_MORE_PRIVATE_TEST_ROOT 'retry-control.json'
$launcher = [pscustomobject]@{
    pid = 111
    creation_time = '2026-08-01T00:00:00Z'
}
$started = New-Object 'System.Collections.Generic.List[object]'
$runId = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
$marker = "tts_more_reliability_run=$runId-comfyui"
$caught = $null
try {
    $null = Start-ProvisionallyTrackedProcess `
        -FilePath (Join-Path $env:TTS_MORE_PRIVATE_TEST_ROOT 'python.exe') `
        -ArgumentList @('-X', $marker, 'main.py') -WorkingDirectory $env:TTS_MORE_PRIVATE_TEST_ROOT `
        -ChildTempRoot $env:TTS_MORE_PRIVATE_TEST_ROOT -LauncherRecord $launcher `
        -StartedProcesses $started -ControlStatePath $statePath `
        -RunId $runId -ProcessLabel 'comfyui' -LaunchMarker $marker `
        -BackendRecord $null -ComfyRecord $null
} catch { $caught = $_ }
if (
    $null -eq $caught -or
    $caught.Exception.Message -ne 'injected provisional persistence failure' -or
    $script:startCalls -ne 1 -or
    $script:stopCalls -ne 1 -or
    $script:atomicCalls -ne 2
) {
    throw 'failed provisional persistence did not attempt safe convergence before exit'
}
if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
    throw 'unconverged process lost its pre-launch recovery intent'
}
$recovered = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
if (
    $recovered.version -ne 2 -or
    $recovered.launch_intents.comfyui.marker -ne $marker -or
    $null -ne $recovered.provisional_processes.comfyui
) {
    throw 'pre-launch intent did not survive provisional persistence failure'
}

Remove-Item -LiteralPath $statePath -Force
$script:atomicCalls = 0
function Write-PrivateJsonAtomic {
    param([string] $Path, [object] $Document)
    $script:atomicCalls += 1
    throw 'injected persistent failure'
}
$script:stopCalls = 0
$script:startCalls = 0
function Stop-ProvisionalStartedProcess {
    param([object] $Token)
    $script:stopCalls += 1
    return $true
}
$started = New-Object 'System.Collections.Generic.List[object]'
$runId = 'cccccccccccccccccccccccccccccccc'
$marker = "tts_more_reliability_run=$runId-comfyui"
$threw = $false
try {
    $null = Start-ProvisionallyTrackedProcess `
        -FilePath (Join-Path $env:TTS_MORE_PRIVATE_TEST_ROOT 'python.exe') `
        -ArgumentList @('-X', $marker, 'main.py') -WorkingDirectory $env:TTS_MORE_PRIVATE_TEST_ROOT `
        -ChildTempRoot $env:TTS_MORE_PRIVATE_TEST_ROOT -LauncherRecord $launcher `
        -StartedProcesses $started -ControlStatePath $statePath `
        -RunId $runId -ProcessLabel 'comfyui' -LaunchMarker $marker `
        -BackendRecord $null -ComfyRecord $null
} catch { $threw = $true }
if (
    -not $threw -or
    $script:startCalls -ne 0 -or
    $script:stopCalls -ne 0 -or
    (Test-Path -LiteralPath $statePath)
) {
    throw 'failed intent persistence did not abort before process launch'
}
Write-Output 'PROVISIONAL_PERSISTENCE_FAILURE_OK'
"""
    environment = os.environ.copy()
    environment["TTS_MORE_RELIABILITY_SCRIPT"] = str(script_path)
    environment["TTS_MORE_PRIVATE_TEST_ROOT"] = str(tmp_path)

    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "PROVISIONAL_PERSISTENCE_FAILURE_OK" in completed.stdout
    combined_output = completed.stdout + completed.stderr
    assert (
        "Provisional process cleanup verification failed; preserving startup evidence"
        in combined_output
    )
    assert "raw injected persistence cleanup query error" not in combined_output


def test_fix_round_2_powershell_launch_intent_resolver_is_unique_and_fail_closed(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "run-windows-comfyui-reliability.ps1"
    command = r"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @('Get-UtcTicks', 'Test-CommandLineArgument', 'Resolve-LaunchIntentProcess')) {
    $function = $ast.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    if ($null -eq $function) { throw "$name is missing" }
    Invoke-Expression $function.Extent.Text
}
$executable = [IO.Path]::GetFullPath((Join-Path $env:TTS_MORE_PRIVATE_TEST_ROOT 'python.exe'))
$marker = 'tts_more_reliability_run=dddddddddddddddddddddddddddddddd-comfyui'
$intent = [pscustomobject]@{
    marker = $marker
    executable_path = $executable
    arguments = @('-X', $marker, 'main.py')
    working_directory = $env:TTS_MORE_PRIVATE_TEST_ROOT
    child_temp_root = $env:TTS_MORE_PRIVATE_TEST_ROOT
    parent_pid = 111
    parent_creation_time = '2026-08-01T00:00:00Z'
    started_after = '2026-08-01T00:00:00Z'
}
function New-Candidate {
    param([int] $CandidatePid, [string] $Marker = $script:marker)
    return [pscustomobject]@{
        ProcessId = $CandidatePid
        CreationDate = [DateTime]::Parse('2026-08-01T00:00:01Z').ToUniversalTime()
        ExecutablePath = $script:executable
        CommandLine = ('"{0}" -X "{1}" main.py' -f $script:executable, $Marker)
        ParentProcessId = 111
        Name = 'python.exe'
    }
}
$script:marker = $marker
$script:executable = $executable
$script:inventory = @((New-Candidate -CandidatePid 4444))
function Get-CimInstance {
    param([string] $ClassName, [string] $Filter, [object] $ErrorAction)
    if ($script:throwInventory) { throw 'injected CIM failure' }
    if ($Filter) {
        $pidText = [regex]::Match($Filter, '\d+').Value
        return @($script:inventory | Where-Object { [int] $_.ProcessId -eq [int] $pidText }) | Select-Object -First 1
    }
    return $script:inventory
}
$resolved = Resolve-LaunchIntentProcess -Intent $intent
if (
    [int] $resolved.pid -ne 4444 -or
    $resolved.executable_path -ne $executable -or
    [int] $resolved.parent_pid -ne 111 -or
    $resolved.parent_creation_time -ne '2026-08-01T00:00:00Z'
) { throw 'unique launch intent was not recovered' }

$script:inventory = @(
    (New-Candidate -CandidatePid 4444),
    (New-Candidate -CandidatePid 4445)
)
$ambiguousThrew = $false
try { $null = Resolve-LaunchIntentProcess -Intent $intent } catch { $ambiguousThrew = $true }
if (-not $ambiguousThrew) { throw 'ambiguous launch intent was accepted' }

$script:inventory = @([pscustomobject]@{
    ProcessId = 4446
    CreationDate = [DateTime]::Parse('2026-08-01T00:00:01Z').ToUniversalTime()
    ExecutablePath = $null
    CommandLine = $null
    ParentProcessId = 111
    Name = 'python.exe'
})
$incompleteThrew = $false
try { $null = Resolve-LaunchIntentProcess -Intent $intent } catch { $incompleteThrew = $true }
if (-not $incompleteThrew) { throw 'incomplete potential launch candidate was treated as absent' }

$script:inventory = @((New-Candidate -CandidatePid 4447 -Marker ($marker + '-suffix')))
if ($null -ne (Resolve-LaunchIntentProcess -Intent $intent)) {
    throw 'launch marker substring was accepted as an exact argv token'
}
$script:throwInventory = $true
$enumerationThrew = $false
try { $null = Resolve-LaunchIntentProcess -Intent $intent } catch { $enumerationThrew = $true }
if (-not $enumerationThrew) { throw 'CIM enumeration failure was treated as zero candidates' }
Write-Output 'LAUNCH_INTENT_RESOLVER_OK'
"""
    environment = os.environ.copy()
    environment["TTS_MORE_RELIABILITY_SCRIPT"] = str(script_path)
    environment["TTS_MORE_PRIVATE_TEST_ROOT"] = str(tmp_path)

    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "LAUNCH_INTENT_RESOLVER_OK" in completed.stdout


def test_task_10_native_restart_cleans_captured_process_when_readiness_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = (tmp_path / "python.exe").resolve()
    executable.write_bytes(b"python")
    created = datetime.now(timezone.utc) - timedelta(minutes=1)
    parent = reliability_validation.RecordedProcessIdentity(
        pid=os.getpid(),
        creation_time=created,
        executable_path=Path(__file__).resolve(),
        command_line="validator-python",
        parent_pid=7000,
        parent_creation_time=created - timedelta(seconds=1),
    )
    provenance = reliability_validation.RecordedProcessIdentity(
        pid=8188,
        creation_time=created,
        executable_path=executable,
        command_line="old-comfyui",
        parent_pid=7000,
        parent_creation_time=created - timedelta(seconds=1),
    )
    system = object.__new__(reliability_validation.NativeWindowsHostSystem)
    system._started_identities = {}
    system._active_tokens = []
    intent: reliability_validation.PrivateRestartLaunchIntent | None = None
    provisional: reliability_validation.PrivateRestartProvisionalProcess | None = None

    class _FakePopen:
        pid = 18_188

        def __init__(self, _command: list[str], **_kwargs: object) -> None:
            pass

        def terminate(self) -> None:
            pytest.fail("captured replacement should use exact owned cleanup")

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

    monkeypatch.setattr(reliability_validation.subprocess, "Popen", _FakePopen)

    def inspect_process(pid: int) -> reliability_validation.RecordedProcessIdentity:
        if pid == os.getpid():
            return parent
        assert intent is not None and provisional is not None and pid == provisional.pid
        return reliability_validation.RecordedProcessIdentity(
            pid=pid,
            creation_time=provisional.started_after,
            executable_path=executable,
            command_line=subprocess.list2cmdline([str(executable), *intent.arguments]),
            parent_pid=parent.pid,
            parent_creation_time=parent.creation_time,
        )

    system.inspect_process = inspect_process
    system.port_owner = lambda _port: (_ for _ in ()).throw(RuntimeError("readiness failed"))
    stopped: list[int] = []
    system.stop_owned = lambda identity: stopped.append(identity.pid)
    launch = reliability_validation.PrivateLaunchSpecification(
        executable_path=executable,
        arguments=("main.py", "--port", "8188"),
        working_directory=tmp_path.resolve(),
        port=8188,
        temp_root=(tmp_path / "runner-temp").resolve(),
    )

    def persist_intent(value: reliability_validation.PrivateRestartLaunchIntent) -> None:
        nonlocal intent
        intent = value

    def persist_provisional(
        value: reliability_validation.PrivateRestartProvisionalProcess,
    ) -> None:
        nonlocal provisional
        provisional = value

    with pytest.raises(reliability_validation.RestartLifecycleError) as exc_info:
        system.restart_owned(
            provenance,
            launch,
            1.0,
            run_id="a" * 32,
            lifecycle=reliability_validation.PrivateRestartLifecycle(
                persist_launch_intent=persist_intent,
                persist_provisional=persist_provisional,
                promote=lambda _identity: None,
            ),
        )

    assert exc_info.value.cleanup_proven is True
    assert stopped == [18188]
    assert system._started_identities == {}


def test_fix_round_2_native_restart_persists_lifecycle_around_process_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = (tmp_path / "python.exe").resolve()
    executable.write_bytes(b"python")
    working_directory = (tmp_path / "working").resolve()
    working_directory.mkdir()
    temp_root = (tmp_path / "runner-temp").resolve()
    temp_root.mkdir()
    parent_created = datetime.now(timezone.utc) - timedelta(minutes=1)
    parent = reliability_validation.RecordedProcessIdentity(
        pid=os.getpid(),
        creation_time=parent_created,
        executable_path=Path(__file__).resolve(),
        command_line="validator-python",
        parent_pid=7000,
        parent_creation_time=parent_created - timedelta(seconds=1),
    )
    provenance = reliability_validation.RecordedProcessIdentity(
        pid=8188,
        creation_time=parent_created,
        executable_path=executable,
        command_line="old-comfyui",
        parent_pid=7000,
        parent_creation_time=parent_created - timedelta(seconds=1),
    )
    launch = reliability_validation.PrivateLaunchSpecification(
        executable_path=executable,
        arguments=("main.py", "--port", "8188"),
        working_directory=working_directory,
        port=8188,
        temp_root=temp_root,
    )
    events: list[str] = []
    intents: list[reliability_validation.PrivateRestartLaunchIntent] = []
    provisional_records: list[reliability_validation.PrivateRestartProvisionalProcess] = []
    promoted: list[reliability_validation.RecordedProcessIdentity] = []
    popen_calls: list[dict[str, object]] = []

    class _FakePopen:
        pid = 18_188

        def __init__(self, command: list[str], **kwargs: object) -> None:
            assert events == ["intent"]
            events.append("start")
            popen_calls.append({"command": command, **kwargs})

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            pytest.fail("successful restart must not terminate the replacement")

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

    monkeypatch.setattr(reliability_validation.subprocess, "Popen", _FakePopen)
    system = object.__new__(reliability_validation.NativeWindowsHostSystem)
    system._started_identities = {}
    system._active_tokens = []

    def inspect_process(pid: int) -> reliability_validation.RecordedProcessIdentity:
        if pid == os.getpid():
            return parent
        assert pid == 18_188
        intent = intents[0]
        provisional = provisional_records[0]
        return reliability_validation.RecordedProcessIdentity(
            pid=pid,
            creation_time=provisional.started_after,
            executable_path=executable,
            command_line=subprocess.list2cmdline([str(executable), *intent.arguments]),
            parent_pid=parent.pid,
            parent_creation_time=parent.creation_time,
        )

    system.inspect_process = inspect_process
    system.port_owner = lambda _port: inspect_process(18_188)

    def persist_intent(intent: reliability_validation.PrivateRestartLaunchIntent) -> None:
        events.append("intent")
        intents.append(intent)

    def persist_provisional(
        provisional: reliability_validation.PrivateRestartProvisionalProcess,
    ) -> None:
        assert events == ["intent", "start"]
        events.append("provisional")
        provisional_records.append(provisional)

    def promote(identity: reliability_validation.RecordedProcessIdentity) -> None:
        assert events == ["intent", "start", "provisional"]
        events.append("promote")
        promoted.append(identity)

    replacement = system.restart_owned(
        provenance,
        launch,
        1.0,
        run_id="a" * 32,
        lifecycle=reliability_validation.PrivateRestartLifecycle(
            persist_launch_intent=persist_intent,
            persist_provisional=persist_provisional,
            promote=promote,
        ),
    )

    assert events == ["intent", "start", "provisional", "promote"]
    assert replacement == promoted[0]
    assert intents[0].marker in intents[0].arguments
    assert popen_calls[0]["command"] == [str(executable), *intents[0].arguments]
    environment = popen_calls[0]["env"]
    assert isinstance(environment, dict)
    assert environment["TEMP"] == str(temp_root)
    assert environment["TMP"] == str(temp_root)
    expected_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    assert popen_calls[0]["creationflags"] == expected_flags
    assert popen_calls[0]["close_fds"] is True
    assert popen_calls[0]["stdin"] is subprocess.DEVNULL
    assert popen_calls[0]["stdout"] is subprocess.DEVNULL
    assert popen_calls[0]["stderr"] is subprocess.DEVNULL


def test_fix_round_2_native_popen_post_create_failure_preserves_probe_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _host_manifest_document(tmp_path)
    manifest = tmp_path / "host-manifest.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    control_state_path = Path(f"{manifest}.current.json")
    owned = document["owned_processes"]
    assert isinstance(owned, dict)
    old_comfyui = reliability_validation.RecordedProcessIdentity.from_document(
        owned["comfyui"]
    )
    parent_created = datetime.now(timezone.utc) - timedelta(minutes=1)
    parent = reliability_validation.RecordedProcessIdentity(
        pid=os.getpid(),
        creation_time=parent_created,
        executable_path=Path(__file__).resolve(),
        command_line="validator-python",
        parent_pid=7000,
        parent_creation_time=parent_created - timedelta(seconds=1),
    )
    system = object.__new__(reliability_validation.NativeWindowsHostSystem)
    system._started_identities = {}
    system._active_tokens = []
    stopped: list[int] = []
    simulated_created_commands: list[list[str]] = []

    def inspect_process(pid: int) -> reliability_validation.RecordedProcessIdentity:
        if pid == os.getpid():
            return parent
        assert pid == old_comfyui.pid
        return old_comfyui

    system.inspect_process = inspect_process
    system.stop_owned = lambda identity: stopped.append(identity.pid)

    class _PostCreateFailurePopen:
        def __init__(self, command: list[str], **_kwargs: object) -> None:
            persisted = json.loads(control_state_path.read_text(encoding="utf-8"))
            intent = persisted["launch_intents"]["comfyui"]
            assert intent is not None
            assert command == [intent["executable_path"], *intent["arguments"]]
            simulated_created_commands.append(command)
            raise OSError("injected failure after CreateProcess")

    monkeypatch.setattr(
        reliability_validation.subprocess,
        "Popen",
        _PostCreateFailurePopen,
    )
    probe = reliability_validation.WindowsReliabilityHostProbe.from_manifest(
        manifest,
        system=system,
    )
    probe.terminate_comfyui()

    with pytest.raises(reliability_validation.LiveValidationError) as exc_info:
        probe.restart_comfyui()

    assert exc_info.value.code == "restart-cleanup-failed"
    assert len(simulated_created_commands) == 1
    persisted = json.loads(control_state_path.read_text(encoding="utf-8"))
    assert persisted["owned_processes"]["comfyui"] is None
    assert persisted["launch_intents"]["comfyui"] is not None
    assert persisted["provisional_processes"]["comfyui"] is None
    assert stopped == [old_comfyui.pid]


def test_task_10_windows_host_probe_revalidates_owned_identity_and_controls_only_comfyui(
    tmp_path: Path,
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    document = _host_manifest_document(tmp_path)
    manifest = tmp_path / "host-manifest.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    system = _FakeWindowsHostSystem(document)

    probe = reliability_validation.WindowsReliabilityHostProbe.from_manifest(
        manifest,
        system=system,
    )
    preflight = probe.preflight(fixture)
    case = reliability_validation.build_case_plan()[0]
    token = probe.begin_case(case)
    observation = probe.finish_case(case, token)
    probe.terminate_comfyui()
    probe.restart_comfyui()
    final = probe.final_state()

    assert preflight.port_owners[8000] == probe.owned_processes["tts-more"]
    assert preflight.port_owners[8188].pid == 8188
    assert observation.processes
    assert system.stopped == [8188]
    assert system.restarted == 1
    assert probe.owned_processes["comfyui"].pid == 18_188
    control_state = json.loads(Path(f"{manifest}.current.json").read_text(encoding="utf-8"))
    assert control_state["run_id"] == "a" * 32
    assert control_state["owned_processes"]["comfyui"]["pid"] == 18_188
    assert final.owned_processes_stopped is True
    assert final.temp_paths_removed is True


def test_task_10_windows_host_probe_rejects_pid_reuse_before_any_control(
    tmp_path: Path,
) -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    document = _host_manifest_document(tmp_path)
    manifest = tmp_path / "host-manifest.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    system = _FakeWindowsHostSystem(document, mismatch=True)
    probe = reliability_validation.WindowsReliabilityHostProbe.from_manifest(
        manifest,
        system=system,
    )

    with pytest.raises(reliability_validation.LiveValidationError) as exc_info:
        probe.preflight(fixture)

    assert exc_info.value.code == "process-identity-mismatch"
    assert system.stopped == []


def test_task_10_restart_handoff_stops_replacement_when_companion_persistence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _host_manifest_document(tmp_path)
    manifest = tmp_path / "host-manifest.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    system = _FakeWindowsHostSystem(document)
    probe = reliability_validation.WindowsReliabilityHostProbe.from_manifest(
        manifest,
        system=system,
    )
    probe.terminate_comfyui()
    original_persist = probe._persist_control_state

    def fail_replacement_handoff() -> None:
        if probe._current.get("comfyui") is not None:
            raise OSError("injected companion persistence failure")
        original_persist()

    monkeypatch.setattr(probe, "_persist_control_state", fail_replacement_handoff)

    with pytest.raises(OSError, match="injected companion persistence failure"):
        probe.restart_comfyui()

    assert system.stopped == [8188, 18188]
    assert "comfyui" not in probe.owned_processes
    control_state = json.loads(Path(f"{manifest}.current.json").read_text(encoding="utf-8"))
    assert control_state["owned_processes"]["comfyui"] is None


def test_fix_round_2_restart_promotes_durable_intent_and_provisional_in_order(
    tmp_path: Path,
) -> None:
    document = _host_manifest_document(tmp_path)
    manifest = tmp_path / "host-manifest.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    control_state_path = Path(f"{manifest}.current.json")
    system = _RestartLifecycleWindowsHostSystem(document, control_state_path)
    probe = reliability_validation.WindowsReliabilityHostProbe.from_manifest(
        manifest,
        system=system,
    )
    probe.terminate_comfyui()

    probe.restart_comfyui()

    intent_state, provisional_state, promoted_state = system.control_snapshots
    assert intent_state["owned_processes"]["comfyui"] is None
    assert intent_state["launch_intents"]["comfyui"]["marker"].startswith(
        "tts_more_reliability_run=" + "a" * 32 + "-comfyui-restart-"
    )
    assert intent_state["provisional_processes"]["comfyui"] is None
    assert provisional_state["owned_processes"]["comfyui"] is None
    assert provisional_state["provisional_processes"]["comfyui"]["pid"] == 18_188
    assert promoted_state["owned_processes"]["comfyui"]["pid"] == 18_188
    assert promoted_state["launch_intents"]["comfyui"] is None
    assert promoted_state["provisional_processes"]["comfyui"] is None


@pytest.mark.parametrize(
    ("interrupt_at", "expects_provisional"),
    [("intent", False), ("provisional", True)],
)
def test_fix_round_2_restart_interruption_preserves_wrapper_recovery_state(
    tmp_path: Path,
    interrupt_at: str,
    expects_provisional: bool,
) -> None:
    document = _host_manifest_document(tmp_path)
    manifest = tmp_path / "host-manifest.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    control_state_path = Path(f"{manifest}.current.json")
    system = _RestartLifecycleWindowsHostSystem(
        document,
        control_state_path,
        interrupt_at=interrupt_at,
    )
    probe = reliability_validation.WindowsReliabilityHostProbe.from_manifest(
        manifest,
        system=system,
    )
    probe.terminate_comfyui()

    with pytest.raises(reliability_validation.LiveValidationError) as exc_info:
        probe.restart_comfyui()

    assert exc_info.value.code == "restart-cleanup-failed"
    persisted = json.loads(control_state_path.read_text(encoding="utf-8"))
    assert persisted["owned_processes"]["comfyui"] is None
    assert persisted["launch_intents"]["comfyui"] is not None
    if expects_provisional:
        assert persisted["provisional_processes"]["comfyui"]["pid"] == 18_188
    else:
        assert persisted["provisional_processes"]["comfyui"] is None
    assert system.stopped == [8188]


def test_fix_round_2_wrapper_consumes_python_restart_recovery_state(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")

    recovery_paths: dict[str, tuple[Path, Path]] = {}
    for interrupt_at in ("intent", "provisional"):
        root = tmp_path / interrupt_at
        root.mkdir()
        document = _host_manifest_document(root)
        manifest = root / "host-manifest.json"
        manifest.write_text(json.dumps(document), encoding="utf-8")
        control_state_path = Path(f"{manifest}.current.json")
        system = _RestartLifecycleWindowsHostSystem(
            document,
            control_state_path,
            interrupt_at=interrupt_at,
        )
        probe = reliability_validation.WindowsReliabilityHostProbe.from_manifest(
            manifest,
            system=system,
        )
        probe.terminate_comfyui()
        with pytest.raises(reliability_validation.LiveValidationError) as exc_info:
            probe.restart_comfyui()
        assert exc_info.value.code == "restart-cleanup-failed"
        recovery_paths[interrupt_at] = (manifest, control_state_path)

    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "run-windows-comfyui-reliability.ps1"
    )
    command = r"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @(
    'Test-ProcessAbsent',
    'Get-UtcTicks',
    'Test-CommandLineArgument',
    'Stop-ProvisionalStartedProcess',
    'Resolve-LaunchIntentProcess',
    'Test-PrivateIdentityRecordsCanBeRemoved',
    'Remove-PrivateIdentityRecordsIfSafe'
)) {
    $function = $ast.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    if ($null -eq $function) { throw "$name is missing" }
    Invoke-Expression $function.Extent.Text
}

$intentControl = Get-Content -LiteralPath $env:TTS_MORE_INTENT_CONTROL -Raw |
    ConvertFrom-Json
$intent = $intentControl.launch_intents.comfyui
if (
    $intentControl.version -ne 2 -or
    $null -eq $intent -or
    $null -ne $intentControl.owned_processes.comfyui -or
    $null -ne $intentControl.provisional_processes.comfyui
) { throw 'Python intent-only control state is invalid' }
$intentCandidate = [pscustomobject]@{
    ProcessId = 18188
    CreationDate = ([DateTimeOffset]::Parse(
        [string] $intent.started_after
    )).UtcDateTime.AddMilliseconds(1)
    ExecutablePath = [string] $intent.executable_path
    CommandLine = ('"{0}" -X "{1}" main.py --listen 127.0.0.1 --port 8188' -f `
        [string] $intent.executable_path, [string] $intent.marker)
    ParentProcessId = [int] $intent.parent_pid
    Name = [IO.Path]::GetFileName([string] $intent.executable_path)
}
$script:intentInventory = @($intentCandidate)
$script:provisionalCandidate = $null
function Get-CimInstance {
    param([string] $ClassName, [string] $Filter, [object] $ErrorAction)
    if (-not $Filter) { return $script:intentInventory }
    $candidatePid = [int] ([regex]::Match($Filter, '\d+').Value)
    if (
        $null -ne $script:provisionalCandidate -and
        $candidatePid -eq [int] $script:provisionalCandidate.ProcessId
    ) { return $script:provisionalCandidate }
    return $null
}
$resolved = Resolve-LaunchIntentProcess -Intent $intent
if (
    [int] $resolved.pid -ne 18188 -or
    $resolved.executable_path -ne [string] $intent.executable_path -or
    [int] $resolved.parent_pid -ne [int] $intent.parent_pid -or
    $resolved.parent_creation_time -ne [string] $intent.parent_creation_time
) { throw 'Wrapper did not recover the Python restart launch intent' }

$provisionalControl = Get-Content -LiteralPath $env:TTS_MORE_PROVISIONAL_CONTROL -Raw |
    ConvertFrom-Json
$provisional = $provisionalControl.provisional_processes.comfyui
if (
    $provisionalControl.version -ne 2 -or
    $null -eq $provisionalControl.launch_intents.comfyui -or
    $null -eq $provisional -or
    $null -ne $provisionalControl.owned_processes.comfyui
) { throw 'Python provisional control state is invalid' }
$script:provisionalCandidate = [pscustomobject]@{
    ProcessId = [int] $provisional.pid
    CreationDate = ([DateTimeOffset]::Parse(
        [string] $provisional.started_after
    )).UtcDateTime.AddMilliseconds(1)
    ExecutablePath = [string] $provisional.executable_path
    CommandLine = [string] $intentCandidate.CommandLine
    ParentProcessId = [int] $provisional.parent_pid
    Name = [IO.Path]::GetFileName([string] $provisional.executable_path)
}
$script:stopCalls = 0
function Stop-Process {
    param([int] $Id, [switch] $Force, [object] $ErrorAction)
    $script:stopCalls += 1
    throw "unexpected stop of PID $Id"
}
if (Stop-ProvisionalStartedProcess -Token $provisional) {
    throw 'Parentless provisional identity was incorrectly accepted as owned'
}
if ($script:stopCalls -ne 0) {
    throw 'Wrapper tried to stop an unproved provisional process'
}
if (Remove-PrivateIdentityRecordsIfSafe `
        -HostManifestPath $env:TTS_MORE_PROVISIONAL_MANIFEST `
        -ControlStatePath $env:TTS_MORE_PROVISIONAL_CONTROL `
        -ProcessCleanupProven $false -TempCleanupProven $false `
        -OwnedProcessCount 1) {
    throw 'Wrapper removed unresolved Python restart recovery records'
}
if (
    -not (Test-Path -LiteralPath $env:TTS_MORE_PROVISIONAL_MANIFEST -PathType Leaf) -or
    -not (Test-Path -LiteralPath $env:TTS_MORE_PROVISIONAL_CONTROL -PathType Leaf)
) { throw 'Wrapper did not preserve unresolved Python restart recovery records' }
Write-Output 'PYTHON_RESTART_WRAPPER_RECOVERY_OK'
"""
    environment = os.environ.copy()
    environment["TTS_MORE_RELIABILITY_SCRIPT"] = str(script_path)
    environment["TTS_MORE_INTENT_CONTROL"] = str(recovery_paths["intent"][1])
    environment["TTS_MORE_PROVISIONAL_MANIFEST"] = str(
        recovery_paths["provisional"][0]
    )
    environment["TTS_MORE_PROVISIONAL_CONTROL"] = str(
        recovery_paths["provisional"][1]
    )

    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "PYTHON_RESTART_WRAPPER_RECOVERY_OK" in completed.stdout


def test_fix_round_3_wrapper_manifest_and_native_restart_use_semantic_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    root = (tmp_path / "semantic argv root with spaces").resolve()
    working_directory = root / "ComfyUI working directory"
    child_temp_root = root / "runner temp"
    comfy_temp_base = root / "ComfyUI temp base with spaces"
    comfy_temp_root = comfy_temp_base / "temp"
    for directory in (working_directory, child_temp_root, comfy_temp_root):
        directory.mkdir(parents=True)
    capture_script = working_directory / "main.py"
    capture_script.write_text(
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['TTS_MORE_ARGV_CAPTURE']).write_text(\n"
        "    json.dumps(sys.argv[1:]), encoding='utf-8'\n"
        ")\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "run-windows-comfyui-reliability.ps1"
    )
    control_state_path = root / "control.json"
    manifest_path = root / "host-manifest.json"
    initial_capture_path = root / "initial-argv.json"
    restart_capture_path = root / "restart-argv.json"
    command = r"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @(
    'Get-UtcTicks',
    'Test-RecordDocumentMatches',
    'ConvertTo-WindowsCommandLineArgument',
    'Write-PrivateJsonAtomic',
    'Write-LaunchIntentRunControlState',
    'Write-ProvisionalRunControlState',
    'Complete-ProvisionalStartupFailure',
    'Start-ProvisionallyTrackedProcess'
)) {
    $function = $ast.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    if ($null -eq $function) { throw "$name is missing" }
    Invoke-Expression $function.Extent.Text
}
function Find-SingleAssignment {
    param([string] $VariableName)
    $matches = @($ast.FindAll({
        param($node)
        $node -is [Management.Automation.Language.AssignmentStatementAst] -and
            $node.Left -is [Management.Automation.Language.VariableExpressionAst] -and
            $node.Left.VariablePath.UserPath -eq $VariableName
    }, $true))
    if ($matches.Count -ne 1) { throw "Expected one $VariableName assignment" }
    return $matches[0]
}

$runId = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
$listenAddress = '127.0.0.1'
$comfyLaunchMarker = "tts_more_reliability_run=$runId-comfyui"
$comfyPythonPath = [IO.Path]::GetFullPath($env:TTS_MORE_TEST_PYTHON)
$comfyRootPath = [IO.Path]::GetFullPath($env:TTS_MORE_TEST_WORKING)
$comfyTempBase = [IO.Path]::GetFullPath($env:TTS_MORE_TEST_COMFY_TEMP)
$runnerTempRoot = [IO.Path]::GetFullPath($env:TTS_MORE_TEST_CHILD_TEMP)
$comfyTempRoot = Join-Path $comfyTempBase 'temp'
$quotedAssignments = @($ast.FindAll({
    param($node)
    $node -is [Management.Automation.Language.AssignmentStatementAst] -and
        $node.Left -is [Management.Automation.Language.VariableExpressionAst] -and
        $node.Left.VariablePath.UserPath -eq 'quotedComfyTempBase'
}, $true))
if ($quotedAssignments.Count -gt 1) { throw 'Quoted temp assignment is ambiguous' }
if ($quotedAssignments.Count -eq 1) {
    Invoke-Expression $quotedAssignments[0].Extent.Text
}
$comfyArgumentsAssignment = Find-SingleAssignment -VariableName 'comfyArguments'
Invoke-Expression $comfyArgumentsAssignment.Extent.Text

$launcherRecord = [pscustomobject]@{
    pid = $PID
    creation_time = '2026-08-01T00:00:00Z'
}
$started = New-Object 'System.Collections.Generic.List[object]'
$env:TTS_MORE_ARGV_CAPTURE = $env:TTS_MORE_INITIAL_CAPTURE
$start = Start-ProvisionallyTrackedProcess -FilePath $comfyPythonPath `
    -ArgumentList $comfyArguments -WorkingDirectory $comfyRootPath `
    -ChildTempRoot $runnerTempRoot -LauncherRecord $launcherRecord `
    -StartedProcesses $started -ControlStatePath $env:TTS_MORE_TEST_CONTROL `
    -RunId $runId -ProcessLabel 'comfyui' -LaunchMarker $comfyLaunchMarker `
    -BackendRecord $null -ComfyRecord $null
$deadline = [DateTime]::UtcNow.AddSeconds(15)
while (
    -not (Test-Path -LiteralPath $env:TTS_MORE_INITIAL_CAPTURE -PathType Leaf) -and
    -not $start.process.HasExited -and
    [DateTime]::UtcNow -lt $deadline
) { Start-Sleep -Milliseconds 50 }
if (-not (Test-Path -LiteralPath $env:TTS_MORE_INITIAL_CAPTURE -PathType Leaf)) {
    throw 'Initial child did not publish argv'
}
if (-not $start.process.HasExited) {
    Stop-Process -Id $start.process.Id -Force -ErrorAction Stop
    if (-not $start.process.WaitForExit(15000)) {
        throw 'Initial child did not stop'
    }
}

$ttsRootPath = $env:TTS_MORE_TEST_ROOT
$suiteRoot = $env:TTS_MORE_TEST_ROOT
$gptRoot = $env:TTS_MORE_TEST_ROOT
$indexRoot = $env:TTS_MORE_TEST_ROOT
$cosyRoot = $env:TTS_MORE_TEST_ROOT
$registryPath = Join-Path $env:TTS_MORE_TEST_ROOT 'resources.yaml'
$references = [ordered]@{ reference = (Join-Path $env:TTS_MORE_TEST_ROOT 'reference.wav') }
$backendRecord = [ordered]@{
    pid = 8000
    creation_time = '2026-08-01T00:00:00Z'
    executable_path = $comfyPythonPath
    command_line = 'python -m uvicorn app.main:app'
    parent_pid = 7000
    parent_creation_time = '2026-07-31T23:59:00Z'
}
$comfyRecord = [ordered]@{
    pid = 8188
    creation_time = '2026-08-01T00:00:00Z'
    executable_path = $comfyPythonPath
    command_line = 'python main.py --port 8188'
    parent_pid = 7000
    parent_creation_time = '2026-07-31T23:59:00Z'
}
$backendLaunchRootRecord = $backendRecord
$comfyLaunchRootRecord = $comfyRecord
$hostManifestAssignment = Find-SingleAssignment -VariableName 'hostManifest'
Invoke-Expression $hostManifestAssignment.Extent.Text
$hostManifest | ConvertTo-Json -Depth 10 |
    Set-Content -LiteralPath $env:TTS_MORE_TEST_MANIFEST -Encoding UTF8
Write-Output 'WRAPPER_MANIFEST_CAPTURE_OK'
"""
    environment = os.environ.copy()
    environment.update(
        {
            "TTS_MORE_RELIABILITY_SCRIPT": str(script_path),
            "TTS_MORE_TEST_ROOT": str(root),
            "TTS_MORE_TEST_PYTHON": str(Path(os.sys.executable).resolve()),
            "TTS_MORE_TEST_WORKING": str(working_directory),
            "TTS_MORE_TEST_CHILD_TEMP": str(child_temp_root),
            "TTS_MORE_TEST_COMFY_TEMP": str(comfy_temp_base),
            "TTS_MORE_TEST_CONTROL": str(control_state_path),
            "TTS_MORE_TEST_MANIFEST": str(manifest_path),
            "TTS_MORE_INITIAL_CAPTURE": str(initial_capture_path),
        }
    )
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "WRAPPER_MANIFEST_CAPTURE_OK" in completed.stdout

    initial_argv = json.loads(initial_capture_path.read_text(encoding="utf-8"))
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    manifest = reliability_validation.PrivateHostManifest.read(manifest_path)
    launch = manifest.launch["comfyui"]
    real_popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []
    intents: list[reliability_validation.PrivateRestartLaunchIntent] = []
    provisional_records: list[
        reliability_validation.PrivateRestartProvisionalProcess
    ] = []
    promoted: list[reliability_validation.RecordedProcessIdentity] = []
    parent_created = datetime.now(timezone.utc) - timedelta(minutes=1)
    parent = reliability_validation.RecordedProcessIdentity(
        pid=os.getpid(),
        creation_time=parent_created,
        executable_path=Path(__file__).resolve(),
        command_line="validator-python",
        parent_pid=7000,
        parent_creation_time=parent_created - timedelta(seconds=1),
    )

    def capturing_popen(
        command_line: list[str],
        **kwargs: object,
    ) -> subprocess.Popen[bytes]:
        child = real_popen(command_line, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(reliability_validation.subprocess, "Popen", capturing_popen)
    monkeypatch.setenv("TTS_MORE_ARGV_CAPTURE", str(restart_capture_path))
    system = object.__new__(reliability_validation.NativeWindowsHostSystem)
    system._started_identities = {}
    system._active_tokens = []

    def inspect_process(pid: int) -> reliability_validation.RecordedProcessIdentity:
        if pid == os.getpid():
            return parent
        assert children and provisional_records and intents
        assert pid == children[0].pid
        return reliability_validation.RecordedProcessIdentity(
            pid=pid,
            creation_time=provisional_records[0].started_after,
            executable_path=launch.executable_path,
            command_line=subprocess.list2cmdline(
                [str(launch.executable_path), *intents[0].arguments]
            ),
            parent_pid=parent.pid,
            parent_creation_time=parent.creation_time,
        )

    system.inspect_process = inspect_process
    system.port_owner = lambda _port: inspect_process(children[0].pid)
    try:
        replacement = system.restart_owned(
            manifest.owned_processes["comfyui"],
            launch,
            1.0,
            run_id=manifest.run_id,
            lifecycle=reliability_validation.PrivateRestartLifecycle(
                persist_launch_intent=intents.append,
                persist_provisional=provisional_records.append,
                promote=promoted.append,
            ),
        )
        deadline = time.monotonic() + 10.0
        while not restart_capture_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert restart_capture_path.exists()
        restart_argv = json.loads(restart_capture_path.read_text(encoding="utf-8"))
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=15)
        system._started_identities.clear()

    semantic_child_argv = [
        "--listen",
        "127.0.0.1",
        "--port",
        "8188",
        "--temp-directory",
        str(comfy_temp_base),
    ]
    semantic_launch_argv = [
        "-X",
        "tts_more_reliability_run=" + "a" * 32 + "-comfyui",
        "main.py",
        *semantic_child_argv,
    ]
    assert {
        "initial_child": initial_argv,
        "persisted_manifest": raw_manifest["launch"]["comfyui"]["arguments"],
        "python_manifest": list(launch.arguments),
        "restart_intent_tail": list(intents[0].arguments[2:]),
        "restart_child": restart_argv,
    } == {
        "initial_child": semantic_child_argv,
        "persisted_manifest": semantic_launch_argv,
        "python_manifest": semantic_launch_argv,
        "restart_intent_tail": semantic_launch_argv,
        "restart_child": semantic_child_argv,
    }
    assert replacement == promoted[0]


def test_task_12_delegated_windows_listener_is_promoted_from_exact_launch_root(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "run-windows-comfyui-reliability.ps1"
    )
    listener_script = tmp_path / "delegated_listener.py"
    listener_script.write_text(
        "import socket, sys, time\n"
        "with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:\n"
        "    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "    listener.bind(('127.0.0.1', int(sys.argv[1])))\n"
        "    listener.listen()\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    launcher_script = tmp_path / "venv_style_launcher.py"
    launcher_script.write_text(
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "raise SystemExit(child.wait())\n",
        encoding="utf-8",
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as available:
        available.bind(("127.0.0.1", 0))
        port = int(available.getsockname()[1])
    base_python = Path(
        getattr(os.sys, "_base_executable", os.sys.executable)
    ).resolve()
    command = r"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @(
    'Get-UtcTicks',
    'Get-PortOwnerPid',
    'Get-ProcessRecord',
    'Test-RecordDocumentMatches',
    'Test-RecordedIdentity',
    'Test-ProcessAbsent',
    'Wait-ProcessRecord',
    'ConvertTo-WindowsCommandLineArgument',
    'Get-ExactCurrentProcessRecord',
    'Resolve-DelegatedListenerRecord',
    'Wait-ExactPortOwner',
    'Stop-RecordedTree'
)) {
    $function = $ast.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    if ($null -eq $function) { throw "$name is missing" }
    Invoke-Expression $function.Extent.Text
}

$startedAfter = [DateTime]::UtcNow.AddMilliseconds(-100).ToString('o')
$arguments = (@(
    (ConvertTo-WindowsCommandLineArgument $env:TTS_MORE_DELEGATED_LAUNCHER),
    (ConvertTo-WindowsCommandLineArgument $env:TTS_MORE_DELEGATED_LISTENER),
    $env:TTS_MORE_DELEGATED_PORT
)) -join ' '
$process = Start-Process -FilePath $env:TTS_MORE_DELEGATED_PYTHON `
    -ArgumentList $arguments -WorkingDirectory $env:TTS_MORE_DELEGATED_ROOT `
    -WindowStyle Hidden -PassThru
$launchRecord = $null
try {
    $launchRecord = Wait-ProcessRecord -ProcessId $process.Id -TimeoutSeconds 10
    $listenerRecord = Wait-ExactPortOwner `
        -Port ([int] $env:TTS_MORE_DELEGATED_PORT) `
        -ProcessId $process.Id -Process $process -LaunchRecord $launchRecord `
        -StartedAfter $startedAfter -TimeoutSeconds 10
    if ([int] $listenerRecord.pid -eq [int] $launchRecord.pid) {
        throw 'delegated listener was not returned as the actual owner'
    }
    if ((Get-PortOwnerPid ([int] $env:TTS_MORE_DELEGATED_PORT)) -ne [int] $listenerRecord.pid) {
        throw 'returned listener no longer owns the delegated port'
    }
    if (-not (Test-RecordedIdentity $launchRecord) -or -not (Test-RecordedIdentity $listenerRecord)) {
        throw 'promoted launch/listener identities are not both current'
    }
    Write-Output ('DELEGATED_LISTENER_PROMOTED:{0}:{1}' -f `
        [int] $launchRecord.pid, [int] $listenerRecord.pid)
} finally {
    if ($null -ne $launchRecord) {
        $null = Stop-RecordedTree -Record $launchRecord
    } elseif (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction Stop
        $process.WaitForExit()
    }
}
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            **os.environ,
            "TTS_MORE_RELIABILITY_SCRIPT": str(script_path),
            "TTS_MORE_DELEGATED_PYTHON": str(base_python),
            "TTS_MORE_DELEGATED_LAUNCHER": str(launcher_script),
            "TTS_MORE_DELEGATED_LISTENER": str(listener_script),
            "TTS_MORE_DELEGATED_ROOT": str(tmp_path),
            "TTS_MORE_DELEGATED_PORT": str(port),
        },
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "DELEGATED_LISTENER_PROMOTED:" in completed.stdout


def test_task_12_delegated_listener_resolution_fails_closed_for_identity_anomalies() -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "run-windows-comfyui-reliability.ps1"
    )
    command = r"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @(
    'Get-UtcTicks',
    'Get-ProcessRecord',
    'Test-RecordDocumentMatches',
    'Get-ExactCurrentProcessRecord',
    'Resolve-DelegatedListenerRecord',
    'Wait-ExactPortOwner'
)) {
    $function = $ast.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    if ($null -eq $function) { throw "$name is missing" }
    Invoke-Expression $function.Extent.Text
}

function New-ProcessRow {
    param(
        [int] $ProcessId,
        [int] $ParentProcessId,
        [string] $Created,
        [string] $ExecutablePath = 'C:\controlled\python.exe'
    )
    return [pscustomobject]@{
        ProcessId = $ProcessId
        ParentProcessId = $ParentProcessId
        CreationDate = [DateTime]::Parse($Created).ToUniversalTime()
        ExecutablePath = $ExecutablePath
        CommandLine = ('python.exe controlled-{0}.py' -f $ProcessId)
        Name = 'python.exe'
    }
}
$script:root = New-ProcessRow 100 50 '2026-08-01T00:00:01Z'
$script:listener = New-ProcessRow 200 100 '2026-08-01T00:00:02Z'
$script:parent = New-ProcessRow 50 4 '2026-08-01T00:00:00Z'
$script:foreign = New-ProcessRow 300 50 '2026-08-01T00:00:02Z'
$script:mode = 'good'
$script:queries = @{}
function Get-CimInstance {
    param([string] $ClassName, [string] $Filter, [object] $ErrorAction)
    if ($script:mode -eq 'query-error') { throw 'injected ancestry query failure' }
    $candidatePid = [int] ([regex]::Match($Filter, '\d+').Value)
    if (-not $script:queries.ContainsKey($candidatePid)) { $script:queries[$candidatePid] = 0 }
    $script:queries[$candidatePid] += 1
    if ($script:mode -eq 'missing-parent' -and $candidatePid -eq 100) { return $null }
    if ($script:mode -eq 'root-reuse' -and $candidatePid -eq 100 -and $script:queries[$candidatePid] -gt 1) {
        return New-ProcessRow 100 50 '2026-08-01T00:00:03Z'
    }
    if ($script:mode -eq 'listener-reuse' -and $candidatePid -eq 200 -and $script:queries[$candidatePid] -gt 1) {
        return New-ProcessRow 200 100 '2026-08-01T00:00:03Z'
    }
    if ($candidatePid -eq 100) { return $script:root }
    if ($candidatePid -eq 200) { return $script:listener }
    if ($candidatePid -eq 300) { return $script:foreign }
    if ($candidatePid -eq 50) { return $script:parent }
    return $null
}
function Get-PortOwnerPid {
    param([int] $Port)
    if ($script:mode -eq 'ambiguous') { throw 'Port 8000 has multiple listening owners' }
    if ($script:mode -eq 'unrelated') { return 300 }
    return 200
}
$tracked = [pscustomobject]@{ HasExited = $false }
$launchRecord = [pscustomobject]@{
    pid = 100
    creation_time = '2026-08-01T00:00:01Z'
    executable_path = 'C:\controlled\python.exe'
    command_line = 'python.exe controlled-100.py'
    parent_pid = 50
    parent_creation_time = '2026-08-01T00:00:00Z'
}
$failures = [System.Collections.Generic.List[string]]::new()

$listenerRecord = Wait-ExactPortOwner -Port 8000 -ProcessId 100 `
    -Process $tracked -LaunchRecord $launchRecord `
    -StartedAfter '2026-08-01T00:00:00Z' -TimeoutSeconds 1
if ([int] $listenerRecord.pid -ne 200) {
    $failures.Add('verified descendant listener was not accepted')
}

foreach ($case in @(
    [pscustomobject]@{ mode = 'unrelated'; started = '2026-08-01T00:00:00Z' },
    [pscustomobject]@{ mode = 'ambiguous'; started = '2026-08-01T00:00:00Z' },
    [pscustomobject]@{ mode = 'missing-parent'; started = '2026-08-01T00:00:00Z' },
    [pscustomobject]@{ mode = 'query-error'; started = '2026-08-01T00:00:00Z' },
    [pscustomobject]@{ mode = 'root-reuse'; started = '2026-08-01T00:00:00Z' },
    [pscustomobject]@{ mode = 'listener-reuse'; started = '2026-08-01T00:00:00Z' },
    [pscustomobject]@{ mode = 'good'; started = '2026-08-01T00:00:02.500Z' }
)) {
    $script:mode = $case.mode
    $script:queries = @{}
    $caught = $null
    try {
        $null = Wait-ExactPortOwner -Port 8000 -ProcessId 100 `
            -Process $tracked -LaunchRecord $launchRecord `
            -StartedAfter $case.started -TimeoutSeconds 1
    } catch { $caught = $_ }
    if ($null -eq $caught) { $failures.Add("$($case.mode) was accepted") }
}
if ($failures.Count -ne 0) { throw ($failures -join '; ') }
Write-Output 'DELEGATED_LISTENER_FAIL_CLOSED_OK'
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "TTS_MORE_RELIABILITY_SCRIPT": str(script_path)},
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "DELEGATED_LISTENER_FAIL_CLOSED_OK" in completed.stdout


def test_task_12_python_control_preserves_launch_root_and_actual_listener(
    tmp_path: Path,
) -> None:
    document = _host_manifest_document(tmp_path)
    owned = document["owned_processes"]
    launch_roots = document["launch_roots"]
    assert isinstance(owned, dict)
    assert isinstance(launch_roots, dict)
    listener = owned["tts-more"]
    assert isinstance(listener, dict)
    root = dict(listener)
    root.update(
        {
            "pid": 7999,
            "creation_time": datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc).isoformat(),
            "command_line": "venv-python.exe -m uvicorn app.main:app",
        }
    )
    listener["parent_pid"] = 7999
    listener["parent_creation_time"] = root["creation_time"]
    launch_roots["tts-more"] = root
    manifest_path = tmp_path / "host-manifest.json"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    manifest = reliability_validation.PrivateHostManifest.read(manifest_path)
    system = _FakeWindowsHostSystem(document)
    probe = reliability_validation.WindowsReliabilityHostProbe.from_manifest(
        manifest_path,
        system=system,
    )
    control = json.loads(
        Path(f"{manifest_path}.current.json").read_text(encoding="utf-8")
    )

    assert manifest.launch_roots["tts-more"].pid == 7999
    assert manifest.owned_processes["tts-more"].pid == 8000
    assert control["launch_roots"]["tts-more"]["pid"] == 7999
    assert control["owned_processes"]["tts-more"]["pid"] == 8000
    assert probe.owned_processes["tts-more"].pid == 8000


def test_task_12_listener_promotion_and_dual_identity_cleanup_fail_closed(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "run-windows-comfyui-reliability.ps1"
    )
    command = r"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @(
    'Get-UtcTicks',
    'Test-RecordDocumentMatches',
    'Write-PrivateJsonAtomic',
    'Write-RunControlState',
    'Write-ListenerRunControlState',
    'Stop-RecordedProcessPair'
)) {
    $function = $ast.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    if ($null -eq $function) { throw "$name is missing" }
    Invoke-Expression $function.Extent.Text
}
function Test-RecordedIdentity { param([object] $Record) return $true }
$root = [pscustomobject]@{
    pid = 100
    creation_time = '2026-08-01T00:00:01Z'
    executable_path = 'C:\controlled\venv-python.exe'
    command_line = 'venv-python.exe -m uvicorn app.main:app'
    parent_pid = 50
    parent_creation_time = '2026-08-01T00:00:00Z'
}
$listener = [pscustomobject]@{
    pid = 200
    creation_time = '2026-08-01T00:00:02Z'
    executable_path = 'C:\controlled\python.exe'
    command_line = 'python.exe -m uvicorn app.main:app'
    parent_pid = 100
    parent_creation_time = '2026-08-01T00:00:01Z'
}
$statePath = Join-Path $env:TTS_MORE_PRIVATE_TEST_ROOT 'control.json'
$runId = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
Write-RunControlState -Path $statePath -RunId $runId `
    -BackendRecord $root -ComfyRecord $null `
    -BackendLaunchRootRecord $root -ComfyLaunchRootRecord $null
$initial = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
if (
    [int] $initial.owned_processes.'tts-more'.pid -ne 100 -or
    [int] $initial.launch_roots.'tts-more'.pid -ne 100
) { throw 'initial launch root was not durably represented' }

$script:realAtomicWriter = (Get-Command Write-PrivateJsonAtomic).ScriptBlock
function Write-PrivateJsonAtomic { throw 'injected listener persistence failure' }
$caught = $null
try {
    Write-ListenerRunControlState -Path $statePath -RunId $runId `
        -ProcessLabel 'tts-more' -LaunchRootRecord $root -ListenerRecord $listener `
        -BackendRecord $root -ComfyRecord $null `
        -BackendLaunchRootRecord $root -ComfyLaunchRootRecord $null
} catch { $caught = $_ }
if ($null -eq $caught -or $caught.Exception.Message -ne 'injected listener persistence failure') {
    throw 'listener persistence failure was not surfaced'
}
$retained = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
if (
    [int] $retained.owned_processes.'tts-more'.pid -ne 100 -or
    [int] $retained.launch_roots.'tts-more'.pid -ne 100
) { throw 'failed listener persistence destroyed launch-root recovery state' }

function Write-PrivateJsonAtomic {
    param([string] $Path, [object] $Document)
    & $script:realAtomicWriter -Path $Path -Document $Document
}
Write-ListenerRunControlState -Path $statePath -RunId $runId `
    -ProcessLabel 'tts-more' -LaunchRootRecord $root -ListenerRecord $listener `
    -BackendRecord $root -ComfyRecord $null `
    -BackendLaunchRootRecord $root -ComfyLaunchRootRecord $null
$promoted = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
if (
    [int] $promoted.owned_processes.'tts-more'.pid -ne 200 -or
    [int] $promoted.launch_roots.'tts-more'.pid -ne 100
) { throw 'listener promotion did not retain both identities' }

$script:stopCalls = [System.Collections.Generic.List[int]]::new()
$script:stopMode = 'success'
function Stop-RecordedTree {
    param([object] $Record)
    $script:stopCalls.Add([int] $Record.pid)
    if ($script:stopMode -eq 'query-error' -and [int] $Record.pid -eq 100) {
        throw 'injected cleanup identity query failure'
    }
    if ($script:stopMode -eq 'listener-failure' -and [int] $Record.pid -eq 200) {
        return $false
    }
    return $true
}
if (-not (Stop-RecordedProcessPair -LaunchRootRecord $root -ListenerRecord $listener)) {
    throw 'proved root/listener cleanup did not converge'
}
if ((@($script:stopCalls) -join ',') -ne '100,200') {
    throw 'cleanup did not cover launch root then actual listener exactly once'
}
foreach ($mode in @('listener-failure', 'query-error')) {
    $script:stopMode = $mode
    $script:stopCalls.Clear()
    if (Stop-RecordedProcessPair -LaunchRootRecord $root -ListenerRecord $listener) {
        throw "$mode cleanup was incorrectly proven"
    }
    if ((@($script:stopCalls) -join ',') -ne '100,200') {
        throw "$mode cleanup did not retain bounded coverage of both identities"
    }
}
$script:stopMode = 'success'
$script:stopCalls.Clear()
if (-not (Stop-RecordedProcessPair -LaunchRootRecord $root -ListenerRecord $root)) {
    throw 'identical root/listener cleanup did not converge'
}
if ((@($script:stopCalls) -join ',') -ne '100') {
    throw 'identical root/listener was stopped more than once'
}
Write-Output 'LISTENER_PROMOTION_AND_CLEANUP_FAIL_CLOSED_OK'
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            **os.environ,
            "TTS_MORE_RELIABILITY_SCRIPT": str(script_path),
            "TTS_MORE_PRIVATE_TEST_ROOT": str(tmp_path),
        },
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "LISTENER_PROMOTION_AND_CLEANUP_FAIL_CLOSED_OK" in completed.stdout


def test_task_12_recorded_tree_rejects_stale_parent_pid_edges_without_stopping_orphans() -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "run-windows-comfyui-reliability.ps1"
    )
    command = r"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @(
    'Get-UtcTicks',
    'Get-ProcessRecord',
    'Test-RecordDocumentMatches',
    'Test-RecordedIdentity',
    'Test-ProcessAbsent',
    'Stop-RecordedTree',
    'Stop-RecordedProcessPair'
)) {
    $function = $ast.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    if ($null -eq $function) { throw "$name is missing" }
    Invoke-Expression $function.Extent.Text
}
function New-Row {
    param([int] $ProcessId, [int] $ParentProcessId, [string] $Created)
    return [pscustomobject]@{
        ProcessId = $ProcessId
        ParentProcessId = $ParentProcessId
        CreationDate = [DateTime]::Parse($Created).ToUniversalTime()
        ExecutablePath = ('C:\controlled\python-{0}.exe' -f $ProcessId)
        CommandLine = ('python-{0}.exe controlled.py' -f $ProcessId)
        Name = ('python-{0}.exe' -f $ProcessId)
    }
}
function New-RootRecord {
    return [pscustomobject]@{
        pid = 100
        creation_time = '2026-08-01T00:00:10Z'
        executable_path = 'C:\controlled\python-100.exe'
        command_line = 'python-100.exe controlled.py'
        parent_pid = 50
        parent_creation_time = '2026-08-01T00:00:00Z'
    }
}
$script:inventory = @()
$script:stopped = [System.Collections.Generic.List[int]]::new()
$script:queryErrorPid = 0
function Get-CimInstance {
    param([string] $ClassName, [string] $Filter, [object] $ErrorAction)
    if (-not $Filter) { return @($script:inventory) }
    $candidatePid = [int] ([regex]::Match($Filter, '\d+').Value)
    if ($candidatePid -eq $script:queryErrorPid) {
        throw 'injected descendant identity query failure'
    }
    return @($script:inventory | Where-Object {
        [int] $_.ProcessId -eq $candidatePid
    }) | Select-Object -First 1
}
function Stop-Process {
    param([int] $Id, [switch] $Force, [object] $ErrorAction)
    $script:stopped.Add($Id)
    $script:inventory = @($script:inventory | Where-Object {
        [int] $_.ProcessId -ne $Id
    })
}
function Set-Inventory {
    param([object[]] $Rows)
    $script:inventory = @($Rows)
    $script:stopped.Clear()
    $script:queryErrorPid = 0
}
$parent = New-Row 50 4 '2026-08-01T00:00:00Z'
$root = New-Row 100 50 '2026-08-01T00:00:10Z'
$rootRecord = New-RootRecord
$failures = [System.Collections.Generic.List[string]]::new()

$staleDirect = New-Row 200 100 '2026-08-01T00:00:05Z'
Set-Inventory @($parent, $root, $staleDirect)
if (Stop-RecordedProcessPair -LaunchRootRecord $rootRecord -ListenerRecord $rootRecord) {
    $failures.Add('stale direct numeric-parent edge was cleanup-proven')
}
if ($script:stopped.Count -ne 0) {
    $failures.Add('stale direct orphan or root was stopped before ancestry proof completed')
}

$validChild = New-Row 200 100 '2026-08-01T00:00:11Z'
$staleGrandchild = New-Row 300 200 '2026-08-01T00:00:10.500Z'
Set-Inventory @($parent, $root, $validChild, $staleGrandchild)
if (Stop-RecordedProcessPair -LaunchRootRecord $rootRecord -ListenerRecord $rootRecord) {
    $failures.Add('stale multi-generation numeric-parent edge was cleanup-proven')
}
if ($script:stopped.Count -ne 0) {
    $failures.Add('multi-generation anomaly stopped a process before full tree proof')
}

Set-Inventory @($parent, $root, $validChild)
$script:queryErrorPid = 200
if (Stop-RecordedProcessPair -LaunchRootRecord $rootRecord -ListenerRecord $rootRecord) {
    $failures.Add('descendant identity query failure was cleanup-proven')
}
if ($script:stopped.Count -ne 0) {
    $failures.Add('descendant query failure stopped a process')
}

$validGrandchild = New-Row 300 200 '2026-08-01T00:00:12Z'
Set-Inventory @($parent, $root, $validChild, $validGrandchild)
if (-not (Stop-RecordedProcessPair -LaunchRootRecord $rootRecord -ListenerRecord $rootRecord)) {
    $failures.Add('fully ordered multi-generation tree did not cleanup-converge')
}
if ((@($script:stopped) -join ',') -ne '300,200,100') {
    $failures.Add('ordered multi-generation tree did not stop only its exact members')
}
if ($failures.Count -ne 0) { throw ($failures -join '; ') }
Write-Output 'RECORDED_TREE_EDGE_ORDERING_OK'
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "TTS_MORE_RELIABILITY_SCRIPT": str(script_path)},
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "RECORDED_TREE_EDGE_ORDERING_OK" in completed.stdout


def test_task_12_recorded_tree_rejects_snapshot_to_live_pid_reuse_without_stopping() -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "run-windows-comfyui-reliability.ps1"
    )
    command = r"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @(
    'Get-UtcTicks',
    'Get-ProcessRecord',
    'Test-RecordDocumentMatches',
    'Test-RecordedIdentity',
    'Test-ProcessAbsent',
    'Stop-RecordedTree',
    'Stop-RecordedProcessPair'
)) {
    $function = $ast.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    if ($null -eq $function) { throw "$name is missing" }
    Invoke-Expression $function.Extent.Text
}
function New-Row {
    param([int] $ProcessId, [int] $ParentProcessId, [string] $Created)
    return [pscustomobject]@{
        ProcessId = $ProcessId
        ParentProcessId = $ParentProcessId
        CreationDate = [DateTime]::Parse($Created).ToUniversalTime()
        ExecutablePath = ('C:\controlled\python-{0}.exe' -f $ProcessId)
        CommandLine = ('python-{0}.exe controlled.py' -f $ProcessId)
        Name = ('python-{0}.exe' -f $ProcessId)
    }
}
function New-RootRecord {
    return [pscustomobject]@{
        pid = 100
        creation_time = '2026-08-01T00:00:10Z'
        executable_path = 'C:\controlled\python-100.exe'
        command_line = 'python-100.exe controlled.py'
        parent_pid = 50
        parent_creation_time = '2026-08-01T00:00:00Z'
    }
}
$script:snapshot = @()
$script:live = @()
$script:querySequences = @{}
$script:queryCounts = @{}
$script:stopped = [System.Collections.Generic.List[int]]::new()
function Get-CimInstance {
    param([string] $ClassName, [string] $Filter, [object] $ErrorAction)
    if (-not $Filter) { return @($script:snapshot) }
    $candidatePid = [int] ([regex]::Match($Filter, '\d+').Value)
    if ($script:querySequences.ContainsKey($candidatePid)) {
        $index = 0
        if ($script:queryCounts.ContainsKey($candidatePid)) {
            $index = [int] $script:queryCounts[$candidatePid]
        }
        $sequence = @($script:querySequences[$candidatePid])
        if ($index -ge $sequence.Count) { $index = $sequence.Count - 1 }
        $script:queryCounts[$candidatePid] = $index + 1
        return $sequence[$index]
    }
    return @($script:live | Where-Object {
        [int] $_.ProcessId -eq $candidatePid
    }) | Select-Object -First 1
}
function Stop-Process {
    param([int] $Id, [switch] $Force, [object] $ErrorAction)
    $script:stopped.Add($Id)
    $script:live = @($script:live | Where-Object {
        [int] $_.ProcessId -ne $Id
    })
}
function Set-State {
    param([object[]] $Snapshot, [object[]] $Live)
    $script:snapshot = @($Snapshot)
    $script:live = @($Live)
    $script:querySequences = @{}
    $script:queryCounts = @{}
    $script:stopped.Clear()
}
$parent = New-Row 50 4 '2026-08-01T00:00:00Z'
$root = New-Row 100 50 '2026-08-01T00:00:10Z'
$rootRecord = New-RootRecord
$failures = [System.Collections.Generic.List[string]]::new()

$snapshotChild = New-Row 200 100 '2026-08-01T00:00:11Z'
$unrelatedParent = New-Row 300 50 '2026-08-01T00:00:10Z'
$reusedChildPid = New-Row 200 300 '2026-08-01T00:00:12Z'
Set-State @($parent, $root, $snapshotChild) @(
    $parent, $root, $unrelatedParent, $reusedChildPid
)
if (Stop-RecordedProcessPair -LaunchRootRecord $rootRecord -ListenerRecord $rootRecord) {
    $failures.Add('snapshot child PID reuse under another parent was cleanup-proven')
}
if ($script:stopped.Count -ne 0) {
    $failures.Add('snapshot child PID reuse stopped a process before exact edge proof')
}

$snapshotGrandchild = New-Row 400 200 '2026-08-01T00:00:12Z'
$newMiddleParentParent = New-Row 500 50 '2026-08-01T00:00:09Z'
$reusedMiddleParent = New-Row 200 500 '2026-08-01T00:00:11.500Z'
Set-State @($parent, $root, $snapshotChild, $snapshotGrandchild) @(
    $parent, $root, $newMiddleParentParent, $reusedMiddleParent, $snapshotGrandchild
)
$script:querySequences[200] = @($snapshotChild, $reusedMiddleParent)
if (Stop-RecordedProcessPair -LaunchRootRecord $rootRecord -ListenerRecord $rootRecord) {
    $failures.Add('multi-generation middle-parent PID reuse was cleanup-proven')
}
if ($script:stopped.Count -ne 0) {
    $failures.Add('middle-parent PID reuse stopped a process before exact edge proof')
}

Set-State @($parent, $root, $snapshotChild, $snapshotGrandchild) @(
    $parent, $root, $snapshotChild, $snapshotGrandchild
)
if (-not (Stop-RecordedProcessPair -LaunchRootRecord $rootRecord -ListenerRecord $rootRecord)) {
    $failures.Add('unchanged exact multi-generation tree did not cleanup-converge')
}
if ((@($script:stopped) -join ',') -ne '400,200,100') {
    $failures.Add('unchanged exact tree did not stop only its recorded members')
}
if ($failures.Count -ne 0) { throw ($failures -join '; ') }
Write-Output 'RECORDED_TREE_SNAPSHOT_REUSE_OK'
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "TTS_MORE_RELIABILITY_SCRIPT": str(script_path)},
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "RECORDED_TREE_SNAPSHOT_REUSE_OK" in completed.stdout


def test_task_12_validator_uses_backend_import_context_and_restores_launcher_cwd() -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    repository_root = Path(__file__).resolve().parents[2]
    backend_root = repository_root / "backend"
    backend_python = backend_root / ".venv" / "Scripts" / "python.exe"
    if not backend_python.is_file():
        pytest.skip("The formal backend venv is unavailable")
    script_path = repository_root / "scripts" / "run-windows-comfyui-reliability.ps1"

    root_probe = subprocess.run(
        [
            str(backend_python),
            "-m",
            "app.comfyui.reliability_validation",
            "--help",
        ],
        cwd=repository_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    assert root_probe.returncode != 0
    assert "No module named 'app'" in root_probe.stderr

    backend_probe = subprocess.run(
        [
            str(backend_python),
            "-m",
            "app.comfyui.reliability_validation",
            "--help",
        ],
        cwd=backend_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    assert backend_probe.returncode == 0, backend_probe.stderr
    assert "usage: reliability_validation.py" in backend_probe.stdout

    command = r"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_RELIABILITY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @('Invoke-ReliabilityValidator', 'Complete-LauncherFailureState')) {
    $function = $ast.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    if ($null -eq $function) { throw "$name is missing" }
    Invoke-Expression $function.Extent.Text
}

$originalLocation = (Get-Location).Path
$moduleArguments = @('-m', 'app.comfyui.reliability_validation', '--help')
$null = Invoke-ReliabilityValidator `
    -PythonPath $env:TTS_MORE_TEST_BACKEND_PYTHON `
    -ValidatorArguments $moduleArguments `
    -WorkingDirectory $env:TTS_MORE_TEST_BACKEND_ROOT
if ((Get-Location).Path -ne $originalLocation) {
    throw 'successful validator invocation did not restore the launcher location'
}

$nonzero = $null
try {
    Invoke-ReliabilityValidator `
        -PythonPath $env:TTS_MORE_TEST_POWERSHELL `
        -ValidatorArguments @('-NoProfile', '-NonInteractive', '-Command', 'exit 23') `
        -WorkingDirectory $env:TTS_MORE_TEST_BACKEND_ROOT
} catch { $nonzero = $_ }
if (
    $null -eq $nonzero -or
    $nonzero.Exception.Message -ne 'Windows ComfyUI reliability gate failed'
) { throw 'nonzero validator exit was not surfaced as the formal gate failure' }
if ((Get-Location).Path -ne $originalLocation) {
    throw 'nonzero validator invocation did not restore the launcher location'
}

$invocationError = $null
try {
    Invoke-ReliabilityValidator `
        -PythonPath (Join-Path $env:TTS_MORE_TEST_BACKEND_ROOT 'missing-validator.exe') `
        -ValidatorArguments @('--never-runs') `
        -WorkingDirectory $env:TTS_MORE_TEST_BACKEND_ROOT
} catch { $invocationError = $_ }
if ($null -eq $invocationError) {
    throw 'thrown validator invocation error was swallowed'
}
if ((Get-Location).Path -ne $originalLocation) {
    throw 'thrown validator invocation did not restore the launcher location'
}

$cleanup = $null
try { throw 'secondary injected cleanup failure' } catch { $cleanup = $_ }
$arbitrated = $null
try {
    Complete-LauncherFailureState `
        -PrimaryFailure $nonzero -CleanupFailure $cleanup
} catch { $arbitrated = $_ }
if (
    $null -eq $arbitrated -or
    $arbitrated.Exception.Message -ne 'Windows ComfyUI reliability gate failed'
) { throw 'validator primary failure was replaced by cleanup failure' }
if ((Get-Location).Path -ne $originalLocation) {
    throw 'failure arbitration changed the launcher location'
}

$validatorCalls = @($ast.FindAll({
    param($node)
    $node -is [Management.Automation.Language.CommandAst] -and
        $node.GetCommandName() -eq 'Invoke-ReliabilityValidator'
}, $true))
if ($validatorCalls.Count -ne 1) {
    throw 'formal validator invocation is missing or ambiguous'
}
function Invoke-ReliabilityValidator {
    param(
        [string] $PythonPath,
        [string[]] $ValidatorArguments,
        [string] $WorkingDirectory
    )
    $script:capturedValidatorCall = [pscustomobject]@{
        python_path = $PythonPath
        arguments = @($ValidatorArguments)
        working_directory = $WorkingDirectory
    }
}
$backendPythonPath = $env:TTS_MORE_TEST_BACKEND_PYTHON
$backendRootPath = $env:TTS_MORE_TEST_BACKEND_ROOT
$pythonArguments = @(
    '-m', 'app.comfyui.reliability_validation',
    '--fixture', 'controlled fixture path',
    '--preflight-only'
)
$script:capturedValidatorCall = $null
Invoke-Expression $validatorCalls[0].Extent.Text
if (
    $null -eq $script:capturedValidatorCall -or
    $script:capturedValidatorCall.python_path -ne $backendPythonPath -or
    $script:capturedValidatorCall.working_directory -ne $backendRootPath -or
    (@($script:capturedValidatorCall.arguments) -join "`0") -ne
        ($pythonArguments -join "`0")
) { throw 'formal validator wiring changed its executable, arguments, or backend context' }
Write-Output 'VALIDATOR_BACKEND_CONTEXT_AND_RESTORATION_OK'
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=repository_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            **os.environ,
            "TTS_MORE_RELIABILITY_SCRIPT": str(script_path),
            "TTS_MORE_TEST_BACKEND_PYTHON": str(backend_python),
            "TTS_MORE_TEST_BACKEND_ROOT": str(backend_root),
            "TTS_MORE_TEST_POWERSHELL": powershell,
        },
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert "VALIDATOR_BACKEND_CONTEXT_AND_RESTORATION_OK" in completed.stdout


REPOSITORY_LABELS = ("tts-more", "tts-audio-suite", "comfyui", "gpt-sovits", "indextts", "cosyvoice")
