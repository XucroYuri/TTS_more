# GPT-SoVITS repo scripts

Use the app-side installer, or copy the bundle contents into a GPT-SoVITS
checkout explicitly. POSIX:

```bash
TTS_MORE_ROOT=/path/to/TTS_more
TTS_REPO=/path/to/GPT-SoVITS
mkdir -p "$TTS_REPO/tts-more"
cp -R "$TTS_MORE_ROOT/deployment/tts-repos/gpt-sovits/." "$TTS_REPO/tts-more/"
bash "$TTS_REPO/tts-more/tts-more-prepare.sh"
```

PowerShell:

```powershell
$TtsMoreRoot = "C:\path\to\TTS_more"
$TtsRepo = "C:\path\to\GPT-SoVITS"
New-Item -ItemType Directory -Force (Join-Path $TtsRepo "tts-more") | Out-Null
Copy-Item -Recurse -Force (Join-Path $TtsMoreRoot "deployment\tts-repos\gpt-sovits\*") (Join-Path $TtsRepo "tts-more")
& (Join-Path $TtsRepo "tts-more\tts-more-prepare.ps1")
```

The resulting entry points are `tts-more/tts-more-prepare.sh` and
`tts-more\tts-more-prepare.ps1`. These manual commands overwrite same-named
helper files and leave unrelated files in place. The automated installer
replaces files owned by the TTS More bundle and removes stale owned files while
preserving user-owned files.

The script delegates to the upstream `install.sh` or `install.ps1` in the
GPT-SoVITS repo root and accepts the same device/source choices through
environment variables:

- `TTS_MORE_DEVICE`: `CU128`, `CU126`, `CPU`, `ROCM`, or `MPS`
- `TTS_MORE_MODEL_SOURCE`: `Auto`, `ModelScope`, `HF-Mirror`, or `HF`

## CUDA certification limits

本目录会被顶层部署器复制到 GPT-SoVITS checkout 的 `tts-more/`。复制后的脚本方便节点排障，但**不是完整认证路径**。

正式认证只运行 [单机 Runbook](../../../docs/cuda-e2e-single-node.md) 的 `run-cuda-validation.ps1`；总入口内部调用 `deploy-local-tts.ps1` 并传递 `-RepoPaths`。直接运行 `deploy-local-tts.ps1` 仅用于通用部署或排障，不是完整认证路径；不要在认证总入口前先运行。顶层部署会核对根 `repo.lock.json`、要求 conda、先安装 `torchcodec==0.13`，再调用上游安装器并验证 CU128 runtime。

受控手工排障时，在复制后的 `tts-more` 目录运行：

```powershell
$env:TTS_MORE_DEVICE = "CU128"
$env:TTS_MORE_MODEL_SOURCE = "Auto"
.\tts-more-prepare.ps1
```

该命令成功不等于认证通过；仍需顶层 doctor、worker 契约、核心 CUDA、Playwright 和人工听审证据。
