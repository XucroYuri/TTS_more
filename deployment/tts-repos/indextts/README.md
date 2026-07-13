# IndexTTS repo scripts

Use the app-side installer, or copy the bundle contents into an IndexTTS
checkout explicitly. POSIX:

```bash
TTS_MORE_ROOT=/path/to/TTS_more
TTS_REPO=/path/to/index-tts
mkdir -p "$TTS_REPO/tts-more"
cp -R "$TTS_MORE_ROOT/deployment/tts-repos/indextts/." "$TTS_REPO/tts-more/"
bash "$TTS_REPO/tts-more/tts-more-prepare.sh"
```

PowerShell:

```powershell
$TtsMoreRoot = "C:\path\to\TTS_more"
$TtsRepo = "C:\path\to\index-tts"
New-Item -ItemType Directory -Force (Join-Path $TtsRepo "tts-more") | Out-Null
Copy-Item -Recurse -Force (Join-Path $TtsMoreRoot "deployment\tts-repos\indextts\*") (Join-Path $TtsRepo "tts-more")
& (Join-Path $TtsRepo "tts-more\tts-more-prepare.ps1")
```

The resulting entry points are `tts-more/tts-more-prepare.sh` and
`tts-more\tts-more-prepare.ps1`. These manual commands overwrite same-named
helper files and leave unrelated files in place. The automated installer
replaces files owned by the TTS More bundle and removes stale owned files while
preserving user-owned files.

Environment knobs:

- `TTS_MORE_MODEL_SOURCE`: `Auto`, `ModelScope`, `HF-Mirror`, or `HF`
- `PIP_INDEX_URL` / `UV_INDEX_URL`: optional package index override
