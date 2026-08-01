from __future__ import annotations

import json
import math
import os
import struct
import wave
from datetime import datetime, timezone
from pathlib import Path

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
    if actual == "completed" and audio is None:
        audio = AudioProof(
            sha256="e" * 64,
            size_bytes=1600,
            sample_rate=16000,
            frames=800,
            peak=0.25,
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
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        audio=audio,
        cleanup=CleanupEvidence(ok=cleanup_ok, owned_processes_stopped=True, temp_paths_removed=True),
        processes=[ProcessEvidence(pid=123, ownership="validator-owned", command_hash="a" * 64, creation_time=datetime.now(timezone.utc), parent_pid=1, parent_creation_time=datetime.now(timezone.utc), executable_name="python.exe", executable_hash="a" * 64, ownership_hash="b" * 64, started=True, stopped=True, descendants_stopped=True, alive_after=False)],
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


def _write_voiced_wav(path: Path) -> None:
    frames = [int(8_000 * math.sin(index / 8)) for index in range(800)]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"".join(struct.pack("<h", frame) for frame in frames))


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
    cases = [
        _case(f"steady-{engine}-{index:02d}", engine)
        for engine in ("gpt-sovits", "indextts", "cosyvoice")
        for index in range(1, 11)
    ]
    cases.extend([
        _case("cancel-index", "indextts", phase="fault", expected="cancelled", actual="cancelled"),
        _case("restart-cosy", "cosyvoice", phase="recovery"),
    ])
    required = {"cancel-index": {"phase": "fault", "expected": "cancelled"}, "restart-cosy": {"phase": "recovery", "expected": "completed"}}
    passed = finalize_run(fixture, cases, required_case_ids=required)
    assert passed.status == "passed"

    duplicate = finalize_run(fixture, cases + [_case("steady-gpt-sovits-01", "gpt-sovits")], required_case_ids=required)
    assert duplicate.status == "failed"
    assert duplicate.duplicate_case_ids == ["steady-gpt-sovits-01"]

    failed = finalize_run(fixture, [_case("steady-gpt-01", "gpt-sovits"), _case("cancel-index", "indextts", phase="fault", expected="cancelled", actual="cancelled", cleanup_ok=False)], required_case_ids=required)
    assert failed.status == "failed"
    assert failed.missing_cases == ["restart-cosy"]
    assert failed.cleanup_failures == ["cancel-index"]


def test_atomic_evidence_preserves_prior_file_when_replace_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "summary.json"
    write_atomic_json(target, {"status": "passed"})
    original = target.read_bytes()

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        write_atomic_json(target, {"status": "failed"})
    assert target.read_bytes() == original
    assert list(tmp_path.glob(".summary.json.*.tmp")) == []
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "passed"}


def test_atomic_evidence_rolls_back_prior_file_when_directory_fsync_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "summary.json"
    write_atomic_json(target, {"status": "passed"})
    original = target.read_bytes()
    monkeypatch.setattr(reliability_validation, "_fsync_directory", lambda _directory: (_ for _ in ()).throw(OSError("fsync failed")))
    with pytest.raises(OSError, match="fsync failed"):
        write_atomic_json(target, {"status": "failed"})
    assert target.read_bytes() == original
    assert list(tmp_path.glob(".summary.json.*.tmp")) == []
    assert list(tmp_path.glob(".summary.json.*.bak")) == []


def test_fix_round_1_required_case_contract_rejects_steady_recovery_and_extra_steady_case() -> None:
    fixture = ReliabilityFixture.model_validate(_fixture_document())
    cases = [
        _case(f"steady-{engine}-{index:02d}", engine)
        for engine in ("gpt-sovits", "indextts", "cosyvoice")
        for index in range(1, 11)
    ]
    cases.append(_case("recover-cosy", "cosyvoice", phase="steady"))
    summary = finalize_run(
        fixture,
        cases,
        required_case_ids={"recover-cosy": {"phase": "recovery", "expected": "completed"}},
    )
    assert summary.status == "failed"
    assert "required case recover-cosy has wrong phase" in summary.validation_failures

    extra = finalize_run(
        fixture,
        cases + [_case("steady-gpt-extra", "gpt-sovits", expected="failed", actual="failed", audio=None)],
        required_case_ids={},
    )
    assert extra.status == "failed"
    assert "steady gpt-sovits count is 11, expected 10" in extra.validation_failures


def test_fix_round_1_raw_payload_safety_rejects_nested_private_values(tmp_path: Path) -> None:
    target = tmp_path / "summary.json"
    with pytest.raises(ValueError, match="unsafe evidence") as exc_info:
        write_atomic_json(target, {"nested": [r"C:\Users\private", {"authorization": "Bearer private-token"}]})
    assert "private-token" not in str(exc_info.value)
    assert target.exists() is False
