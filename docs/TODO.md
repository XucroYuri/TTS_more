# 待决策事项

本文件记录需要人工（项目维护者）决策、但当前不阻塞主线的事项。Agent 可参考但不应自行决定。

## 状态总览

| # | 事项 | 状态 |
|---|---|---|
| 1 | 应用本体跨平台 / 下级 repo 的 CUDA 依赖 | ✅ 已核验 + 跨平台修复（路径 normalize/venv 守卫/Makefile） |
| 2 | GPT-SoVITS 接入能力与上游兼容性 | ✅ 已解决（非侵入式 worker，见 `docs/workers.md`） |
| 3 | 未合并 feature 分支 | ✅ dev-xu 已合并；两个死分支已删除 |
| 4 | `frontend/design.md` 过时 | ✅ 已删除 |
| 5 | Windows CUDA 全流程认证 | 🔬 自动化与文档已建立，待真实单机和四机首次认证 |
| 6 | macOS 控制面 + LAN Windows CUDA | 📐 补充门禁方案已设计，待真实执行和跨平台编排实现 |

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

## 2. GPT-SoVITS 接入能力分析 ✅

详见 `docs/gpt-sovits-integration.md`。当前结论：

- **主路径**：使用 TTS More 的非侵入式 `tts-more-v1` worker，直接 import 上游模型类，不要求改 GPT-SoVITS 仓库。
- **兼容路径**：用户已有 Gradio WebUI 时仍可接入，但自动发现能力少于 worker。
- **子仓 prompt**：`docs/agent-prompts/gpt-sovits-fork-enhancement.md` 只用于明确维护 GPT-SoVITS 子仓的场景，不是 TTS More 本仓默认任务。

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

## 5. Windows CUDA 首次认证 🔬

代码侧的 topology、三 worker 工件协议、共享资源切换、CUDA 判定器、报告格式和 runbook 不再列为 TODO。剩余工作必须在真实硬件上完成：

1. 准备 Windows 11/Server、CUDA 12.8、至少 16 GB VRAM 的单机 runner，并注册标签 `[self-hosted, Windows, X64, cuda, tts-more-gpu]`。
2. 在 runner 本地创建被忽略的 topology、repo 路径确认文件和 validation fixture，准备三服务参考音频及 GPT `v2ProPlus`/`v2Pro` 权重。
3. 完成第一次 `single-clean`，由两名审核者签核，建立 16 GB 冷加载、短句、warm p95 和显存恢复基线。
4. 准备一台控制节点和三台独立 GPU worker，配置 Windows OpenSSH、DNS/时间同步、端口与防火墙。
5. 在真实四机上完成第一次 `distributed` 认证，确认已实现的 30 条重叠检测、远端证据采集和 15 秒故障恢复脚本通过 Windows OpenSSH 实测。
6. 审核私有 CI 工件的脱敏和访问控制，保存单机/分布式运行 URL 与人工听审记录。
7. 首次两类认证均通过后，才将其启用为稳定发布的强制门禁。

执行说明见 [CUDA 验证总入口](cuda-e2e-validation.md)、[单机 runbook](cuda-e2e-single-node.md) 和 [四机 runbook](cuda-e2e-distributed.md)。

---

## 6. macOS 控制面与 LAN Windows CUDA 📐

已确定两级拓扑：当前 macOS 运行完整应用，一台 Windows GPU 主机承载三个服务用于共享资源组验证；随后使用三台 Windows GPU 主机各承载一个服务完成并行和故障隔离验证。远端控制固定使用密钥认证、host key 固定的 Windows OpenSSH，音频使用 `artifact-transfer`，不依赖共享文件系统。

当前阶段只作为可审计补充门禁。升级为稳定发布门禁前仍需：

1. 抽取跨平台 Python 编排核心，提供 POSIX 和 PowerShell 薄入口；
2. 去除控制节点必须为 Windows、必须有本地 `nvidia-smi` 的假设；
3. 为共享 GPU 和三 GPU topology 分别实现加载互斥、UUID 唯一性和性能规则；
4. 自动完成远端 clean deploy、commit 核对、监控、故障注入和证据回收；
5. 让一次性 preflight 同时绑定 topology、fixture、commit 和 SSH host key 哈希；
6. 在真实 LAN 上先完成共享 GPU 补充认证，再完成三 GPU 首次认证和两名审核者签核；
7. 与 Windows 控制节点的正式结果对比，确认没有未解释差异后再修改发布治理。

完整设计和阶段一操作见 [macOS LAN CUDA 验证](cuda-e2e-macos-lan.md)。
