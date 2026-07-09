# 待决策事项

本文件记录需要人工（项目维护者）决策、但当前不阻塞主线的事项。Agent 可参考但不应自行决定。

## 状态总览

| # | 事项 | 状态 |
|---|---|---|
| 1 | 应用本体跨平台 / 下级 repo 的 CUDA 依赖 | ✅ 已核验 + 跨平台修复（路径 normalize/venv 守卫/Makefile） |
| 2 | GPT-SoVITS 接入能力与上游兼容性 | ✅ 已解决（非侵入式 worker，见 `docs/workers.md`） |
| 3 | 未合并 feature 分支 | ✅ dev-xu 已合并；两个死分支已删除 |
| 4 | `frontend/design.md` 过时 | ✅ 已删除 |
| 5 | 真实 TTS 端到端 CI | 📋 部署模型已定（本机应用 + 网络接入 GPU 机器），见 `docs/ci-architecture.md` |

---

## 1. 应用本体跨平台 ✅

**结论：应用本体（`backend/app` + `frontend/src`）是跨平台兼容的，不含 CUDA/torch 硬依赖。**

核验结果：

- `backend/pyproject.toml` 依赖只有 `fastapi`/`uvicorn`/`pydantic`/`httpx`/`PyYAML`/`python-dotenv`/`python-multipart`，**无 torch/CUDA**。
- `backend/app/hardware.py`：`nvidia-smi` 是**可选探测**（`shutil.which` 找不到就返回 `unavailable`），非硬依赖。`os.getloadavg()` 用 `hasattr` 守卫（Windows 无此函数）。
- `backend/app/workers/indextts_line_launcher.py`：`--cuda-kernel`/`--fp16`/`--deepspeed` 是**透传给 IndexTTS 库的 CLI 参数**，只在子进程 worker 里 `from indextts.infer_v2 import IndexTTS2`，**不在 Web 应用进程内**。
- `backend/app/main.py` 的 runtime checks 是**子进程运行时探测**（检查 GPT-SoVITS 分支 repo 的 venv 是否装了 torch），应用本体不 import torch。

**CUDA/uv-Windows 专属逻辑只存在于**：(a) 可选硬件探测（优雅降级）；(b) 子 worker launcher（独立进程）；(c) `scripts/prepare-models.ps1`（Windows 模型准备脚本，非应用本体）。这些在文档层面已记录，待运行环境符合时验证。

详细跨平台说明见 `docs/architecture.md` 的"跨平台"章节。

---

## 2. GPT-SoVITS 接入能力分析 📋

详见 `docs/gpt-sovits-integration.md`。核心结论：

- **合成（生成音频）对上游官方 GPT-SoVITS 可行**：Gradio `get_tts_wav` + `change_*_weights` 或 api-v2 `/tts` + `/set_*_weights` 都是上游端点。
- **模型/参考音频自动发现是 fork 专属**：`on_select_ref_audio`、`update_model_choices`、api-v2 `/models`、`/models/{}/samples` 都是 fork 新增，上游没有。
- **局域网分布式部署的短板**：远端 GPT-SoVITS 的 `logs/` 目录 TTS More 无法访问，自动发现失效；参考音频需手动输入；上游 3–10s 参考音频硬限制会阻断。
- **方案**：要么部署 fork，要么在任意 GPT-SoVITS 构建上实现 `gpt-sovits-fork-enhancement.md` 的四个端点。

---

## 3. 未合并 feature 分支方案 📋

经 git 三方合并分析：

| 分支 | 状态 | 处理 |
|---|---|---|
| `feature/hardening-generation-workbench` | **已完全合并入 master**（tip 是 master 祖先） | 可直接删除 |
| `feature/ui-arch-refactor-and-storage-rework` | **已完全合并入 master** | 可直接删除 |
| `dev-xu/llm-first-parser` | 领先 28 提交，落后 8（安全提交），**唯一活跃分叉** | 需合并（见下） |

`dev-xu/llm-first-parser` 含 LLM-first 解析器 + 工作台简化（28 个提交，主题集中），且已包含另两个分支的全部内容。合并冲突面很小：仅 8 个文件，其中安全相关 2 个（`parser.py` 的 SSRF 脱敏、`supervisor.py` 的命令白名单），其余是文档/测试/脚本。

**推荐方案（merge，非 rebase）**：
1. 先删除两个已合并的死分支（`feature/hardening-generation-workbench`、`feature/ui-arch-refactor-and-storage-rework`）。
2. `git merge dev-xu/llm-first-parser` 到 master。
3. 解决 8 个冲突：`parser.py` 保留 master 的 `scrub_error` + dev-xu 的 LLM-first 逻辑；`supervisor.py` 保留 master 的命令白名单；文档/测试取 dev-xu 版本后补回 master 的安全说明。
4. 跑全量测试确认。

rebase 会让 28 个提交逐个撞安全代码（冲突分散），merge 更干净。cherry-pick 不适合（24 个 `ref(workbench)` 提交相互依赖）。**此项需维护者执行**（涉及分支删除与合并决策）。

---

## 4. `frontend/design.md` ✅

已删除（第三方 Vercel Geist 设计 token dump，已过时）。README 引用已移除。

---

## 5. 真实 TTS 端到端 CI 📋

详见 `docs/ci-architecture.md`。核心探讨：当前 CI 在 GitHub-hosted ubuntu/windows runner 上跑单元测试（无 GPU）。真实 TTS 验收需要大模型 + GPU，三种部署架构的得失：

| 方案 | 得 | 失 |
|---|---|---|
| 自托管 GPU runner | 真验收、防回归 | 需维护 GPU 机器、runner 注册、成本 |
| 手动验收（现状） | 零 CI 成本 | 回归风险、依赖人 |
| 容器化 GPU + 模型缓存 | 可复现、可调度 | 镜像大、模型下载慢、复杂 |

推荐：保持单元测试在 hosted runner，真实验收用**自托管 GPU runner + 模型缓存卷**，仅在 release 前触发（非每次 push）。
