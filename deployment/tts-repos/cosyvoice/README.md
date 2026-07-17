# CosyVoice repo scripts

Before running either prepare helper, use the app's managed sync-repos path with
the complete repository confirmation file:

```bash
cp deployment/app/repo-paths.example.json deployment/app/repo-paths.local.json
scripts/tts-more.sh sync-repos --service-ids local-cosyvoice --repo-paths deployment/app/repo-paths.local.json
```

That sync selects the final locked/latest superproject state, validates every
resolved `.gitmodules` URL, and updates submodules. The prepare bundle **does not run Git submodule commands**;
it only installs dependencies and models.

CosyVoice does not receive the four-file standalone updater. Run all repository
updates through the TTS More managed `sync-repos` workflow so the superproject
and its validated submodules move together.

Use the app-side installer, or copy the bundle contents into a CosyVoice
checkout explicitly. POSIX:

```bash
TTS_MORE_ROOT=/path/to/TTS_more
TTS_REPO=/path/to/CosyVoice
mkdir -p "$TTS_REPO/tts-more"
cp -R "$TTS_MORE_ROOT/deployment/tts-repos/cosyvoice/." "$TTS_REPO/tts-more/"
bash "$TTS_REPO/tts-more/tts-more-prepare.sh"
```

PowerShell:

```powershell
$TtsMoreRoot = "C:\path\to\TTS_more"
$TtsRepo = "C:\path\to\CosyVoice"
New-Item -ItemType Directory -Force (Join-Path $TtsRepo "tts-more") | Out-Null
Copy-Item -Recurse -Force (Join-Path $TtsMoreRoot "deployment\tts-repos\cosyvoice\*") (Join-Path $TtsRepo "tts-more")
& (Join-Path $TtsRepo "tts-more\tts-more-prepare.ps1")
```

The resulting entry points are `tts-more/tts-more-prepare.sh` and
`tts-more\tts-more-prepare.ps1`. These manual commands overwrite same-named
helper files and leave unrelated files in place. The automated installer
replaces files owned by the TTS More bundle and removes stale owned files while
preserving user-owned files.

Environment knobs:

- `TTS_MORE_MODEL_SOURCE`: `Auto`, `ModelScope`, `HF-Mirror`, or `HF`
- `TTS_MORE_BASE_PYTHON`: optional Python executable for the repo venv

## CUDA certification limits

本目录会被顶层部署器复制到 CosyVoice checkout 的 `tts-more/`。复制后的脚本方便节点排障，但**不是完整认证路径**。

正式认证只运行 [单机 Runbook](../../../docs/cuda-e2e-single-node.md) 的 `run-cuda-validation.ps1`；总入口内部调用 `deploy-local-tts.ps1` 并传递 `-RepoPaths`。直接运行 `deploy-local-tts.ps1` 仅用于通用部署或排障，不是完整认证路径；不要在认证总入口前先运行。顶层流程会处理 `openai-whisper`/setuptools 兼容、避开 requirements 中旧 torch，并安装验证指定 CU128 runtime。

受控手工排障时，在复制后的 `tts-more` 目录运行：

```powershell
$env:TTS_MORE_MODEL_SOURCE = "Auto"
$env:TTS_MORE_BASE_PYTHON = "python"
.\tts-more-prepare.ps1
```

该命令成功不等于认证通过；仍需顶层 doctor、核心 CUDA、Playwright 和人工听审证据。
