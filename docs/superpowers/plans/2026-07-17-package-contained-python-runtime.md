# Package-Contained Python Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace portable-package Miniforge initialization with locked CPython embeddable ZIP plus a locked uv executable, then build and verify four relocatable Windows Full packages.

**Architecture:** A shared PowerShell helper downloads and validates Python/uv through the existing transactional asset downloader, safely extracts a relocatable runtime, and returns `python.exe`, `uv.exe`, and `Lib/site-packages`. Controller and worker initializers retain their existing device, dependency, model, probe, and atomic-live logic but install dependencies with uv directly into the embedded runtime. Conda remains a source-build adapter only.

**Tech Stack:** Windows PowerShell 5.1, .NET `System.IO.Compression`, CPython 3.11.9/3.10.11 embeddable ZIPs, uv 0.11.28, pytest, existing `portable_install.py`, four Git worktrees.

## Global Constraints

- No portable `Initialize.cmd`, `Start.cmd`, `Repair.cmd`, or Full build staging initialization may call `bootstrap-conda.ps1`, `conda create`, system Python, system uv, pip, tar, or 7-Zip.
- Python and uv assets must be immutable URL + exact byte size + SHA-256 entries in runtime lock files.
- TTS More, GPT-SoVITS and IndexTTS use CPython 3.11.9; CosyVoice uses CPython 3.10.11.
- Runtime staging must contain no `pyvenv.cfg`, external `base_prefix`, Miniforge installation, Conda package cache, machine path, or reparse point.
- Existing `.partial`, `Range`, mirror fallback, cancellation, operation ownership, model locks, device locks, probes, atomic live switch, and install-state semantics remain unchanged.
- uv dependency installation uses `--link-mode copy`; no runtime file may depend on a content-addressed cache hardlink.
- Full packages are local-only and must never enter a GitHub upload path.
- No package is called deliverable until four real ZIPs pass random-path offline Start/Stop/Repair and the three workers pass real short-audio synthesis.

---

### Task 1: Lock and Safely Extract the Embedded Runtime

**Files:**
- Create: `scripts/portable-python.ps1`
- Create: `integrations/windows/portable-python.ps1`
- Create: `backend/tests/test_portable_python_runtime.py`
- Modify: `backend/tests/test_prepare_scripts.py`
- Modify: `scripts/sync_integrations.py`
- Modify: `packaging/portable/runtime.lock.json`
- Modify: `integrations/components/gpt-sovits/component-source.json`
- Modify: `integrations/components/indextts/component-source.json`
- Modify: `integrations/components/cosyvoice/component-source.json`
- Modify: `integrations/components/gpt-sovits/runtime.lock.json`
- Modify: `integrations/components/indextts/runtime.lock.json`
- Modify: `integrations/components/cosyvoice/runtime.lock.json`

**Interfaces:**
- Produces: `Install-PortablePythonRuntime -PackageRoot -RuntimeLock -Destination [-OperationRoot] [-CancelFile]` returning one object with `Python`, `Uv`, and `SitePackages` absolute paths.
- Consumes: a PowerShell bootstrap downloader for the Python ZIP, then existing `portable_install.py ensure-asset` for uv after staging Python is runnable.

- [ ] **Step 1: Write failing lock and script contract tests**

Add tests that require exact Python assets and reject the old initialization dependency:

```python
PY311 = {
    "id": "cpython-3.11.9-embed-amd64",
    "sha256": "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b",
    "size_bytes": 11249023,
}
PY310 = {
    "id": "cpython-3.10.11-embed-amd64",
    "sha256": "608619f8619075629c9c69f361352a0da6ed7e62f83a0e19c63e0ea32eb7629d",
    "size_bytes": 8629277,
}

def test_runtime_locks_pin_embeddable_python_and_uv() -> None:
    controller = json.loads((REPO_ROOT / "packaging/portable/runtime.lock.json").read_text())
    assert controller["assets"]["python"] | PY311 == controller["assets"]["python"]
    for component, expected in (("gpt-sovits", PY311), ("indextts", PY311), ("cosyvoice", PY310)):
        lock = json.loads((REPO_ROOT / f"integrations/components/{component}/runtime.lock.json").read_text())
        assert lock["assets"]["python"] == expected | {"urls": lock["assets"]["python"]["urls"], "archive_entry": lock["assets"]["python"]["archive_entry"]}
        assert lock["assets"]["uv"]["sha256"] == "f4fcf2c8d9f1444b900e6b8dbbb828825fb76eca01acd18aeaa5c90240408cda"

def test_portable_python_helper_is_zip_safe_and_has_no_conda_dependency() -> None:
    text = (REPO_ROOT / "scripts/portable-python.ps1").read_text(encoding="utf-8")
    assert "Install-PortablePythonRuntime" in text
    assert "System.IO.Compression" in text
    assert "uv-0.11.28.data/scripts/uv.exe" in text
    assert "Lib\\site-packages" in text
    assert "import site" in text
    assert "pyvenv.cfg" in text
    assert "bootstrap-conda" not in text
    assert "conda create" not in text
```

Use exact assertions for URL, size, SHA-256, Python archive entry, uv wheel entry and absence of mutable-version fields. Add synthetic ZIP tests parameterized over both helper copies that prove absolute paths, `..`, duplicate normalized targets, reparse destinations, missing/duplicate uv entries and unexpected `_pth` layouts fail before publication. Require exactly one `python311._pth`/`python310._pth` and exactly four effective entries.

- [ ] **Step 2: Run RED**

Run:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_portable_python_runtime.py backend/tests/test_prepare_scripts.py -q -k "portable_python or embeddable"
```

Expected: FAIL because the helper and `assets.python` entries do not exist.

- [ ] **Step 3: Implement the minimal helper and locks**

Use .NET `HttpClient`/file streams for the first Python download and prove interrupted eight-byte transfer resumes with `Range: bytes=8-`; prove the first mirror returning 503 falls back to the second identical-hash asset. Only after staging Python passes the exact patch-version probe may the helper use `portable_install.py ensure-asset` for uv. Use .NET `ZipArchive` and normalized destination checks. The helper must configure the single expected `_pth` file as:

```text
python311.zip   # or python310.zip
.
Lib\site-packages
import site
```

Extract only the exact uv entry declared in the lock (default `uv-0.11.28.data/scripts/uv.exe`). Download locks are materialized beneath `data/cache/portable/locks`; archives remain beneath `data/cache/portable/assets`. Reject an existing destination and publish only a completely validated temporary extraction directory. Update `python_version` and component sources to exact patch versions and teach `scripts/sync_integrations.py` to generate the same values.

- [ ] **Step 4: Run GREEN and real relocation smoke**

Run the focused pytest command, PowerShell 5.1 AST parsing for both helpers, then a local smoke using the two official ZIPs and uv wheel. The smoke installs `packaging==24.2` with:

```powershell
& $uv pip install --python $python --target $sitePackages --link-mode copy "packaging==24.2"
& $uv pip check --python $python
```

Copy the runtime to a different path containing Chinese characters and spaces; both `sys.prefix` and `sys.base_prefix` must resolve to the copied directory, `importlib.metadata.version("packaging")`, `import packaging`, and `uv pip check --python` must succeed.

- [ ] **Step 5: Commit**

```powershell
git add scripts/portable-python.ps1 integrations/windows/portable-python.ps1 packaging/portable/runtime.lock.json integrations/components backend/tests/test_portable_python_runtime.py backend/tests/test_prepare_scripts.py
git commit -m "feat: add package-contained Python runtime"
```

---

### Task 2: Convert TTS More Initialization to Embedded Python

**Files:**
- Modify: `scripts/initialize-portable.ps1`
- Modify: `Build-Package.ps1`
- Modify: `backend/tests/test_portable_install.py`
- Modify: `backend/tests/test_portable_packages.py`
- Modify: `backend/tests/test_portable_first_run_harness.py`
- Modify: `scripts/test-portable-first-run.ps1`

**Interfaces:**
- Consumes: `Install-PortablePythonRuntime` from Task 1.
- Produces: controller `runtime/live` with dependencies in `Lib/site-packages`, no Conda or `pyvenv.cfg`.

- [ ] **Step 1: Write failing initializer tests**

Require the controller initializer to dot-source `portable-python.ps1`, call `Install-PortablePythonRuntime`, use the returned paths, install with `--target $PortableRuntime.SitePackages --link-mode copy`, and run `uv pip check --python`. Explicitly assert the initializer does not contain `bootstrap-conda.ps1`, `$Conda`, `conda create`, `-m pip`, or `Scripts\uv.exe`.

Extend the Full package audit fixture to fail if staging or the archive contains `pyvenv.cfg`, `conda-meta`, `condabin`, `Miniforge` or a build-machine prefix. Before compression, isolate the uv cache and copy staging to another volume to prove runtime files are independent; do not attempt to infer NTFS hardlinks from ZIP metadata. Change `scripts/test-portable-first-run.ps1` to synthesize a locked embeddable Python ZIP and a wheel containing the exact uv entry under restricted PATH, and remove `Install-FixtureCondaAdapter` plus `runtime/Scripts/uv.exe` assumptions.

- [ ] **Step 2: Run RED**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_portable_install.py backend/tests/test_portable_packages.py backend/tests/test_portable_first_run_harness.py -q -k "runtime or full or initialize"
```

Expected: FAIL because the current controller initializer invokes package Conda.

- [ ] **Step 3: Implement the controller flow**

The tested order is exact: Python install -> patch-version probe -> device selection -> runtime/model assets -> uv dependency install -> probes -> atomic publish. Run `portable_install.py select-device` with staging Python. Preserve `uv lock --check` and `uv export --frozen --no-dev --no-emit-project`; replace pip installation with:

```powershell
& $PortableRuntime.Uv pip install `
  --python $PortableRuntime.Python `
  --target $PortableRuntime.SitePackages `
  --link-mode copy `
  --requirement $requirements
& $PortableRuntime.Uv pip check --python $PortableRuntime.Python
```

Preserve import probe, atomic `staging -> live`, lock hashes and install-state write. `Build-Package.ps1` must copy `scripts/portable-python.ps1` into controller staging. Full builder still invokes the real staged initializer.

- [ ] **Step 4: Verify and commit**

Run the focused tests, full controller start suite, PowerShell parser, and a real TTS More Full build from a deep I-drive temporary path.

```powershell
git add scripts/initialize-portable.ps1 Build-Package.ps1 backend/tests/test_portable_install.py backend/tests/test_portable_packages.py backend/tests/test_portable_first_run_harness.py
git commit -m "fix: build controller runtime without package Conda"
```

---

### Task 3: Convert Worker Initialization and Synchronization

**Files:**
- Modify: `integrations/windows/Initialize.ps1`
- Modify: `integrations/windows/Build-Package.ps1`
- Modify: `integrations/contract_tests/test_portable_integration.py`
- Modify: `scripts/tts_more_deploy.py`
- Modify: `scripts/sync_integrations.py`
- Modify: `backend/tests/test_integration_sync.py`
- Modify: generated integration manifests/bundles produced by the repository sync command

**Interfaces:**
- Consumes: integration `portable-python.ps1` and component runtime locks from Task 1.
- Produces: synchronized worker bundles whose initializer has no package Conda dependency.

- [ ] **Step 1: Write failing worker contract tests**

Require worker `Initialize.ps1` to use the helper with the exact order Python -> patch probe -> select-device -> runtime payload -> model payload -> uv install -> probes -> atomic publish, install the selected requirements lock with uv `--target`/`--link-mode copy`, and run the existing component import probe. Require sync manifests and worker package staging to include `portable-python.ps1` with its SHA-256.

- [ ] **Step 2: Run RED**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest integrations/contract_tests/test_portable_integration.py backend/tests/test_integration_sync.py -q
```

Expected: FAIL because the worker initializer invokes Conda and manifests omit the helper.

- [ ] **Step 3: Implement and regenerate controlled mirrors**

Prepare embedded Python before selecting the device. Use staging Python for `portable_install.py`, preserve runtime payloads, locked model downloads and all probes, and replace only environment creation/install/check commands. Add the helper to `scripts/sync_integrations.py` source-copy rules and update generated ordinary-user guidance so it describes Python/uv assets rather than `data/cache/portable/conda`. Do not generate fork manifests until the canonical TTS More implementation is committed.

- [ ] **Step 4: Verify and commit**

Run integration contracts, canonical sync generation tests, worker package tests, all PowerShell AST checks and full backend focused gate. Commit the canonical TTS More implementation first; its exact SHA becomes the integration source revision used by Task 4.

```powershell
git add integrations scripts/tts_more_deploy.py backend/tests/test_integration_sync.py
git commit -m "feat: sync embedded Python worker initialization"
```

---

### Task 4: Update and Verify the Three Forks

**Files:**
- Modify in GPT worktree: controlled `tts_more/` files and integration manifest
- Modify in Index worktree: controlled `tts_more/` files and integration manifest
- Modify in CosyVoice worktree: controlled `tts_more/` files and integration manifest
- Modify in TTS More: `repo.lock.json` after fork commits

**Interfaces:**
- Consumes: canonical integration bundle from Task 3.
- Produces: three clean fork commits and a controller lock bound to their exact revisions.

- [ ] **Step 1: Sync each fork through the canonical sync command**

Do not copy ad hoc. Preserve untracked models, environments, caches and historical artifacts. The sync command may write only controlled root launchers and `tts_more/` paths.

- [ ] **Step 2: Run fork RED/GREEN contract cycle**

Before sync, prove each fork contract fails on the missing Python helper. After sync, run copied integration contracts, repository-native lightweight tests, PowerShell 5.1 parsing, runtime-lock validation and `Build-Package.ps1 -Profile Bootstrap -Device CPU` audits.

- [ ] **Step 3: Commit and review each fork**

Create one intentional commit per fork, independently review each diff, then push the existing `dev-xu/windows-dual-portable-v2` PR branches. Do not merge.

- [ ] **Step 4: Converge repo.lock and commit**

Update only GPT main, Index and Cosy commit fields to the three pushed PR heads. Run `build-four-pack.ps1 -PlanOnly`, lock tests and independent review before committing and pushing TTS More PR #13.

---

### Task 5: Build and Certify Four Full Packages

**Files:**
- Generate only beneath an ignored local output root on I drive.
- Verify: four ZIPs, 20 component sidecars, `compatibility-matrix.json`, `four-pack.provenance.json`.

**Interfaces:**
- Consumes: clean four worktrees bound by `repo.lock.json`.
- Produces: exactly four local Full ZIPs and a factual acceptance report.

- [ ] **Step 1: Run the complete source gate**

Run TTS More backend tests, frontend tests/build, integration sync checks, three fork contract suites, `git diff --check`, tracked-clean checks and PR checks. Record failures separately from GPU/model certification.

- [ ] **Step 2: Build the four-pack transaction**

Use a new output directory and an I-drive temporary root. Run `build-four-pack.ps1 -Profile Full` indirectly through its fixed full-only interface with `-Device Auto`. The builder must resolve a concrete worker profile and publish nothing unless all four packages plus sidecars validate.

- [ ] **Step 3: Validate the published asset set**

Recompute every ZIP SHA-256, validate schema v2, exact `SHA256SUMS.txt` coverage, single archive root, source revisions, resolved profiles, licenses, SBOM, acceptance sidecars, compatibility matrix and four-pack provenance. Scan for Miniforge, Conda cache, `pyvenv.cfg`, external prefixes, machine paths, credentials and mutable model revisions.

- [ ] **Step 4: Random-path offline lifecycle acceptance**

Extract each ZIP to a fresh path containing Chinese characters and spaces on a different drive. Disable proxy/model-download environment variables and network access for the process. Execute real root `Start.cmd -> Stop.cmd -> Repair.cmd`; Full must never download. Verify endpoints 8000/9880/9881/9882, process ownership, port release and no residual children.

- [ ] **Step 5: Real synthesis acceptance**

Load each worker's locked default model, synthesize a short audio sample through `tts-more-v1`, validate WAV structure, nonzero duration and SHA-256 artifact delivery, unload, restart and synthesize again. Register all three packages in TTS More and execute one orchestrated generation. Report the exact GPU profile actually certified.

- [ ] **Step 6: Final review and handoff**

Dispatch a whole-branch review against the design and this plan. Fix all Critical and Important findings, rerun covering tests, then provide absolute ZIP paths, byte sizes, SHA-256 values, resolved profiles and precise certification boundaries. Do not upload Full assets to GitHub.
