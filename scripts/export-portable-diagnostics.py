from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import uuid
import zipfile
from pathlib import Path
from typing import Mapping


SENSITIVE_KEY_PARTS = {
    "api_key",
    "apikey",
    "args",
    "audio",
    "authorization",
    "command",
    "credential",
    "device_id",
    "device_uuid",
    "environment",
    "hostname",
    "machine",
    "output_path",
    "password",
    "private",
    "proxy",
    "secret",
    "token",
    "user_name",
    "username",
}
MANIFEST_FIELDS = (
    "schema_version",
    "component",
    "package_id",
    "release_version",
    "package_profile",
    "build_id",
)
STATE_FIELDS = ("schema_version", "status", "selected_device", "error_code")
OPERATION_FIELDS = ("schema_version", "component", "action", "status", "exit_code", "error_code", "message", "events")
EVENT_FIELDS = ("seq", "phase", "percent", "error_code", "message")
WINDOWS_PATH = re.compile(
    r"(?i)(?:[a-z]:[\\/]|\\\\(?:\?\\)?)[^\r\n;,|]*"
)
AUTHORIZATION = re.compile(r"(?i)\b(?:authorization|bearer|token|api[_-]?key|secret|password)\b\s*[:=]?\s*\S+")
URL_WITH_AUTH = re.compile(r"(?i)\bhttps?://[^\s]+")
MACHINE_IDENTIFIER = re.compile(
    r"(?i)\b(?:DESKTOP|LAPTOP|WIN|GPU)-[A-Z0-9][A-Z0-9_-]*\b|"
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b|"
    r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b"
)
PRIVATE_FILENAME = re.compile(
    r'(?i)[^\\/:*?"<>|\r\n;]+\.(?:wav|mp3|flac|m4a|ogg|aac|bin|pt|pth|ckpt|safetensors|onnx)\b'
)


def redact(value: object, *, package_root: Path) -> object:
    """Recursively redact paths, credentials and identity-bearing fields."""
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.casefold().replace("-", "_")
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                continue
            sanitized[key] = redact(item, package_root=package_root)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [redact(item, package_root=package_root) for item in value]
    if isinstance(value, str):
        text = value
        root_variants = {str(package_root), str(package_root.absolute())}
        for variant in sorted(root_variants, key=len, reverse=True):
            if variant:
                text = re.sub(re.escape(variant), "<PACKAGE_ROOT>", text, flags=re.IGNORECASE)
                text = re.sub(
                    re.escape(variant.replace("\\", "/")),
                    "<PACKAGE_ROOT>",
                    text,
                    flags=re.IGNORECASE,
                )
        text = WINDOWS_PATH.sub("<REDACTED_PATH>", text)
        text = URL_WITH_AUTH.sub(_redact_url, text)
        text = AUTHORIZATION.sub("<REDACTED_SECRET>", text)
        text = MACHINE_IDENTIFIER.sub("<REDACTED_IDENTITY>", text)
        text = PRIVATE_FILENAME.sub("<REDACTED_FILE>", text)
        identity_values = {
            package_root.parent.name,
            os.environ.get("USERNAME", ""),
            os.environ.get("COMPUTERNAME", ""),
        }
        for identity in sorted(identity_values, key=len, reverse=True):
            if len(identity) >= 3:
                text = re.sub(re.escape(identity), "<REDACTED_IDENTITY>", text, flags=re.IGNORECASE)
        return text
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "<REDACTED>"


def build_diagnostic_report(
    *,
    package_root: Path,
    operation: Mapping[str, object] | None = None,
    probe: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build a deterministic whitelist-only support report."""
    root = package_root.absolute()
    manifest = _read_json_regular(root, root / "package" / "tts-more-package.json")
    state = _read_json_regular(root, root / "data" / "local" / "install-state.json")
    manifest_report = {field: manifest[field] for field in MANIFEST_FIELDS if field in manifest}
    state_report = {field: state[field] for field in STATE_FIELDS if field in state}
    operation_report = {
        field: operation[field]
        for field in OPERATION_FIELDS
        if field != "events" and operation is not None and field in operation
    }
    if operation is not None and isinstance(operation.get("events"), list):
        operation_report["events"] = [
            {field: event[field] for field in EVENT_FIELDS if field in event}
            for event in operation["events"][-50:]
            if isinstance(event, Mapping)
        ]
    locks: dict[str, str] = {}
    for label, section in (("runtime", "runtime"), ("models", "models")):
        descriptor = manifest.get(section)
        if not isinstance(descriptor, Mapping):
            continue
        relative = descriptor.get("lock")
        if not isinstance(relative, str):
            continue
        lock = _safe_relative_file(root, relative)
        if lock is not None:
            locks[label] = hashlib.sha256(lock.read_bytes()).hexdigest()
    report: dict[str, object] = {
        "schema_version": 1,
        "manifest": manifest_report,
        "lock_sha256": locks,
        "status": state_report,
        "operation": operation_report,
        "probe": dict(probe or {}),
    }
    return redact(report, package_root=root)  # type: ignore[return-value]


def export_diagnostic_zip(
    *,
    package_root: Path,
    output: Path,
    operation: Mapping[str, object] | None = None,
    probe: Mapping[str, object] | None = None,
) -> Path:
    """Atomically publish one deterministic report ZIP without overwriting files."""
    root = package_root.absolute()
    if not root.is_dir():
        raise FileNotFoundError(f"portable package root does not exist: {root}")
    diagnostics_root = root / "data" / "local" / "diagnostics"
    destination = output.absolute()
    if destination.suffix.casefold() != ".zip" or not _lexically_within(diagnostics_root, destination):
        raise ValueError("diagnostic ZIP output must be inside the package diagnostics directory")
    _assert_no_reparse_points(root, diagnostics_root.parent)
    diagnostics_root.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_points(root, destination.parent)
    if destination.exists():
        raise FileExistsError(f"diagnostic ZIP already exists: {destination}")

    report = build_diagnostic_report(package_root=root, operation=operation, probe=probe)
    payload = (json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.partial"
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
        ) as archive:
            info = zipfile.ZipInfo("diagnostics/report.json", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        _publish_no_replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _read_json_regular(root: Path, path: Path) -> dict[str, object]:
    if not _is_regular_contained_file(root, path):
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_relative_file(root: Path, relative: str) -> Path | None:
    normalized = relative.replace("\\", "/")
    path_value = Path(normalized)
    if path_value.is_absolute() or ":" in normalized or ".." in path_value.parts:
        return None
    candidate = root / path_value
    return candidate if _is_regular_contained_file(root, candidate) else None


def _is_regular_contained_file(root: Path, path: Path) -> bool:
    if not _lexically_within(root, path):
        return False
    try:
        _assert_no_reparse_points(root, path)
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _lexically_within(root: Path, path: Path) -> bool:
    try:
        return os.path.commonpath((str(root.absolute()), str(path.absolute()))) == str(root.absolute())
    except ValueError:
        return False


def _is_reparse_point(path: Path) -> bool:
    metadata = path.lstat()
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & reparse_flag)


def _assert_no_reparse_points(root: Path, path: Path) -> None:
    if not _lexically_within(root, path):
        raise ValueError("path is outside the portable package")
    current = root
    if _is_reparse_point(current):
        raise ValueError("portable package root is a symlink or reparse point")
    relative = path.absolute().relative_to(root.absolute())
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_reparse_point(current):
            raise ValueError("diagnostic path contains a symlink or reparse point")


def _redact_url(match: re.Match[str]) -> str:
    return "<REDACTED_URL>"


def _publish_no_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        import ctypes

        move_file = ctypes.windll.kernel32.MoveFileExW
        move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move_file.restype = ctypes.c_int
        if not move_file(str(source), str(destination), 0x8):  # MOVEFILE_WRITE_THROUGH, no replace
            error = ctypes.get_last_error()
            if destination.exists():
                raise FileExistsError(f"diagnostic ZIP already exists: {destination}")
            raise OSError(error, "could not publish diagnostic ZIP atomically", str(destination))
        return
    os.link(source, destination)


def _load_operation(root: Path, operation_id: str) -> dict[str, object]:
    try:
        normalized = str(uuid.UUID(operation_id))
    except ValueError as exc:
        raise ValueError("operation id must be a UUID") from exc
    if normalized.casefold() != operation_id.casefold():
        raise ValueError("operation id must use canonical UUID form")
    directory = root / "data" / "local" / "operations" / normalized
    operation = _read_json_regular(root, directory / "operation.json")
    events: list[dict[str, object]] = []
    events_path = directory / "events.jsonl"
    if _is_regular_contained_file(root, events_path):
        for line in events_path.read_text(encoding="utf-8-sig").splitlines()[-50:]:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    if events:
        operation["events"] = events
    return operation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a redacted TTS More portable diagnostic ZIP")
    parser.add_argument("--package-root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--operation-id", default="")
    args = parser.parse_args(argv)
    root = args.package_root.absolute()
    output = args.output or (root / "data" / "local" / "diagnostics" / "portable-diagnostics.zip")
    operation = _load_operation(root, args.operation_id) if args.operation_id else None
    exported = export_diagnostic_zip(package_root=root, output=output, operation=operation)
    print(exported)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
