# IndexTTS repo scripts

Copy this directory into an IndexTTS checkout when deploying that repo manually,
or let `scripts/deploy-local-tts.*` copy it into `<repo>/tts-more/`.

Run from the copied `tts-more` directory:

```bash
bash tts-more-prepare.sh
```

PowerShell:

```powershell
.\tts-more-prepare.ps1
```

Environment knobs:

- `TTS_MORE_MODEL_SOURCE`: `Auto`, `ModelScope`, `HF-Mirror`, or `HF`
- `PIP_INDEX_URL` / `UV_INDEX_URL`: optional package index override
