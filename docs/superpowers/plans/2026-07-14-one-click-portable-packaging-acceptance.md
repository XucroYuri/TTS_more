# Portable Packaging and Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce auditable Bootstrap and local Full packages, support safe version migration, and prove the four-package system on clean Windows and real hardware.

**Architecture:** Package builders create a clean user-facing root with source under `app/`, immutable package metadata and separated user/local/cache data. A hash-aware importer copies user data and reuses only exact locked assets. CI audits Bootstrap artifacts, while a Windows acceptance harness simulates first-run failure modes before real CPU/CUDA and non-developer certification.

**Tech Stack:** PowerShell 5.1, Python 3.11, ZIP64, SHA-256, SPDX 2.3, GitHub Actions, pytest, local fixture HTTP server, Playwright for TTS More UI acceptance.

## Global Constraints

- Bootstrap ZIPs are GitHub-releaseable and contain no `runtime/live`, model weights, cache, user data, secrets or machine paths.
- Full ZIPs are local-only, include all required runtime and models, and start offline.
- TTS More Full is device-neutral; three worker Full filenames contain the resolved `cpu`, `cu126` or `cu128` profile.
- Full builds requested with `Auto` must resolve and record an actual profile before naming the ZIP.
- Upgrades use new directories; old packages remain available for rollback.
- Only exact SHA-256-matching assets are reused across versions.
- Formal delivery requires real synthesis, not only health endpoints.
- Any failed component blocks the four-package release train.

---

### Task 1: Stage the clean user-facing package layout

**Files:**
- Modify: `Build-Package.ps1`
- Modify: `integrations/windows/Build-Package.ps1`
- Modify: `scripts/portable_packages.py`
- Modify: `backend/tests/test_portable_packages.py`
- Create: `packaging/portable/使用说明-先看这里.txt`

**Interfaces:**
- Consumes: completed manifest and controller from Phase A.
- Produces: one top-level directory with root launchers, `app/`, `package/`, `runtime/`, `models/`, `data/` and `licenses/`.

- [ ] **Step 1: Write failing ZIP layout tests**

```python
def read_zip_json(zip_path: Path, relative_path: str) -> dict[str, object]:
    with ZipFile(zip_path) as archive:
        suffix = "/" + relative_path.lstrip("/")
        member = next(name for name in archive.namelist() if name.endswith(suffix))
        return json.loads(archive.read(member).decode("utf-8-sig"))

def build_fixture_zip(tmp_path: Path, profile: str) -> Path:
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(REPO_ROOT / "Build-Package.ps1"), "-Profile", profile, "-Device", "CPU", "-Version", "0.2.0-test", "-OutputRoot", str(tmp_path)],
        cwd=REPO_ROOT,
        check=True,
    )
    packages = list(tmp_path.glob("*.zip"))
    assert len(packages) == 1
    return packages[0]

def test_zip_has_clean_user_root_and_no_source_checkout_clutter(tmp_path: Path) -> None:
    zip_path = build_fixture_zip(tmp_path, profile="bootstrap")
    with ZipFile(zip_path) as archive:
        relative = {name.split("/", 1)[1] for name in archive.namelist() if "/" in name}
    assert {"Start.cmd", "Stop.cmd", "Repair.cmd", "Initialize.cmd", "使用说明-先看这里.txt"} <= relative
    assert any(name.startswith("app/") for name in relative)
    assert not any(name.startswith(".git/") or name.startswith("artifacts/") for name in relative)
    manifest = read_zip_json(zip_path, "package/tts-more-package.json")
    model_lock = read_zip_json(zip_path, manifest["models"]["lock"])
    assert all(asset["target"].startswith("app/") for asset in model_lock["assets"])
```

- [ ] **Step 2: Run package tests and confirm layout failure**

Run: `py -3.11 -m pytest backend/tests/test_portable_packages.py -q`

Expected: FAIL because worker builders currently copy the source checkout directly into the package root.

- [ ] **Step 3: Stage source under app and keep wrappers at root**

```powershell
$stageApp = Join-Path $stage "app"
New-Item -ItemType Directory -Force -Path $stageApp, (Join-Path $stage "data\user"), (Join-Path $stage "data\local"), (Join-Path $stage "data\cache"), (Join-Path $stage "licenses") | Out-Null
Copy-PortableTree -Source $Root -Destination $stageApp -ExcludedNames $excluded
foreach ($name in @("Start.cmd", "Stop.cmd", "Repair.cmd", "Initialize.cmd", "Build-Package.ps1", "Start-WebUI.cmd")) {
    Copy-Item -LiteralPath (Join-Path $Root $name) -Destination (Join-Path $stage $name) -Force
}
Copy-Item -LiteralPath (Join-Path $Bundle "使用说明-先看这里.txt") -Destination (Join-Path $stage "使用说明-先看这里.txt")
```

For worker packages, rewrite only the staged `component.json` to add `source_root: "app"`; rewrite the staged model lock’s `target` and `required_paths` values with an `app/` prefix. `Start-Worker.ps1`, `Initialize.ps1` and `Start-WebUI.cmd` resolve upstream source and model locations from this staged value. Keep source-checkout locks unchanged. Keep Bootstrap runtime/models absent from the archive audit; keep Full assets under the staged lock paths.

- [ ] **Step 4: Run package tests and build all four CPU Bootstrap ZIPs**

Run: `py -3.11 -m pytest backend/tests/test_portable_packages.py backend/tests/test_portable_discovery.py -q`

Run: `.\Build-Package.ps1 -Profile Bootstrap -Device CPU -Version 0.2.0-plancheck`

Expected: tests PASS; the TTS More Bootstrap ZIP has one clean top-level root and passes release audit. The three worker Bootstrap layouts are verified when Phase C builds each fork.

- [ ] **Step 5: Commit the package layout**

```powershell
git add Build-Package.ps1 integrations/windows/Build-Package.ps1 scripts/portable_packages.py backend/tests/test_portable_packages.py packaging/portable/使用说明-先看这里.txt
git commit -m "feat: stage portable packages for normal users"
```

### Task 2: Add safe previous-version import

**Files:**
- Create: `scripts/import-portable-data.py`
- Create: `backend/tests/test_portable_migration.py`
- Modify: `scripts/Invoke-PortableStart.ps1`
- Modify: `Build-Package.ps1`
- Modify: `frontend/src/components/LocalPortableServicesPanel.tsx`

**Interfaces:**
- Consumes: old and new manifests, locks and `data/user`.
- Produces: `plan_import(old_root, new_root) -> ImportPlan` and `apply_import(plan) -> ImportReport`; no deletion of old data.
- Test helper: `_write_version_pair(root: Path, matching_asset: bool) -> tuple[Path, Path]` creates old/new completed manifests, old `data/user/project.json`, old PID state, and model lock entries whose hash either matches or differs.

- [ ] **Step 1: Write failing migration safety tests**

```python
def test_import_copies_user_data_reuses_matching_assets_and_skips_pid_state(tmp_path: Path) -> None:
    old, new = _write_version_pair(tmp_path, matching_asset=True)
    report = apply_import(plan_import(old, new))
    assert (new / "data/user/project.json").read_text(encoding="utf-8") == "user-data"
    assert report.reused_assets == ["models/base.safetensors"]
    assert not (new / "data/local/run/worker.pid.json").exists()
    assert (old / "data/user/project.json").exists()
```

- [ ] **Step 2: Run migration tests and confirm import failure**

Run: `py -3.11 -m pytest backend/tests/test_portable_migration.py -q`

Expected: FAIL because the importer does not exist.

- [ ] **Step 3: Implement a copy-only, hash-aware importer**

```python
@dataclass(frozen=True)
class ImportPlan:
    old_root: Path
    new_root: Path
    user_files: tuple[Path, ...]
    reusable_assets: tuple[tuple[Path, Path], ...]

@dataclass(frozen=True)
class ImportReport:
    copied_user_files: int
    reused_assets: list[str]

def apply_import(plan: ImportPlan) -> ImportReport:
    for relative in plan.user_files:
        destination = plan.new_root / "data" / "user" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plan.old_root / "data" / "user" / relative, destination)
    for source, destination in plan.reusable_assets:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return ImportReport(copied_user_files=len(plan.user_files), reused_assets=[str(item[1].relative_to(plan.new_root)) for item in plan.reusable_assets])
```

Never import `data/local`, install state, PID records, operation records or virtual environments. Offer import only when the user selects an old package root and confirms the read-only plan summary.

- [ ] **Step 4: Run migration, package and frontend tests**

Run: `py -3.11 -m pytest backend/tests/test_portable_migration.py backend/tests/test_portable_packages.py -q`

Run: `pnpm --dir frontend test -- portableServices.test.ts`

Expected: PASS.

- [ ] **Step 5: Commit controlled migration**

```powershell
git add scripts/import-portable-data.py scripts/Invoke-PortableStart.ps1 Build-Package.ps1 backend/tests/test_portable_migration.py frontend/src/components/LocalPortableServicesPanel.tsx
git commit -m "feat: import portable user data safely"
```

### Task 3: Enforce profile-resolved names and release audits

**Files:**
- Modify: `Build-Package.ps1`
- Modify: `build-four-pack.ps1`
- Modify: `integrations/windows/Build-Package.ps1`
- Modify: `scripts/portable_packages.py`
- Modify: `backend/tests/test_portable_packages.py`
- Modify: `.github/workflows/portable-release.yml`
- Modify in each fork: `.github/workflows/portable-release.yml`

**Interfaces:**
- Consumes: Full install state `profile` and Bootstrap manifest.
- Produces: `full_package_name(component: str, version: str, resolved_profile: str) -> str`; device-neutral TTS More Full name; profile-suffixed worker Full names; GitHub fail-closed upload audit.

- [ ] **Step 1: Write failing filename and upload-audit tests**

```python
def test_full_worker_name_contains_resolved_profile_and_tts_more_does_not() -> None:
    assert full_package_name("tts-more", "0.2.0", "cpu") == "TTS-More-0.2.0-windows-x64-full.zip"
    assert full_package_name("gpt-sovits", "0.2.0", "cu128") == "gpt-sovits-0.2.0-windows-x64-full-cu128.zip"
    with pytest.raises(ValueError, match="resolved profile"):
        full_package_name("indextts", "0.2.0", "auto")
```

- [ ] **Step 2: Run package tests and confirm failure**

Run: `py -3.11 -m pytest backend/tests/test_portable_packages.py -q`

Expected: FAIL because current worker Full names do not contain the resolved profile.

- [ ] **Step 3: Resolve Auto before final naming and harden CI**

```powershell
if ($Profile -eq "Full" -and $config.component -ne "tts-more") {
    & (Join-Path $stage $initializeScript) -Device $Device
    $resolvedDevice = [string](Get-Content (Join-Path $stage "data\local\install-state.json") -Raw | ConvertFrom-Json).profile
    if ($resolvedDevice -notin @("cpu", "cu126", "cu128")) { throw "full worker package requires a resolved profile" }
    $packageName = "$($config.component)-$Version-windows-x64-full-$resolvedDevice"
}
```

Implement `full_package_name()` in `scripts/portable_packages.py` and call the same naming rule from both builders. TTS More always uses the device-neutral name and still validates that its Full package contains no worker runtime or model payload. In `build-four-pack.ps1`, pass `CPU` to TTS More and the requested device to each worker, then discover created ZIPs from their manifests rather than the old `*-full.zip` filename pattern. In every release workflow, enumerate candidate ZIPs, run `audit-release` on each, and fail if any manifest profile is `full`. Upload only `.zip`, `.sha256`, `.spdx.json`, `.licenses.json`, `.provenance.json` and `.acceptance.json` from the Bootstrap directory.

- [ ] **Step 4: Run package tests and local Full naming dry run**

Run: `py -3.11 -m pytest backend/tests/test_portable_packages.py -q`

Run: `.\build-four-pack.ps1 -Device CPU -Version 0.2.0-plancheck -PlanOnly`

Expected: tests PASS; the plan records the four components and the CPU request without building or uploading. Filename behavior is asserted by the focused tests.

- [ ] **Step 5: Commit release governance**

```powershell
git add Build-Package.ps1 build-four-pack.ps1 integrations/windows/Build-Package.ps1 scripts/portable_packages.py backend/tests/test_portable_packages.py .github/workflows/portable-release.yml
git commit -m "ci: enforce portable profile release boundaries"
```

Commit each fork workflow in its own repository with message `ci: enforce portable profile release boundaries`.

### Task 4: Build a deterministic clean-Windows simulation harness

**Files:**
- Create: `scripts/serve-portable-fixtures.py`
- Create: `scripts/test-portable-first-run.ps1`
- Create: `backend/tests/test_portable_first_run_harness.py`
- Modify: `.github/workflows/portable-release.yml`

**Interfaces:**
- Consumes: four Bootstrap ZIPs and miniature locked assets.
- Produces: sanitized JSON/JUnit evidence for interruption, resume, corruption, path and repeated-start behavior.

- [ ] **Step 1: Write failing fixture-server range tests**

```python
def test_fixture_server_supports_range_interrupt_and_resume(tmp_path: Path) -> None:
    server = PortableFixtureServer(tmp_path, interrupt_after=8)
    first = httpx.get(server.url("runtime.bin"), headers={"Range": "bytes=0-"})
    assert first.status_code == 503
    resumed = httpx.get(server.url("runtime.bin"), headers={"Range": "bytes=8-"})
    assert resumed.status_code == 206
    assert resumed.headers["Content-Range"].startswith("bytes 8-")
```

- [ ] **Step 2: Run harness tests and confirm missing-server failure**

Run: `py -3.11 -m pytest backend/tests/test_portable_first_run_harness.py -q`

Expected: FAIL because the fixture server and harness do not exist.

- [ ] **Step 3: Implement the local asset server and Windows orchestration**

```powershell
param([Parameter(Mandatory)][string[]]$Packages, [Parameter(Mandatory)][string]$Output)
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\WindowsPowerShell\v1.0"
foreach ($zip in $Packages) {
    $root = Expand-TestPackage -Zip $zip -Destination (New-RandomUnicodePath)
    Invoke-InterruptedStart -PackageRoot $root
    Assert-PartialAssetExists -PackageRoot $root
    Invoke-ResumedStart -PackageRoot $root
    Assert-ServiceReadyAndStopCleanly -PackageRoot $root
    Invoke-CorruptionRepair -PackageRoot $root
}
Write-SanitizedAcceptance -Output $Output
```

The fixture server must support `Range`, a configured first-request interruption, proxy failure and byte corruption. The harness removes Python/Conda/Node/Git from PATH, uses spaces and Chinese paths, repeats Start/Stop, and verifies no unexpected child process or listener remains.

- [ ] **Step 4: Run the harness tests and Windows CI simulation**

Run: `py -3.11 -m pytest backend/tests/test_portable_first_run_harness.py -q`

Run on Windows: `.\scripts\test-portable-first-run.ps1 -Packages (Get-ChildItem artifacts\portable\bootstrap\*.zip).FullName -Output artifacts\portable-acceptance`

Expected: four components pass interruption, resume, corruption repair, Unicode path, duplicate start and clean stop; evidence contains no machine identity.

- [ ] **Step 5: Commit the simulated acceptance gate**

```powershell
git add scripts/serve-portable-fixtures.py scripts/test-portable-first-run.ps1 backend/tests/test_portable_first_run_harness.py .github/workflows/portable-release.yml
git commit -m "test: simulate clean Windows portable first run"
```

### Task 5: Perform real hardware, offline Full and non-developer acceptance

**Files:**
- Create: `docs/portable-user-acceptance.md`
- Create: `docs/portable-acceptance-record.md`
- Modify: `repo.lock.json`
- Modify: `docs/deployment.md`
- Modify: `docs/release-governance.md`

**Interfaces:**
- Consumes: merged commits from four repositories and outputs from Tasks 1-4.
- Produces: final compatibility matrix, pinned commits and signed-off release record.

- [ ] **Step 1: Pin the four merged commits before building acceptance packages**

```powershell
$payload = Get-Content repo.lock.json -Raw | ConvertFrom-Json
$revisions = @{
  "GPT-SoVITS-main" = (git -C F:/Code/Github/.codex-worktrees/GPT-SoVITS-dual-portable-v2 rev-parse origin/main).Trim()
  "index-tts" = (git -C F:/Code/Github/.codex-worktrees/index-tts-dual-portable-v2 rev-parse origin/main).Trim()
  "CosyVoice" = (git -C F:/Code/Github/.codex-worktrees/CosyVoice-dual-portable-v2 rev-parse origin/main).Trim()
}
foreach ($repository in $payload.repositories) {
  if ($revisions.ContainsKey([string]$repository.name)) {
    $revision = [string]$revisions[[string]$repository.name]
    if ($revision -notmatch '^[0-9a-f]{40}$') { throw "invalid merged revision for $($repository.name)" }
    $repository.commit = $revision
  }
}
$payload | Add-Member -NotePropertyName release_train -NotePropertyValue "0.2.0" -Force
$payload | Add-Member -NotePropertyName controller_revision -NotePropertyValue ((git rev-parse HEAD).Trim()) -Force
$payload | ConvertTo-Json -Depth 8 | Set-Content repo.lock.json -Encoding UTF8
```

Run `py -3.11 scripts/tts_more_deploy.py --root . sync-repos --dry-run` after writing the lock and require zero revision drift.

- [ ] **Step 2: Build and verify CPU, CU126 and CU128 packages on matching hardware**

Run on the matching certified hosts:

```powershell
.\build-four-pack.ps1 -Device CPU -Version 0.2.0 -OutputRoot artifacts\portable\full-four\0.2.0-cpu
.\build-four-pack.ps1 -Device CU126 -Version 0.2.0 -OutputRoot artifacts\portable\full-four\0.2.0-cu126
.\build-four-pack.ps1 -Device CU128 -Version 0.2.0 -OutputRoot artifacts\portable\full-four\0.2.0-cu128
```

Expected: four Full ZIPs are produced per selected profile; each is moved to a random path and completes real model load, short synthesis, unload, restart and clean stop while offline. Unsupported profiles are absent from manifests.

- [ ] **Step 3: Verify the TTS More path workbench and trusted LAN boundary**

Run:

```powershell
$env:TTS_MORE_VALIDATION_SSH_USER = (Get-Content data/local/acceptance-ssh-user.txt -Raw).Trim()
$env:TTS_MORE_VALIDATION_REMOTE_ROOT = (Get-Content data/local/acceptance-remote-root.txt -Raw).Trim()
.\scripts\run-cuda-validation.ps1 `
  -Mode distributed `
  -Services data\local\services.json `
  -Fixture data\validation\cuda-fixture.local.json `
  -Topology deployment\app\topology.four-node-lan.local.json `
  -Output artifacts\portable-lan-acceptance
```

Expected: three independent worker identities, artifact delivery for LAN, `managed:false` for every external worker, fault recovery and no remote process-control endpoint.

- [ ] **Step 4: Run the two-person normal-user acceptance**

Provide only four ZIPs and `使用说明-先看这里.txt`. Each tester independently extracts, starts each needed component, configures paths in TTS More, synthesizes audio, views status/logs and stops services. Record every action and result in `docs/portable-acceptance-record.md`; no developer may remotely operate the test computer.

- [ ] **Step 5: Commit the convergence evidence after every gate passes**

```powershell
git add repo.lock.json docs/portable-user-acceptance.md docs/portable-acceptance-record.md docs/deployment.md docs/release-governance.md
git commit -m "docs: certify one-click portable release train"
```

Do not create the stable tag or mark the release train complete if any automated, real synthesis, offline Full, path-workbench, LAN or non-developer gate is incomplete.
