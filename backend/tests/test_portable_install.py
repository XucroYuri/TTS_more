from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_installer():
    module_path = REPO_ROOT / "scripts" / "portable_install.py"
    assert module_path.is_file(), "portable installer core is missing"
    spec = importlib.util.spec_from_file_location("portable_install", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_lock() -> dict[str, object]:
    return {
        "schema_version": 1,
        "profiles": {
            "cu128": {"minimum_nvidia_driver": "570.0"},
            "cu126": {"minimum_nvidia_driver": "560.0"},
            "cpu": {},
        },
        "auto_order": ["cu128", "cu126", "cpu"],
    }


def test_select_device_profile_uses_cim_driver_without_nvidia_smi() -> None:
    installer = _load_installer()
    controllers = [{"name": "NVIDIA GeForce RTX 5090", "driver_version": "32.0.15.7652"}]

    selected = installer.select_device_profile(_runtime_lock(), "auto", controllers)

    assert selected == "cu128"
    assert installer.nvidia_marketing_driver("32.0.15.7652") == (576, 52)


def test_select_device_profile_falls_back_but_explicit_cuda_fails_closed() -> None:
    installer = _load_installer()
    controllers = [{"name": "NVIDIA GeForce", "driver_version": "32.0.15.6120"}]

    assert installer.select_device_profile(_runtime_lock(), "auto", controllers) == "cu126"
    with pytest.raises(RuntimeError, match="cu128 requires NVIDIA driver 570.0"):
        installer.select_device_profile(_runtime_lock(), "cu128", controllers)


def test_select_device_profile_uses_cpu_when_no_nvidia_controller_exists() -> None:
    installer = _load_installer()

    selected = installer.select_device_profile(
        _runtime_lock(), "auto", [{"name": "Microsoft Basic Display Adapter", "driver_version": "10.0"}]
    )

    assert selected == "cpu"


def test_ensure_locked_asset_resumes_partial_and_promotes_only_after_hash(tmp_path: Path) -> None:
    installer = _load_installer()
    payload = b"portable-runtime-payload"
    destination = tmp_path / "cache" / "runtime.zip"
    partial = destination.with_name("runtime.zip.partial")
    partial.parent.mkdir(parents=True)
    partial.write_bytes(payload[:9])
    calls: list[tuple[str, Path, int]] = []

    def download(url: str, target: Path, resume_from: int) -> None:
        calls.append((url, target, resume_from))
        with target.open("ab") as handle:
            handle.write(payload[resume_from:])

    report = installer.ensure_locked_asset(
        {
            "id": "runtime",
            "urls": ["https://example.invalid/runtime.zip"],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        },
        destination,
        downloader=download,
    )

    assert report == {"path": str(destination), "reused": False, "source": "https://example.invalid/runtime.zip"}
    assert calls == [("https://example.invalid/runtime.zip", partial, 9)]
    assert destination.read_bytes() == payload
    assert not partial.exists()


def test_ensure_locked_asset_keeps_corrupt_partial_and_never_promotes_it(tmp_path: Path) -> None:
    installer = _load_installer()
    destination = tmp_path / "model.bin"

    def download(_url: str, target: Path, _resume_from: int) -> None:
        target.write_bytes(b"corrupt")

    with pytest.raises(RuntimeError, match="failed SHA-256 verification"):
        installer.ensure_locked_asset(
            {
                "id": "model",
                "urls": ["https://example.invalid/model.bin"],
                "sha256": hashlib.sha256(b"expected").hexdigest(),
                "size_bytes": 8,
            },
            destination,
            downloader=download,
        )

    assert not destination.exists()
    assert destination.with_name("model.bin.partial").read_bytes() == b"corrupt"


def test_write_install_state_is_atomic_and_records_lock_digests(tmp_path: Path) -> None:
    installer = _load_installer()
    state = tmp_path / "data" / "local" / "install-state.json"

    installer.write_install_state(
        state,
        component="gpt-sovits",
        build_id="build-1",
        profile="cu128",
        runtime_lock_sha256="a" * 64,
        model_lock_sha256="b" * 64,
    )

    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["component"] == "gpt-sovits"
    assert payload["profile"] == "cu128"
    assert payload["runtime_lock_sha256"] == "a" * 64
    assert payload["model_lock_sha256"] == "b" * 64
    assert payload["ready"] is True
    assert not state.with_suffix(".json.tmp").exists()


def test_windows_powershell_utf8_bom_json_is_accepted(tmp_path: Path) -> None:
    installer = _load_installer()
    path = tmp_path / "asset.json"
    path.write_text(json.dumps({"id": "locked-asset"}), encoding="utf-8-sig")

    assert installer.load_json(path) == {"id": "locked-asset"}
