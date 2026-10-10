# Windows 工作台启动 / Windows workstation startup

完成 Python 3.11、Node >=20、pnpm 依赖、ComfyUI 和本机资源注册配置后，在仓库根目录运行 `Start-TTSMore.bat`。默认打开浏览器；所有新服务绑定 `127.0.0.1`。

After installing dependencies and registering local TTS resources, run `Start-TTSMore.bat` from the repository. New services bind to loopback and the browser opens after readiness succeeds.

```powershell
.\Start-TTSMore.bat --check
.\Start-TTSMore.bat --plan
.\Start-TTSMore.bat --no-browser
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-workstation-launcher.ps1
```

最后一条命令在仓库上一级安装入口，安装前备份已有文件。安装入口调用仓库内的启动器，后续拉取源码即可更新启动逻辑。当前机器的入口为 `E:\Start-TTSMore.bat`，原入口保留于 `E:\Start-TTSMore.before-20261010.bat`。

The installer creates a wrapper next to the checkout and backs up an existing wrapper. It delegates to the versioned repository launcher.

## 端口与实例 / Ports and instances

- 默认首选端口：后端 `8000`、前端 `5173`、ComfyUI `8188`。占用时检查首选端口及后续最多 300 个端口；绑定时发生抢占最多重试 3 次。
- 按命令行、可执行文件和父进程创建时间验证归属；不依赖陈旧 PID 文件，不终止其他程序。
- 复用本项目健康实例，包括非默认端口。ComfyUI 还须提供 bridge capabilities 和所需资源；前端代理必须指向当前后端实例。
- ComfyUI 新实例使用独立 SQLite 文件，避免默认数据库被另一实例锁定。
- 自动更新本机 ComfyUI 服务地址，保留远程端点、自定义端口及 portable 管理端点；配置通过现有锁和原子写入保存。
- 生成中重复点击会等待启动互斥锁，随后复用实例。仅在路由地址确实不一致且队列空闲时重载后端配置。
- 单个服务未就绪会报告对应日志；配置或依赖错误立即失败，不反复重试。已经就绪的实例保留，修复后可再次点击。

Preferred ports fall back automatically. Owned healthy instances are reused, frontend proxy identity is checked, and bind races are retried. Existing processes are preserved. Routing reloads wait for an idle generation queue. Dependency failures include log paths.

这提供可靠的启动与重复启动行为。已经归属本项目但无法通过就绪检查的后端会明确报错，避免对同一项目创建第二个有状态调度器；需要先检查该实例日志。脚本不是持续运行的故障监控服务，也不能替代模型、驱动和依赖安装。

An unhealthy owned backend fails explicitly to avoid duplicate stateful schedulers. This launcher does not provide a permanent watchdog or install missing model dependencies.

## 本机配置 / Local settings

| 环境变量 / Variable | 用途 / Purpose |
| --- | --- |
| `TTS_MORE_PROJECT_ROOT` | 外部入口的仓库路径 / Checkout for external wrapper |
| `TTS_MORE_COMFYUI_ROOT` | ComfyUI 根目录；本机默认 `D:\ComfyUI-master` / ComfyUI directory |
| `TTS_AUDIO_SUITE_RESOURCES` | 资源 YAML 绝对路径；默认 `data/local/tts-audio-suite-resources.yaml` / Resource registry |
| `TTS_MORE_BACKEND_PORT` | 首选后端端口 / Preferred backend port |
| `TTS_MORE_FRONTEND_PORT` | 首选前端端口 / Preferred frontend port |
| `TTS_MORE_COMFYUI_PORT` | 首选 ComfyUI 端口 / Preferred ComfyUI port |
| `TTS_MORE_API_TOKEN` | 已启用认证时的后端令牌 / Existing API token |
| `TTS_MORE_LAUNCH_LANGUAGE` | `en` 使用英文；默认中文 / English or default Chinese |

PowerShell 高级调用支持 `-ProjectRoot`、`-ComfyRoot`、三个端口参数、`-TimeoutSeconds`（默认 180 秒）、`-Mode check/plan/start`、`-NoBrowser`。`--plan` 不启动服务或改写配置；`--check` 检查必需文件与运行时依赖，不加载 TTS 模型。

PID、就绪状态和实际地址写入 `data/local/run/tts-more.pid.json`；日志在 `data/local/run/one-click/`。前端代理自动使用实际后端端口。脚本文件保留 UTF-8 BOM，兼容 Windows PowerShell 5.1 的中文解析。
