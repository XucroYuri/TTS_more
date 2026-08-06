# 设计：完整移除 CUDA 验证子系统

**日期**: 2026-08-06
**范围**: TTS More 仓库，遗留生态清理第一阶段
**决策**: 采用方案 B（按真实影响面完整删除 CUDA 验证生态），接受扩大后的删除范围

---

## 1. 背景与动机

### 1.1 项目当前定位

TTS More 是剧本配音工作台（编排层）。正式运行路径已转轨到 **ComfyUI + TTS-Audio-Suite 编排**：调用 ComfyUI HTTP `/prompt`，把 TTS 引擎与任务队列下沉给 ComfyUI/远端。旧的 `tts-more-v1` worker、Gradio、portable 包、自研 CUDA 认证验证均为**已废弃的历史迁移路径**（见 `docs/current-state-and-simplification-plan.md` 与 README「遗留路径」段）。

### 1.2 审计发现的问题

「只删真孤儿」方向的精确依赖核查（AST 级源码 import 图）确认：**`cuda_validation.py` 是后端运行时唯一的真孤儿**——`backend/app/` 内 0 引用、`main.py` 0 引用、`hardware.py` 不依赖它。但它不是独立小包：它牵出跨 6 个目录的 CUDA 验证生态共约 20+ 文件。

### 1.3 为什么删

- 该 CUDA 认证自动化（单机/四机/macOS+LAN）**从未在真实硬件跑通**（`docs/TODO.md` 第 5、6 项状态 `🔬 待真实执行`）。
- 它为**已废弃的自研 worker 路线**设计（`repo.lock.json` 本地服务、`app.workers.*`），与当前 ComfyUI-only 路径不符。
- README 已明确「macOS 和普通 hosted CI 不能签发 Windows CUDA 认证」，该门禁不构成正式发布约束。
- 保留它会让代码库持续地与「ComfyUI-only 宣称」矛盾。

### 1.4 设计边界（明确不删）

以下生态**保留**，因为有活跃消费者，与 CUDA 无关或仍在主路径：

| 模块 | 保留原因 |
|---|---|
| `backend/app/hardware.py` | `main.py` 用它做运行时硬件状态采集（`collect_local_hardware_status`） |
| `backend/app/supervisor.py`、`open_source_tts.py`、`portable_*` | 前端 `api.ts` 仍调用其端点（`/api/local-portable-services/*`、`/api/open-source-tts/*` 等） |
| `backend/app/workers/*` | `scripts/tts_more_deploy.py` 与 `repo.lock.json` 仍对标其 launcher 契约 |
| `scripts/` 中非 CUDA 的 deploy/update/start/portable 脚本 | 是活跃运行脚本 |
| `comfyui/` 目录全部（client、workflow_builder、reliability_*、live_validation） | 是当前 ComfyUI 路径核心，12 个测试在引用 |

**本次不做**前端转轨（不新增 ComfyUI 前端面板、不替换 `api.ts` 旧端点）——由 `2026-08-06 现状与转轨路线图` 文档另行跟踪。

---

## 2. 删除清单

按图层列出端到端删除目标。

### Layer 1 — 后端运行时代码
- `backend/app/cuda_validation.py`（1852 行，唯一真孤儿）

### Layer 2 — 后端测试
**整文件删除：**
- `backend/tests/test_cuda_validation.py`（2425 行）
- `backend/tests/test_cuda_evidence_sanitizer.py`（570 行）
- `backend/tests/test_gpu_workflow.py`（611 行，整个文件都在验证 CUDA 生态：23 个 test 全测 CUDA 脚本/CI/Playwright 存在性）

**保留 + 精修：**
- `backend/tests/test_hardware.py`：删除第 170–171 行，这两行直接 `read_text` 读 `cuda_validation.py` 与 `start-cuda-gpu-monitor.ps1` 断言含 `nvidia-smi`。该文件主体是测 `hardware.py`（保留）。
- `backend/tests/test_prepare_scripts.py`（64 个 test）：主体是测 `prepare-*.ps1` 脚本（保留），仅精修触及 CUDA 子系统的断言：
  - `test_committed_topology_examples_are_sanitized_and_valid`（579）
  - `test_windows_prepare_bootstraps_torchcodec_before_upstream_cuda_install`（297）——若该断言仅依赖 CUDA 子系统，则删除或改写
  - 逐一 grep 其余 `cuda` 命中后裁决

### Layer 3 — scripts/ 脚本（删 8 个）
- `scripts/run-cuda-validation.py`
- `scripts/run-cuda-validation.ps1`
- `scripts/sanitize-cuda-evidence.py`
- `scripts/cleanup-cuda-validation-processes.ps1`
- `scripts/cleanup-distributed-cuda-validation-processes.ps1`
- `scripts/register-cuda-validation-process.ps1`
- `scripts/start-cuda-gpu-monitor.ps1`
- `scripts/stop-cuda-gpu-monitor.ps1`

### Layer 4 — CI
- `.github/workflows/windows-gpu-validation.yml`（独立 workflow，已确认 `ci.yml` 无 CUDA 引用）

### Layer 5 — docs/（删 6 篇 + 修断链）
**整文件删除：**
- `docs/cuda-e2e-validation.md`
- `docs/cuda-e2e-single-node.md`
- `docs/cuda-e2e-distributed.md`
- `docs/cuda-e2e-macos-lan.md`
- `docs/cuda-e2e-acceptance-record.md`
- `docs/cuda-windows-codex-handoff-prompt.md`

**断链修复**（删除后 grep 确认，逐一更新引用）：
- `README.md`（CUDA 段落、参考文档列表）
- `docs/ci-architecture.md`
- `docs/release-governance.md`（`cuda-e2e-validation` 引用）
- `docs/TODO.md`（第 5、6 项是 CUDA 认证事项，改为「已移除」）
- `docs/superpowers/plans|specs/` 中 3 篇历史文档（`2026-07-10-*-cuda-validation-*`）——这些是历史计划文档，改为标注「设计已作废/子系统已移除」，不硬删（保留审计痕迹）

### Layer 6 — 前端
**整文件删除：**
- `frontend/e2e/cuda-workstation.spec.ts`
- `frontend/e2e/cuda-fixture.ts`
- `frontend/e2e/cuda-fixture.test.ts`

**配置清理：**
- `frontend/package.json`：删除 `test:cuda-fixture`、`cuda:e2e`、`cuda:e2e:install` 三个 scripts
- `frontend/playwright.config.ts`：删除对 `cuda-workstation.spec.ts` 的引用
- `frontend/playwright.config.ts`/config 中残留的 cuda project 定义

---

## 3. 错误处理与回归策略

### 3.1 删除执行顺序
按 Layer 顺序执行，每层后跑一次针对性 grep，确认无残留引用再进下一层。先删独立文件（L1/L3/L4/L6 后端文档），最后处理交叉精修（L2 的 test_hardware/test_prepare_scripts、L5 断链）。

### 3.2 回归验证（删除完成后）
```bash
# 后端：确认 CUDA 相关测试与脚本已同步移除后全量绿
.venv/bin/python -m pytest backend -q
# 前端：build + 单测必须绿，且不依赖被删的 e2e
pnpm --dir frontend build
pnpm --dir frontend test
# 无残留引用（输出应为空）
grep -rniE "cuda_validation|run-cuda-validation|sanitize-cuda|windows-gpu-validation|cuda-e2e|cuda-workstation|cuda-fixture" backend/app backend/tests scripts .github frontend/src frontend/package.json docs 2>/dev/null | grep -v "superpowers/plans\|superpowers/specs"
```

### 3.3 已知边界 / 消除的假设
- `ci.yml` 无 CUDA 引用（已确认），只需删独立 workflow。
- `test_gpu_workflow.py` 删除后，其对 `ci.yml` 的断言（test_github_workflows_use_node24）一并消失——该断言无关 CUDA，若需保护可迁入 `test_production_static.py`，但按 YAGNI 不迁。
- 文档断链修复只改「指向过时 CUDA 路径」的引用，不改历史 plan/spec 内容（保留审计痕迹）。

---

## 4. 验收标准

1. 全仓（排除 `superpowers/plans|specs` 历史审计文档）无任何文件 import/引用 `cuda_validation` 或 8 个 CUDA 脚本名。
2. `pytest backend -q` 全绿。
3. `pnpm --dir frontend build` + `pnpm --dir frontend test` 全绿，无 cuda e2e 文件。
4. 无指向 6 篇 cuda 文档的断链（grep 确认）。
5. `README`/`docs/TODO.md`/`docs/release-governance.md`/`docs/ci-architecture.md` 的 CUDA 段落已更新为「已移除」，不再宣称 CUDA 认证路径，与 ComfyUI-only 定位一致。

---

## 5. 后续（不在本次范围）

- 前端转轨到 ComfyUI 路径（新增生成/服务接入面板，替换 `api.ts` 旧端点）——待独立 brainstorm/plan。
- 剩余 portable/worker/supervisor 生态清理——需先完成前端转轨，属后续阶段。
- 真实 GPU 端到端验收（ComfyUI→真实音频）——需 GPU 机器，独立跟踪。
