from __future__ import annotations

import ctypes
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


class _WindowsUnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_wchar_p),
    ]


class _WindowsObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ulong),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_WindowsUnicodeString)),
        ("Attributes", ctypes.c_ulong),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _WindowsIoStatusBlock(ctypes.Structure):
    _fields_ = [
        ("Status", ctypes.c_void_p),
        ("Information", ctypes.c_size_t),
    ]


class _WindowsFileTime(ctypes.Structure):
    _fields_ = [
        ("LowDateTime", ctypes.c_ulong),
        ("HighDateTime", ctypes.c_ulong),
    ]


class _WindowsFileInformation(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", ctypes.c_ulong),
        ("CreationTime", _WindowsFileTime),
        ("LastAccessTime", _WindowsFileTime),
        ("LastWriteTime", _WindowsFileTime),
        ("VolumeSerialNumber", ctypes.c_ulong),
        ("FileSizeHigh", ctypes.c_ulong),
        ("FileSizeLow", ctypes.c_ulong),
        ("NumberOfLinks", ctypes.c_ulong),
        ("FileIndexHigh", ctypes.c_ulong),
        ("FileIndexLow", ctypes.c_ulong),
    ]


def _windows_handle_information(handle: int) -> _WindowsFileInformation:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_WindowsFileInformation),
    ]
    get_information.restype = ctypes.c_int
    information = _WindowsFileInformation()
    if not get_information(handle, ctypes.byref(information)):
        raise EvidenceStoreError("evidence handle identity is unavailable")
    return information


def _windows_close_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    close_handle(handle)


def _windows_open_root_directory(path: Path) -> int:
    before = _require_real_directory(path)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x80 | 0x00100000,
        0x1 | 0x2 | 0x4,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in {None, invalid_handle}:
        raise EvidenceStoreError("evidence root handle is unavailable")
    try:
        information = _windows_handle_information(handle)
        after = _require_real_directory(path)
        file_index = (information.FileIndexHigh << 32) | information.FileIndexLow
        if (
            information.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or not information.FileAttributes & 0x10
            or before.st_ino != file_index
            or after.st_ino != file_index
        ):
            raise EvidenceStoreError("evidence root handle is unsafe")
        return int(handle)
    except BaseException:
        _windows_close_handle(int(handle))
        raise


def _windows_relative_name(
    name: str,
) -> tuple[ctypes.Array[ctypes.c_wchar], _WindowsUnicodeString]:
    if not name or name in {".", ".."} or "\\" in name or "/" in name:
        raise EvidenceStoreError("evidence relative name is invalid")
    buffer = ctypes.create_unicode_buffer(name)
    byte_length = len(name.encode("utf-16-le"))
    unicode_name = _WindowsUnicodeString(
        Length=byte_length,
        MaximumLength=byte_length + 2,
        Buffer=ctypes.cast(buffer, ctypes.c_wchar_p),
    )
    return buffer, unicode_name


def _windows_nt_create(
    parent_handle: int,
    name: str,
    *,
    directory: bool,
    create_new: bool,
    create_if_missing: bool = False,
) -> int:
    _buffer, unicode_name = _windows_relative_name(name)
    object_attributes = _WindowsObjectAttributes(
        Length=ctypes.sizeof(_WindowsObjectAttributes),
        RootDirectory=parent_handle,
        ObjectName=ctypes.pointer(unicode_name),
        Attributes=0x40 | 0x1000,
        SecurityDescriptor=None,
        SecurityQualityOfService=None,
    )
    io_status = _WindowsIoStatusBlock()
    output_handle = ctypes.c_void_p()
    ntdll = ctypes.WinDLL("ntdll")
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.POINTER(_WindowsObjectAttributes),
        ctypes.POINTER(_WindowsIoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    nt_create_file.restype = ctypes.c_long
    desired_access = 0x80 | 0x00100000
    if directory:
        desired_access |= 0x1
    elif create_new:
        desired_access |= 0x2
    else:
        desired_access |= 0x1
    create_options = 0x20 | 0x00200000
    create_options |= 0x1 if directory else 0x40
    status = nt_create_file(
        ctypes.byref(output_handle),
        desired_access,
        ctypes.byref(object_attributes),
        ctypes.byref(io_status),
        None,
        0x80,
        0x1 | 0x2 | 0x4,
        3 if create_if_missing else (2 if create_new else 1),
        create_options,
        None,
        0,
    )
    unsigned_status = status & 0xFFFFFFFF
    if status < 0:
        if unsigned_status == 0xC0000035:
            raise FileExistsError(name)
        raise EvidenceStoreError("evidence relative open was rejected")
    handle = int(output_handle.value)
    try:
        information = _windows_handle_information(handle)
        if information.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise EvidenceStoreError(
                "evidence directory is unsafe" if directory else "evidence file is unsafe"
            )
        if directory != bool(information.FileAttributes & 0x10):
            raise EvidenceStoreError("evidence relative handle has the wrong type")
        return handle
    except BaseException:
        _windows_close_handle(handle)
        raise


def _windows_evidence_layout(path: Path) -> tuple[Path, tuple[str, ...]] | None:
    absolute = Path(path).absolute()
    parts = absolute.parts
    for index in range(len(parts) - 2):
        if parts[index] != "runs" or _ARTIFACT_NAME.fullmatch(parts[index + 1]) is None:
            continue
        if re.fullmatch(r"[0-9a-f]{64}", parts[index + 1]) is None:
            continue
        relative = tuple(parts[index:])
        if len(relative) not in {3, 4}:
            continue
        return Path(*parts[:index]), relative
    return None


def _windows_open_evidence_descriptor(path: Path, *, create_new: bool) -> int | None:
    if os.name != "nt":
        return None
    layout = _windows_evidence_layout(path)
    if layout is None:
        return None
    root, relative = layout
    root_handle = _windows_open_root_directory(root)
    directory_handles = [root_handle]
    file_handle: int | None = None
    try:
        parent = root_handle
        for component in relative[:-1]:
            parent = _windows_nt_create(
                parent,
                component,
                directory=True,
                create_new=False,
            )
            directory_handles.append(parent)
        file_handle = _windows_nt_create(
            parent,
            relative[-1],
            directory=False,
            create_new=create_new,
        )
        import msvcrt

        flags = (os.O_WRONLY if create_new else os.O_RDONLY) | getattr(
            os,
            "O_BINARY",
            0,
        )
        descriptor = msvcrt.open_osfhandle(file_handle, flags)
        file_handle = None
        return descriptor
    finally:
        if file_handle is not None:
            _windows_close_handle(file_handle)
        for handle in reversed(directory_handles):
            _windows_close_handle(handle)


def _windows_prepare_run_directories(
    output_root: Path,
    run_key: str,
    relative_name: str,
) -> bool:
    if os.name != "nt":
        return False
    root, _ = _validated_root(output_root)
    safe_key = _validated_run_key(run_key)
    components = ["runs", safe_key, *relative_name.split("/")[:-1]]
    root_handle = _windows_open_root_directory(root)
    handles = [root_handle]
    try:
        parent = root_handle
        for component in components:
            parent = _windows_nt_create(
                parent,
                component,
                directory=True,
                create_new=False,
                create_if_missing=True,
            )
            handles.append(parent)
        return True
    finally:
        for handle in reversed(handles):
            _windows_close_handle(handle)


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
        if os.name == "nt":
            _require_real_directory(target.parent)
        else:
            _ensure_real_directory(target.parent)
    else:
        _require_real_directory(target.parent)
    try:
        target.parent.resolve(strict=True).relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise EvidenceStoreError("artifact destination is unsafe") from exc
    return target


def _read_bounded_regular(path: Path, *, max_bytes: int) -> bytes:
    descriptor = _windows_open_evidence_descriptor(path, create_new=False)
    before: os.stat_result | None = None
    if descriptor is None:
        before = _require_real_regular_file(path)
        if before.st_size > max_bytes:
            raise EvidenceStoreError("evidence file exceeds its size limit")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise EvidenceStoreError("evidence file could not be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > max_bytes:
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
    identity_opened = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before is not None:
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        if identity_before != identity_opened:
            raise EvidenceStoreError("evidence file identity changed")
    if identity_opened != identity_after:
        raise EvidenceStoreError("evidence file identity changed")
    if len(payload) != opened.st_size or len(payload) > max_bytes:
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
        descriptor = _windows_open_evidence_descriptor(path, create_new=True)
        if descriptor is None:
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


@contextmanager
def _run_operation_lock(output_root: Path, run_key: str) -> Iterator[None]:
    root, resolved_root = _validated_root(output_root)
    safe_key = _validated_run_key(run_key)
    lock_identity = hashlib.sha256(
        (os.path.normcase(str(resolved_root)) + "\0" + safe_key).encode("utf-8")
    ).hexdigest()
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        create_mutex.restype = ctypes.c_void_p
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        wait_for_single_object.restype = ctypes.c_uint32
        release_mutex = kernel32.ReleaseMutex
        release_mutex.argtypes = [ctypes.c_void_p]
        release_mutex.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int

        handle = create_mutex(
            None,
            False,
            f"Local\\TTSMoreReliabilityRun-{lock_identity}",
        )
        if not handle:
            raise EvidenceStoreError("run operation lock is unavailable")
        acquired = False
        try:
            wait_result = wait_for_single_object(handle, 30_000)
            if wait_result not in {0x0, 0x80}:
                raise EvidenceStoreError("run operation lock timed out")
            acquired = True
            yield
        finally:
            if acquired:
                release_mutex(handle)
            close_handle(handle)
        return

    lock_path = root / f".run-{lock_identity}.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise EvidenceStoreError("run operation lock is unavailable") from exc
    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
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
    safe_key = _validated_run_key(run_key)
    with _run_operation_lock(output_root, safe_key):
        if not _windows_prepare_run_directories(
            output_root,
            safe_key,
            "supervisor.json",
        ):
            _run_root(output_root, safe_key, create=True)
        run, resolved_root = _run_root(output_root, safe_key, create=False)
        if os.path.lexists(run / "terminal.json"):
            raise EvidenceStoreError("run is already frozen")
        _windows_prepare_run_directories(output_root, safe_key, relative_name)
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


def read_artifact(
    output_root: Path,
    run_key: str,
    kind: str,
    *,
    name: str | None = None,
) -> bytes:
    """Read one exact immutable run member with the store's safety checks."""
    relative_name = _artifact_relative_name(kind, name)
    safe_key = _validated_run_key(run_key)
    run, resolved_root = _run_root(output_root, safe_key, create=False)
    target = _artifact_path(
        run,
        relative_name,
        resolved_root,
        create_parent=False,
    )
    return _read_bounded_regular(target, max_bytes=MAX_ARTIFACT_BYTES)


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
    payload = _canonical_json(validated)
    with _run_operation_lock(output_root, validated.run_key):
        run, resolved_root = _run_root(
            output_root,
            validated.run_key,
            create=False,
        )
        terminal_path = run / "terminal.json"
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
