from __future__ import annotations

import argparse
import hashlib
import json
import math
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
COMPONENTS = {"tts-more", "gpt-sovits", "indextts", "cosyvoice"}
OPERATION_PHASES = {
    "not_initialized",
    "checking",
    "downloading",
    "installing",
    "validating",
    "starting",
    "ready",
    "stopped",
    "repairable",
    "blocked",
}
PROBE_STATUSES = {"ok", "passed", "failed", "blocked", "ready", "unavailable", "unknown"}
PROBE_NAMES = {"python", "runtime", "imports", "torch", "onnx", "cuda", "models", "disk", "network", "endpoint"}
DEVICE_PROFILES = {"auto", "cpu", "cu126", "cu128"}
ALLOWED_ERROR_CODES = {
    "CANCELLED",
    "CUDA_PROBE_FAILED",
    "DISK_SPACE_INSUFFICIENT",
    "DOWNLOAD_NETWORK_INTERRUPTED",
    "OPERATION_ACTIVE",
    "PACKAGE_CORRUPT",
    "PACKAGE_NOT_WRITABLE",
    "PORT_IN_USE",
}
VERSION_VALUE = re.compile(r"^[0-9]+(?:\.[0-9]+){0,3}$")
RELEASE_VALUE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$")
BUILD_VALUE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,191}$")
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
    manifest_report = _project_manifest(manifest)
    state_report = _project_state(state)
    operation_report = _project_operation(operation or {})
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
        "probe": _project_probe(probe or {}),
    }
    return redact(report, package_root=root)  # type: ignore[return-value]


def _project_manifest(manifest: Mapping[str, object]) -> dict[str, object]:
    projected: dict[str, object] = {}
    schema_version = manifest.get("schema_version")
    if isinstance(schema_version, int) and not isinstance(schema_version, bool) and schema_version in {1, 2}:
        projected["schema_version"] = schema_version
    component = manifest.get("component")
    if isinstance(component, str) and component in COMPONENTS:
        projected["component"] = component
    package_id = manifest.get("package_id")
    if isinstance(package_id, str) and package_id in COMPONENTS:
        projected["package_id"] = package_id
    release_version = manifest.get("release_version")
    if isinstance(release_version, str) and RELEASE_VALUE.fullmatch(release_version):
        projected["release_version"] = release_version
    profile = manifest.get("package_profile")
    if isinstance(profile, str) and profile in {"bootstrap", "full"}:
        projected["package_profile"] = profile
    build_id = manifest.get("build_id")
    if isinstance(build_id, str) and BUILD_VALUE.fullmatch(build_id):
        projected["build_id"] = build_id
    return projected


def _project_state(state: Mapping[str, object]) -> dict[str, object]:
    projected: dict[str, object] = {}
    schema_version = state.get("schema_version")
    if isinstance(schema_version, int) and not isinstance(schema_version, bool) and 1 <= schema_version <= 2:
        projected["schema_version"] = schema_version
    status = state.get("status")
    if isinstance(status, str) and status in OPERATION_PHASES | PROBE_STATUSES:
        projected["status"] = status
    selected_device = state.get("selected_device")
    if isinstance(selected_device, str) and selected_device.casefold() in DEVICE_PROFILES:
        projected["selected_device"] = selected_device.casefold()
    error_code = _project_error_code(state.get("error_code"))
    if error_code is not None:
        projected["error_code"] = error_code
    return projected


def _project_operation(operation: Mapping[str, object]) -> dict[str, object]:
    projected: dict[str, object] = {}
    status = operation.get("status")
    if isinstance(status, str) and status in OPERATION_PHASES:
        projected["status"] = status
    exit_code = operation.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and 0 <= exit_code <= 65535:
        projected["exit_code"] = exit_code
    error_code = _project_error_code(operation.get("error_code"))
    if error_code is not None:
        projected["error_code"] = error_code
    events = operation.get("events")
    if isinstance(events, list):
        projected_events = [
            projected_event
            for event in events[-50:]
            if isinstance(event, Mapping)
            for projected_event in (_project_event(event),)
            if projected_event
        ]
        if projected_events:
            projected["events"] = projected_events
    return projected


def _project_event(event: Mapping[str, object]) -> dict[str, object]:
    projected: dict[str, object] = {}
    seq = event.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool) and 1 <= seq <= 2_147_483_647:
        projected["seq"] = seq
    phase = event.get("phase")
    if isinstance(phase, str) and phase in OPERATION_PHASES:
        projected["phase"] = phase
    percent = event.get("percent")
    if (
        isinstance(percent, (int, float))
        and not isinstance(percent, bool)
        and math.isfinite(float(percent))
        and 0 <= float(percent) <= 100
    ):
        projected["percent"] = percent
    error_code = _project_error_code(event.get("error_code"))
    if error_code is not None:
        projected["error_code"] = error_code
    return projected


def _project_probe(probe: Mapping[str, object]) -> dict[str, object]:
    projected: dict[str, object] = {}
    status = probe.get("status")
    if isinstance(status, str) and status in PROBE_STATUSES:
        projected["status"] = status
    error_code = _project_error_code(probe.get("error_code"))
    if error_code is not None:
        projected["error_code"] = error_code
    checks = probe.get("checks")
    if isinstance(checks, list):
        projected_checks = [
            projected_check
            for check in checks[:100]
            if isinstance(check, Mapping)
            for projected_check in (_project_probe_check(check),)
            if projected_check
        ]
        if projected_checks:
            projected["checks"] = projected_checks
    return projected


def _project_probe_check(check: Mapping[str, object]) -> dict[str, object]:
    projected: dict[str, object] = {}
    name = check.get("name")
    if isinstance(name, str) and name in PROBE_NAMES:
        projected["name"] = name
    status = check.get("status")
    if isinstance(status, str) and status in PROBE_STATUSES:
        projected["status"] = status
    passed = check.get("passed")
    if isinstance(passed, bool):
        projected["passed"] = passed
    version = check.get("version")
    if isinstance(version, str) and VERSION_VALUE.fullmatch(version):
        projected["version"] = version
    duration_ms = check.get("duration_ms")
    if (
        isinstance(duration_ms, (int, float))
        and not isinstance(duration_ms, bool)
        and math.isfinite(float(duration_ms))
        and 0 <= float(duration_ms) <= 600_000
    ):
        projected["duration_ms"] = duration_ms
    error_code = _project_error_code(check.get("error_code"))
    if error_code is not None:
        projected["error_code"] = error_code
    return projected if projected.get("name") else {}


def _project_error_code(value: object) -> str | None:
    return value if isinstance(value, str) and value in ALLOWED_ERROR_CODES else None


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
