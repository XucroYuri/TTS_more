# 单机 Windows CUDA 验收 Runbook

本 runbook 执行应用本体和三个正式 worker 在一台 Windows CUDA 设备上的完整闭环。门禁定义以 [CUDA 全流程闭环验证](cuda-e2e-validation.md) 为准。

## 1. 机器准备

确认：

- Windows 11 或 Windows Server；
- NVIDIA 驱动支持 CUDA 12.8，GPU 至少 16 GB VRAM；
- Python 3.11、Git、Node.js、pnpm、PowerShell 可用；
- 验证用 `.venv` 安装 `faster-whisper`，并能加载 `large-v3`；
- `nvidia-smi` 可执行，系统盘、repo 盘和模型缓存盘有足够空间；
- 防火墙允许本机访问 `127.0.0.1:8000`、`:5173`、`:9880`、`:9881`、`:9882`。

记录以下输出到验收记录：

```powershell
Get-ComputerInfo | Select-Object WindowsProductName, WindowsVersion, OsBuildNumber
nvidia-smi
python --version
git --version
node --version
pnpm --version
```

## 2. 创建本机配置

```powershell
Copy-Item deployment\app\repo-paths.example.json deployment\app\repo-paths.local.json
Copy-Item deployment\app\topology.single-windows.example.json deployment\app\topology.single-windows.local.json
```

在人类确认后编辑 `repo-paths.local.json`。单机 topology 保持三个正式服务归属 `gpu-worker`，并保持：

```json
{
  "host": "localhost",
  "bind_host": "127.0.0.1",
  "resource_group": "cuda-0",
  "capacity": 1
}
```

从脱敏模板创建被忽略的 fixture，填写三份参考音频、GPT `v2ProPlus`/`v2Pro` 权重、prompt、测试文本、`faster-whisper large-v3` 和审核者：

```powershell
Copy-Item deployment\validation\fixture.example.json data\validation\cuda-fixture.local.json
```

可以保留模板中的 `${TTS_MORE_VALIDATION_*}` 并在 runner 环境设置对应变量。检查这些文件确实不会提交：

```powershell
git check-ignore -v deployment\app\repo-paths.local.json
git check-ignore -v deployment\app\topology.single-windows.local.json
git check-ignore -v data\validation\cuda-fixture.local.json
```

## 3. 首次认证 `single-clean`

`single-clean` 必须证明空白设备能从锁定源完成部署。开始前保存必要的私有模型和 fixture 备份，然后移除应用 venv、三个服务 repo 和服务 repo 内 venv。第一次认证不复用旧 repo 或旧 Python 环境；模型也重新下载或从已批准的离线发布缓存恢复，并在记录中写明来源。

```powershell
Remove-Item -Recurse -Force .venv -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force repo\GPT-SoVITS-main -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force repo\index-tts -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force repo\CosyVoice -ErrorAction SilentlyContinue
```

执行完整部署：

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

不得使用 `-SkipRepoSync`、`-SkipRepoPrepare`、`-SkipInstall` 或 `-SkipDownloads`。部署结束后确认：

1. `repo.lock.json` 中三个正式 repo 的提交与本地 `HEAD` 一致；
2. 应用 `.venv` 与三个服务环境存在；
3. 模型和 fixture 中指定的 GPT 权重存在；
4. `data/local/services.json` 只有三个正式服务，均为 `resource_group: cuda-0`、`capacity: 1`；
5. `python scripts/tts_more_deploy.py doctor` 没有 repo、分支、提交或 venv 错误。

`backend[dev]` 当前不包含 `faster-whisper`。fixture 的 `asr.required` 固定为 `true`；在执行验证器前将 `faster-whisper` 安装到本次验证的 `.venv`。缺失时预检会失败，不能通过 fixture 关闭 ASR 来绕过发布门禁。

## 4. 日常发布 `single-release`

`single-release` 保留已认证模型缓存，不删除模型目录；它仍必须重新同步锁定 repo、安装依赖、复制 `deployment/tts-repos/<provider>` 附加脚本，并重新渲染 `data/local/services.json`。

```powershell
.\scripts\deploy-local-tts.ps1 `
  -Device CU128 `
  -Profile local-all `
  -Topology deployment\app\topology.single-windows.local.json `
  -Node gpu-worker `
  -Targets default `
  -RepoPaths deployment\app\repo-paths.local.json
```

不要使用 `-SkipRepoSync` 或 `-SkipInstall`。允许下载器命中已批准缓存，但记录缓存位置和命中情况。若锁定提交、依赖锁或模型清单变化，重新执行受影响部分并在验收记录说明。

## 5. 启动与静态预检

让三个 worker 同时在线：

```powershell
.\scripts\start-service-workers.ps1 `
  -Topology deployment\app\topology.single-windows.local.json `
  -Node gpu-worker `
  -RepoPaths deployment\app\repo-paths.local.json `
  -Detach
```

启动应用：

```powershell
.\scripts\start-dev.ps1
```

分别检查 `/health`、`/capabilities`、`/status`。三个 `/capabilities` 都必须包含 `artifact-transfer`；`/status` 必须报告 `device`、`cuda_runtime:12.8`、`loaded`、`model` 和 `memory`。

```powershell
Invoke-RestMethod http://127.0.0.1:9880/health
Invoke-RestMethod http://127.0.0.1:9881/health
Invoke-RestMethod http://127.0.0.1:9882/health
Invoke-RestMethod http://127.0.0.1:9880/status
Invoke-RestMethod http://127.0.0.1:9881/status
Invoke-RestMethod http://127.0.0.1:9882/status
```

预检必须确认 CUDA 12.8、VRAM >=16 GB、磁盘余量、模型文件、三个进程和端口。三进程在线不代表三模型同时驻留。

## 6. 自动验证

为每次运行使用新的输出目录：

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

首次认证将 `-Mode` 改为 `single-clean` 并可移除 `-RequireBaseline`；该模式会忽略已有基线要求并产出待批准的首个 warm p95。它把 `-CleanRepos` 传给同步工具，删除 `repo/` 下已有 checkout（包括 repo 内 venv）后重建；首次设备认证仍应按第 3 节先清理应用 `.venv`，CI 的全新 checkout 也必须重新创建它。验证期间持续采集 `nvidia-smi`。必须观察到 provider 切换前旧服务 `/unload`，随后显存回落，再加载新服务；不得用三个模型同时驻留绕过该路径。

核心验证器自动执行 5 个模型用例、单机 GPT `path`/`artifact` 对照及音频、ASR、显存和耗时判定。GPU workflow 随后用 Playwright 提交 30 条真实混合队列，每服务 10 条，并断言共享资源组最多只有一个加载签名；三条代表性历史音频必须可由 `/api/audio` 读取。两部分都通过才算自动门禁通过。

若本机使用 `repo-paths.local.json` 的自定义路径，当前 CUDA 总入口不会转发 `-RepoPaths`。先按第 3/4 节手动部署，再给验证入口加 `-SkipDeploy`，避免它重新按默认路径部署。

## 7. UI 与人工验收

Playwright 门禁加载专用验证项目，等待三个服务 ready，执行 30 条真实混合队列，等待队列结束，并抽查三个服务各一条历史音频的 `/api/audio` 返回有效媒体。保留 Playwright report、trace 或失败截图 URL。

打开运行目录的 `human-listening-review.md`，按 [验收记录模板](cuda-e2e-acceptance-record.md) 逐样本评分。首次 `single-clean` 需要两名审核者；后续发布至少一名。清晰度、音色相似度、情绪/韵律和伪影控制均不得低于 3/5，总均分不得低于 3.5。

## 8. 建立与比较基线

第一次完整通过的 16 GB `single-clean` 运行建立冷加载、短句耗时、warm p95、显存基线。后续 `single-release`：

- 无 OOM；峰值空闲显存至少 512 MiB；
- 卸载后 30 秒内回到基线 +1 GiB；
- 冷加载不超过 10 分钟，短句不超过 5 分钟；
- warm p95 不得比已批准基线退化超过 30%。

从首个通过报告取 warm p95，经审核批准后写入受控 fixture 的 `performance_baseline.warm_p95_seconds`。后续 `single-release` 必须使用 `-RequireBaseline`，缺少该字段时预检直接失败。

复制自动运行 URL、基线版本和人工记录链接到发布候选。任何失败或缺失结果都阻止发布。
