from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts import portable_operations
from scripts.portable_operations import append_event, create_operation, finish_operation, read_operation
from scripts.sync_integrations import sync_integration


REPO_ROOT = Path(__file__).resolve().parents[2]
OPERATION_ID = "11111111-1111-4111-8111-111111111111"


def test_operation_events_are_ordered_and_finish_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def track_replace(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(portable_operations.os, "replace", track_replace)

    created = create_operation(tmp_path, OPERATION_ID, "gpt-sovits", "start", "direct")
    append_event(tmp_path, OPERATION_ID, "checking", "正在检查电脑")
    append_event(tmp_path, OPERATION_ID, "downloading", "正在下载模型", percent=25.0)
    finished = finish_operation(tmp_path, OPERATION_ID, "repairable", 20)

    operation, events = read_operation(tmp_path, OPERATION_ID)
    assert [event["seq"] for event in events] == [1, 2]
    assert operation["status"] == "repairable"
    assert operation["exit_code"] == 20
    assert created["started_at"] == operation["started_at"]
    assert finished == operation
    assert [destination.name for _, destination in replacements] == ["operation.json", "operation.json"]
    assert all(source.parent == destination.parent for source, destination in replacements)
    assert not list((tmp_path / OPERATION_ID).glob("*.tmp"))


def test_create_operation_records_initial_state(tmp_path: Path) -> None:
    operation = create_operation(tmp_path, OPERATION_ID, "cosyvoice", "repair", "workbench")

    assert operation["operation_id"] == OPERATION_ID
    assert operation["component"] == "cosyvoice"
    assert operation["action"] == "repair"
    assert operation["initiator"] == "workbench"
    assert operation["status"] == "not_initialized"
    assert operation["exit_code"] is None
    assert isinstance(operation["started_at"], str)


@pytest.mark.parametrize("operation_id", ["../escape", "not-a-uuid"])
def test_operation_id_must_be_a_valid_uuid(tmp_path: Path, operation_id: str) -> None:
    with pytest.raises(ValueError, match="UUID"):
        create_operation(tmp_path, operation_id, "gpt-sovits", "start", "direct")

    assert not (tmp_path.parent / "escape").exists()


def test_operation_id_is_normalized_to_canonical_uuid_text(tmp_path: Path) -> None:
    canonical_id = "abcdefab-cdef-4abc-8def-abcdefabcdef"
    uppercase_id = canonical_id.upper()

    operation = create_operation(tmp_path, uppercase_id, "gpt-sovits", "start", "direct")
    reread, events = read_operation(tmp_path, uppercase_id)

    assert operation["operation_id"] == canonical_id
    assert reread == operation
    assert events == []
    assert (tmp_path / canonical_id / "operation.json").is_file()


def test_operation_directory_cannot_escape_through_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations_root = tmp_path / "operations"
    outside = tmp_path / "outside"
    operations_root.mkdir()
    outside.mkdir()
    try:
        (operations_root / OPERATION_ID).symlink_to(outside, target_is_directory=True)
    except OSError:
        real_resolve = Path.resolve
        operation_path = operations_root / OPERATION_ID

        def resolve_with_escaped_operation(path: Path, strict: bool = False) -> Path:
            if path == operation_path:
                return outside
            return real_resolve(path, strict=strict)

        monkeypatch.setattr(Path, "resolve", resolve_with_escaped_operation)

    with pytest.raises(ValueError, match="escapes operations root"):
        create_operation(operations_root, OPERATION_ID, "gpt-sovits", "start", "direct")

    assert not (outside / "operation.json").exists()


def test_append_event_validates_phase_and_clamps_percent(tmp_path: Path) -> None:
    create_operation(tmp_path, OPERATION_ID, "indextts", "start", "direct")

    first = append_event(tmp_path, OPERATION_ID, "downloading", "等待下载", percent=-1, error_code="NETWORK")
    second = append_event(tmp_path, OPERATION_ID, "installing", "正在安装", percent=101)

    assert first["percent"] == 0.0
    assert first["error_code"] == "NETWORK"
    assert second["percent"] == 100.0
    with pytest.raises(ValueError, match="unsupported operation phase"):
        append_event(tmp_path, OPERATION_ID, "unknown", "未知状态")


def test_finish_operation_rejects_unsupported_status(tmp_path: Path) -> None:
    create_operation(tmp_path, OPERATION_ID, "gpt-sovits", "start", "direct")

    with pytest.raises(ValueError, match="unsupported operation status"):
        finish_operation(tmp_path, OPERATION_ID, "unknown", 1)


def test_operation_protocol_is_included_in_controlled_mirror(tmp_path: Path) -> None:
    target = tmp_path / "GPT fork"

    manifest = sync_integration(REPO_ROOT, target, "gpt-sovits", "a" * 40)

    relative = "tts_more/portable_operations.py"
    assert (target / relative).is_file()
    assert relative in manifest["files"]
