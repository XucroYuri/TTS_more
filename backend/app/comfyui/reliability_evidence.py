from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Iterator, Literal, NotRequired, TypedDict

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)


ABSENT_POINTER_TOKEN = "absent"
MAX_POINTER_BYTES = 4096
MAX_TERMINAL_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_RUN_MEMBERS = 512
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_ARTIFACT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_FIXED_ARTIFACT_NAMES = {
    "supervisor": "supervisor.json",
    "run-result": "run-result.json",
    "preflight": "preflight.json",
    "failure": "failure.json",
    "summary": "reliability-summary.json",
}
_VARIABLE_ARTIFACT_NAMES = {
    "case": ("cases", ".json"),
    "audio": ("audio", ".wav"),
    "log": ("logs", ".log"),
}
RunKey = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
SHA256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
RelativeArtifactName = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=(
            r"^(?:supervisor\.json|run-result\.json|preflight\.json|failure\.json|"
            r"reliability-summary\.json|cases/[a-z0-9][a-z0-9-]{0,63}\.json|"
            r"audio/[a-z0-9][a-z0-9-]{0,63}\.wav|"
            r"logs/[a-z0-9][a-z0-9-]{0,63}\.log)$"
        ),
    ),
]


class EvidenceStoreError(RuntimeError):
    """A public, sanitized immutable-evidence failure."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
        frozen=True,
        revalidate_instances="always",
        allow_inf_nan=False,
    )


class CurrentTerminalPointer(_StrictModel):
    schema_version: StrictInt
    kind: Literal["reliability-current-terminal"]
    run_key: RunKey
    mode: Literal["preflight", "matrix"]
    outcome: Literal["passed", "failed"]
    terminal_size_bytes: StrictInt = Field(gt=0, le=MAX_TERMINAL_BYTES)
    terminal_sha256: SHA256
    previous_pointer_sha256: SHA256 | None

    @field_validator("schema_version")
    @classmethod
    def _schema_version_one(cls, value: int) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported pointer schema")
        return value


class ArtifactCommitment(_StrictModel):
    relative_name: RelativeArtifactName
    size_bytes: StrictInt = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    sha256: SHA256


class RunTerminal(_StrictModel):
    schema_version: StrictInt
    kind: Literal["reliability-run-terminal"]
    run_key: RunKey
    mode: Literal["preflight", "matrix"]
    outcome: Literal["passed", "failed"]
    failure_source: Literal["none", "launcher", "validator", "cleanup"]
    evidence_complete: StrictBool
    launcher_exit_code: StrictInt = Field(ge=-(2**31), le=2**31 - 1)
    validator_exit_code: StrictInt | None = Field(
        default=None,
        ge=-(2**31),
        le=2**31 - 1,
    )
    cleanup_status: Literal["completed", "failed", "not-started"]
    preflight: ArtifactCommitment | None
    failure: ArtifactCommitment | None
    summary: ArtifactCommitment | None
    cases: tuple[ArtifactCommitment, ...]
    artifacts: tuple[ArtifactCommitment, ...]

    @field_validator("schema_version")
    @classmethod
    def _terminal_schema_version_one(cls, value: int) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported terminal schema")
        return value

    @field_validator("cases", "artifacts", mode="before")
    @classmethod
    def _immutable_commitment_sequence(cls, value: object) -> object:
        if type(value) is list:
            return tuple(value)
        return value

    @model_validator(mode="after")
    def _terminal_contract(self) -> "RunTerminal":
        if self.evidence_complete is not True:
            raise ValueError("terminal evidence is incomplete")

        if self.preflight is not None and self.preflight.relative_name != "preflight.json":
            raise ValueError("preflight commitment has the wrong role")
        if self.failure is not None and self.failure.relative_name != "failure.json":
            raise ValueError("failure commitment has the wrong role")
        if self.summary is not None and self.summary.relative_name != "reliability-summary.json":
            raise ValueError("summary commitment has the wrong role")
        if any(not item.relative_name.startswith("cases/") for item in self.cases):
            raise ValueError("case commitment has the wrong role")
        if any(
            item.relative_name
            not in {"supervisor.json", "run-result.json"}
            and not item.relative_name.startswith(("logs/", "audio/"))
            for item in self.artifacts
        ):
            raise ValueError("artifact commitment has the wrong role")

        case_names = [item.relative_name for item in self.cases]
        artifact_names = [item.relative_name for item in self.artifacts]
        if case_names != sorted(case_names) or len(case_names) != len(set(case_names)):
            raise ValueError("case commitments are not uniquely ordered")
        if artifact_names != sorted(artifact_names) or len(artifact_names) != len(
            set(artifact_names)
        ):
            raise ValueError("artifact commitments are not uniquely ordered")
        all_items = [
            item
            for item in (self.preflight, self.failure, self.summary)
            if item is not None
        ] + list(self.cases) + list(self.artifacts)
        all_names = [item.relative_name for item in all_items]
        if len(all_names) != len(set(all_names)):
            raise ValueError("terminal commitments are not unique")
        if not {"supervisor.json", "run-result.json"}.issubset(artifact_names):
            raise ValueError("terminal lifecycle commitments are incomplete")

        if self.outcome == "passed":
            if (
                self.failure_source != "none"
                or self.launcher_exit_code != 0
                or self.validator_exit_code != 0
                or self.cleanup_status != "completed"
                or self.failure is not None
            ):
                raise ValueError("passing terminal result is incoherent")
        else:
            if (
                self.failure_source == "none"
                or self.launcher_exit_code == 0
                or self.failure is None
            ):
                raise ValueError("failed terminal result is incoherent")
            if self.failure_source == "launcher" and self.validator_exit_code is not None:
                raise ValueError("launcher failure cannot have a validator exit")
            if self.failure_source == "validator" and (
                self.validator_exit_code is None or self.validator_exit_code == 0
            ):
                raise ValueError("validator failure requires a nonzero validator exit")
            if self.failure_source == "cleanup" and self.cleanup_status != "failed":
                raise ValueError("cleanup failure requires failed cleanup")

        if self.mode == "preflight":
            if self.summary is not None or self.cases:
                raise ValueError("preflight terminal contains matrix commitments")
            if self.outcome == "passed" and self.preflight is None:
                raise ValueError("passing preflight commitment is missing")
            if self.outcome == "failed" and self.preflight is not None:
                raise ValueError("failed preflight cannot commit a passing preflight")
        elif self.outcome == "passed":
            if self.preflight is None or self.summary is None or not self.cases:
                raise ValueError("passing matrix commitments are incomplete")

        return self


class TerminalCommitment(_StrictModel):
    size_bytes: StrictInt = Field(gt=0, le=MAX_TERMINAL_BYTES)
    sha256: SHA256


class RunVerification(_StrictModel):
    status: Literal["verified"]
    run_key: RunKey
    terminal_size_bytes: StrictInt = Field(gt=0, le=MAX_TERMINAL_BYTES)
    terminal_sha256: SHA256
    terminal: RunTerminal


class CurrentVerification(_StrictModel):
    status: Literal["current"]
    token: SHA256
    pointer: CurrentTerminalPointer
    run: RunVerification


class PointerSnapshot(TypedDict):
    status: Literal["absent", "valid"]
    token: str
    legacy_eligible: bool
    pointer: NotRequired[dict[str, object]]


def _is_reparse(stat_result: os.stat_result) -> bool:
    return stat.S_ISLNK(stat_result.st_mode) or bool(
        getattr(stat_result, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _require_real_directory(path: Path) -> os.stat_result:
    try:
        result = path.lstat()
    except OSError as exc:
        raise EvidenceStoreError("evidence directory is unavailable") from exc
    if _is_reparse(result) or not stat.S_ISDIR(result.st_mode):
        raise EvidenceStoreError("evidence directory is unsafe")
    return result


def _require_real_regular_file(path: Path) -> os.stat_result:
    try:
        result = path.lstat()
    except OSError as exc:
        raise EvidenceStoreError("evidence file is unavailable") from exc
    if _is_reparse(result) or not stat.S_ISREG(result.st_mode):
        raise EvidenceStoreError("evidence file is unsafe")
    return result


def _ensure_real_directory(path: Path) -> None:
    try:
        path.mkdir()
    except FileExistsError:
        pass
    except OSError as exc:
        raise EvidenceStoreError("evidence directory could not be created") from exc
    _require_real_directory(path)


def _validated_root(output_root: Path) -> tuple[Path, Path]:
    root = Path(output_root).absolute()
    _require_real_directory(root)
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise EvidenceStoreError("evidence output root is unavailable") from exc
    if resolved != root:
        raise EvidenceStoreError("evidence output root is unsafe")
    return root, resolved


def _validated_run_key(value: str) -> str:
    try:
        return TypeAdapter(RunKey).validate_python(value, strict=True)
    except ValidationError as exc:
        raise EvidenceStoreError("run key is invalid") from exc


def _run_root(output_root: Path, run_key: str, *, create: bool) -> tuple[Path, Path]:
    root, resolved_root = _validated_root(output_root)
    safe_key = _validated_run_key(run_key)
    runs = root / "runs"
    run = runs / safe_key
    if create:
        _ensure_real_directory(runs)
        _ensure_real_directory(run)
    else:
        _require_real_directory(runs)
        _require_real_directory(run)
    try:
        resolved_run = run.resolve(strict=True)
        resolved_run.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise EvidenceStoreError("run directory is unsafe") from exc
    return run, resolved_root


def _artifact_relative_name(kind: str, name: str | None) -> str:
    fixed = _FIXED_ARTIFACT_NAMES.get(kind)
    if fixed is not None:
        if name is not None:
            raise EvidenceStoreError("artifact name is invalid")
        return fixed
    variable = _VARIABLE_ARTIFACT_NAMES.get(kind)
    if variable is None or name is None or _ARTIFACT_NAME.fullmatch(name) is None:
        raise EvidenceStoreError("artifact name is invalid")
    directory, suffix = variable
    return f"{directory}/{name}{suffix}"


def _artifact_path(
    run_root: Path,
    relative_name: str,
    resolved_root: Path,
    *,
    create_parent: bool,
) -> Path:
    parts = relative_name.split("/")
    target = run_root.joinpath(*parts)
    if len(parts) == 2 and create_parent:
        _ensure_real_directory(target.parent)
    else:
        _require_real_directory(target.parent)
    try:
        target.parent.resolve(strict=True).relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise EvidenceStoreError("artifact destination is unsafe") from exc
    return target


def _read_bounded_regular(path: Path, *, max_bytes: int) -> bytes:
    before = _require_real_regular_file(path)
    if before.st_size > max_bytes:
        raise EvidenceStoreError("evidence file exceeds its size limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvidenceStoreError("evidence file could not be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size != before.st_size:
            raise EvidenceStoreError("evidence file identity changed")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    after = _require_real_regular_file(path)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_opened = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_opened or identity_before != identity_after:
        raise EvidenceStoreError("evidence file identity changed")
    if len(payload) != before.st_size or len(payload) > max_bytes:
        raise EvidenceStoreError("evidence file exceeds its size limit")
    return payload


def _first_write_bytes(path: Path, payload: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = _read_bounded_regular(path, max_bytes=MAX_ARTIFACT_BYTES)
        if existing != payload:
            raise EvidenceStoreError("artifact conflict")
        return
    except OSError as exc:
        raise EvidenceStoreError("artifact could not be created") from exc
    try:
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise EvidenceStoreError("artifact write was incomplete")
            written += count
        os.fsync(descriptor)
    except OSError as exc:
        raise EvidenceStoreError("artifact write failed") from exc
    finally:
        os.close(descriptor)


def write_artifact(
    output_root: Path,
    run_key: str,
    kind: str,
    payload: bytes,
    *,
    name: str | None = None,
) -> ArtifactCommitment:
    if type(payload) is not bytes or len(payload) > MAX_ARTIFACT_BYTES:
        raise EvidenceStoreError("artifact payload is invalid")
    relative_name = _artifact_relative_name(kind, name)
    run, resolved_root = _run_root(output_root, run_key, create=True)
    if os.path.lexists(run / "terminal.json"):
        raise EvidenceStoreError("run is already frozen")
    target = _artifact_path(
        run,
        relative_name,
        resolved_root,
        create_parent=True,
    )
    _first_write_bytes(target, payload)
    return ArtifactCommitment(
        relative_name=relative_name,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _canonical_json(model: BaseModel) -> bytes:
    return (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _all_commitments(terminal: RunTerminal) -> tuple[ArtifactCommitment, ...]:
    special = tuple(
        item
        for item in (terminal.preflight, terminal.failure, terminal.summary)
        if item is not None
    )
    return special + terminal.cases + terminal.artifacts


def _expected_membership(
    terminal: RunTerminal,
    *,
    include_terminal: bool,
) -> tuple[set[str], set[str]]:
    files = {item.relative_name for item in _all_commitments(terminal)}
    if include_terminal:
        files.add("terminal.json")
    directories: set[str] = set()
    for relative_name in files:
        parts = relative_name.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            directories.add("/".join(parts[:index]))
    return files, directories


def _scan_run_membership(run_root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    pending: list[tuple[Path, str]] = [(run_root, "")]
    members = 0
    while pending:
        current, prefix = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise EvidenceStoreError("run membership could not be read") from exc
        for entry in entries:
            members += 1
            if members > MAX_RUN_MEMBERS:
                raise EvidenceStoreError("run membership exceeds its limit")
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            try:
                result = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise EvidenceStoreError("run membership could not be read") from exc
            if _is_reparse(result):
                raise EvidenceStoreError("run membership contains a reparse member")
            if stat.S_ISDIR(result.st_mode):
                directories.add(relative)
                pending.append((Path(entry.path), relative))
            elif stat.S_ISREG(result.st_mode):
                files.add(relative)
            else:
                raise EvidenceStoreError("run membership contains a non-regular member")
    return files, directories


def _assert_exact_membership(
    run_root: Path,
    terminal: RunTerminal,
    *,
    include_terminal: bool,
) -> None:
    expected_files, expected_directories = _expected_membership(
        terminal,
        include_terminal=include_terminal,
    )
    actual_files, actual_directories = _scan_run_membership(run_root)
    if actual_files != expected_files or actual_directories != expected_directories:
        raise EvidenceStoreError("run membership mismatch")


def _verify_commitments(
    run_root: Path,
    resolved_root: Path,
    terminal: RunTerminal,
) -> None:
    for commitment in _all_commitments(terminal):
        target = _artifact_path(
            run_root,
            commitment.relative_name,
            resolved_root,
            create_parent=False,
        )
        payload = _read_bounded_regular(target, max_bytes=MAX_ARTIFACT_BYTES)
        if len(payload) != commitment.size_bytes or hashlib.sha256(payload).hexdigest() != (
            commitment.sha256
        ):
            raise EvidenceStoreError("artifact commitment mismatch")


def _first_write_terminal(path: Path, payload: bytes) -> None:
    if len(payload) > MAX_TERMINAL_BYTES:
        raise EvidenceStoreError("terminal exceeds its size limit")
    if os.path.lexists(path):
        existing = _read_bounded_regular(path, max_bytes=MAX_TERMINAL_BYTES)
        if existing != payload:
            raise EvidenceStoreError("terminal conflict")
        return
    _first_write_bytes(path, payload)


def write_terminal(
    output_root: Path,
    terminal: RunTerminal,
) -> TerminalCommitment:
    try:
        validated = RunTerminal.model_validate(
            terminal.model_dump(mode="json"),
            strict=True,
        )
    except ValidationError as exc:
        raise EvidenceStoreError("terminal is invalid") from exc
    run, resolved_root = _run_root(output_root, validated.run_key, create=False)
    terminal_path = run / "terminal.json"
    payload = _canonical_json(validated)
    if not os.path.lexists(terminal_path):
        _assert_exact_membership(run, validated, include_terminal=False)
        _verify_commitments(run, resolved_root, validated)
    _first_write_terminal(terminal_path, payload)
    verify_run(output_root, validated.run_key)
    return TerminalCommitment(
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def verify_run(output_root: Path, run_key: str) -> RunVerification:
    run, resolved_root = _run_root(output_root, run_key, create=False)
    terminal_path = run / "terminal.json"
    try:
        raw_terminal = _read_bounded_regular(
            terminal_path,
            max_bytes=MAX_TERMINAL_BYTES,
        )
        terminal = RunTerminal.model_validate_json(raw_terminal, strict=True)
        if raw_terminal != _canonical_json(terminal):
            raise EvidenceStoreError("terminal is invalid")
    except (EvidenceStoreError, ValidationError, ValueError, TypeError) as exc:
        raise EvidenceStoreError("terminal is invalid") from exc
    if terminal.run_key != run_key:
        raise EvidenceStoreError("terminal run key mismatch")
    _assert_exact_membership(run, terminal, include_terminal=True)
    _verify_commitments(run, resolved_root, terminal)
    return RunVerification(
        status="verified",
        run_key=run_key,
        terminal_size_bytes=len(raw_terminal),
        terminal_sha256=hashlib.sha256(raw_terminal).hexdigest(),
        terminal=terminal,
    )


def snapshot_current(output_root: Path) -> PointerSnapshot:
    root, _ = _validated_root(output_root)
    pointer_path = root / "current-terminal.json"
    if not os.path.lexists(pointer_path):
        return {
            "status": "absent",
            "token": ABSENT_POINTER_TOKEN,
            "legacy_eligible": True,
        }
    try:
        raw_pointer = _read_bounded_regular(pointer_path, max_bytes=MAX_POINTER_BYTES)
        pointer_model = CurrentTerminalPointer.model_validate_json(raw_pointer, strict=True)
        if raw_pointer != _canonical_json(pointer_model):
            raise EvidenceStoreError("current pointer is invalid")
    except (OSError, ValueError, TypeError, ValidationError) as exc:
        raise EvidenceStoreError("current pointer is invalid") from exc
    return {
        "status": "valid",
        "token": hashlib.sha256(raw_pointer).hexdigest(),
        "legacy_eligible": False,
        "pointer": pointer_model.model_dump(mode="json"),
    }


def load_terminal(path: Path) -> RunTerminal:
    try:
        payload = _read_bounded_regular(Path(path).absolute(), max_bytes=MAX_TERMINAL_BYTES)
        return RunTerminal.model_validate_json(payload, strict=True)
    except (EvidenceStoreError, ValidationError, ValueError, TypeError) as exc:
        raise EvidenceStoreError("terminal is invalid") from exc


def _validated_expected_token(value: str) -> str:
    if value == ABSENT_POINTER_TOKEN or re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    raise EvidenceStoreError("pointer token is invalid")


@contextmanager
def _current_pointer_lock(output_root: Path) -> Iterator[None]:
    root, _ = _validated_root(output_root)
    lock_path = root / ".current-terminal.lock"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise EvidenceStoreError("current pointer lock is unavailable") from exc
    locked = False
    try:
        opened = os.fstat(descriptor)
        on_disk = _require_real_regular_file(lock_path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (on_disk.st_dev, on_disk.st_ino)
        ):
            raise EvidenceStoreError("current pointer lock is unsafe")
        if opened.st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)

        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + 30.0
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise EvidenceStoreError("current pointer lock timed out") from exc
                    time.sleep(0.01)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True

        after_lock = _require_real_regular_file(lock_path)
        if (opened.st_dev, opened.st_ino) != (after_lock.st_dev, after_lock.st_ino):
            raise EvidenceStoreError("current pointer lock is unsafe")
        yield
    except OSError as exc:
        raise EvidenceStoreError("current pointer lock failed") from exc
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


def _atomic_replace_pointer(path: Path, payload: bytes) -> None:
    if len(payload) > MAX_POINTER_BYTES:
        raise EvidenceStoreError("current pointer exceeds its size limit")
    temporary = path.parent / (
        f".current-terminal.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(temporary, flags, 0o600)
        try:
            view = memoryview(payload)
            written = 0
            while written < len(view):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise EvidenceStoreError("current pointer write was incomplete")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        if os.name == "nt":
            import ctypes

            move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
            move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            move_file_ex.restype = ctypes.c_int
            replace_existing = 0x1
            write_through = 0x8
            if not move_file_ex(
                str(temporary),
                str(path),
                replace_existing | write_through,
            ):
                raise OSError(ctypes.get_last_error(), "MoveFileExW failed")
        else:
            os.replace(temporary, path)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except (OSError, EvidenceStoreError) as exc:
        try:
            if os.path.lexists(temporary):
                os.unlink(temporary)
        except OSError:
            pass
        if isinstance(exc, EvidenceStoreError):
            raise
        raise EvidenceStoreError("current pointer could not be replaced") from exc


def compare_and_swap_current(
    output_root: Path,
    run_key: str,
    *,
    expected_token: str,
) -> CurrentTerminalPointer:
    safe_expected = _validated_expected_token(expected_token)
    safe_run_key = _validated_run_key(run_key)
    root, _ = _validated_root(output_root)
    with _current_pointer_lock(root):
        before = snapshot_current(root)
        if before["token"] != safe_expected:
            raise EvidenceStoreError("pointer compare-and-swap conflict")
        run = verify_run(root, safe_run_key)
        pointer = CurrentTerminalPointer(
            schema_version=1,
            kind="reliability-current-terminal",
            run_key=run.run_key,
            mode=run.terminal.mode,
            outcome=run.terminal.outcome,
            terminal_size_bytes=run.terminal_size_bytes,
            terminal_sha256=run.terminal_sha256,
            previous_pointer_sha256=(
                None if before["status"] == "absent" else before["token"]
            ),
        )
        _atomic_replace_pointer(root / "current-terminal.json", _canonical_json(pointer))
        published = snapshot_current(root)
        if published["token"] != pointer_token(pointer):
            raise EvidenceStoreError("current pointer publication failed")
        return pointer


def pointer_token(pointer: CurrentTerminalPointer) -> str:
    try:
        validated = CurrentTerminalPointer.model_validate(
            pointer.model_dump(mode="json"),
            strict=True,
        )
    except (AttributeError, ValidationError, ValueError, TypeError) as exc:
        raise EvidenceStoreError("current pointer is invalid") from exc
    return hashlib.sha256(_canonical_json(validated)).hexdigest()


def verify_current(output_root: Path) -> CurrentVerification | PointerSnapshot:
    snapshot = snapshot_current(output_root)
    if snapshot["status"] == "absent":
        return snapshot
    try:
        pointer = CurrentTerminalPointer.model_validate(snapshot["pointer"], strict=True)
    except (ValidationError, ValueError, TypeError, KeyError) as exc:
        raise EvidenceStoreError("current pointer is invalid") from exc
    run = verify_run(output_root, pointer.run_key)
    if (
        pointer.mode != run.terminal.mode
        or pointer.outcome != run.terminal.outcome
        or pointer.terminal_size_bytes != run.terminal_size_bytes
        or pointer.terminal_sha256 != run.terminal_sha256
    ):
        raise EvidenceStoreError("pointer terminal mismatch")
    return CurrentVerification(
        status="current",
        token=snapshot["token"],
        pointer=pointer,
        run=run,
    )
