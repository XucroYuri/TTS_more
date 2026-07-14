# Windows 四仓双形态便携部署设计

## 目标

TTS More、GPT-SoVITS main、IndexTTS 和 CosyVoice 各自成为可独立构建、初始化、启动和停止的 Windows x64 组件。四仓使用同一发行版本与 `tts-more-v1` 服务契约，但不要求部署在同一目录或同一主机。

每个组件生成两种互斥产物：

- `bootstrap` 发布到 GitHub Releases，只包含源码、构建后的静态资源、锁文件、自举器、许可证和资产清单。首次启动在包内补齐私有运行时、精确锁定依赖和模型，成功后可离线运行。
- `full` 只在本地生成，预置私有运行时、依赖和默认模型。解压后断网启动，不得进入 GitHub Release 上传路径。

源码仓库只提交可重建配方，不提交环境、模型、缓存、用户数据或生成 ZIP。

## 包协议

`tts-more-package.json` schema v2 是四组件的统一发现与构建接口。它声明组件、发行版本、构建 ID、`bootstrap|full` profile、Windows x64 平台、源码 revision、集成版本、设备 profile、依赖锁与模型锁摘要、相对数据目录、初始化状态、启动器、endpoint、能力和许可证清单。

所有文件路径必须相对包根目录，禁止盘符、用户名、主机名和 `..`。TTS More 可以读取旧 schema v1，所有新构建只生成 v2。

四仓根入口统一为：

- `Initialize.cmd`：显式运行可恢复初始化事务；
- `Start.cmd`：必要时自动初始化，等待服务真正 ready 后返回；
- `Stop.cmd`：验证进程所有权并等待端口释放；
- `Repair.cmd`：只重新获取缺失或损坏的受控资产；
- `Build-Package.ps1`：构建 `Bootstrap|Full` 和 `Auto|CU128|CU126|CPU` 组合。

默认端口是 TTS More 8000、GPT-SoVITS 9880、IndexTTS 9881、CosyVoice 9882。未知端口占用只报告，不终止占用者。loopback 可使用受控 path delivery；可信 LAN 强制 artifact delivery，且外部服务为 `managed:false`。

## 初始化与锁定

初始化固定经过：磁盘和路径预检、Windows CIM 设备检测、选择锁文件、断点下载到 `.partial`、SHA-256 校验、临时运行时构建、依赖与设备探针、模型校验、原子提升为 live 目录、写入 `install-state.json`。失败不得覆盖上一个可用 live 状态。

TTS More、GPT-SoVITS 和 IndexTTS 使用 Python 3.11；CosyVoice 使用 Python 3.10。Node 和 pnpm 只参与 TTS More 前端构建。设备自动选择顺序是 CU128、CU126、CPU；显式 CUDA profile 探针失败时不得静默降级。

依赖锁必须固定全部传递依赖和哈希。GPT-SoVITS main 至少固定 FastAPI 0.115.2、Starlette 0.40.0、Gradio 4.44.1、Pydantic 2.10.6，并包含 CUDA profile 对应的 Torch、TorchCodec、ONNX Runtime 和 FFmpeg。模型锁使用官方不可变 revision、逐文件哈希、大小、相对目标和许可证；可变 `main` 或 `master` 不能出现在运行时解析路径中。

## 受控镜像

TTS More 是 `tts_more_worker`、公共打包核心、schema 和组件适配器的规范源。同步器只写三个 fork 的 `tts_more/` 受控目录和根包装入口，不再使用 `.git/info/exclude` 隐藏集成代码。

每个 fork 提交 `tts_more/integration.manifest.json`，记录集成版本、TTS More 源 commit、受控文件列表和哈希。CI 运行 `sync-integrations --check`，任何手工漂移都会失败。更新顺序固定为 TTS More 规范源、三个 fork 镜像 PR、TTS More 锁提交收口 PR。

GPT-SoVITS 正式基线为 main；dev 仅用于预览和回归。三个 fork 的 `Start.cmd` 启动标准 worker，原生 Gradio WebUI 保留独立入口。

## Windows 可靠性

生产服务禁止 Uvicorn `--reload`。启动器把 stdout/stderr 写入包内日志，并按进程存活、监听建立、`/health ready=true` 三阶段等待。PID 记录包含 PID、进程创建时间、父子进程、可执行路径、命令摘要、包根、端口和 build ID。

停止器只终止与记录完全匹配的进程树，等待端口释放后再删除记录；陈旧记录、所有权变化和残留监听都以非零状态退出。GPU 初检不直接依赖 `nvidia-smi.exe`，最终判定由包内 Torch 和 ONNX 探针完成，避免 Windows loader 弹窗。

TTS More 生产包由 FastAPI 托管构建后的 SPA，统一使用端口 8000，不携带 Node、不运行 Vite。开发态保留显式 `Start-Dev.cmd`。

## 发布与验收

四仓使用同步版本 tag。每个 GitHub Release 只发布本仓 bootstrap ZIP、SHA-256、SBOM、许可证清单和脱敏验证报告。full 包输出到 git-ignore 本地目录，发布工作流检测到 full profile 必须失败。

验收覆盖 schema、锁漂移、镜像哈希、下载恢复、损坏修复、中文/空格/不同盘符路径、端口冲突、陈旧 PID、无系统 Python/Conda/Node、CU128/CU126/CPU 探针、真实模型加载和合成、离线 full 包、TTS More 包发现及可信 LAN artifact 传输。

TTS More 自有代码与集成层采用 Apache-2.0；上游代码和模型保留原许可证与 NOTICE。
