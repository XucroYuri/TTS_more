# 移除 CUDA 验证子系统 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从仓库端到端删除废弃的 CUDA 验证/认证子系统（从未在真实硬件跑通、服务于已废弃的自研 worker 路线），使代码库与 ComfyUI-only 正式运行路径一致。

**Architecture:** 分层删除：后端孤儿代码 → 后端测试 → scripts → CI → 文档+断链 → 前端 e2e。每层删除后跑 grep 确认无残留引用，最后全量回归（`pytest backend` + 前端 build/test）确证主路径无损。

**Tech Stack:** Python (pytest/hardware), FastAPI app, GitHub Actions, frontend (Playwright/vitest), docs.

## Global Constraints

- 本计划是**删除性**任务，无新功能，不适用 TDD「先写失败测试」。每任务 = 删除 + 无残留 grep + 回归 + 提交。
- **保留**以下模块（在 spec §1.4 明确，不得误删）：`backend/app/hardware.py`、`supervisor.py`、`open_source_tts.py`、`portable_*`、`app/workers/*`、`comfyui/` 全部、`scripts/prepare-models.ps1`、`scripts/prepare-tts-repos.ps1`、`scripts/tts_more_deploy.py`、`frontend/src/api.ts`、`LocalPortableServicesPanel.tsx`。
- **删除范围定义**：「CUDA 验证/认证子系统」= validation/certification 门禁代码。**不包括** CUDA 运行时部署（`prepare-tts-repos.ps1` 装 torch/torchcodec 属部署系统，保留）。
- 删除后全仓（排除 `docs/superpowers/plans|specs` 历史审计文档）无任何文件 import/引用 `cuda_validation` 或 8 个被删脚本名。
- 文档断链修复只改「指向过时 CUDA 路径」的引用，不删历史 `docs/superpowers/plans|specs` doc（保留审计痕迹）。
- `docs/superpowers/` 被 `.gitignore` 忽略，新增/修改其中文档需 `git add -f`。

---

### Task 1: 删除后端孤儿代码 `cuda_validation.py`

**Files:**
- Delete: `backend/app/cuda_validation.py`
- Verify: `backend/tests/` 无残留 import

**Interfaces:**
- Consumes: 无（本任务独立）。
- Produces: 为 Task 2 提供前提（删除其测试依赖的文件）。

- [ ] **Step 1: 删除文件**

```bash
git rm backend/app/cuda_validation.py
```

- [ ] **Step 2: 确认 app/ 内无残留引用**

```bash
grep -rn "cuda_validation" backend/app --include="*.py"
# 期望：无输出（cuda_validation.py 已删，main.py 从未引用它）
```

- [ ] **Step 3: 提交**

```bash
git add -A
git commit -m "chore: remove orphan cuda_validation module

app-internal orphan with zero references in main.py and the rest of
backend/app; services the retired self-built CUDA certification path.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"
```

---

### Task 2: 删除后端 CUDA 验证测试文件

**Files:**
- Delete: `backend/tests/test_cuda_validation.py`
- Delete: `backend/tests/test_cuda_evidence_sanitizer.py`
- Delete: `backend/tests/test_gpu_workflow.py`
- Modify: 无

**Interfaces:**
- Consumes: Task 1 已删 `cuda_validation.py`。
- Produces: 这些测试删除后，`pytest backend` 的必然依赖消除。

- [ ] **Step 1: 删除三个测试文件**

```bash
git rm backend/tests/test_cuda_validation.py \
       backend/tests/test_cuda_evidence_sanitizer.py \
       backend/tests/test_gpu_workflow.py
```

- [ ] **Step 2: 确认无其他文件引用这三个测试**

```bash
grep -rn "test_cuda_validation\|test_cuda_evidence_sanitizer\|test_gpu_workflow" backend scripts .github --include="*.py" --include="*.yml" 2>/dev/null
# 期望：无输出
```

- [ ] **Step 3: 提交**

```bash
git add -A
git commit -m "chore: remove CUDA validation test files

test_gpu_workflow.py exclusively asserted the CUDA validation scripts,
CI workflow, and Playwright cuda e2e file presence; all now removed.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"
```

---

### Task 3: 删除 scripts/ 下 8 个 CUDA 脚本

**Files:**
- Delete (all under `scripts/`): `run-cuda-validation.py`, `run-cuda-validation.ps1`, `sanitize-cuda-evidence.py`, `cleanup-cuda-validation-processes.ps1`, `cleanup-distributed-cuda-validation-processes.ps1`, `register-cuda-validation-process.ps1`, `start-cuda-gpu-monitor.ps1`, `stop-cuda-gpu-monitor.ps1`
- Verify: 全仓无残留引用（grep 时排除 `test_prepare_scripts.py` 已知残留——Task 5 处理）

**Interfaces:**
- Consumes: 无。
- Produces: 移除脚本，`test_prepare_scripts.py` 的 `run-cuda-validation.ps1` 引用变为悬空，Task 5 修。

- [ ] **Step 1: 删除 8 个脚本**

```bash
cd scripts
git rm run-cuda-validation.py run-cuda-validation.ps1 sanitize-cuda-evidence.py \
       cleanup-cuda-validation-processes.ps1 cleanup-distributed-cuda-validation-processes.ps1 \
       register-cuda-validation-process.ps1 start-cuda-gpu-monitor.ps1 stop-cuda-gpu-monitor.ps1
cd ..
```

- [ ] **Step 2: 确认脚本名无残留引用（排除将被 Task 5 修的测试）**

```bash
grep -rn "run-cuda-validation\|sanitize-cuda\|cleanup-cuda\|gpu-monitor\|register-cuda" backend/app scripts .github deployment frontend/src --include="*.py" --include="*.ps1" --include="*.yml" 2>/dev/null
# 期望：仅 backend/tests/test_prepare_scripts.py、test_hardware.py 的已知残留（Task 5 修）
```

- [ ] **Step 3: 提交**

```bash
git add -A
git commit -m "chore: remove CUDA validation runner and monitor scripts

8 standalone CUDA certification scripts (run/sanitize/cleanup/monitor)
removed; they were never exercised on real hardware and serve the
retired worker path.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"
```

---

### Task 4: 删除 CI Windows GPU validation workflow

**Files:**
- Delete: `.github/workflows/windows-gpu-validation.yml`
- Verify: `ci.yml` 无 CUDA 引用（已确认无）

**Interfaces:**
- Consumes: 无。
- Produces: 移除 CI 门禁触发点。

- [ ] **Step 1: 删除 workflow**

```bash
git rm .github/workflows/windows-gpu-validation.yml
```

- [ ] **Step 2: 确认 ci.yml 无 CUDA 引用且无其他文件引用该 workflow**

```bash
grep -rn "windows-gpu-validation\|windows-gpu*\|cuda" .github/workflows/ci.yml 2>/dev/null
grep -rn "windows-gpu-validation" . --include="*.py" --include="*.yml" --include="*.md" 2>/dev/null | grep -v "superpowers/plans\|superpowers/specs"
# 期望：无输出
```

- [ ] **Step 3: 提交**

```bash
git add -A
git commit -m "chore: remove Windows GPU validation CI workflow

standalone workflow_dispatch gate for CUDA certification removed; no
ci.yml reference existed.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"
```

---

### Task 5: 精修保留的测试（删除悬空 CUDA 引用）

**Files:**
- Modify: `backend/tests/test_hardware.py:170-171`（删除两行直接 read 已删文件的断言）
- Modify: `backend/tests/test_prepare_scripts.py`
  - 删除常量 `CUDA_DOC_PATHS`（第 18-23 行左右）
  - 删除 6 个 CUDA 文档验证 test：`test_windows_cuda_single_node_runbook_is_the_only_copy_paste_certification_entrypoint`、`test_windows_cuda_copy_paste_powershell_has_no_pseudo_syntax`、`test_windows_cuda_copy_paste_powershell_parses`、`test_single_node_runbook_documents_one_formal_path_and_separate_ui_gate`、`test_single_node_runbook_distinguishes_skip_control_flow_and_outcomes`、`test_cuda_docs_use_root_lock_and_current_repopaths_contract`
  - 删除 `test_windows_deploy_and_worker_scripts_forward_topology_selection` 中读 `run-cuda-validation.ps1` 的 2 行（约 539-540）
  - **保留** `test_windows_prepare_bootstraps_torchcodec_before_upstream_cuda_install`（测 prepare-tts-repos.ps1，非被删子系统）

**Interfaces:**
- Consumes: Task 3 删除了脚本，Task 5 清理对此的悬空引用。
- Produces: `pytest backend` 可全绿，无文件引用已删目标。

- [ ] **Step 1: 改 test_hardware.py，删第 170-171 两行**

用 Edit 精准删除：
```python
    assert "nvidia-smi" in (repo_root / "backend" / "app" / "cuda_validation.py").read_text(encoding="utf-8").casefold()
    assert "nvidia-smi" in (repo_root / "scripts" / "start-cuda-gpu-monitor.ps1").read_text(encoding="utf-8").casefold()
```

- [ ] **Step 2: 改 test_prepare_scripts.py，删除 `CUDA_DOC_PATHS` 常量**

用 Edit 删除 `CUDA_DOC_PATHS = (...)` 到 `)` 的整块（约第 18-23 行）。

- [ ] **Step 3: 改 test_prepare_scripts.py，删除 6 个 CUDA 文档验证 test**

用 Edit 依次删除 `test_windows_cuda_single_node_runbook_is_the_only_copy_paste_certification_entrypoint` 至 `test_cuda_docs_use_root_lock_and_current_repopaths_contract` 整段（约第 1173-1276 行）。以 `def test_cuda_docs_use_root_lock_and_current_repopaths_contract` 结尾为块末。

- [ ] **Step 4: 改 test_prepare_scripts.py，删除 test_windows_deploy_and_worker_scripts_forward_topology_selection 中的 2 行 validator 断言**

用 Edit 删除：
```python
    validator = (REPO_ROOT / "scripts" / "run-cuda-validation.ps1").read_text(encoding="utf-8")
    assert "Stop-ConfiguredWorkerListeners" in validator
```

- [ ] **Step 5: 运行这两个测试文件确认绿**

```bash
.venv/bin/python -m pytest backend/tests/test_hardware.py backend/tests/test_prepare_scripts.py -q
# 期望：全部 PASS
```

- [ ] **Step 6: 全量 grep 确认无悬空引用**

```bash
grep -rn "cuda_validation\|cuda-e2e\|run-cuda-validation\|sanitize-cuda\|windows-gpu-validation\|stop-cuda-gpu-monitor\|start-cuda-gpu-monitor" backend --include="*.py" 2>/dev/null
# 期望：无输出
```

- [ ] **Step 7: 提交**

```bash
git add backend/tests/test_hardware.py backend/tests/test_prepare_scripts.py
git commit -m "test: drop dangling CUDA subsystem references in retained tests

test_hardware and test_prepare_scripts asserted the removed CUDA
validation scripts/docs; tests covering the deployment CUDA-runtime
install (prepare-tts-repos) are intentionally kept.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"
```

---

### Task 6: 删除 docs/ 下 6 篇 CUDA 文档并修复断链

**Files:**
- Delete: `docs/cuda-e2e-validation.md`, `docs/cuda-e2e-single-node.md`, `docs/cuda-e2e-distributed.md`, `docs/cuda-e2e-macos-lan.md`, `docs/cuda-e2e-acceptance-record.md`, `docs/cuda-windows-codex-handoff-prompt.md`
- Modify: `README.md`、`docs/ci-architecture.md`、`docs/release-governance.md`、`docs/TODO.md` 中引用 CUDA 文档/认证路径的段落

**Interfaces:**
- Consumes: 前序任务已删代码/脚本/CI。
- Produces: 仓库无 CUDA 文档断链，README 等与 ComfyUI-only 一致。

- [ ] **Step 1: 删除 6 篇文档**

```bash
cd docs
git rm cuda-e2e-validation.md cuda-e2e-single-node.md cuda-e2e-distributed.md \
       cuda-e2e-macos-lan.md cuda-e2e-acceptance-record.md cuda-windows-codex-handoff-prompt.md
cd ..
```

- [ ] **Step 2: 定位所有引用被删文档的文件**

```bash
grep -rln "cuda-e2e\|cuda-windows-codex\|cuda-e2e-acceptance" README.md docs deployment --include="*.md" 2>/dev/null | grep -v "superpowers/plans\|superpowers/specs"
```

对每个列出的文件，用 Edit 移除或改写指向被删 CUDA 文档的链接，使其不再引用不存在的文件。具体到 4 个已知文件：

- `README.md`：删除「CUDA 全流程闭环验证」相关段落与参考文档列表中 6 篇 cuda-*.md 链接；将「macOS 和普通 hosted CI 不能签发 Windows CUDA 认证」这句中 CUDA 认证已移除。搜索 `cuda-e2e-validation`、`cuda-e2e-single-node`、`cuda-e2e-distributed`、`cuda-e2e-macos-lan` 出现处。
- `docs/ci-architecture.md`：删/改 引 cuda-e2e-validation 的段落。
- `docs/release-governance.md`：删/改 引 cuda-e2e-validation 的段落。
- `docs/TODO.md`：把第 5、6 项（Windows CUDA 首次认证 🔬、macOS 控制面+LAN CUDA 📐）状态改为「✅ 已移除」，正文注明 CUDA 验证子系统已删除。

- [ ] **Step 3: 确认无指向被删文档的断链（排除历史 plan/spec）**

```bash
grep -rn "cuda-e2e-single-node\|cuda-e2e-validation\|cuda-e2e-distributed\|cuda-e2e-acceptance\|windows-gpu-validation" README.md docs deployment .github --include="*.md" --include="*.yml" 2>/dev/null | grep -v "superpowers/plans\|superpowers/specs"
# 期望：无输出
```

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "docs: remove CUDA validation docs and fix dangling references

6 cuda-e2e docs removed alongside their retired certification path;
README/ci-architecture/release-governance/TODO updated so no links
point at removed files.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"
```

---

### Task 7: 删除前端 CUDA e2e 并清理 package.json scripts

**Files:**
- Delete: `frontend/e2e/cuda-workstation.spec.ts`, `frontend/e2e/cuda-fixture.ts`, `frontend/e2e/cuda-fixture.test.ts`
- Modify: `frontend/package.json`（删 `test:cuda-fixture`, `cuda:e2e`, `cuda:e2e:install` 三个 scripts）
- Verify: `playwright.config.ts` 无 cuda 引用（已确认无）；`frontend/src` 无引用

**Interfaces:**
- Consumes: 无。
- Produces: 前端无 cuda e2e，`pnpm build`/`pnpm test` 不依赖它们。

- [ ] **Step 1: 删除 3 个前端 cuda e2e 文件**

```bash
git rm frontend/e2e/cuda-workstation.spec.ts frontend/e2e/cuda-fixture.ts frontend/e2e/cuda-fixture.test.ts
```

- [ ] **Step 2: 清理 frontend/package.json 的 3 个 cuda scripts**

用 Edit 从 `frontend/package.json` 删除（按实际存在的行）：
```json
    "test:cuda-fixture": "vitest run e2e/cuda-fixture.test.ts",
    "cuda:e2e": "playwright test e2e/cuda-workstation.spec.ts",
    "cuda:e2e:install": "playwright install chromium"
```

- [ ] **Step 3: 确认无残留引用**

```bash
grep -rn "cuda\|Cuda\|CUDA\|cuda-workstation\|cuda-fixture" frontend/src frontend/package.json frontend/playwright.config.ts 2>/dev/null | grep -v "node_modules"
# 期望：无输出（若 playwright.config 无 cuda project 定义）
```

- [ ] **Step 4: 前端回归**

```bash
pnpm --dir frontend build
pnpm --dir frontend test
# 期望：build 成功、vitest 全绿，无引用被删 e2e
```

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "chore: remove frontend CUDA e2e specs and scripts

cuda-workstation/cuda-fixture Playwright specs and their package.json
npm scripts removed; they validated the retired CUDA certification run.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"
```

---

### Task 8: 全量回归与最终验收

**Files:**
- Modify: 无（验证任务）

**Interfaces:**
- Consumes: 全部前序任务完成。
- Produces: 验收通过，可发 PR。

- [ ] **Step 1: 后端全量测试**

```bash
.venv/bin/python -m pytest backend -q
# 期望：全部 PASS（无 CUDA 子系统相关 failure）
```

- [ ] **Step 2: 全仓最终无残留 grep（排除历史 plan/spec 审计文档）**

```bash
grep -rniE "cuda_validation|run-cuda-validation|sanitize-cuda|windows-gpu-validation|cuda-e2e|cuda-workstation|cuda-fixture|cuda-gpu-monitor" backend/app backend/tests scripts .github frontend/src frontend/package.json docs README.md --include="*.py" --include="*.ps1" --include="*.yml" --include="*.md" 2>/dev/null | grep -v "superpowers/plans\|superpowers/specs"
# 期望：无输出
```

- [ ] **Step 3: 确认保留模块完好（hardware.py 仍被引用、前端 portable 端点仍存在）**

```bash
grep -c "collect_local_hardware_status" backend/app/main.py   # > 0，hardware.py 保留未受影响
grep -c "local-portable-services" frontend/src/api.ts          # > 0，portable 前端路径保留
```

- [ ] **Step 4: 提交工作树干净确认**

```bash
git status
# 期望：clean，所有删除已提交
```

---

### Task 9: 推送并创建 PR

**Files:**
- 无（Git 操作）

**Interfaces:**
- Consumes: Task 8 验收通过。
- Produces: PR 到 `origin/master`（master 即主分支，按项目约定直接面向 master）。

- [ ] **Step 1: 确认 master 干净并推送**

```bash
git push origin master
```

- [ ] **Step 2: 用 gh 创建 PR**

```bash
gh pr create --base master --head master \
  --title "chore: remove retired CUDA validation subsystem" \
  --body "$(cat <<'EOF'
## Summary
- Remove the CUDA validation/certification subsystem (never verified on real hardware; serves the retired self-built worker path)
- Delete `cuda_validation.py` orphan, 3 test files, 8 runner/monitor scripts, Windows GPU CI workflow, 6 cuda-e2e docs, 3 frontend cuda e2e specs
- Fix dangling references in `test_hardware.py`, `test_prepare_scripts.py`, README, ci-architecture, release-governance, TODO
- **Retained** (still consumed by active front-end / deployment): hardware.py, supervisor, open_source_tts, portable_*, comfyui/, prepare-tts-repos.ps1

## Test plan
- [ ] `pytest backend -q` passes (CUDA subsystem tests removed alongside code)
- [ ] `pnpm --dir frontend build` and `pnpm test` pass, no cuda e2e refs
- [ ] No dangling references to removed CUDA files (grep across repo)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: 返回 PR URL 给用户**

报告 `gh pr view <url>` 结果。

---
