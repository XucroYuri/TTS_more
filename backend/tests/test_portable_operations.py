from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest

from scripts import portable_operations
from scripts.portable_operations import append_event, create_operation, finish_operation, read_operation
from scripts.sync_integrations import sync_integration


REPO_ROOT = Path(__file__).resolve().parents[2]
OPERATION_ID = "11111111-1111-4111-8111-111111111111"
PYTHON_310_URL = "https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip"
PYTHON_310_SHA256 = "608619f8619075629c9c69f361352a0da6ed7e62f83a0e19c63e0ea32eb7629d"
PYTHON_310_SIZE = 8629277


APPEND_WORKER = r"""
import sys
import time
from pathlib import Path

from scripts.portable_operations import append_event

root = Path(sys.argv[1])
operation_id = sys.argv[2]
ready_path = Path(sys.argv[3])
gate_path = Path(sys.argv[4])
worker_id = int(sys.argv[5])
event_count = int(sys.argv[6])
ready_path.write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 30.0
while not gate_path.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("concurrent append gate was not released")
    time.sleep(0.005)
for event_index in range(event_count):
    append_event(root, operation_id, "checking", f"worker-{worker_id}-event-{event_index}")
"""


def test_operation_progress_runs_under_official_python_31011_embeddable(tmp_path: Path) -> None:
    archive = tmp_path / "python-3.10.11-embed-amd64.zip"
    with urllib.request.urlopen(PYTHON_310_URL, timeout=120) as response:
        archive.write_bytes(response.read())
    assert archive.stat().st_size == PYTHON_310_SIZE
    import hashlib

    assert hashlib.sha256(archive.read_bytes()).hexdigest() == PYTHON_310_SHA256
    runtime = tmp_path / "runtime"
    with zipfile.ZipFile(archive) as payload:
        payload.extractall(runtime)
    (runtime / "python310._pth").write_text(
        "python310.zip\n.\nLib/site-packages\nimport site\n", encoding="ascii"
    )
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "portable_operations.py").write_bytes(
        (REPO_ROOT / "scripts" / "portable_operations.py").read_bytes()
    )
    (bundle / "portable_launcher.py").write_bytes(
        (REPO_ROOT / "scripts" / "portable_launcher.py").read_bytes()
    )
    operation_root = tmp_path / "operations"
    code = """
import platform, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from portable_operations import append_event, create_operation, read_operation
import portable_launcher
root = Path(sys.argv[2])
operation_id = sys.argv[3]
create_operation(root, operation_id, 'cosyvoice', 'initialize', 'acceptance')
append_event(root, operation_id, 'downloading', 'cosy progress', percent=25)
_, events = read_operation(root, operation_id)
assert platform.python_version() == '3.10.11'
assert events[-1]['percent'] == 25.0
assert portable_launcher._normalize_process_creation_time('2026-07-17T00:00:00.1234567+00:00').tzinfo is not None
print(platform.python_version())
"""
    completed = subprocess.run(
        [str(runtime / "python.exe"), "-c", code, str(bundle), str(operation_root), OPERATION_ID],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "3.10.11"


LOCK_HOLDER = r"""
import sys
import time
from pathlib import Path

from scripts import portable_operations

directory = portable_operations._operation_dir(Path(sys.argv[1]), sys.argv[2])
with portable_operations._operation_lock(directory, timeout=5.0):
    Path(sys.argv[3]).write_text("ready", encoding="utf-8")
    time.sleep(30.0)
"""


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


def test_duplicate_operation_creation_preserves_existing_state_and_events(tmp_path: Path) -> None:
    create_operation(tmp_path, OPERATION_ID, "gpt-sovits", "start", "direct")
    append_event(tmp_path, OPERATION_ID, "checking", "正在检查电脑")
    directory = tmp_path / OPERATION_ID
    state_before = (directory / "operation.json").read_bytes()
    events_before = (directory / "events.jsonl").read_bytes()

    with pytest.raises(FileExistsError, match="operation already exists"):
        create_operation(tmp_path, OPERATION_ID, "cosyvoice", "repair", "workbench")

    assert (directory / "operation.json").read_bytes() == state_before
    assert (directory / "events.jsonl").read_bytes() == events_before


def test_concurrent_event_writers_produce_contiguous_decodable_jsonl(tmp_path: Path) -> None:
    create_operation(tmp_path, OPERATION_ID, "gpt-sovits", "start", "direct")
    ready_directory = tmp_path / "ready"
    ready_directory.mkdir()
    gate_path = tmp_path / "append.gate"
    worker_count = 8
    events_per_worker = 8
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                APPEND_WORKER,
                str(tmp_path),
                OPERATION_ID,
                str(ready_directory / str(worker_id)),
                str(gate_path),
                str(worker_id),
                str(events_per_worker),
            ],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for worker_id in range(worker_count)
    ]
    try:
        deadline = time.monotonic() + 30.0
        while len(list(ready_directory.iterdir())) != worker_count:
            exited = [process for process in processes if process.poll() is not None]
            if exited:
                _, stderr = exited[0].communicate()
                pytest.fail(f"concurrent append worker exited before the gate: {stderr}")
            if time.monotonic() >= deadline:
                pytest.fail("concurrent append workers did not become ready")
            time.sleep(0.01)
        gate_path.write_text("go", encoding="utf-8")
        for process in processes:
            _, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)

    events_path = tmp_path / OPERATION_ID / "events.jsonl"
    records = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    expected_count = worker_count * events_per_worker
    assert [record["seq"] for record in records] == list(range(1, expected_count + 1))
    _, events = read_operation(tmp_path, OPERATION_ID)
    assert events == records


def test_event_append_flushes_and_fsyncs_one_complete_utf8_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def track_fsync(descriptor: int) -> None:
        fsync_calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(portable_operations.os, "fsync", track_fsync)
    create_operation(tmp_path, OPERATION_ID, "gpt-sovits", "start", "direct")
    fsync_calls.clear()

    append_event(tmp_path, OPERATION_ID, "checking", "正在检查电脑")

    contents = (tmp_path / OPERATION_ID / "events.jsonl").read_bytes()
    assert fsync_calls
    assert contents.endswith(b"\n")
    assert len(contents.splitlines()) == 1
    assert json.loads(contents.decode("utf-8"))["message"] == "正在检查电脑"


def test_operation_lock_times_out_and_is_released_when_holder_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_operation(tmp_path, OPERATION_ID, "gpt-sovits", "start", "direct")
    ready_path = tmp_path / "lock.ready"
    holder = subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER, str(tmp_path), OPERATION_ID, str(ready_path)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15.0
        while not ready_path.exists():
            if holder.poll() is not None:
                _, stderr = holder.communicate()
                pytest.fail(f"operation lock holder exited before acquiring the lock: {stderr}")
            if time.monotonic() >= deadline:
                pytest.fail("operation lock holder did not become ready")
            time.sleep(0.01)
        monkeypatch.setattr(portable_operations, "LOCK_TIMEOUT_SECONDS", 0.1, raising=False)
        started_at = time.monotonic()
        with pytest.raises(TimeoutError, match="timed out acquiring operation lock"):
            append_event(tmp_path, OPERATION_ID, "checking", "等待锁")
        assert time.monotonic() - started_at < 2.0
    finally:
        if holder.poll() is None:
            holder.terminate()
        holder.wait(timeout=10)

    event = append_event(tmp_path, OPERATION_ID, "checking", "锁已释放")
    assert event["seq"] == 1


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


def test_read_operation_ignores_only_one_malformed_final_event_record(tmp_path: Path) -> None:
    create_operation(tmp_path, OPERATION_ID, "gpt-sovits", "start", "direct")
    append_event(tmp_path, OPERATION_ID, "checking", "正在检查电脑")
    events_path = tmp_path / OPERATION_ID / "events.jsonl"
    valid_record = events_path.read_bytes()
    events_path.write_bytes(valid_record + b'{"seq":2')

    _, events = read_operation(tmp_path, OPERATION_ID)

    assert [event["seq"] for event in events] == [1]
    valid_final = json.dumps({"seq": 3, "phase": "ready", "message": "ok"}).encode("utf-8") + b"\n"
    events_path.write_bytes(valid_record + b'{"seq":2\n' + valid_final)
    with pytest.raises(json.JSONDecodeError):
        read_operation(tmp_path, OPERATION_ID)


def test_operation_protocol_is_included_in_controlled_mirror(tmp_path: Path) -> None:
    target = tmp_path / "GPT fork"

    manifest = sync_integration(REPO_ROOT, target, "gpt-sovits", "a" * 40)

    relative = "tts_more/portable_operations.py"
    assert (target / relative).is_file()
    assert relative in manifest["files"]
