from __future__ import annotations

import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "tts_more"


class PortableIntegrationContractTests(unittest.TestCase):
    def test_controlled_mirror_has_no_hash_drift(self) -> None:
        manifest = json.loads((BUNDLE / "integration.manifest.json").read_text(encoding="utf-8"))
        expected = manifest["files"]
        for relative, digest in expected.items():
            path = ROOT / relative
            self.assertTrue(path.is_file(), relative)
            canonical = path.read_bytes().replace(b"\r\n", b"\n")
            self.assertEqual(hashlib.sha256(canonical).hexdigest(), digest, relative)
        controlled = {
            path.relative_to(ROOT).as_posix()
            for path in BUNDLE.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.name != "integration.manifest.json"
        }
        self.assertEqual(controlled, {name for name in expected if name.startswith("tts_more/")})
        tracked = set(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(ROOT),
                    "-c",
                    "core.quotePath=false",
                    "ls-files",
                    "--",
                    *sorted(expected),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
        )
        self.assertEqual(set(expected), tracked, "controlled integration files must be Git tracked")

    def test_package_entrypoints_and_native_webui_are_separate(self) -> None:
        for name in (
            "Initialize.cmd",
            "Start.cmd",
            "Stop.cmd",
            "Repair.cmd",
            "Build-Package.ps1",
            "Start-WebUI.cmd",
            "使用说明-先看这里.txt",
        ):
            self.assertTrue((ROOT / name).is_file(), name)
        start = (ROOT / "Start.cmd").read_text(encoding="utf-8")
        webui = (ROOT / "Start-WebUI.cmd").read_text(encoding="utf-8")
        self.assertIn("tts_more\\Invoke-PortableStart.ps1", start)
        self.assertNotIn("Start-Worker.ps1", start)
        self.assertNotEqual(start, webui)

    def test_controller_uses_manifest_operations_worker_delegate_and_private_runtime(self) -> None:
        controller = (BUNDLE / "Invoke-PortableStart.ps1").read_text(encoding="utf-8")
        worker = (BUNDLE / "Start-Worker.ps1").read_text(encoding="utf-8")

        self.assertIn('"package\\tts-more-package.json"', controller)
        self.assertIn("[int]$manifest.schema_version -eq 2", controller)
        self.assertIn("([string]$manifest.data.operations) -Label \"data.operations\"", controller)
        self.assertIn('Join-Path $bundle "Start-Worker.ps1"', controller)
        self.assertIn("Invoke-ChildPowerShell -Script $context.ServiceScript", controller)
        self.assertIn('$Python = Join-Path $Root "runtime\\live\\python.exe"', worker)
        self.assertNotIn(".venv", worker)
        self.assertNotIn("TTS_MORE_PYTHON_EXE", worker)

    def test_operation_protocol_is_controlled(self) -> None:
        self.assertTrue((BUNDLE / "portable_operations.py").is_file(), "portable operation protocol")

    def test_model_and_device_locks_are_complete_and_immutable(self) -> None:
        model_lock = json.loads((BUNDLE / "locks" / "models.lock.json").read_text(encoding="utf-8"))
        self.assertTrue(model_lock["complete"], model_lock["missing_required_paths"])
        targets = {asset["target"] for asset in model_lock["assets"]}
        self.assertTrue(set(model_lock["required_paths"]) <= targets)
        for asset in model_lock["assets"]:
            self.assertRegex(asset["source_revision"], r"^[0-9a-f]{40}$")
            self.assertRegex(asset["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(asset["size_bytes"], 0)
            self.assertTrue(all(asset["source_revision"] in url for url in asset["urls"]))
        for profile in ("cpu", "cu126", "cu128"):
            contents = (BUNDLE / "locks" / f"requirements-{profile}.lock.txt").read_text(encoding="utf-8")
            starts = list(re.finditer(r"(?m)^[A-Za-z0-9_.-]+==[^\s\\]+", contents))
            self.assertTrue(starts, profile)
            for index, start in enumerate(starts):
                end = starts[index + 1].start() if index + 1 < len(starts) else len(contents)
                self.assertIn("--hash=sha256:", contents[start.start():end], start.group(0))

    def test_full_release_is_fail_closed_in_github_actions(self) -> None:
        builder = (BUNDLE / "Build-Package.ps1").read_text(encoding="utf-8")
        self.assertIn('$env:GITHUB_ACTIONS -eq "true"', builder)
        self.assertIn("audit-release --zip", builder)


if __name__ == "__main__":
    unittest.main()
