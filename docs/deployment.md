# TTS More 部署方案

本文覆盖应用本体与上游 TTS repo 的集中部署、分开部署和验证流程。部署入口以 `repo.lock.json` 为单一来源，当前锁定：

## 普通用户：Windows 四包解压即用

1. 把 `TTS-More`、`GPT-SoVITS`、`IndexTTS`、`CosyVoice` 四个组件解压到任意**可写**目录。四个文件夹建议保持同级；目录名可以包含空格和中文，整套文件夹也可以移动或改名。
2. 分别双击需要运行的组件根目录中的 `Start.cmd`。也可以先启动 TTS More，再在“本地便携 TTS 服务”的三张独立卡片中为 GPT-SoVITS、IndexTTS、CosyVoice 选择对应目录，并逐个点击启动。
3. TTS More **不会自动批量启动**三个 TTS 服务。每个组件仍可从自己的 `Start.cmd` 独立启动，也可独立停止或修复；只启动当前任务需要的服务即可。
4. TTS More 只有在本机 loopback 访问时才能维护路径、浏览目录以及执行启动、停止、修复和打开目录。可信 LAN 服务可以注册并用于合成，但保持 `managed:false`，不能从网络远程执行这些本地控制操作。
5. `bootstrap` 包不含可重建运行依赖和模型，首次启动需要联网，由 `Initialize.cmd`/`Start.cmd` 自动下载、校验并补齐资产；初始化成功后可以离线运行。`full` 包只允许在本地构建，已经包含运行时、依赖和默认模型，解压后可断网运行，并且禁止上传 GitHub。
6. 不同电脑的盘符和目录本来就可以不同。不要在配置、快捷方式或脚本中写死 Conda、Python、模型权重或源码仓库的绝对路径；本机选择结果只保存在 TTS More 包内的 `data/local/services.json`，整套移动后会优先按同级相对路径重新发现。

首次使用 `bootstrap` 时如下载中断，重新运行 `Start.cmd` 会继续初始化；确定资产损坏或缺失时运行该组件的 `Repair.cmd`。`full` 包的“离线可运行”仍以其构建报告中已通过的设备配置为准，不等同于本机已经完成真实 GPU/模型认证。

| 目标 | 分支 | 默认 | 目录 | 端口 |
|---|---|---|---|---|
| GPT-SoVITS main | `main` | 是 | `repo/GPT-SoVITS-main` | 9880 |
| GPT-SoVITS dev | `dev` | 否，仅回归 | `repo/GPT-SoVITS-dev` | 9883 |
| GPT-SoVITS proplus | `xucroyuri/proplus-hc-dev` | 否，仅旧功能审计 | `repo/GPT-SoVITS-proplus-hc-dev` | 9884 |
| IndexTTS | `main` | 是 | `repo/index-tts` | 9881 |
| CosyVoice | `main` | 是 | `repo/CosyVoice` | 9882 |

`default_selected` 是提交清单中的默认部署开关。默认命令只选择 GPT-SoVITS main、IndexTTS 和 CosyVoice；`dev` 与 `proplus-hc-dev` 仍保留在锁文件中，供显式回归和收敛审计使用。

## 目录职责

部署相关内容分成两类，方便 Agent 分工，也方便人类用户手动复制：

- `scripts/`：应用本体入口脚本，包括一键部署、更新、渲染服务配置、启动 worker。
- `deployment/app/`：应用本体部署资料。`repo-paths.example.json` 用于确认当前设备上的服务 repo 路径；本机副本 `repo-paths.local.json` 已被 git ignore。
- `deployment/tts-repos/gpt-sovits/`：可复制到 GPT-SoVITS repo 的附加脚本包。
- `deployment/tts-repos/indextts/`：可复制到 IndexTTS repo 的附加脚本包。
- `deployment/tts-repos/cosyvoice/`：可复制到 CosyVoice repo 的附加脚本包。

一键脚本会把对应 provider 的脚本包复制到服务 repo 的 `tts-more/` 目录，并写入该 repo 的本地 `.git/info/exclude`，避免误提交到上游 TTS repo。

Managed checkout 的 `.git` 必须是 checkout 内的真实目录。为保证所有 Git 读写都使用同一 metadata boundary，当前版本明确拒绝 `.git` symlink/reparse point、损坏目录，以及 worktree/submodule 使用的 `gitdir:` 文件；这些布局请改用独立 clone。

Bundle 文件逐个通过临时文件替换，单文件写入具备原子性，但安装 **not atomic as a whole bundle**。安装开始前会写入无时间戳的 `tts-more-install-pending.json`，其中记录旧 ownership hashes 与目标 manifest；正常完成后删除。若进程中断，保留目录原状并 **rerun the identical install command**，安装器只接受旧哈希或本次目标哈希并继续；其它本地修改会中止，不会删除或覆盖。不要手工编辑 `tts-more-repo.json` 或 pending journal。

删除权不由服务 checkout 内的 manifest 自证。应用在 ignored 的 `data/local/deployment-ownership/<service_id>.json` 保存 checkout 外 trust anchor，并把它绑定到目标 manifest 的字节哈希；**lost anchor fails closed**，不会把同身份、自洽 hashes 的既有 schema-3 manifest 当成可信来源。迁移旧安装时先人工核对 manifest 与文件，再单独执行：

```bash
python scripts/tts_more_deploy.py install-repo-bundles --adopt-existing --repo-paths deployment/app/repo-paths.local.json
```

**adoption does not upgrade, overwrite, or delete files**；它只校验现有 owned hashes 并建立 anchor，之后必须再次运行不带 `--adopt-existing` 的安装命令。已有损坏 anchor、pending install、缺失或修改过的 owned 文件均拒绝 adoption。

所有 app-side Git 和复制到服务 repo 的 updater Git 都使用同一 hardened runner：忽略 system/global config 与 `GIT_CONFIG_*`/SSH/askpass 注入，禁用 hooks/fsmonitor/credential helper，并把协议限制为 allowlisted GitHub HTTPS/SSH。Git executable 只从平台固定安装目录或显式 `TTS_MORE_TRUSTED_GIT` 解析；只有实际将访问的 SSH transport 才同样从固定目录或 `TTS_MORE_TRUSTED_SSH` 解析 SSH 并固定 SSH command，纯 HTTPS 使用不可执行 sentinel 且不要求安装 SSH。checkout-local 的 hooksPath、fsmonitor、sshCommand、credential helper、URL rewrite、filter/process、include 和 executable submodule update 配置会在 `status/config/fetch/checkout/pull` 前拒绝。

Submodule 同步发生 **after the final superproject checkout**：latest 模式先完成 branch fast-forward/reset，locked 模式先完成锁定 commit 的 fetch/checkout，之后才读取最终 tree 的 `.gitmodules`。解析器只接受不重复的 `submodule "<name>"`、`path` 和 `url`；unknown/duplicate/unsafe metadata 会 fail closed。**relative submodule URLs are resolved against the validated actual origin**，并且 **every resolved submodule URL must pass the GitHub allowlist**。更新命令使用进程级、已验证 URL override，不把 submodule URL 写入 local config；嵌套 submodule 逐层验证后再更新。**HTTPS-only submodules do not require SSH**；**any SSH submodule requires trusted SSH**。

并发攻击者仍可能在最终 pathname 检查与替换之间交换一般输出目录；**concurrent parent-swap remains a residual threat**。POSIX worker logs 会以 `O_DIRECTORY | O_NOFOLLOW` 打开 `logs_dir` 并通过 dirfd 创建日志，但一般 bundle/output rename 尚未改成完整 `openat`/`renameat` 链，且 **Windows handle-based parent protection is not implemented**。因此当前保证覆盖静态 symlink/reparse 与单文件替换，不宣称跨平台 race-free。
## 一键本机部署

macOS/Linux：

```bash
cp deployment/app/repo-paths.example.json deployment/app/repo-paths.local.json
scripts/deploy-local-tts.sh --device CU128 --repo-paths deployment/app/repo-paths.local.json
```

Windows：

```powershell
Copy-Item deployment\app\repo-paths.example.json deployment\app\repo-paths.local.json
.\scripts\deploy-local-tts.ps1 -Device CU128 -RepoPaths deployment\app\repo-paths.local.json
```

默认步骤：

1. 安装应用本体后端和前端依赖。
2. 校验当前设备上的服务 repo 路径。
3. 同步 `repo.lock.json` 中标记为 `default_selected` 的 TTS repo。
4. 安装每个 repo 的 `deployment/tts-repos/<provider>` 附加脚本包。
5. 准备服务 repo 依赖和 baseline 模型。
6. 渲染 `data/local/services.json`，让应用本体直接接入当前设备上的 worker。
7. 执行 `doctor` 输出路径、分支、提交和 venv 诊断。

只预览命令。该模式仍会解析 selector、校验完整路径确认和现有 Git
origin/dirty 状态，并输出 clone/fetch、bundle、依赖、模型与服务配置计划，但不会写文件：

```bash
scripts/deploy-local-tts.sh --dry-run --repo-paths deployment/app/repo-paths.local.json
```

跳过大型依赖/模型准备，只完成路径、脚本包和服务配置：

```bash
scripts/deploy-local-tts.sh --skip-repo-prepare --repo-paths deployment/app/repo-paths.local.json
```

## 本机 repo 路径确认

人类用户需要通过 service-id keyed JSON 确认每个选中服务 repo 在当前设备上的部署路径。
即使采用 `repo.lock.json` 中的默认路径，也必须提供完整确认文件：

```bash
cp deployment/app/repo-paths.example.json deployment/app/repo-paths.local.json
```

编辑 `deployment/app/repo-paths.local.json` 后运行。只允许唯一的正式 `service_id` key；
未知 key、provider/name/variant 别名、缺少任一选中服务或重复 JSON key 都会失败：

```bash
scripts/deploy-local-tts.sh --repo-paths deployment/app/repo-paths.local.json
```

Windows：

```powershell
.\scripts\deploy-local-tts.ps1 -RepoPaths deployment\app\repo-paths.local.json
```

当前应用内置的 managed local worker 只接受 `<TTS More>/repo/` 专用区域内的路径。
现有 checkout 还必须是 Git repo，且 `origin` 身份与 lock 中的 remote 一致；SSH 与 HTTPS
形式会规范化后比较。若 TTS repo 部署在该区域外，请按各 provider README 的 POSIX/PowerShell
命令把 `deployment/tts-repos/<provider>/` 内容复制到 `<repo>/tts-more/`，并在应用里按
`app-only`/外部 endpoint 接入。

## 部署 profile

- `local-all`：应用本体和选中的 worker 在同一台机器上，生成 `data/local/services.json`，本机可托管启动。
- `app-only`：只跑 TTS More 应用；配合 topology 时为每个服务生成独立 LAN 地址，远端服务为 `managed:false`。
- `worker-node`：只在某台 GPU 机器上准备该节点负责的 repo、模型和 worker，不要求启动前端；必须配合 topology 的节点选择使用。

### Topology manifest

Windows CUDA 验收使用 manifest 明确机器、服务和 GPU 资源归属：

- `deployment/app/topology.single-windows.example.json`：单机应用 + 三个 worker，三服务共享 `cuda-0`、`capacity:1`；
- `deployment/app/topology.four-node-lan.example.json`：一台应用控制节点 + 三台独立 GPU worker。

复制示例为 `deployment/app/topology.<name>.local.json` 并填入真实 LAN hostname/IP；真实文件已被 git ignore。字段和校验规则见 [CUDA 验证契约](cuda-e2e-validation.md#拓扑)。

单机渲染：

```powershell
.\scripts\deploy-local-tts.ps1 -RepoPaths deployment\app\repo-paths.local.json `
  -Profile local-all `
  -Topology deployment\app\topology.single-windows.local.json `
  -Node gpu-worker `
  -Device CU128
```

四机控制节点渲染：

```powershell
.\scripts\deploy-local-tts.ps1 -RepoPaths deployment\app\repo-paths.local.json `
  -Profile app-only `
  -Topology deployment\app\topology.four-node-lan.local.json `
  -Node app-controller
```

单个 worker 节点把 profile 改为 `worker-node`，并使用 `-Node gpt-worker|index-worker|cosy-worker`。底层 CLI 对应 `--topology` 和 `--node`。分布式基线只支持可信 LAN；公网、TLS 和反向代理不在当前发布门禁内。

## Network Auto Mode

`Source` 默认是 `Auto`。包装脚本会先执行：

```text
scripts/tts_more_deploy.py probe-network --write --source Auto
```

生成的网络 profile 会写入 `data/local/network-profile.json`，并已加入 git ignore。中国大陆网络会优先尝试国内可达的源；如果失败，再回退到全球 Hugging Face 和 PyPI 路线。

覆盖行为：

- `TTS_MORE_NETWORK_PROFILE=auto|china|global`
- `TTS_MORE_MODEL_SOURCE=Auto|ModelScope|HF-Mirror|HF`
- `TTS_MORE_CACHE_ROOT=data/cache`
- `TTS_MORE_PIP_INDEX_URL=<custom pip index>`
- `TTS_MORE_HF_ENDPOINT=<custom Hugging Face endpoint>`

推荐安装器只准备 full-quality baseline models，不会自动选 quantized、distilled、simplified、small 或 low-memory 模型；这些都保留为 manual 高级选项。

## 清理并重拉 repo

Windows：

```powershell
.\scripts\tts-more.ps1 sync-repos --clean --repo-paths deployment\app\repo-paths.local.json
```

macOS/Linux：

```bash
./scripts/tts-more.sh sync-repos --clean --repo-paths deployment/app/repo-paths.local.json
```

只预览命令：

```bash
python scripts/tts_more_deploy.py sync-repos --clean --dry-run --repo-paths deployment/app/repo-paths.local.json
```

## 一键更新

应用本体更新入口：

```bash
scripts/update.sh --repo-paths deployment/app/repo-paths.local.json
```

Windows：

```powershell
.\scripts\update.ps1 --repo-paths deployment\app\repo-paths.local.json
```

它会按顺序做四件事：

1. `git fetch --prune` + `git pull --ff-only` 更新应用本体当前分支。
2. 安全更新 `repo.lock.json` 中的服务 repo：先检查是否有本地改动，再执行 fetch / checkout / fast-forward pull。
3. 向支持 standalone updater 的服务 repo 写入 `tts-more-update.sh`、`tts-more-update.ps1`、`tts-more-update.py` 和 `tts-more-update.json` 四个文件；submodule repo 会明确报告 managed-sync-only 且不写入其中任何文件。
4. 如果 `data/local/services.json` 不存在，生成本机服务配置；已有本机配置默认保留。

常用变体：

```bash
scripts/update.sh --dry-run --repo-paths deployment/app/repo-paths.local.json
scripts/update.sh --skip-app --repo-paths deployment/app/repo-paths.local.json
scripts/update.sh --skip-repos --repo-paths deployment/app/repo-paths.local.json
scripts/update.sh --latest-repos --write-lock --repo-paths deployment/app/repo-paths.local.json
scripts/update.sh --service-ids local-indextts --repo-paths deployment/app/repo-paths.local.json
scripts/update.sh --force-render-services --repo-paths deployment/app/repo-paths.local.json
scripts/update.sh --force-reset-repos --repo-paths deployment/app/repo-paths.local.json
```

`--force-reset-repos` 会允许服务 repo 执行硬重置，只适合确认没有要保留的本地改动时使用。

服务 repo 内的轻量更新脚本用于分布式部署设备。复制时必须把 `tts-more-update.sh`、`tts-more-update.ps1`、`tts-more-update.py`、`tts-more-update.json` 四个文件一起放到目标 service repo 根目录。**repositories with submodules do not receive the standalone updater**，因为该 updater 不更新 submodule；它们 **must be updated from TTS More managed sync-repos**，工具会报告限制且不会部分写入四个 updater 文件。schema 3 sidecar 只记录 portable executable policy 和 `requires_ssh`，**does not store installer-host absolute executable paths**；updater **resolves Git independently on the destination device**，从目标机固定安装目录或显式 `TTS_MORE_TRUSTED_GIT` 解析。它先仅用 trusted Git 审计 checkout、读取 actual origin 并验证 GitHub identity，再按 actual origin transport 决定是否解析 `TTS_MORE_TRUSTED_SSH`；**sidecar transport does not override the actual origin transport**。对 actual transport 而言，**HTTPS remotes do not require SSH**，而 **SSH remotes require a trusted SSH executable**；sidecar 的 `requires_ssh` 仍必须与 sidecar remote 一致以检测篡改。

复制完成后，在服务 repo 根目录运行：

```bash
./tts-more-update.sh
./tts-more-update.sh --pinned
```

`--pinned` 会回到 `repo.lock.json` 当前记录的提交；不加参数则快进到该服务分支最新版。生成脚本会写入该 repo 的本地 `.git/info/exclude`，避免把这些辅助脚本误当成服务 repo 的业务改动。

## 安装依赖和模型

Windows：

```powershell
.\scripts\prepare-tts-repos.ps1 -SyncRepos -CleanRepos -Source ModelScope -Device CU128 -RepoPaths deployment\app\repo-paths.local.json
```

macOS/Linux：

```bash
bash scripts/prepare-tts-repos.sh --sync-repos --clean-repos --source ModelScope --device CU128 --repo-paths deployment/app/repo-paths.local.json
```

常用参数：

- `Source`: `Auto`, `ModelScope`, `HF`, `HF-Mirror`；推荐保持默认 `Auto`
- `Device`: Windows 支持 `CU128`, `CU126`, `CPU`；Linux 还可用 `ROCM`；macOS 可用 `MPS` 或 `CPU`
- `Targets`: `default`, `all`, `gpt-sovits`, `indextts`, `cosyvoice`, `main`, `dev`, `proplus-hc-dev`, 或具体 `service_id`
- `RepoPaths` / `--repo-paths`：读取 `deployment/app/repo-paths.local.json`，确认当前设备上的服务 repo 路径
- `SkipInstall` / `--skip-install`：只下载模型或渲染服务
- `SkipDownloads` / `--skip-downloads`：只安装依赖
- `DryRun` / `--dry-run`：打印命令，不执行

上游要求：

- GPT-SoVITS：优先使用每个分支自带的 `install.ps1`/`install.sh`，当前 managed prepare 只支持 conda；**micromamba is not currently supported**。选中 GPT-SoVITS 且未跳过依赖安装时，缺少 conda（包括仅安装 micromamba）会在任何 repo preparation 前以非零状态退出。
- IndexTTS：优先 `uv sync --all-extras`，下载 `IndexTeam/IndexTTS-2`，并准备 BigVGAN 辅助模型。
- CosyVoice：`sync-repos` 在最终 superproject commit/branch 确定后结构化校验 `.gitmodules`，逐层更新 allowlisted submodule；随后准备 Python 3.10 venv、`requirements.txt`，默认下载 `CosyVoice-300M`。

### 便携包构建用私有 Conda

四个离线绿色包的构建不依赖电脑中已安装的 Conda。Windows 构建入口会从 `packaging/portable/toolchain.lock.json` 读取锁定的 Miniforge 下载地址与 SHA-256，将其仅安装到项目的 `data/cache/portable/conda/`，并把包缓存设为该目录下的 `conda-pkgs/`；不会写入用户 PATH、注册表或全局 Conda 环境。

可先预览，不会下载或安装：

```powershell
.\scripts\bootstrap-conda.ps1 -DryRun
```

首次构建时运行同一脚本（或由对应 `build-portable-*.ps1` 自动调用）即可创建私有构建工具链。最终 ZIP 已包含可重定位运行环境，解压后启动完全不需要 Conda、Python 或网络。

## 渲染服务配置

本机默认正式 worker：

```powershell
.\scripts\tts-more.ps1 render-services --profile local-all --platform windows --output data\local\services.json --repo-paths deployment\app\repo-paths.local.json
```

应用本体连接远端 worker：

```bash
./scripts/tts-more.sh render-services --profile app-only --host tts-gpu.local --output data/local/services.json
```

只渲染部分 worker：

```bash
python scripts/tts_more_deploy.py render-services --profile local-all --service-ids local-gpt-sovits-dev,local-indextts --repo-paths deployment/app/repo-paths.local.json
```

一键脚本的等价选择方式：

```bash
scripts/deploy-local-tts.sh --targets dev --repo-paths deployment/app/repo-paths.local.json  # 只部署 GPT-SoVITS dev 回归实例
scripts/deploy-local-tts.sh --targets all --repo-paths deployment/app/repo-paths.local.json  # 部署锁文件中的全部实例
```

GPT-SoVITS `main` 在 CUDA 门禁通过并发布前仍使用锁文件中的现有提交。分支收敛流程见 [GPT-SoVITS 分支收敛](gpt-sovits-branch-convergence.md)。

## 启动 worker

Windows：

```powershell
.\scripts\start-service-workers.ps1 -RepoPaths deployment\app\repo-paths.local.json
.\scripts\start-service-workers.ps1 -Services local-gpt-sovits-main,local-indextts -RepoPaths deployment\app\repo-paths.local.json
```

macOS/Linux：

```bash
./scripts/start-service-workers.sh --repo-paths deployment/app/repo-paths.local.json
./scripts/start-service-workers.sh --services local-gpt-sovits-dev,local-cosyvoice --repo-paths deployment/app/repo-paths.local.json
```

全部 GPT 分支同时启动会占用大量显存；普通验证建议一次启动一个 GPT 分支。

## 验证

先做静态诊断：

```bash
python scripts/tts_more_deploy.py doctor --repo-paths deployment/app/repo-paths.local.json
```

再逐个 worker 检查：

```bash
curl http://127.0.0.1:9880/health
curl http://127.0.0.1:9883/health
curl http://127.0.0.1:9884/health
curl http://127.0.0.1:9881/health
curl http://127.0.0.1:9882/health
```

聚焦真实合成的 pytest 需要模型、参考音频和对应硬件：

```bash
export TTS_MORE_SERVICE_MODE=real
export TTS_MORE_RUN_REAL_TTS=1
.venv/bin/python -m pytest backend/tests/test_real_tts_validation.py -q
```

本页只解释通用部署接口，不是认证 runbook。Windows 单机的唯一可复制路径见 [单机 Runbook](cuda-e2e-single-node.md)；四机见 [分布式 Runbook](cuda-e2e-distributed.md)；协议和阈值见 [CUDA 验证契约](cuda-e2e-validation.md)。

## 离线和缓存

推荐把模型缓存放在 repo 内的默认目录，方便 worker-node 独立迁移：

- GPT-SoVITS：`GPT_SoVITS/pretrained_models`
- IndexTTS：`checkpoints` 和 `checkpoints/hf_cache`
- CosyVoice：`pretrained_models/CosyVoice-300M`

离线运行前设置 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`，并用 `doctor` 和 worker `/health` 确认缺失文件。
