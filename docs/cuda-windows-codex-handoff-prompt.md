# Windows CUDA Codex 验证交接 Prompt

把下面完整代码块粘贴到新 Windows CUDA 设备上的 Codex 新会话。该 Prompt 要求 Codex 先核对环境和真实路径，再执行单机或分布式认证；没有真实 GPU 证据时不得声称验收通过。

````text
你正在一台新的 Windows CUDA 设备上继续 TTS More 的真实闭环验证。请作为执行工程师完成环境核对、部署、测试、证据归档和问题修复，不要只给出计划，也不要用 mock、跳过参数或无 GPU 单测代替真实认证。

## 仓库与任务边界

- 主仓库：`https://github.com/XucroYuri/TTS_more.git`
- 工作分支：`dev-xu/cuda-e2e-validation`
- 首先 fetch 并 checkout 该远端分支，执行 `git rev-parse HEAD`，把实际提交 SHA 写入验收记录。不要猜测或使用本机旧分支。
- 若当前目录已有 checkout，先检查 `git status --short --branch`。保留用户已有的本地配置、模型、fixture 和未提交内容；任何清理或覆盖前先说明影响并取得人类明确确认。
- 不要提交真实 hostname、IP、SSH 用户、密钥、参考音频、权重路径、机器路径或审核者身份。
- TTS More 应用代码留在本仓库；三个上游 TTS repo 只接收 `deployment/tts-repos/<provider>/` 对应附加包。不要把应用代码混入上游 repo。
- GPT-SoVITS 三分支收敛属于独立仓库任务。本次只验证 `deployment/app/repo.lock.json` 锁定的正式 GPT main，不重写其分支历史。

建议在新的空目录执行：

```powershell
git clone --branch dev-xu/cuda-e2e-validation --single-branch https://github.com/XucroYuri/TTS_more.git TTS_more
Set-Location TTS_more
git fetch origin dev-xu/cuda-e2e-validation
git checkout dev-xu/cuda-e2e-validation
git pull --ff-only origin dev-xu/cuda-e2e-validation
git status --short --branch
git rev-parse HEAD
```

开始前完整阅读并以这些文档为真相源：

1. `docs/cuda-e2e-validation.md`
2. `docs/cuda-e2e-single-node.md`
3. `docs/cuda-e2e-distributed.md`
4. `docs/cuda-e2e-acceptance-record.md`
5. `docs/deployment.md`
6. `docs/workers.md`

## 硬件和软件前提

正式认证环境必须满足：

- Windows 11 或 Windows Server；
- NVIDIA 驱动支持 CUDA 12.8，部署使用 `-Device CU128`；
- 每个推理节点至少 16 GB VRAM；
- Python 3.11、Git、Node.js、pnpm、PowerShell、`nvidia-smi` 可用；
- 分布式模式为一台应用控制节点加三台独立 Windows GPU worker，位于可信 LAN；
- 控制节点可通过已固定 host key 的 Windows OpenSSH 密钥无交互登录三个 worker。

先执行并保存原始输出，不满足条件时停止认证并报告阻塞，不要降低门槛：

```powershell
Get-ComputerInfo | Select-Object WindowsProductName, WindowsVersion, OsBuildNumber
nvidia-smi
python --version
git --version
node --version
pnpm --version
```

分布式时在四台机器上额外记录时间同步、hostname/IP、Windows `MachineGuid` 和 GPU UUID。证据中只保存“是否唯一”和脱敏标识，不公开原始 `MachineGuid`、内网 IP 或用户名。

## 控制面依赖与非 GPU 回归

创建全新应用虚拟环境并安装真实 ASR 门禁。`faster-whisper large-v3` 是必需项，不能通过 fixture 关闭：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e 'backend[dev]'
& .\.venv\Scripts\python.exe -m pip install faster-whisper
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend cuda:e2e:install
```

先运行不依赖 GPU 的回归；任何失败先定位根因，不要直接改测试或降低断言：

```powershell
& .\.venv\Scripts\python.exe -m pytest backend/tests -q
& .\.venv\Scripts\python.exe -m compileall -q backend scripts
pnpm --dir frontend test
pnpm --dir frontend build
git diff --check
```

记录每条命令、退出码、测试数量和警告。非 GPU 回归通过只表示控制逻辑可继续，不代表 CUDA 认证通过。

## 创建本机私有配置

从脱敏示例创建被 `.gitignore` 忽略的真实文件：

```powershell
Copy-Item deployment\app\repo-paths.example.json deployment\app\repo-paths.local.json
Copy-Item deployment\app\topology.single-windows.example.json deployment\app\topology.single-windows.local.json
Copy-Item deployment\validation\fixture.example.json data\validation\cuda-fixture.local.json
```

必须请人类逐项确认 `repo-paths.local.json` 中 GPT-SoVITS main、IndexTTS、CosyVoice 的本机部署路径。填写 fixture 中三份参考音频、GPT `v2ProPlus`/`v2Pro` 权重、prompt、测试文本、审核者和 worker 日志来源。可以用 `TTS_MORE_VALIDATION_*` 环境变量引用真实路径。

验证私有文件确实被忽略：

```powershell
git check-ignore -v deployment\app\repo-paths.local.json
git check-ignore -v deployment\app\topology.single-windows.local.json
git check-ignore -v data\validation\cuda-fixture.local.json
```

不要把真实配置加入 Git，也不要在最终回复中泄露其内容。

## 单机首次认证：single-clean

目标是一台设备同时运行应用本体和三个 worker，三服务同属 `cuda-0`、`capacity: 1`，按共享资源组顺序加载和卸载。三个进程可以同时在线，但不能让三个模型同时驻留来绕过 provider 切换。

`single-clean` 会删除或重建应用 venv、三个服务 repo/venv，并重新准备模型。先展示将要删除的绝对路径，请人类确认已经备份私有权重、参考音频和 fixture；未确认不得执行清理。

首次部署命令：

```powershell
.\scripts\deploy-local-tts.ps1 `
  -Device CU128 `
  -Profile local-all `
  -Topology deployment\app\topology.single-windows.local.json `
  -Node gpu-worker `
  -Targets default `
  -RepoPaths deployment\app\repo-paths.local.json `
  -CleanRepos
```

不得使用 `-SkipRepoSync`、`-SkipRepoPrepare`、`-SkipInstall` 或 `-SkipDownloads`。部署后核对三个 repo 的实际 `HEAD` 与 `deployment/app/repo.lock.json` 完全一致，服务环境和模型存在，`data/local/services.json` 只有三个正式服务，并运行 deploy doctor。

启动三个 worker 和应用：

```powershell
.\scripts\start-service-workers.ps1 `
  -Topology deployment\app\topology.single-windows.local.json `
  -Node gpu-worker `
  -RepoPaths deployment\app\repo-paths.local.json `
  -Detach

.\scripts\start-dev.ps1
```

检查三个 worker 的 `/health`、`/capabilities`、`/status`。必须有 `artifact-transfer`，并报告 `device`、`cuda_runtime:12.8`、`loaded`、`model`、`memory`。

首次自动认证不要求已有性能基线：

```powershell
$RunId = "single-clean-$(Get-Date -Format yyyyMMdd-HHmmss)"
.\scripts\run-cuda-validation.ps1 `
  -Mode single-clean `
  -Services data\local\services.json `
  -Fixture data\validation\cuda-fixture.local.json `
  -Topology deployment\app\topology.single-windows.local.json `
  -Node gpu-worker `
  -Output "data\validation\runs\$RunId"
```

如果真实 repo 使用自定义路径，应先按上面的部署命令完成部署，再在总入口使用 `-SkipDeploy`；总入口当前不转发 `-RepoPaths`。只允许在单机这个已记录的场景使用该参数，不得用于分布式认证。

检查输出目录至少包含 `controller.log`、`summary.json`、`junit.xml`、`nvidia-smi.csv`、`wav/`、`worker-log-references.json` 和 `human-listening-review.md`。确认 5 个核心模型用例、单机 `path`/`artifact` 往返、WAV、CER、显存、耗时和 30 条 Playwright 工作台队列均通过；三条代表性历史必须可由 `/api/audio` 读取。

首次认证需要两名人工审核者。清晰度、音色相似度、情绪/韵律、伪影控制每项都必须 >=3/5，总均分 >=3.5。人工签核未完成时，状态只能写“自动门禁通过，人工门禁待完成”，不能写“认证通过”。

从首个完整通过且获批的报告提取有限正数 `warm_p95_seconds`，写入私有 fixture 的 `performance_baseline.warm_p95_seconds`，且必须满足 `0 < value <= 300`。保留批准人、运行目录和基线来源。

## 单机发布回归：single-release

首次基线批准后执行：

```powershell
$RunId = "single-release-$(Get-Date -Format yyyyMMdd-HHmmss)"
.\scripts\run-cuda-validation.ps1 `
  -Mode single-release `
  -Services data\local\services.json `
  -Fixture data\validation\cuda-fixture.local.json `
  -Topology deployment\app\topology.single-windows.local.json `
  -Node gpu-worker `
  -Output "data\validation\runs\$RunId" `
  -RequireBaseline
```

该模式可复用已批准模型缓存，但必须重新同步锁定 repo、安装依赖、复制 provider 附加包并渲染服务配置。验证无 OOM、峰值空闲显存 >=512 MiB、卸载后 30 秒内回到基线 +1 GiB、冷加载 <=10 分钟、短句 <=5 分钟、warm p95 相比批准基线退化 <=30%。

## 四机可信 LAN 分布式认证

只有在具备一台控制节点和三台独立 GPU worker 时执行。复制并填写：

```powershell
Copy-Item deployment\app\topology.four-node-lan.example.json deployment\app\topology.four-node-lan.local.json
```

三个正式 service ID 必须各自唯一归属 `gpt-worker`、`index-worker`、`cosy-worker`，`app-controller` 不承载 worker。三个 `bind_host` 通常为 `0.0.0.0`，但 Windows 防火墙只允许可信 LAN。每台 worker 的 repo 路径由人类确认。当前认证要求三台远端使用相同 `-RemoteRoot`，且锁文件中的相对 repo 路径可从该根目录解析。

在执行前验证：

- 四个 hostname 解析为互不重复的非 loopback IP；
- 四台 Windows `MachineGuid` 唯一；
- 三个 `/status.device_uuid` 存在且唯一；
- 时间同步；端口 22 和各 worker 端口可达；host key 已固定；
- 三个远端 checkout 在 fetch 前和 detached checkout 后都完全干净；
- 远端最终 `HEAD` 与控制器 `git rev-parse HEAD` 一致。

首次分布式认证用于建立分布式基线，省略 `-RequireBaseline`：

```powershell
$RunId = "distributed-$(Get-Date -Format yyyyMMdd-HHmmss)"
.\scripts\run-cuda-validation.ps1 `
  -Mode distributed `
  -Services data\local\services.json `
  -Fixture data\validation\cuda-fixture.local.json `
  -Topology deployment\app\topology.four-node-lan.local.json `
  -SshUser <ssh-user> `
  -RemoteRoot <remote-tts-more-checkout> `
  -Output "data\validation\runs\$RunId"
```

后续分布式回归增加 `-RequireBaseline`。完整分布式认证严禁传 `-Node`、`-SkipDeploy`、`-SkipStart` 或 `-SkipFaultRecovery`。不要直接运行 Python CLI 来签发分布式通过结果；必须使用 PowerShell 总入口生成绑定 topology SHA-256、控制器 commit、随机令牌哈希和 12 小时时间窗的一次性 `orchestration-preflight.json`。

分布式必查：

- 三节点并行完成 30 条队列，至少两个 GPU 节点存在重叠加载窗口；
- 参考音频上传、远端 artifact 合成、大小和 SHA-256 校验、本地历史原子写入、远端删除完整闭环；
- worker 均为 `managed:false`，应用本地 supervisor 不管理远端进程；
- 强制停止一个 worker 后 15 秒内降级，另外两个服务和应用继续；重启后核心 CUDA 用例重试成功；
- 输出包含 `distributed-evidence.json`、`fault-recovery.json`、`recovery/`、`worker-logs/` 和四节点 GPU/worker 证据；
- 单节点 warm p95 相比批准分布式基线退化 <=30%。

## GitHub Actions 手动门禁

本地真实认证稳定后，可在 `Windows GPU validation` workflow 上选择 `single-clean`、`single-release` 或 `distributed`。runner 标签必须为 `[self-hosted, Windows, X64, cuda, tts-more-gpu]`，真实 topology/fixture 通过 runner 本地路径或受保护 variables 提供。首次认证可将 `require_baseline` 设为 `false`；批准基线后的回归必须为 `true`。release 事件不得关闭基线门禁。

## 问题处理规则

- 遇到失败时先保留日志和最小复现，定位是环境、锁定 repo、模型资产、worker 契约、应用编排、ASR、性能还是 UI 问题。
- 只有确认是本仓库缺陷时才修改代码。先补能复现问题的测试，再做最小修复，运行相关测试和完整非 GPU 回归。
- 不删除、覆盖或提交用户真实配置和模型。不要回退无关修改。
- 不降低大小、音频、CER、显存、耗时、故障恢复或人工听审阈值；不把失败改成 skip；不伪造 WAV、报告或硬件证据。
- 修复通过后，把代码提交到 `dev-xu/cuda-e2e-validation`；只有在人类明确要求并确认凭据后才推送。报告 commit、变更、测试和仍需重跑的真实门禁。

## 验收记录和最终回复

复制 `docs/cuda-e2e-acceptance-record.md` 为本次受控验收记录，填写：

- 分支和完整 commit SHA；
- Windows、Python、Node、pnpm、驱动、CUDA、GPU/VRAM；
- topology 和 fixture SHA-256；
- 三个 TTS repo 的锁定提交；
- 执行命令、开始/结束时间和退出码；
- `summary.json`、JUnit、Playwright、WAV、worker 日志、`nvidia-smi`、故障恢复和人工听审证据路径或受控 URL；
- 每个自动指标、基线比较、异常、修复 commit；
- 人工审核者数量和签核状态。

最终回复必须明确分成：环境、实际执行、自动门禁结果、人工门禁结果、证据位置、代码修改、阻塞与下一步。只使用以下结论之一：

1. `通过`：所有必过自动门禁和所需人工签核均完成；
2. `自动门禁通过，人工门禁待完成`；
3. `失败`：列出失败项和证据；
4. `阻塞`：列出缺失硬件、资产、凭据或人类确认。

不要因为脚本启动、单测通过或部分样本成功就宣布闭环完成。稳定版本只有在单机、分布式、人工听审三类记录全部通过后才能发布。
````
