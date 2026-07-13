from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_portable_packages():
    module_path = REPO_ROOT / "scripts" / "portable_packages.py"
    assert module_path.is_file(), "portable package validator script is missing"
    spec = importlib.util.spec_from_file_location("portable_packages", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_gpt_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "component": "gpt-sovits-dev",
        "version": "0.1.0",
        "build_id": "test-build",
        "api_contract": "tts-more-v1",
        "default_endpoint": "http://127.0.0.1:9883",
        "port": 9883,
        "launcher": "Start.cmd",
        "health_path": "/health",
        "capabilities": ["tts", "artifact-transfer"],
        "model_profile": "full-quality-default",
        "runtime": "runtime/runtime.7z",
        "sha256_manifest": "SHA256SUMS.txt",
    }


def test_validate_manifest_rejects_absolute_paths_and_missing_worker_fields(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    manifest = tmp_path / "tts-more-package.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "component": "gpt-sovits-dev", "launcher": "C:/bad/Start.cmd"}),
        encoding="utf-8",
    )

    report = packages.validate_manifest(manifest, tmp_path)

    assert report["valid"] is False
    assert "launcher must be a relative path" in report["errors"]
    assert "default_endpoint is required" in report["errors"]


def test_validate_manifest_accepts_staged_gpt_worker_package(tmp_path: Path) -> None:
    packages = _load_portable_packages()
    (tmp_path / "Start.cmd").write_text("@echo off\r\n", encoding="utf-8")
    manifest = tmp_path / "package" / "tts-more-package.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps(_valid_gpt_manifest()), encoding="utf-8")

    report = packages.validate_manifest(manifest, tmp_path)

    assert report == {
        "valid": True,
        "errors": [],
        "component": "gpt-sovits-dev",
        "default_endpoint": "http://127.0.0.1:9883",
        "launcher": "Start.cmd",
    }
