# CosyVoice repo scripts

Copy this directory into a CosyVoice checkout when deploying that repo manually,
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
- `TTS_MORE_BASE_PYTHON`: optional Python executable for the repo venv
