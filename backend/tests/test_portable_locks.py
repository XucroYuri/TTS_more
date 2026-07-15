from __future__ import annotations

import ast
import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = ("gpt-sovits", "indextts", "cosyvoice")
PROFILES = ("cpu", "cu126", "cu128")
PROBE_FILES = ("component-source.json", "runtime.lock.json")
LOCAL_COMPONENT_MODULES = {
    "gpt-sovits": {"gpt_sovits"},
    "indextts": {"indextts"},
    "cosyvoice": {"cosyvoice"},
}


def _canonical_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _probe_import_roots(statement: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(statement, mode="exec")):
        if isinstance(node, ast.Import):
            roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.partition(".")[0])
    return roots


def _locked_distribution_names(contents: str) -> set[str]:
    return {
        _canonical_distribution_name(match.group(1))
        for match in re.finditer(r"(?m)^([A-Za-z0-9_.-]+)==", contents)
    }


def _accepted_distributions_for_module(module: str) -> set[str]:
    canonical_module = _canonical_distribution_name(module)
    if canonical_module == "onnxruntime":
        return {"onnxruntime", "onnxruntime-gpu"}
    return {canonical_module}


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
            assert re.search(r"(?i)(?:^|\s)[a-z]:\\", contents) is None, (
                f"build-machine absolute path in {lock_path}"
            )
            starts = list(re.finditer(r"(?m)^[A-Za-z0-9_.-]+==[^\s\\]+", contents))
            assert starts, f"empty dependency lock: {lock_path}"
            for index, start in enumerate(starts):
                end = starts[index + 1].start() if index + 1 < len(starts) else len(contents)
                assert "--hash=sha256:" in contents[start.start() : end], (
                    f"unhashed requirement in {lock_path}: {start.group(0)}"
                )


def test_each_component_only_excludes_its_own_local_probe_module() -> None:
    expected = {
        "gpt-sovits": {"gpt_sovits"},
        "indextts": {"indextts"},
        "cosyvoice": {"cosyvoice"},
    }
    assert LOCAL_COMPONENT_MODULES == expected

    all_component_modules = set().union(*expected.values())
    for component, local_modules in expected.items():
        external_modules = all_component_modules - LOCAL_COMPONENT_MODULES[component]
        assert external_modules == all_component_modules - local_modules


def test_import_probes_only_reference_locked_or_component_local_modules() -> None:
    missing: list[str] = []
    for component in COMPONENTS:
        component_root = REPO_ROOT / "integrations" / "components" / component
        probe_modules: dict[str, set[str]] = {}
        for probe_file in PROBE_FILES:
            payload = json.loads((component_root / probe_file).read_text(encoding="utf-8"))
            probe_modules[probe_file] = {
                module
                for module in _probe_import_roots(payload["import_probe"])
                if module.lower() not in LOCAL_COMPONENT_MODULES[component]
            }

        for profile in PROFILES:
            lock_path = component_root / f"requirements-{profile}.lock.txt"
            locked_distributions = _locked_distribution_names(
                lock_path.read_text(encoding="utf-8")
            )
            for probe_file, modules in probe_modules.items():
                for module in sorted(modules):
                    accepted = _accepted_distributions_for_module(module)
                    if accepted.isdisjoint(locked_distributions):
                        missing.append(
                            f"{component}/{probe_file}/{profile}: import {module!r} "
                            f"requires one of {sorted(accepted)!r}"
                        )

    assert not missing, "Import probes reference dependencies absent from their locks:\n" + "\n".join(
        missing
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
