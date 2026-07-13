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
