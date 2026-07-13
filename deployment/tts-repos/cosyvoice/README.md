# CosyVoice repo scripts

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
