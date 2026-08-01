from __future__ import annotations

import json
import math
import os
import struct
import warnings
import wave
from datetime import datetime, timedelta, timezone
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


def _assert_scrubbed_atomic_error(error: BaseException, target: Path) -> None:
    message = str(error)
    assert "atomic evidence" in message
    assert "injected-sentinel" not in message
    assert str(target.parent) not in message


def _atomic_artifacts(target: Path, suffix: str) -> list[Path]:
    return sorted(target.parent.glob(f".{target.name}.*.{suffix}"))


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


REPOSITORY_LABELS = ("tts-more", "tts-audio-suite", "comfyui", "gpt-sovits", "indextts", "cosyvoice")
