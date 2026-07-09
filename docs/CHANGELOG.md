# 变更记录

## 2026-07-09 — 非侵入式 TTS worker 架构 + 分支合并 + 跨平台优化

架构转向：从"Gradio scrape / fork 深度改造"转向"非侵入式嵌入式 worker 脚本"。三个开源 TTS 服务（GPT-SoVITS、IndexTTS、CosyVoice）现在通过在上游 repo 进程内 import 模型的 FastAPI worker 接入，暴露统一 `tts-more-v1` 契约 + 发现端点，**不改上游任何文件**，对上游官方版本也兼容。Gradio 保留为能力有限的兜底。

### 分支合并（A）
- 合并 `dev-xu/llm-first-parser`（28 提交，工作台简化 + LLM-first 解析器）到 master，解决 9 个冲突（2 安全相关：parser scrub、supervisor 命令白名单），安全特性全部保留。
- 删除两个已完全合并的死分支（`feature/hardening-generation-workbench`、`feature/ui-arch-refactor-and-storage-rework`）。

### 跨平台（B）
- 修复 `services.py` `_normalize_path_for_compare` POSIX 路径比较 bug（改为正斜杠中性形式）。
- `indextts_worker.py` venv 解释器解析加 `Path.exists()` 守卫，跨平台回退 `sys.executable`。
- Makefile `dev`/`workers` 目标按 OS 分支。

### 非侵入式 worker（C）
- **C1 GPT-SoVITS worker**（`gpt_sovits_worker.py`）：import `GPT_SoVITS.TTS`，标准契约 + `/models` `/models/{}/samples` `/status` `/upload_ref` 发现端点。模型名发现按权重文件名前缀（logs 名）配对，上游兼容。常驻 + 可 unload。
- **C2 CosyVoice worker**（`cosyvoice_worker.py`）：新建，4 模式（sft/zero_shot/cross_lingual/instruct），标准契约。
- **C3 IndexTTS 常驻**：适配器改为常驻模型（默认）+ 子进程 fallback（`TTS_MORE_INDEXTTS_RESIDENT=0`），`/unload` 释放显存。
- **C4 服务注册**：`data/services.json` 三个本地服务改为 `tts-more-v1` + `managed:true` + start_command；跨平台启动脚本（sh + ps1）；`make workers`；`open_source_tts.py` catalog worker 优先；`repo.lock.json` 补 CosyVoice。
- **C5 Gradio 兜底**：GradioWebUIServiceClient 标注为 LIMITED FALLBACK；`gpt_sovits_launcher.py` 标 LEGACY。
- **C6 文档**：新增 `docs/workers.md`（mermaid 架构），重写 `docs/gpt-sovits-integration.md`（worker-first）。
- **关键修复**：`models.py` `populate_compat_fields` 不再把 `tts-more-v1` 当未设置覆盖为 Gradio——显式 worker 契约现在正确保留并路由到 worker。

### CI/部署（D）
- `docs/ci-architecture.md` 更新：本机应用本体 + 网络接入 GPU 机器的部署模型。
- CI 验证应用本体（单元/安全/跨平台/worker 契约），真实推理手动验收（本机接入 GPU worker）。

### 测试
- 后端：241 passed, 2 skipped。新增 `test_workers.py`（worker 契约 + 发现，13 案例）。
- 前端：97 passed, build OK。

### 待 GPU 环境验证
- GPT-SoVITS worker 真实合成（`TTS.run` → wav）；
- CosyVoice 上游 import 路径 + 推理签名；
- IndexTTS 常驻模式推理。

契约与发现层已在 macOS 验证通过（无 GPU）。

---

## 2026-07-08 — 安全加固 + 跨平台标准化

一次集中的安全与工程治理 pass，按优先级分 8 个语义化提交推送到 master。

### P0 修复

- **测试跨平台崩溃**：`test_service_supervisor` 用 `monkeypatch` 改全局 `os.name="nt"`，导致 `pathlib.Path()` 在 POSIX 上构造 `WindowsPath` 抛 `NotImplementedError`，整个 pytest 会话 `INTERNALERROR`（只能在 Windows 上跑）。抽出 `_is_windows()` 函数，测试改 mock 它；顺带修了 `_resolve_path_env` 用宿主 `os.pathsep` 分割目标服务 PATH 的跨平台 bug。
- **SSRF 防护**：`/api/open-source-tts/detect` 与 `/api/parser/providers/test` 接受用户 URL 直接请求，可扫内网/云元数据。新增 `net_guard.validate_egress_url`（link-local 永远拒绝，覆盖 `169.254.169.254`）+ `scrub_error`（脱敏 Bearer/key/password）。
- **命令执行面**：`start_command[0]` 现经白名单校验（裸名或项目内路径），阻断任意二进制执行。
- **文件读根**：角色库配置的 `logs_root`/`weights_root` 必须在项目/数据根内或操作员白名单中，否则 `/api/audio`、`/api/assets/image` 不读；图片改 magic-byte 校验。
- **上传限制**：25 MiB 上限，超限 413。
- **可选认证**：`TTS_MORE_API_TOKEN` 未设置=开放；设置后写/出口端点强制 Bearer。前端 TokenGate 组件。

### P2 加固

- **DoS 防护**：作业队列有界（MAX_JOBS=200、MAX_ACTIVE_JOBS=8）、惰性清理、cancel 在 line 间生效。

### 跨平台

- 新增 `scripts/start-dev.sh`、`Makefile`、`.github/workflows/ci.yml`（ubuntu+windows 矩阵）。
- `.env.example` 路径改为跨平台默认。
- README 全面重写，bash + PowerShell 并列。

### 文档

- 新增 `docs/architecture.md`、`docs/security.md`（含 mermaid 图）。
- 重写 README、release-governance、open-source-tts-services，加 mermaid。

### 测试

- 后端：215 passed, 1 skipped（macOS）。新增 net_guard(24)、auth(9)、storage-security 扩展、queue DoS(3)、supervisor 命令白名单(3)。
- 前端：98 passed（含 token 存储 3）。

### 安全 env 速查

`TTS_MORE_API_TOKEN`、`TTS_MORE_ALLOWED_EXECUTABLES`、`TTS_MORE_ALLOWED_DATA_ROOTS`、`TTS_MORE_MAX_UPLOAD_BYTES`、`TTS_MORE_MAX_JOBS`、`TTS_MORE_MAX_ACTIVE_JOBS`、`TTS_MORE_JOB_RETENTION_SECONDS`。
