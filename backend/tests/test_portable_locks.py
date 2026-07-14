from __future__ import annotations

import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = ("gpt-sovits", "indextts", "cosyvoice")
PROFILES = ("cpu", "cu126", "cu128")


def test_model_locks_are_complete_immutable_and_hash_pinned() -> None:
    for component in COMPONENTS:
        lock_path = REPO_ROOT / "integrations" / "components" / component / "models.lock.json"
        payload = json.loads(lock_path.read_text(encoding="utf-8"))

        assert payload["complete"] is True, f"{component}: {payload['missing_required_paths']}"
        assert payload["mutable_revisions_allowed"] is False
        assert re.fullmatch(r"[0-9a-f]{64}", payload["snapshot_revision"])
        targets = {asset["target"] for asset in payload["assets"]}
        assert set(payload["required_paths"]) <= targets
        assert len(targets) == len(payload["assets"])
        for asset in payload["assets"]:
            assert re.fullmatch(r"[0-9a-f]{40}", asset["source_revision"])
            assert re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])
            assert asset["size_bytes"] > 0
            assert asset["urls"]
            for url in asset["urls"]:
                assert asset["source_revision"] in url
                assert "/main/" not in url and "/master/" not in url


def test_every_device_requirements_lock_is_exact_and_hash_pinned() -> None:
    for component in COMPONENTS:
        component_root = REPO_ROOT / "integrations" / "components" / component
        for profile in PROFILES:
            lock_path = component_root / f"requirements-{profile}.lock.txt"
            contents = lock_path.read_text(encoding="utf-8")
            starts = list(re.finditer(r"(?m)^[A-Za-z0-9_.-]+==[^\s\\]+", contents))
            assert starts, f"empty dependency lock: {lock_path}"
            for index, start in enumerate(starts):
                end = starts[index + 1].start() if index + 1 < len(starts) else len(contents)
                assert "--hash=sha256:" in contents[start.start() : end], (
                    f"unhashed requirement in {lock_path}: {start.group(0)}"
                )


def test_gpt_windows_contract_versions_are_frozen() -> None:
    expected = {
        "fastapi": "0.115.2",
        "starlette": "0.40.0",
        "gradio": "4.44.1",
        "pydantic": "2.10.6",
    }
    for profile in PROFILES:
        contents = (
            REPO_ROOT / "integrations" / "components" / "gpt-sovits" / f"requirements-{profile}.lock.txt"
        ).read_text(encoding="utf-8")
        for package, version in expected.items():
            assert re.search(rf"(?m)^{re.escape(package)}=={re.escape(version)}(?:\s|\\|$)", contents)
