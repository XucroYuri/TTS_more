# 移除 portable 面板与 local-control 支撑 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 端到端移除前端 `LocalPortableServicesPanel` 面板与后端 `local_control.py`/`portable_imports.py` 支撑，删除 14 个 legacy-skipped portable 测试与 standalone 脚本，让代码库贴近 ComfyUI-only 定位。

**Architecture:** 分层删除（前端壳 → 前端 api → 后端支撑 → 鉴权 → 测试 → 脚本/文档），每层后 grep 确认无残留。**保留** 5 个主路径复用的 portable 模块（services/discovery/control/endpoint_trust/locator_mutations）+ `supervisor`/`open_source_tts`/`hardware`。

**Tech Stack:** React/TS 前端、FastAPI 后端、pytest/vitest。

## Global Constraints

- **删除性任务**，不适用 TDD「先写失败测试」；每任务 = 删除 + 无残留 grep + 回归 + 提交。
- **必须保留**（spec §1.4）：`portable_services`/`portable_discovery`/`portable_control`/`portable_endpoint_trust`/`portable_locator_mutations` + `portable_manifest`/`portable_file_io`/`windows_job`（主路径传递引用）+ `supervisor`/`open_source_tts`/`hardware`/`resources`/`service_store_io`/`service_config`。
- 前端保留 `open_source_tts`(catalog/detect/configure)、`generateTasks`、`fetchServices*`、`fetchGptSovitsModelCatalog`。
- main.py 的 `/api/portable-packages/discover|register` 路由删除（前端不调），但 **`portable_discovery` 模块保留**（其他主路径用）。
- L6 脚本删除前逐一确认无 deployment/README/CI 硬依赖（已验证：仅 docs/superpowers 历史审计文档引用，可删）。
- 文档断链只改「指向被删项」的引用，不删 `docs/superpowers/plans|specs` 历史审计文档。
- `docs/superpowers/` 被 .gitignore 忽略，新增文档需 `git add -f`。

---

### Task 1: 删除前端面板壳（组件 + lib + tests + tsx/test）

**Files:**
- Delete: `frontend/src/components/LocalPortableServicesPanel.tsx`
- Delete: `frontend/src/lib/portableServices.ts`, `frontend/src/lib/portableProxy.ts`
- Delete: `frontend/src/components/LocalPortableServicesPanel.dom.test.tsx`, `frontend/src/lib/portableServices.test.ts`, `frontend/src/lib/portableImportLifecycle.test.ts`, `frontend/src/lib/portableImport.test.tsx`
- Verify: 无其他文件 import 这些模块

**Interfaces:**
- Consumes: 无。
- Produces: 前端面板壳移除，为 api.ts 清理(Task2) 提供前提。

- [ ] **Step 1: 删除 7 个面板/测试文件**

```bash
cd /Volumes/SSD/Code/07-TTS/TTS_more
git rm frontend/src/components/LocalPortableServicesPanel.tsx frontend/src/components/LocalPortableServicesPanel.dom.test.tsx \
       frontend/src/lib/portableServices.ts frontend/src/lib/portableServices.test.ts \
       frontend/src/lib/portableProxy.ts frontend/src/lib/portableImportLifecycle.test.ts frontend/src/lib/portableImport.test.tsx
```

- [ ] **Step 2: 确认无残留 import (除 App.tsx/i18n/types, 在 Task2/4 清)**

```bash
grep -rn "LocalPortableServicesPanel\|portableServices\|portableProxy\|portableImport" frontend/src --include="*.ts" --include="*.tsx" | grep -v "node_modules"
# 期望: 仅 App.tsx(import/渲染)、i18n.ts、types.ts 残留, 其余无
```

- [ ] **Step 3: 提交**

```bash
git add -A
git commit -m "chore: remove portable panel shell components and libs

LocalPortableServicesPanel, portableServices, portableProxy and their
tests removed; App-level imports/apis cleaned in follow-up tasks.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"
```

---

### Task 2: 清理前端 App.tsx / i18n.ts / types.ts 的 portable 引用

**Files:**
- Modify: `frontend/src/App.tsx`（移除 `LocalPortableServicesPanel` import 与渲染 `<LocalPortableServicesPanel .../>`，及面板相关 state/effect/serviceStatusRefresh 逻辑）
- Modify: `frontend/src/i18n.ts`（移除 `portableServices.*` key 区块）
- Modify: `frontend/src/types.ts`（移除 portable 相关接口：`LocalPortableService`/`LocalPortableServicesResponse` 等）

**Interfaces:**
- Consumes: Task1 删了面板组件。
- Produces: 前端无面板引用，`pnpm build`/`tsc` 可过。

- [ ] **Step 1: 读 App.tsx 定位面板相关代码**

```bash
grep -n "LocalPortableServicesPanel\|portableService\|portableImport\|portableAction" frontend/src/App.tsx | head -20
```

逐一查看上下文，用 Edit 删除 `import { LocalPortableServicesPanel }...`（约 80 行）与渲染 `<LocalPortableServicesPanel onServicesStatusRefresh={...} />`（约 1885 行）及相关的 state/setState 定义与 effect。

- [ ] **Step 2: 用 Edit 移除 App.tsx 面板渲染与 import**

删除 import 行与 `{showPortable && <LocalPortableServicesPanel .../>}`（或无条件渲染处）；若面板相关 state（如 `portableServices`、`portableToken`）仅被面板用则一并删。

- [ ] **Step 3: 清理 i18n.ts 的 portableServices key**

```bash
grep -n "portableServices" frontend/src/i18n.ts | head
```
用 Edit 删除 `portableServices: {...}` 整块（中英各一处）以及 i18n.test.ts 中的对应断言。

- [ ] **Step 4: 清理 types.ts portable 接口**

```bash
grep -n "LocalPortableService\|PortableDiscovery\|PortableImport" frontend/src/types.ts | head
```
用 Edit 删除相关 interface 定义。

- [ ] **Step 5: 前端 ts 检查 + 测试**

```bash
cd frontend && pnpm exec tsc --noEmit
pnpm test
# 期望: tsc 无报错(无未使用import), vitest 绿
```

- [ ] **Step 6: 提交**

```bash
cd /Volumes/SSD/Code/07-TTS/TTS_more
git add frontend/src/App.tsx frontend/src/i18n.ts frontend/src/i18n.test.ts frontend/src/types.ts
git commit -m "feat: remove portable panel wiring from App shell and i18n

App no longer renders LocalPortableServicesPanel; portableServices i18n
keys and portable types removed. open-source TTS (ComfyUI) wiring kept.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"
```

---

### Task 3: 清理前端 api.ts 的 portable 端点函数

**Files:**
- Modify: `frontend/src/api.ts`
- Delete functions: `portableRequest`, `getLocalControlToken`, `fetchLocalPortableServices`, `discoverLocalPortableServices`, `selectLocalPortableFolder`, `registerLocalPortableService`, `planLocalPortableImport`, `applyLocalPortableImport`, `portableServiceAction`, `fetchPortableActionStatus`, `fetchPortableOperation`, `fetchPortableOperationLogs`
- Keep: `fetchOpenSourceTTSCatalog`/`detectOpenSourceTTS`/`configureOpenSourceTTS`, `fetchServices*`, `generateTasks`, `fetchGptSovitsModelCatalog`

**Interfaces:**
- Consumes: Task1/2。
- Produces: api.ts 无 portable 端点引用，其余导出完好。

- [ ] **Step 1: 定位 portable 函数行区**

```bash
grep -nE "portableRequest|/api/local-control|/api/local-portable-services" frontend/src/api.ts
# 预计 70(portableRequest def) 与 148-308(函数区)
```

- [ ] **Step 2: 用 Edit 删除 `portableRequest` helper 定义及其上下的相关类型 import**

删除 `async function portableRequest...`（约 70-90 行）定义。

- [ ] **Step 3: 用 Edit 删除 10 个 portable 函数（约 159-310 行整体块）**

从 `export async function fetchLocalPortableServices` 到 `fetchPortableOperationLogs` 的整段删除。**保留紧邻的 `fetchOpenSourceTTSCatalog`(311 起)**。

- [ ] **Step 4: 确认 api.ts 无 portable、open_source 保留**

```bash
grep -nE "portable|local-control|local-portable" frontend/src/api.ts || echo "portable已删"
grep -cE "open-source-tts|generateTasks|fetchServices" frontend/src/api.ts  # 应>0
```

- [ ] **Step 5: tsc + test + build**

```bash
cd frontend && pnpm exec tsc --noEmit && pnpm test
```

- [ ] **Step 6: 提交**

```bash
cd /Volumes/SSD/Code/07-TTS/TTS_more
git add frontend/src/api.ts
git commit -m "refactor: drop portable endpoint functions from api client

Removed 10 local-portable-services functions + portableRequest helper;
kept open-source-tts (ComfyUI wiring), services, generate, model-catalog.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"
```

---

### Task 4: 删除后端 local_control.py + portable_imports.py 及 main.py 接线

**Files:**
- Delete: `backend/app/local_control.py`, `backend/app/portable_imports.py`
- Modify: `backend/app/main.py`（删 `from app.local_control import install_local_control`、`install_local_control(...)` 调用、`/api/portable-packages/discover|register` 路由、`from app.portable_discovery import ...read_portable_package` 中仅面板用的符号）
- Modify: `backend/app/auth.py`（删 local-control/local-portable-services/token 白名单）

**Interfaces:**
- Consumes: Task3 前端已不再调这些端点。
- Produces: 后端无 local_control 支撑。

- [ ] **Step 1: 删两个后端模块**

```bash
git rm backend/app/local_control.py backend/app/portable_imports.py
```

- [ ] **Step 2: 读 main.py 定位接线**

```bash
grep -nE "install_local_control|portable_packages|read_portable_package|portable_discovery" backend/app/main.py
```

- [ ] **Step 3: 用 Edit 移除 main.py 的 local_control 接线与 portable-packages 路由**

删除 `from app.local_control import install_local_control`(22) 与 `install_local_control(...)` 调用块(约 166 起)。若 `from app.portable_discovery import ...` 中的 `read_portable_package`/`endpoint_from_portable_package` 仅被 `/api/portable-packages` 路由用，则删对应路由(285-307)及 import 符号；**保留** `PortablePackageDiscoverRequest` 若仍被引用。确认 `portable_discovery` 模块不被删（spec §1.4）。

- [ ] **Step 4: 用 Edit 移除 auth.py 白名单条目**

```bash
grep -n "local-control\|local-portable-services" backend/app/auth.py
```
删除 `"/api/local-control"`, `"/api/local-portable-services"`, `/api/local-control/token`（若在 `_OPEN_EXACT_PATHS`）。

- [ ] **Step 5: 后端 import 健全检查**

```bash
.venv/bin/python -c "import ast; [ast.parse(open(f).read()) for f in ['backend/app/main.py','backend/app/auth.py']]; print('语法OK')"
grep -rn "local_control\|portable_imports" backend/app --include="*.py" || echo "app内无残留"
```

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "chore: remove local-control panel backend and auth whitelist

Deleted local_control.py + portable_imports.py, their main.py routes
(/api/portable-packages, install_local_control), and auth whitelist
entries. Retained portable_discovery (used by service core).

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"
```

---

### Task 5: 删除 15 个 portable/local-control 测试 + 清理 conftest

**Files:**
- Delete (14 legacy-skip): `test_integration_sync.py`, `test_portable_control.py`, `test_portable_diagnostics.py`, `test_portable_discovery.py`, `test_portable_file_io.py`, `test_portable_first_run_harness.py`, `test_portable_install.py`, `test_portable_launcher.py`, `test_portable_locks.py`, `test_portable_migration.py`, `test_portable_operations.py`, `test_portable_packages.py`, `test_portable_python_runtime.py`, `test_portable_services.py`, `test_portable_start_controller.py`
- Delete: `backend/tests/test_local_control.py`
- Modify: `backend/tests/conftest.py`（清理 LEGACY_SKIP 中已删条目）
- Modify (精修悬空引用): `backend/tests/test_auth.py`, `backend/tests/test_api.py`（若 import 被删模块）

**Interfaces:**
- Consumes: Task4 删了 local_control/portable_imports。
- Produces: 脱管测试移除，保留测试无悬空引用。

- [ ] **Step 1: 删除 16 个测试文件**

```bash
cd /Volumes/SSD/Code/07-TTS/TTS_more
git rm backend/tests/test_local_control.py backend/tests/test_integration_sync.py backend/tests/test_portable_control.py backend/tests/test_portable_diagnostics.py backend/tests/test_portable_discovery.py backend/tests/test_portable_file_io.py backend/tests/test_portable_first_run_harness.py backend/tests/test_portable_install.py backend/tests/test_portable_launcher.py backend/tests/test_portable_locks.py backend/tests/test_portable_migration.py backend/tests/test_portable_operations.py backend/tests/test_portable_packages.py backend/tests/test_portable_python_runtime.py backend/tests/test_portable_services.py backend/tests/test_portable_start_controller.py
```

- [ ] **Step 2: 清理 conftest 的 LEGACY_SKIP 列表**

```bash
grep -n "test_local_control\|test_portable\|test_integration_sync" backend/tests/conftest.py
```
用 Edit 删除已删文件的条目，保留 `test_prepare_scripts.py`（若仍在）。

- [ ] **Step 3: 精修 test_auth.py / test_api.py 的悬空引用**

```bash
grep -n "local_control\|local-portable\|portable" backend/tests/test_auth.py backend/tests/test_api.py
```
用 Edit 删除/改写引用被删端点或模块的断言。

- [ ] **Step 4: 回归确认无本 PR 引入失败**

```bash
.venv/bin/python -m pytest backend/tests/test_release_governance.py backend/tests/test_auth.py -q 2>&1 | tail -5
# 期望: 绿(或仅预存基线失败)
```

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "test: remove 16 portable/local-control test files and conftest skips

Deleted 14 legacy-skipped test_portable_* files, test_local_control,
and test_integration_sync; pruned conftest LEGACY_SKIP and dangling
refs in test_auth/test_api.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"
```

---

### Task 6: 删除 standalone portable 脚本

**Files:**
- Delete (逐一核无 VCS/文档硬依赖后再删): `scripts/initialize-portable.ps1`, `scripts/select-portable-folder.ps1`, `scripts/repair-portable.ps1`, `scripts/portable_package_runner.py`, `scripts/portable_packages.py`, `scripts/import-portable-data.py`, `scripts/import_portable_data.py`, `scripts/portable_operations.py`, `scripts/portable_launcher.py`, `scripts/portable_install.py`, `scripts/serve-portable-fixtures.py`, `scripts/export-portable-diagnostics.py`, `scripts/build-portable-gpt-dev.ps1`, `scripts/portable-python.ps1`, `scripts/test-portable-first-run.ps1`
- **保留**(活跃运行): `scripts/tts_more_deploy.py`, `scripts/prepare-tts-repos.*`, `scripts/deploy-local-tts.*`, `scripts/update.*`, `scripts/sync_integrations.py`, `scripts/start-*.sh/ps1`

**Interfaces:**
- Consumes: 前置任务已移除面板后端。
- Produces: scripts/ 无 standalone portable 工具。

- [ ] **Step 1: 逐核每个候选脚本无 deployment/README/CI 硬依赖**

```bash
for f in initialize-portable.ps1 select-portable-folder.ps1 repair-portable.ps1 portable_package_runner.py portable_packages.py import-portable-data.py import_portable_data.py portable_operations.py portable_launcher.py portable_install.py serve-portable-fixtures.py export-portable-diagnostics.py build-portable-gpt-dev.ps1 portable-python.ps1 test-portable-first-run.ps1; do
  refs=$(grep -rlE "$f|portable" deployment docs README.md .github --include="*.md" --include="*.yml" --include="*.json" 2>/dev/null | grep -v superpowers)
  echo "$f: 硬依赖=[${refs:-无,可删}]"
done
```

- [ ] **Step 2: 删除确认无硬依赖的脚本**

```bash
cd /Volumes/SSD/Code/07-TTS/TTS_more/scripts
git rm <上一步标记为"无"的脚本>
cd ..
```
逐个用 git rm 删除标记「无」的脚本（不删 `tts_more_deploy.py`/`prepare-*`/`deploy-local-*`/`update.*`/`sync_integrations.py`/`start-*`）。

- [ ] **Step 3: 确认 scripts/ 仍含运行脚本、无 standalone portable 残留**

```bash
ls scripts/ | grep -c "portable" 
# 期望: 0 (standalone 全删) 或仅保留无硬依赖判断后需保留者
grep -rln "portable_launcher\|portable_packages\|portable_install" scripts --include="*.py" --include="*.ps1" || echo "无残留引用"
```

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "chore: remove standalone portable installer/launcher scripts

15 legacy standalone scripts removed after confirming no deployment/
README/CI hard dependencies; active deploy/prepare/update scripts kept.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"
```

---

### Task 7: 更新旧文档（worker/portable/gpt-sovits-integration）

**Files:**
- Modify: `docs/workers.md`, `docs/gpt-sovits-integration.md`, `docs/open-source-tts-services.md`
- Verify: 无指向被删模块/脚本的断链

**Interfaces:**
- Consumes: 前置任务删除。
- Produces: 文档与 ComfyUI-only 现状一致。

- [ ] **Step 1: 读每篇文档找旧路径描述**

```bash
grep -lnE "portable|worker|tts-more-v1|Gradio WebUI" docs/workers.md docs/gpt-sovits-integration.md docs/open-source-tts-services.md
```

- [ ] **Step 2: 逐篇改写为 ComfyUI-only 或标记历史**

`docs/workers.md`（18 行）：若描述自研 worker 为当前路径，改为「历史迁移路径已移除，正式路径见 comfyui-integration.md」。
`docs/gpt-sovits-integration.md`（77 行）：改为说明 GPT-SoVITS 通过 ComfyUI/TTS-Audio-Suite 接入的现代路径，标注旧 worker 已移除。
`docs/open-source-tts-services.md`：保留（它是 ComfyUI 服务接入的基础文档），仅清理 portable 引用。

- [ ] **Step 3: 无断链 grep**

```bash
grep -rnE "workers.md.*worker|portable_launcher|portable_install|local_control|LocalPortableServicesPanel" README.md docs --include="*.md" | grep -v superpowers || echo "无断链"
```

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "docs: rewrite worker/portable integration docs for ComfyUI-only

Retire description of self-built worker path; direct to ComfyUI + TTS
Audio Suite integration. No dangling references to removed code.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"
```

---

### Task 8: 全量回归与最终验收

**Files:**
- 无（验证任务）

**Interfaces:**
- Consumes: Task1-7 完成。
- Produces: 验收通过。

- [ ] **Step 1: 后端全量**

```bash
.venv/bin/python -m pytest backend -q --continue-on-collection-errors 2>&1 | tail -5
# 期望: 无本 PR 引入失败; 预存基线(comfyui_reliability_validation/service_queue/deployment-docs)按历史基线
```

- [ ] **Step 2: 前端全量**

```bash
cd frontend && pnpm build && pnpm test
```

- [ ] **Step 3: 全仓无残留 grep**

```bash
cd /Volumes/SSD/Code/07-TTS/TTS_more
grep -rniE "LocalPortableServicesPanel|portableServices|portableProxy|local_control|local-portable-services|portable_imports|portableRequest" frontend/src backend/app backend/tests scripts README.md docs --include="*.ts" --include="*.tsx" --include="*.py" --include="*.md" --include="*.ps1" 2>/dev/null | grep -v "superpowers/plans\|superpowers/specs"
# 期望: 无输出
```

- [ ] **Step 4: 确认主路径路由完好**

```bash
grep -cE "/api/services\"|/api/open-source-tts|/api/generate|supervisor" backend/app/main.py  # main.py 主路径仍在
ls backend/app/supervisor.py backend/app/open_source_tts.py backend/app/portable_services.py backend/app/portable_discovery.py backend/app/portable_control.py  # 保留模块在
```

- [ ] **Step 5: 提交工作树干净确认**

```bash
git status
# 期望: clean
```

---

### Task 9: 推送并创建 PR

**Files:**
- 无（Git 操作）

**Interfaces:**
- Consumes: Task8 验收通过。
- Produces: PR 到 master。

- [ ] **Step 1: 新建分支 + 提交推送**

```bash
cd /Volumes/SSD/Code/07-TTS/TTS_more
git checkout -b remove-portable-panel
git push -u origin remove-portable-panel
```

- [ ] **Step 2: gh 创建 PR（body 用文件避免反引号转义）**

写出 PR body 到 `/tmp/pr-body2.md`（Summary: 删面板+local_control+portable_imports+16测试+15脚本+3文档; Retained: 5个主路径portable模块+supervisor/open_source; Test: 无本PR引入失败），然后：
```bash
gh pr create --base master --head remove-portable-panel \
  --title "chore: remove portable panel and local-control backend" \
  --body-file /tmp/pr-body2.md
```

- [ ] **Step 3: 报告 PR URL**

---
