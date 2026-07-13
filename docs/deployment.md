# TTS More 部署方案

本文覆盖应用本体与上游 TTS repo 的集中部署、分开部署和验证流程。部署入口以 `repo.lock.json` 为单一来源，当前锁定：

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
- `app-only`：只跑 TTS More 应用，`services.json` 指向局域网或云端 worker。
- `worker-node`：只在某台 CPU/GPU 机器上准备 repo、模型和 worker，不要求启动前端；渲染出来仍是该机器本地可管理的 worker 配置，通常配合 `--service-ids` 只选择本节点负责的服务。

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
3. 向已存在的服务 repo 写入 `tts-more-update.sh` 和 `tts-more-update.ps1`。
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

服务 repo 内的轻量更新脚本用于分布式部署设备。复制时必须把 `tts-more-update.sh`、`tts-more-update.ps1`、`tts-more-update.py`、`tts-more-update.json` 四个文件一起放到目标 service repo 根目录。schema 3 sidecar 只记录 portable executable policy 和 `requires_ssh`，**does not store installer-host absolute executable paths**；updater **resolves Git independently on the destination device**，从目标机固定安装目录或显式 `TTS_MORE_TRUSTED_GIT` 解析。它先仅用 trusted Git 审计 checkout、读取 actual origin 并验证 GitHub identity，再按 actual origin transport 决定是否解析 `TTS_MORE_TRUSTED_SSH`；**sidecar transport does not override the actual origin transport**。对 actual transport 而言，**HTTPS remotes do not require SSH**，而 **SSH remotes require a trusted SSH executable**；sidecar 的 `requires_ssh` 仍必须与 sidecar remote 一致以检测篡改。

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

- GPT-SoVITS：优先使用每个分支自带的 `install.ps1`/`install.sh`，官方脚本依赖 conda/micromamba 环境。
- IndexTTS：优先 `uv sync --all-extras`，下载 `IndexTeam/IndexTTS-2`，并准备 BigVGAN 辅助模型。
- CosyVoice：`sync-repos` 在最终 superproject commit/branch 确定后结构化校验 `.gitmodules`，逐层更新 allowlisted submodule；随后准备 Python 3.10 venv、`requirements.txt`，默认下载 `CosyVoice-300M`。

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
.\scripts\start-service-workers.ps1 -RepoPaths deployment\app\repo-paths.local.json
```

macOS/Linux：

```bash
./scripts/start-service-workers.sh --repo-paths deployment/app/repo-paths.local.json
./scripts/start-service-workers.sh --services local-gpt-sovits-dev,local-cosyvoice --repo-paths deployment/app/repo-paths.local.json
./scripts/start-service-workers.sh --repo-paths deployment/app/repo-paths.local.json
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

GPU/真实合成验收需要模型、参考音频和对应硬件：

```bash
export TTS_MORE_SERVICE_MODE=real
export TTS_MORE_RUN_REAL_TTS=1
.venv/bin/python -m pytest backend/tests/test_real_tts_validation.py -q
```

## 离线和缓存

推荐把模型缓存放在 repo 内的默认目录，方便 worker-node 独立迁移：

- GPT-SoVITS：`GPT_SoVITS/pretrained_models`
- IndexTTS：`checkpoints` 和 `checkpoints/hf_cache`
- CosyVoice：`pretrained_models/CosyVoice-300M`

离线运行前设置 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`，并用 `doctor` 和 worker `/health` 确认缺失文件。
