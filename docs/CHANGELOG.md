# 变更记录

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
