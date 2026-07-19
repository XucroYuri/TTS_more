# Three-Fork One-Click Portable Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror the approved one-click controller into GPT-SoVITS main, IndexTTS and CosyVoice without hand-editing controlled files.

**Architecture:** TTS More remains the sole source of controlled `tts_more/` files and root wrappers. Each fork gets a separate sync, test, CPU Bootstrap build and commit gate. Component-specific upstream source, model locks, Python versions and native WebUI launchers remain independent.

**Tech Stack:** Python sync utility, PowerShell 5.1 package builders, unittest/pytest, Git, GitHub Actions.

## Global Constraints

- TTS More source PR is merged before mirror commits are generated.
- GPT-SoVITS baseline is main and uses Python 3.11 on port 9880.
- IndexTTS uses Python 3.11 on port 9881 and retains upstream `uv.lock`.
- CosyVoice uses Python 3.10 on port 9882 and retains locked submodules.
- Root `Start.cmd` starts the `tts-more-v1` worker; `Start-WebUI.cmd` starts the upstream WebUI.
- Controlled mirror changes come only from `scripts/sync_integrations.py`.
- Each fork independently builds and audits one CPU Bootstrap package before commit.
- Full packages remain local-only and GitHub Actions must reject them.

---

### Task 1: Extend the controlled mirror contract in TTS More

**Files:**
- Modify: `scripts/sync_integrations.py`
- Modify: `backend/tests/test_integration_sync.py`
- Modify: `integrations/contract_tests/test_portable_integration.py`
- Modify: `integrations/windows/Build-Package.ps1`

**Interfaces:**
- Consumes: merged Phase A controller and operation helpers.
- Produces: fork-controlled `Invoke-PortableStart.ps1`, `Show-PortableProgress.ps1`, `portable_operations.py`, completed schema v2 and root Start wrapper.

- [ ] **Step 1: Write a failing controlled-file manifest test**

```python
def test_sync_includes_one_click_controller_and_operation_protocol(tmp_path: Path) -> None:
    manifest = sync_integration(REPO_ROOT, tmp_path, "gpt-sovits", "a" * 40)
    expected = {
        "tts_more/Invoke-PortableStart.ps1",
        "tts_more/Show-PortableProgress.ps1",
        "tts_more/portable_operations.py",
        "tts_more/error-catalog.zh-CN.json",
    }
    assert expected <= set(manifest["files"])
    assert "Invoke-PortableStart.ps1" in (tmp_path / "Start.cmd").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run sync tests and confirm missing-file failure**

Run: `py -3.11 -m pytest backend/tests/test_integration_sync.py -q`

Expected: FAIL because the new controller files are not copied.

- [ ] **Step 3: Copy the new controlled files and generate the root wrapper**

```python
for name in ("portable_install.py", "portable_launcher.py", "portable_operations.py", "portable_packages.py"):
    _copy_file(source_root / "scripts" / name, controlled / name)
for name in ("Invoke-PortableStart.ps1", "Show-PortableProgress.ps1"):
    _copy_file(source_root / "scripts" / name, controlled / name)
_copy_file(source_root / "packaging" / "portable" / "error-catalog.zh-CN.json", controlled / "error-catalog.zh-CN.json")

start_payload = (
    '@echo off\nsetlocal EnableExtensions\n'
    'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tts_more\\Invoke-PortableStart.ps1" '
    '-PackageRoot "%~dp0" -InitializeScript "tts_more\\Initialize.ps1" '
    '-ServiceScript "tts_more\\Start-Worker.ps1" %*\nexit /b %errorlevel%\n'
)
```

Add generated `使用说明-先看这里.txt` to `ROOT_ENTRIES` and the integration manifest. Do not copy TTS More frontend or backend into forks beyond existing worker dependencies.

- [ ] **Step 4: Run sync and contract tests**

Run: `py -3.11 -m pytest backend/tests/test_integration_sync.py backend/tests/test_portable_packages.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the canonical mirror update**

```powershell
git add scripts/sync_integrations.py integrations/windows/Build-Package.ps1 integrations/contract_tests/test_portable_integration.py backend/tests/test_integration_sync.py
git commit -m "feat: mirror one-click portable controls"
```

### Task 2: Sync and verify GPT-SoVITS main

**Files:**
- Regenerate: `F:/Code/Github/.codex-worktrees/GPT-SoVITS-dual-portable-v2/tts_more/**`
- Regenerate: `F:/Code/Github/.codex-worktrees/GPT-SoVITS-dual-portable-v2/Start.cmd`
- Regenerate: `F:/Code/Github/.codex-worktrees/GPT-SoVITS-dual-portable-v2/Initialize.cmd`
- Regenerate: `F:/Code/Github/.codex-worktrees/GPT-SoVITS-dual-portable-v2/Stop.cmd`
- Regenerate: `F:/Code/Github/.codex-worktrees/GPT-SoVITS-dual-portable-v2/Repair.cmd`
- Regenerate: `F:/Code/Github/.codex-worktrees/GPT-SoVITS-dual-portable-v2/Build-Package.ps1`
- Regenerate: `F:/Code/Github/.codex-worktrees/GPT-SoVITS-dual-portable-v2/使用说明-先看这里.txt`
- Modify: `F:/Code/Github/.codex-worktrees/GPT-SoVITS-dual-portable-v2/.github/workflows/portable-release.yml`

**Interfaces:**
- Consumes: Task 1 source revision.
- Produces: GPT package using Python 3.11, port 9880 and `tts_more_worker.gpt_sovits:app`.

- [ ] **Step 1: Prove the existing mirror is stale**

Run from TTS More: `py -3.11 scripts/sync_integrations.py --target F:/Code/Github/.codex-worktrees/GPT-SoVITS-dual-portable-v2 --check`

Expected: FAIL listing the new controlled files as missing or hash-mismatched.

- [ ] **Step 2: Regenerate the GPT mirror from the exact TTS More commit**

```powershell
$sourceRevision = (git rev-parse HEAD).Trim()
py -3.11 scripts/sync_integrations.py `
  --target F:/Code/Github/.codex-worktrees/GPT-SoVITS-dual-portable-v2 `
  --component gpt-sovits `
  --source-revision $sourceRevision
```

- [ ] **Step 3: Run GPT contract and build tests**

Run: `py -3.11 F:/Code/Github/.codex-worktrees/GPT-SoVITS-dual-portable-v2/tts_more/tests/test_portable_integration.py -v`

Run from the GPT worktree: `.\Build-Package.ps1 -Profile Bootstrap -Device CPU -Version 0.2.0-plancheck`

Expected: contract tests PASS; exactly one audited GPT Bootstrap ZIP is created without runtime or model assets.

- [ ] **Step 4: Update the GPT workflow to run the one-click contract test**

```yaml
- name: Verify one-click portable contract
  shell: pwsh
  run: |
    python tts_more\tests\test_portable_integration.py -v
    if (-not (Select-String -LiteralPath Start.cmd -Pattern 'Invoke-PortableStart.ps1' -Quiet)) { throw 'Start.cmd bypasses the portable controller' }
```

- [ ] **Step 5: Commit the GPT mirror**

```powershell
git -C F:/Code/Github/.codex-worktrees/GPT-SoVITS-dual-portable-v2 add Start.cmd Initialize.cmd Stop.cmd Repair.cmd Build-Package.ps1 Start-WebUI.cmd 使用说明-先看这里.txt tts_more .github/workflows/portable-release.yml
git -C F:/Code/Github/.codex-worktrees/GPT-SoVITS-dual-portable-v2 commit -m "feat: add one-click portable startup"
```

### Task 3: Sync and verify IndexTTS

**Files:**
- Regenerate: `F:/Code/Github/.codex-worktrees/index-tts-dual-portable-v2/tts_more/**`
- Regenerate: root portable entries and `使用说明-先看这里.txt`
- Modify: `F:/Code/Github/.codex-worktrees/index-tts-dual-portable-v2/.github/workflows/portable-release.yml`

**Interfaces:**
- Consumes: Task 1 source revision.
- Produces: IndexTTS package using Python 3.11, port 9881, upstream `uv.lock` and `tts_more_worker.indextts:app`.

- [ ] **Step 1: Prove the IndexTTS mirror is stale**

Run: `py -3.11 scripts/sync_integrations.py --target F:/Code/Github/.codex-worktrees/index-tts-dual-portable-v2 --check`

Expected: FAIL listing missing or changed one-click controlled files.

- [ ] **Step 2: Regenerate the IndexTTS mirror**

```powershell
$sourceRevision = (git rev-parse HEAD).Trim()
py -3.11 scripts/sync_integrations.py `
  --target F:/Code/Github/.codex-worktrees/index-tts-dual-portable-v2 `
  --component indextts `
  --source-revision $sourceRevision
```

- [ ] **Step 3: Run IndexTTS contract, lock and CPU Bootstrap checks**

Run: `py -3.11 F:/Code/Github/.codex-worktrees/index-tts-dual-portable-v2/tts_more/tests/test_portable_integration.py -v`

Run from the IndexTTS worktree: `uv lock --check; .\Build-Package.ps1 -Profile Bootstrap -Device CPU -Version 0.2.0-plancheck`

Expected: contract test PASS, `uv lock --check` succeeds, and one audited IndexTTS ZIP is created.

- [ ] **Step 4: Add the one-click contract workflow step**

```yaml
- name: Verify one-click portable contract
  shell: pwsh
  run: |
    python tts_more\tests\test_portable_integration.py -v
    if (-not (Select-String Start.cmd -Pattern 'Invoke-PortableStart.ps1' -Quiet)) { throw 'Start.cmd bypasses the portable controller' }
```

- [ ] **Step 5: Commit the IndexTTS mirror**

```powershell
git -C F:/Code/Github/.codex-worktrees/index-tts-dual-portable-v2 add Start.cmd Initialize.cmd Stop.cmd Repair.cmd Build-Package.ps1 Start-WebUI.cmd 使用说明-先看这里.txt tts_more .github/workflows/portable-release.yml
git -C F:/Code/Github/.codex-worktrees/index-tts-dual-portable-v2 commit -m "feat: add one-click portable startup"
```

### Task 4: Sync and verify CosyVoice

**Files:**
- Regenerate: `F:/Code/Github/.codex-worktrees/CosyVoice-dual-portable-v2/tts_more/**`
- Regenerate: root portable entries and `使用说明-先看这里.txt`
- Modify: `F:/Code/Github/.codex-worktrees/CosyVoice-dual-portable-v2/.github/workflows/portable-release.yml`

**Interfaces:**
- Consumes: Task 1 source revision.
- Produces: CosyVoice package using Python 3.10, port 9882, locked submodules and `tts_more_worker.cosyvoice:app`.

- [ ] **Step 1: Prove the CosyVoice mirror is stale**

Run: `py -3.11 scripts/sync_integrations.py --target F:/Code/Github/.codex-worktrees/CosyVoice-dual-portable-v2 --check`

Expected: FAIL listing missing or changed one-click controlled files.

- [ ] **Step 2: Regenerate the CosyVoice mirror**

```powershell
$sourceRevision = (git rev-parse HEAD).Trim()
py -3.11 scripts/sync_integrations.py `
  --target F:/Code/Github/.codex-worktrees/CosyVoice-dual-portable-v2 `
  --component cosyvoice `
  --source-revision $sourceRevision
```

- [ ] **Step 3: Verify submodules, contract and CPU Bootstrap build**

Run from the CosyVoice worktree: `git submodule status --recursive; py -3.11 tts_more/tests/test_portable_integration.py -v; .\Build-Package.ps1 -Profile Bootstrap -Device CPU -Version 0.2.0-plancheck`

Expected: no submodule line begins with `-` or `+`; contract tests PASS; one audited CosyVoice ZIP is created.

- [ ] **Step 4: Add the one-click contract workflow step**

```yaml
- name: Verify one-click portable contract
  shell: pwsh
  run: |
    python tts_more\tests\test_portable_integration.py -v
    if (-not (Select-String Start.cmd -Pattern 'Invoke-PortableStart.ps1' -Quiet)) { throw 'Start.cmd bypasses the portable controller' }
```

- [ ] **Step 5: Commit the CosyVoice mirror without artifacts**

```powershell
git -C F:/Code/Github/.codex-worktrees/CosyVoice-dual-portable-v2 add Start.cmd Initialize.cmd Stop.cmd Repair.cmd Build-Package.ps1 Start-WebUI.cmd 使用说明-先看这里.txt tts_more .github/workflows/portable-release.yml
git -C F:/Code/Github/.codex-worktrees/CosyVoice-dual-portable-v2 commit -m "feat: add one-click portable startup"
```

Before committing, `git status --short` may show the pre-existing ignored/local `artifacts/` directory; do not add it.
