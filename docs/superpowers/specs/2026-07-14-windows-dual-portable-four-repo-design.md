# Windows 四仓双形态便携部署设计

状态：已批准
日期：2026-07-14

## 1. 目标与交付定义

TTS More、GPT-SoVITS main、IndexTTS 和 CosyVoice 各自成为可独立构建、初始化、启动、停止、修复和迁移的 Windows x64 组件。支持范围为 Windows 10 22H2 和 Windows 11 x64。四仓使用同一发行列车版本与 `tts-more-v1` 服务契约，但不要求位于同一目录或同一主机。

普通用户的唯一主路径是：解压 ZIP，然后双击该组件根目录的 `Start.cmd`。用户不需要先运行 `Initialize.cmd`，也不需要预装或配置 Python、Conda、Node、Git、CUDA Toolkit、FFmpeg 或模型下载工具。

每个组件生成两种互斥产物：

- `bootstrap` 发布到 GitHub Releases，不包含本地运行环境和模型权重。首次运行 `Start.cmd` 时自动补齐精确锁定的运行时、依赖和模型；初始化成功后可以离线运行。
- `full` 只在本地生成，预置私有运行时、依赖和默认模型。解压后断网直接运行，不得进入 GitHub Release 上传路径。

四仓保持独立启动，不由 TTS More 隐式批量启动。TTS More 工作台维护三个 TTS 组件的本机路径，并为每个组件分别提供启动、停止、修复和日志快捷按钮。

正式交付用语固定为：

- Bootstrap：“解压、双击 Start、首次联网自动准备，之后可离线。”
- Full：“解压、双击 Start、全程断网可用。”

源码仓库只提交可重建配方，不提交环境、模型、缓存、用户数据或生成 ZIP。

## 2. 统一公开入口

四仓根目录统一提供：

- `Start.cmd`：普通用户唯一主入口；必要时自动初始化，等待服务真正 ready 后返回。
- `Stop.cmd`：验证进程所有权并等待端口释放。
- `Repair.cmd`：校验并重新获取损坏或缺失的受控资产，保留用户数据。
- `Initialize.cmd`：高级用户显式执行初始化或更换设备配置。
- `Build-Package.ps1 -Profile Bootstrap|Full -Device Auto|CU128|CU126|CPU`：构建本组件包。
- `Start-WebUI.cmd`：仅三个 TTS fork 提供，用于启动上游原生 WebUI；`Start.cmd` 始终启动 `tts-more-v1` worker。
- `使用说明-先看这里.txt`：只描述解压和双击 `Start.cmd` 的普通流程，把修复、代理和设备选择放入高级章节。

TTS More 另提供 `build-four-pack.ps1`，一次生成同一发行列车版本的四个本地 Full ZIP。开发入口保持显式 `Start-Dev.cmd`，不得与生产入口混用。

默认端口为 TTS More 8000、GPT-SoVITS 9880、IndexTTS 9881、CosyVoice 9882。端口可以在本机配置中覆盖，但未知端口占用只报告所有者，绝不终止未知进程。

## 3. 用户视图与包内目录

每个 ZIP 解压后只产生一个顶层目录。Git 仓库继续保持正常源码结构，只有构建产物按下列用户视图整理：

```text
组件名称/
├── Start.cmd
├── Stop.cmd
├── Repair.cmd
├── Initialize.cmd
├── Build-Package.ps1
├── 使用说明-先看这里.txt
├── Start-WebUI.cmd          # 仅三个 TTS 包
├── app/                     # 应用源码，普通用户无需进入
├── package/                 # manifest、provenance、兼容矩阵
├── runtime/                 # 私有运行环境
├── models/                  # 模型或模型映射
├── data/
│   ├── user/                # 项目、参考音频、生成结果和用户模型
│   ├── local/               # 本机路径、安装状态、PID 和操作记录
│   └── cache/               # 可重建下载缓存
└── licenses/                # 上游代码、集成层和模型许可证
```

所有运行文件必须位于包根目录内。不得写入系统 Python、系统 Conda、注册表、Program Files 或用户全局缓存。所有入口默认不请求管理员权限，不自动开放 Windows 防火墙。包根、`runtime/` 和 `data/` 必须可写；如果用户从只读介质、受保护目录或 ZIP 预览中直接启动，预检必须停止并提示先完整解压到可写目录，不得请求提权。

“只读源码区”支持范围仅指构建完成后的 `app/` 可以只读；Bootstrap 初始化目录、运行时和数据目录不能位于只读介质。

TTS More 生产包由 FastAPI 托管构建后的 SPA，统一使用端口 8000，不携带 Node，不运行 Vite，也不启用 Uvicorn `--reload`。

## 4. Package schema v2

`package/tts-more-package.json` schema v2 是四组件的统一发现、兼容和构建接口。TTS More 可以读取旧 schema v1，但所有新构建只生成 v2。

v2 至少声明：

- `schema_version`、`component`、`package_id`、`release_version` 和 `build_id`；
- `package_profile`，值为 `bootstrap|full`；
- 平台、架构、源码 revision 和集成版本；
- `tts-more-v1` 协议版本与兼容范围；
- Python 版本、设备 profile、依赖锁摘要和模型锁摘要；
- 相对数据目录、安装状态路径和操作记录目录；
- 初始化、启动、停止、修复和原生 WebUI 入口；
- 默认 endpoint、能力、许可证和来源清单。

manifest 中所有文件路径必须相对包根目录，禁止盘符、用户名、主机名和 `..`。本机绝对路径只允许写入 git-ignore 的 `data/local/services.json`，不得写入 manifest、锁文件、ZIP 或发布证据。

## 5. 统一启动控制协议

普通用户直接双击与 TTS More 快捷启动必须走相同代码路径，避免出现两套初始化或启动逻辑。

```text
Start.cmd
  -> 创建或接收 operation ID
  -> 获取包级单实例锁
  -> 检查 manifest、安装状态和现有进程
  -> 必要时执行事务初始化或修复
  -> 启动服务
  -> 等待健康检查 ready
  -> 显示地址或自动打开 TTS More 页面
```

`Start.cmd` 无参数时打开 Windows 原生中文进度窗口；图形窗口不可用时退回中文控制台。TTS More 调用对应仓库的 `Start.cmd` 时传入内部操作 ID 与非交互标记，在工作台内显示同一操作的进度，不复制安装逻辑。

每次操作写入：

```text
data/local/operations/<operation-id>/
├── operation.json
├── events.jsonl
└── launcher.log
```

`operation.json` 至少记录组件、build ID、动作、发起方式、开始时间、当前状态和最终退出码。`events.jsonl` 的每条事件至少包含递增序号、时间、阶段、可选百分比、用户可读消息、可选错误编号和详细日志引用。

统一状态为：

- `not_initialized`
- `checking`
- `downloading`
- `installing`
- `validating`
- `starting`
- `ready`
- `stopped`
- `repairable`
- `blocked`

重复运行 `Start.cmd` 时，如果同一包正在初始化或启动，第二个入口接入现有 operation 并显示同一进度；如果服务已经 ready，则返回成功并显示现有地址，TTS More 直接打开现有页面。不得创建重复事务或重复进程。

用户关闭进度窗口时必须先显示“最小化、继续后台运行、取消操作”选择。取消操作在安全检查点终止当前下载或 staging 事务，保留 `.partial` 与已校验缓存，将状态写为 `repairable`，不得删除上一个可用 `live`。

TTS More 就绪后自动打开浏览器。三个 worker 就绪后显示服务地址和复制地址入口，但不自动打开上游 WebUI。

## 6. Bootstrap 初始化事务

初始化顺序固定为：

1. 检查目录可写、路径长度、磁盘空间和网络。
2. 通过锁文件计算准确下载量、缓存空间、临时安装空间和最终占用。
3. 使用 Windows CIM 进行硬件初检，根据认证矩阵选择设备锁。
4. 下载运行时、依赖和模型到 `.partial`，支持断点续传。
5. 对每项资产执行 SHA-256、大小和目标路径校验。
6. 在 `staging` 中构建私有运行时、安装依赖并校验模型。
7. 执行 `pip check`、核心 import、Torch、ONNX、FFmpeg 和设备探针。
8. 所有探针通过后原子切换到 `live`。
9. 写入 `data/local/install-state.json`。

失败不得覆盖上一个可用 `live`。模型和运行时的半成品不得被启动器视为可用。

下载策略：

- 官方不可变来源优先；失败后只允许切换到锁文件列出的同哈希镜像。
- Hugging Face、ModelScope 和镜像站不得在运行时解析可变 `main` 或 `master`。
- 下载进度显示当前文件、已完成大小、总大小、速度和预计时间。
- 网络中断保留 `.partial`；再次运行 `Start.cmd` 自动续传。
- 成功后的内容寻址缓存保留在 `data/cache`，供修复和版本导入复用；高级设置可以清理。
- 默认读取 Windows 系统代理和标准代理环境变量。所有自动源均失败后才显示手动代理输入。

`Repair.cmd` 只重新获取或重建缺失、损坏和探针失败的受控资产，不删除 `data/user`。再次运行 `Start.cmd` 也会自动进入必要的修复事务，不要求普通用户判断应该先运行哪个脚本。

## 7. 设备选择

Bootstrap 默认显示“自动（推荐）”。初始化前根据显卡类型、驱动版本、组件支持列表和认证矩阵，按 CU128、CU126、CPU 的顺序选择最高兼容配置。

GPU 初检不直接执行 `nvidia-smi.exe`。最终设备能力由包内 Torch 和 ONNX 探针确认，避免系统 loader 弹窗与系统 CUDA 漂移。

普通界面使用“使用 NVIDIA GPU 加速”或“使用 CPU 兼容模式”，CU128、CU126、Torch 和 ONNX 版本放在折叠的详细信息中。

显式 CUDA profile 探针失败时必须退出，不得降级。Auto 预选后的真实探针失败时不得静默重装 CPU；操作进入 `repairable`，界面提供“改用兼容模式并重试”。更换硬件后复用旧包时遵循同一规则，保留模型和用户数据。

Bootstrap 每个组件只生成一个 ZIP，内含全部已经认证的设备锁。TTS More 本体不依赖 CUDA。三个 TTS 的 Full 包必须包含并标明一个实际设备配置，最终文件名不得使用含糊的 `Auto`：

```text
TTS-More-0.2.0-windows-x64-full.zip
GPT-SoVITS-0.2.0-windows-x64-full-cu128.zip
IndexTTS-0.2.0-windows-x64-full-cu128.zip
CosyVoice-0.2.0-windows-x64-full-cu128.zip
```

未通过真实硬件验证的设备 profile 不得出现在 manifest 支持列表或正式文件名中。

## 8. Full 包离线边界

Full 包启动时只做快速完整性检查，禁止联网补齐后继续声称为 Full。缺少必要运行时、依赖或模型时必须拒绝启动并报告具体损坏项。

为避免成倍增加包体积，Full 包不重复保存整套恢复副本。离线严重损坏时，从原始 Full ZIP 解压到新目录，再通过受控迁移导入 `data/user`。如果用户主动联网运行 `Repair.cmd`，仍只能获取锁文件中相同哈希的资产，不改变包的原始 provenance；修复后的状态必须明确记录。

## 9. TTS More 本机服务维护

TTS More 工作台新增“本地 TTS 服务”区域，为 GPT-SoVITS、IndexTTS 和 CosyVoice 各提供一张独立卡片。每张卡片显示：

- 当前路径、组件版本和兼容性；
- Bootstrap/Full 类型和 CPU/CU126/CU128 配置；
- 初始化、操作和运行状态；
- 服务地址、端口和健康状态；
- 启动、停止、修复、打开服务、打开目录和查看日志按钮。

三仓保持分别启动，不提供隐式“全部启动”。快捷按钮调用该包受控的根入口。

本机服务配置写入 `data/local/services.json`。每项保存组件身份与可迁移定位器，而不是只保存一个绝对路径：

```text
component
package_id
relative_to_tts_more
absolute_path_last_seen
build_id_last_seen
port_override
```

路径解析顺序固定为：

1. 尝试相对 TTS More 的路径。
2. 尝试上次绝对路径。
3. 扫描 TTS More 同级目录中的 package manifest。
4. 等待用户使用 Windows 文件夹选择器重新定位。

不得递归扫描整台电脑。整体复制四个目录到另一块硬盘、盘符变化或目录改名后，应优先通过相对定位和 package identity 自动恢复。

路径保存前必须验证真实目录、组件身份、manifest、集成版本和受控入口。配置只允许选择包根目录，不允许指定任意命令、EXE 或脚本。

本地路径控制 API 只接受 loopback、同源且携带本次页面控制令牌的请求。Windows 文件夹选择器也只允许由 loopback 页面触发。LAN 注册服务始终是 `managed:false`，只能检查、使用和打开地址，不得远程执行启动、停止、修复或文件选择。

## 10. 进程、端口与健康检查

启动器按进程存活、监听建立、健康端点 ready 三阶段等待。创建进程不代表启动成功。

PID 记录至少包含 PID、进程创建时间、父子进程、可执行路径、命令摘要、包根、端口和 build ID。停止器只终止与记录完全匹配的进程树，等待端口释放后才删除记录。

陈旧 PID、所有权变化、端口被其他程序占用和停止后仍有监听都以明确的非零状态与错误编号结束。未知进程永远不被终止。TTS More 重启后结合操作记录、PID 所有权和 `/health` 恢复真实状态。

loopback 服务可以使用受控 path delivery。显式可信 LAN 模式强制 artifact delivery，外部服务保持 `managed:false`，本设计不承诺公网部署。

## 11. 普通用户错误体验

默认进度只显示用户能理解的阶段：检查电脑、下载运行组件、下载语音模型、安装、验证加速和启动服务。技术细节默认折叠。

每个错误必须包含：

```text
发生了什么
可能原因
程序没有修改什么
建议执行的下一步
错误编号
```

允许的恢复动作包括重试、修复、改用兼容模式、更换尚未写入用户数据的目录、查看详细日志和导出诊断包。默认界面不得只显示 PowerShell 堆栈。

诊断 ZIP 可以包含版本、状态、错误事件、锁摘要和探针结果，但必须移除用户名、完整机器路径、音频内容、密钥、代理凭据、主机名和 GPU UUID。

Windows 图形进度窗口不可用时退回中文控制台，功能和退出码保持一致。初始化或启动失败前，窗口不得自动消失。

## 12. 版本兼容与升级迁移

四仓使用同一发布列车版本。TTS More 根据协议版本和兼容范围判断能否管理组件。版本不兼容时仍允许用户从该组件目录独立运行，但 TTS More 不执行快捷控制，并明确提示需要匹配版本。

v2 不进行覆盖式自动更新。新版本解压到新目录，旧版本保留用于回退。首次启动可以选择“从旧版本导入”：

- 复制 `data/user`，不删除旧数据；
- 只有 SHA-256 与新锁完全一致的缓存或模型才允许复用；
- 运行时锁不同则重新构建，不复制旧虚拟环境；
- 不迁移绝对路径、PID、安装状态和进行中的操作记录；
- 新版本完成真实健康和业务验证后，再由用户自行删除旧目录。

## 13. 依赖、模型与受控镜像

TTS More、GPT-SoVITS main 和 IndexTTS 使用 Python 3.11；CosyVoice 使用 Python 3.10。Node 和 pnpm 只参与 TTS More 前端构建。

依赖锁必须固定全部传递依赖和哈希。GPT-SoVITS main 至少固定 FastAPI 0.115.2、Starlette 0.40.0、Gradio 4.44.1、Pydantic 2.10.6，并包含设备 profile 对应的 Torch、TorchCodec、ONNX Runtime 和 FFmpeg。CosyVoice 固定 setuptools、openai-whisper、Matcha-TTS、Torch 和 Windows ONNX 兼容组合。

模型锁使用官方不可变 revision、逐文件哈希、大小、相对目标和许可证。ModelScope、HF Mirror 或其他镜像只作为相同哈希资产的回退来源，不得替换为量化、小模型、蒸馏或低质量变体。

## 14. 受控镜像与四仓同步

TTS More 是 `tts_more_worker`、公共打包核心、schema、操作协议和组件适配器的规范源。同步器只写三个 fork 的 `tts_more/` 受控目录和根包装入口，不使用 `.git/info/exclude` 隐藏集成代码。

每个 fork 提交 `tts_more/integration.manifest.json`，记录集成版本、TTS More 源 commit、受控文件列表和哈希。CI 运行 `sync-integrations --check`，任何手工漂移都会失败。

更新顺序固定为：TTS More 规范源 PR、三个 fork 镜像 PR、TTS More 更新 `repo.lock.json` 与兼容矩阵的收口 PR。GPT-SoVITS 正式基线为 main；dev 仅用于预览和回归。

## 15. 构建、许可证与发布

每个 GitHub Release 只发布本仓 Bootstrap ZIP、SHA-256、SBOM、许可证清单和脱敏验证报告。GitHub 工作流审计 ZIP，确认不包含 `.venv`、`runtime/live`、模型、缓存、用户数据、机器路径或密钥。

Full 包输出到 git-ignore 的本地目录，写入统一 provenance 和兼容矩阵。任何 GitHub 上传步骤检测到 `package_profile=full` 必须失败。

TTS More 自有代码和同步集成层采用 Apache-2.0。上游代码与模型保留各自许可证、NOTICE 和来源清单。除非具体模型许可证明确要求确认，不增加阻塞式许可页面。

## 16. 测试与普通用户交付门禁

### 16.1 自动化与模拟测试

- schema v1/v2、锁漂移、镜像哈希、路径约束和兼容范围；
- 操作状态机、事件顺序、单实例锁、退出码和诊断脱敏；
- 下载中断、断点续传、镜像回退、哈希损坏和事务恢复；
- PID 所有权、陈旧记录、重复启动、端口冲突和停止后端口释放；
- 相对路径、绝对回退、同级发现、目录移动和版本导入；
- `uv lock --check`、`pip check`、核心 import、Torch、ONNX 和 FFmpeg；
- Bootstrap Release 审计与 Full 上传拒绝。

### 16.2 干净 Windows Bootstrap 验收

- 设备没有系统 Python、Conda、Node、Git 或开发环境；
- 测试者只执行解压和双击 `Start.cmd`；
- 自动完成设备选择、依赖、模型和服务启动；
- 下载中断后再次双击能够续传；
- 人为损坏缓存后能够检测并修复；
- 不需要管理员权限、命令行输入或手工编辑配置；
- 最终执行真实模型加载和短音频合成，不以 `/health` 代替业务验证。

### 16.3 Full 离线验收

- 随机目录解压并保持断网；
- 不读取系统运行时或用户全局缓存；
- 三个 TTS 分别执行真实音频合成；
- 移动到不同盘符后仍能运行；
- 缺失必要文件时拒绝启动，不联网补齐后伪装为 Full。

### 16.4 TTS More 工作台验收

- 自动发现同级三个组件；
- 手动选择不同盘符、中文和空格路径；
- 三个服务分别执行启动、停止、修复和日志查看；
- TTS More 重启后恢复真实状态；
- 目录整体移动后通过相对定位器恢复；
- LAN 服务保持 `managed:false`；
- 未知端口占用者不被终止；
- 不兼容版本阻止快捷控制但保留独立启动。

### 16.5 路径与设备矩阵

路径至少覆盖空格、中文、长路径临界值、不同盘符、USB 移动硬盘、盘符变化、只读 `app/` 源码区和非 ASCII Windows 用户名。整个包根只读时必须给出可操作的迁移提示并停止，不计为可启动场景。

设备至少覆盖 CPU、已认证 CU126、已认证 CU128、无 NVIDIA 显卡、驱动不满足要求以及更换显卡后的重新配置。未通过的 profile 不得发布。

### 16.6 非开发者可用性验收

- 至少两名未参与开发的测试者；
- 只提供四个 ZIP 和各包的 `使用说明-先看这里.txt`；
- 开发者不得远程代为安装；
- 测试者独立完成启动、查看状态、执行一次合成和停止服务；
- 所有失败都能从界面确定下一步；
- 脱敏验收报告不含用户名、机器路径、音频、密钥或 GPU UUID。

四仓任一正式门禁失败，整套同步版本不得标记为完成、打稳定 tag 或作为普通用户正式版交付。
