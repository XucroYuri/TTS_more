# TTS More 部署方案

本文覆盖应用本体与上游 TTS repo 的集中部署、分开部署和验证流程。部署入口以 `repo.lock.json` 为单一来源，当前锁定：

| 目标 | 分支 | 目录 | 端口 |
|---|---|---|---|
| GPT-SoVITS main | `main` | `repo/GPT-SoVITS-main` | 9880 |
| GPT-SoVITS dev | `dev` | `repo/GPT-SoVITS-dev` | 9883 |
| GPT-SoVITS proplus | `xucroyuri/proplus-hc-dev` | `repo/GPT-SoVITS-proplus-hc-dev` | 9884 |
| IndexTTS | `main` | `repo/index-tts` | 9881 |
| CosyVoice | `main` | `repo/CosyVoice` | 9882 |

## 部署 profile

- `local-all`：应用本体和所有 worker 在同一台机器上，生成 `data/local/services.json`，本机可托管启动。
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
.\scripts\tts-more.ps1 sync-repos --clean
```

macOS/Linux：

```bash
./scripts/tts-more.sh sync-repos --clean
```

只预览命令：

```bash
python scripts/tts_more_deploy.py sync-repos --clean --dry-run
```

## 安装依赖和模型

Windows：

```powershell
.\scripts\prepare-tts-repos.ps1 -SyncRepos -CleanRepos -Source ModelScope -Device CU128
```

macOS/Linux：

```bash
bash scripts/prepare-tts-repos.sh --sync-repos --clean-repos --source ModelScope --device CU128
```

常用参数：

- `Source`: `ModelScope`, `HF`, `HF-Mirror`
- `Device`: Windows 支持 `CU128`, `CU126`, `CPU`；Linux 还可用 `ROCM`；macOS 可用 `MPS` 或 `CPU`
- `Targets`: `all`, `gpt-sovits`, `indextts`, `cosyvoice`, `main`, `dev`, `proplus-hc-dev`, 或具体 `service_id`
- `SkipInstall` / `--skip-install`：只下载模型或渲染服务
- `SkipDownloads` / `--skip-downloads`：只安装依赖
- `DryRun` / `--dry-run`：打印命令，不执行

上游要求：

- GPT-SoVITS：优先使用每个分支自带的 `install.ps1`/`install.sh`，官方脚本依赖 conda/micromamba 环境。
- IndexTTS：优先 `uv sync --all-extras`，下载 `IndexTeam/IndexTTS-2`，并准备 BigVGAN 辅助模型。
- CosyVoice：需要 `git submodule update --init --recursive`，Python 3.10 venv，`requirements.txt`，默认下载 `CosyVoice-300M`。

## 渲染服务配置

本机全部 worker：

```powershell
.\scripts\tts-more.ps1 render-services --profile local-all --platform windows --output data\local\services.json
```

应用本体连接远端 worker：

```bash
./scripts/tts-more.sh render-services --profile app-only --host tts-gpu.local --output data/local/services.json
```

只渲染部分 worker：

```bash
python scripts/tts_more_deploy.py render-services --profile local-all --service-ids local-gpt-sovits-dev,local-indextts
```

## 启动 worker

Windows：

```powershell
.\scripts\start-service-workers.ps1
.\scripts\start-service-workers.ps1 -Services local-gpt-sovits-main,local-indextts
```

macOS/Linux：

```bash
./scripts/start-service-workers.sh
./scripts/start-service-workers.sh --services local-gpt-sovits-dev,local-cosyvoice
```

全部 GPT 分支同时启动会占用大量显存；普通验证建议一次启动一个 GPT 分支。

## 验证

先做静态诊断：

```bash
python scripts/tts_more_deploy.py doctor
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
