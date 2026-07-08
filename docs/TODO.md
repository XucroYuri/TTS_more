# 待决策事项

本文件记录需要人工（项目维护者）决策、但当前不阻塞主线的事项。Agent 可参考但不应自行决定。

## 需维护者醒来处理

### 1. `prepare-models.ps1` 深度跨平台

`scripts/prepare-models.ps1` 含 CUDA（`+cu126`/`+cu128`）、uv-Windows 布局、`ffmpeg.exe`/DLL 等 Windows 专属逻辑，重写为 macOS MPS / Linux CUDA 通用脚本风险较高。当前仅在 README 注明"Windows 专属，macOS/Linux 手动准备模型"。

**决策点**：是否要做一个 Python 跨平台版（modelscope 下载 + 通用 pip）？需要真实 GPU 环境验证，Agent 无法独立完成。

### 2. 生产部署认证

单共享 Token（`TTS_MORE_API_TOKEN`）适合本地/小团队。公网暴露需真实认证（OAuth / 反向代理 + Identity Provider）。

**决策点**：是否有公网部署计划？若有，选哪种认证方案？

### 3. 未合并 feature 分支

- `dev-xu/llm-first-parser`：领先 master 28 提交，工作台/解析器重构主力。
- `feature/ui-arch-refactor-and-storage-rework`：落后 13，改 59 文件。
- `feature/hardening-generation-workbench`：落后 24，改 68 文件。

**决策点**：合并策略（直接 merge / rebase / cherry-pick / 废弃）。本轮未碰分支。

### 4. `frontend/design.md`

当前是第三方 Vercel "Geist" 设计 token dump（YAML front-matter），非项目专属设计文档。

**决策点**：是否替换为项目专属设计说明，或保留作为设计参考？

### 5. 真实 TTS 端到端验收

`test_real_tts_validation.py` 默认 skip，需要本机/网络 endpoint、大模型、GPU。Agent 无法独立验证。

**决策点**：在 CI 上跑真实验收（需自托管 GPU runner）还是保持手动？
