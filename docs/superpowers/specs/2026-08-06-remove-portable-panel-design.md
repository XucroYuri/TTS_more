# 设计：移除 portable 面板与 local-control 支撑

**日期**: 2026-08-06
**范围**: TTS More 仓库，遗留生态清理第二阶段（方案 A：面板根治）
**前置**: PR #36 已合并（CUDA 验证子系统已移除）

---

## 1. 背景与动机

### 1.1 项目当前定位

TTS More 正式运行路径为 ComfyUI + TTS-Audio-Suite 编排。前端核心生成走 ComfyUI 契约（`lib/ttsAccess.ts` 的 `buildComfyUIEndpointRequest`、`services.py` 的 `COMFYUI_TTS_AUDIO_SUITE_CONTRACT`）。**portable 概念已在代码库语义扩散**："portable 本地便携服务面板"是一个活跃 UI（`LocalPortableServicesPanel` 渲染在 `App.tsx:1885`），但它混入了`portable_*` 服务端点配置基础设施（被服务路由/管理核心复用）。

### 1.2 审计发现的问题（精确 AST import 图 + 前端端点映射）

- 前端 `LocalPortableServicesPanel` 消费 10 个 `/api/local-portable-services/*` + `/api/local-control/token` 端点，后端集中在 `local_control.py`(1454L)。
- `local_control.py` 是面板专属后端单元；其依赖 `portable_imports.py`(274L) 仅被它引用。
- **但** `portable_services/discovery/control/endpoint_trust/locator_mutations` 被 `supervisor.py`（服务管理）与 `services.py`（服务路由中枢）复用，函数如 `sanitize_portable_endpoint`、`require_managed_portable_locators_unchanged` 作用于**所有服务端点（含 ComfyUI）**——**不能删**。
- `portable_manifest/file_io/windows_job` 被上述主路径模块传递引用——**不能独立删**。
- `open_source_tts.py`(340L) 是 ComfyUI 服务接入编织层（capabilities 含 `comfyui`/`tts-audio-suite`，resource_group `comfyui-local-0`），前端 App.tsx 深度使用——**主路径，不删**。

### 1.3 为什么要删

- portable 面板管理「本地便携服务安装/导入」，与当前 ComfyUI-only 编排路径无关，用户经 `open_source_tts` 的 ComfyUI 接入即可完成 TTS 服务配置。
- 14 个 `test_portable_*.py`（20,058 行）**全部被 conftest `LEGACY_SKIP` 跳过**，CI 不真跑，是脱管死测试。
- 移除它们使代码库进一步贴近「ComfyUI-only」宣称。

### 1.4 设计边界（明确保留）

| 模块 | 保留原因 |
|---|---|
| `portable_services/discovery/control/endpoint_trust/locator_mutations` | 被 services.py/supervisor.py 复用，是服务端点配置基础设施 |
| `portable_manifest/file_io/windows_job` | 被上述主路径模块传递引用 |
| `supervisor.py`、`open_source_tts.py`、`hardware.py`、`resources.py`、`service_store_io.py`、`service_config.py` | main.py 主路径直接引用 |
| 前端 `open_source_tts`(catalog/detect/configure)、`generateTasks`、`fetchServices*`、`fetchGptSovitsModelCatalog` | 主路径工作流 |

---

## 2. 删除清单

按图层列出。

### L1 前端面板壳
- Delete: `frontend/src/components/LocalPortableServicesPanel.tsx`(967L)、`frontend/src/lib/portableServices.ts`(792L)、`frontend/src/lib/portableProxy.ts`(14L)
- Delete tests: `LocalPortableServicesPanel.dom.test.tsx`(308)、`portableServices.test.ts`(504)、`portableImportLifecycle.test.ts`(118)、`portableImport.test.tsx`(439)
- Modify: `frontend/src/App.tsx` 移除 `LocalPortableServicesPanel` import 与渲染（约 1885 行）及其 state/effect；`frontend/src/i18n.ts` 清理 `portableServices.*` key；`frontend/src/types.ts` 清理 portable 类型

### L2 前端 api 层
- Modify: `frontend/src/api.ts`，删除 10 个 portable 函数（`fetchLocalPortableServices`/`discover`/`selectLocalPortableFolder`/`register`/`planLocalPortableImport`/`apply`/`portableServiceAction`/`fetchPortableActionStatus`/`fetchPortableOperation`/`fetchPortableOperationLogs`）+ `portableRequest` helper + `getLocalControlToken`
- **保留** openSource(3)/generateTasks/services/modelCatalog

### L3 后端面板支撑
- Delete: `backend/app/local_control.py`(1454L)、`backend/app/portable_imports.py`(274L)
- Modify: `backend/app/main.py` 移除 `from app.local_control import install_local_control`、`install_local_control(...)` 调用、`from app.portable_discovery ... read_portable_package`（若该 import 仅面板用）；移除 `/api/portable-packages/discover|register` 路由（若前端无其他消费者）

### L4 鉴权白名单
- Modify: `backend/app/auth.py`，删除 `"/api/local-control"`、`"/api/local-portable-services"`、`"/api/local-control/token"` 白名单条目

### L5 测试
- Delete 14 个 legacy-skip 测试（20,058 行）：`test_integration_sync, test_portable_control, test_portable_diagnostics, test_portable_discovery, test_portable_file_io, test_portable_first_run_harness, test_portable_install, test_portable_launcher, test_portable_locks, test_portable_migration, test_portable_operations, test_portable_packages, test_portable_python_runtime, test_portable_services, test_portable_start_controller`
- Delete `backend/tests/test_local_control.py`(3920)
- Modify: `backend/tests/conftest.py` 清理 `LEGACY_SKIP` 中已删条目
- **逐一核**：保留的 `test_prepare_scripts.py`、`test_release_governance.py`、`test_subprocess_safety.py` 是否 import 被删本地模块；修悬空引用

### L6 脚本 + 文档
- Delete standalone 面板脚本（逐核无 VCS/README 依赖）：`initialize-portable.ps1`、`select-portable-folder.ps1`、`repair-portable.ps1`、`portable_package_runner.py`、`portable_packages.py`、`import_portable_data.py`/`import_portable_data.py`、`portable_operations.py`、`portable_launcher.py`、`portable_install.py`、`serve-portable-fixtures.py`、`export-portable-diagnostics.py`、`build-portable-gpt-dev.ps1`、`portable-python.ps1`、`test-portable-first-run.ps1`（合计约 8,700 行）
- Modify docs: `workers.md`、`gpt-sovits-integration.md`、`open-source-tts-services.md` 中描述旧 worker/portable 路径的段落（改写为 ComfyUI-only，或标记历史）

---

## 3. 错误处理与回归策略

### 3.1 执行顺序
按 L1→L6 顺序，每层后跑针对性 grep 确认无残留引用再进下一层。先删独立文件（L1/L3/L5/L6），后处理交叉修改（L2/L4 + test 悬空引用）。

### 3.2 回归验证
```bash
.venv/bin/python -m pytest backend -q --continue-on-collection-errors
pnpm --dir frontend build && pnpm --dir frontend test
# 无残留
grep -rniE "LocalPortableServicesPanel|portableServices|local_control|local-portable-services|portable_imports|portableProxy" frontend/src backend/app backend/tests scripts 2>/dev/null
```

### 3.3 已知边界
- 5 个主路径 portable 模块 + manifest/file_io/windows_job **保留**：不因"复用 portable_* 命名"而删。
- 保留测试若 import 被删本地模块，仅精修悬空引用，不重写。
- 前端 `open_source_tts` ComfyUI 接入面板**保留**（替代 portable 面板完成 TTS 服务配置）。

---

## 4. 验收标准

1. `pytest backend` 无本 PR 引入失败（预存基线 test_service_queue/test_comfyui_reliability_validation/deployment-docs 除外）。
2. `pnpm build` + `pnpm test` 绿，无 portable 面板/端点引用。
3. 全仓无 `LocalPortableServicesPanel`、`local_control`、`portable_imports`、`local-portable-services`、`portableRequest` 残留。
4. 主路径路由完好：`/api/services`、`/api/open-source-tts/*`、`/api/generate`、`supervisor` 启动/停止仍在。
5. 保留的 5 个 portable 模块（services/discovery/control/endpoint_trust/locator_mutations）未动，`supervisor`/`open_source_tts` 正常。

---

## 5. 后续（不在本次范围）

- 保留的 5 个「服务端点配置基础设施」模块可从 `portable_*` 重命名为中性名（endpoint_trust/service_locator 等），消除语义误值——作为独立重构，避免本次 diff 过大。
- 真实 GPU 端到端验收（ComfyUI→真实音频），需 GPU 机器。
