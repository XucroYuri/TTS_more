from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = (
    "schema_version",
    "component",
    "version",
    "build_id",
    "api_contract",
    "default_endpoint",
    "port",
    "launcher",
    "health_path",
    "capabilities",
    "model_profile",
    "runtime",
    "sha256_manifest",
)


def validate_manifest(manifest_path: Path, package_root: Path) -> dict[str, object]:
    """Validate a portable component manifest without depending on its host path."""
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = [f"{field} is required" for field in REQUIRED_FIELDS if not payload.get(field)]
    launcher = str(payload.get("launcher") or "")
    if launcher and not _is_relative_package_path(launcher):
        errors.append("launcher must be a relative path")
    elif launcher and not (package_root / launcher).is_file():
        errors.append("launcher does not exist in package")
    if payload.get("api_contract") and payload.get("api_contract") != "tts-more-v1":
        errors.append("api_contract must be tts-more-v1")
    if payload.get("health_path") and not str(payload["health_path"]).startswith("/"):
        errors.append("health_path must start with /")
    return {
        "valid": not errors,
        "errors": errors,
        "component": payload.get("component", ""),
        "default_endpoint": payload.get("default_endpoint", ""),
        "launcher": launcher,
    }


def _is_relative_package_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    path = Path(normalized)
    return not path.is_absolute() and ":" not in normalized and ".." not in normalized.split("/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate TTS More portable component packages")
    subcommands = parser.add_subparsers(dest="command", required=True)
    validate = subcommands.add_parser("validate-manifest")
    validate.add_argument("--manifest", required=True, type=Path)
    validate.add_argument("--package-root", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "validate-manifest":
        report = validate_manifest(args.manifest, args.package_root)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["valid"] else 1
    raise AssertionError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
