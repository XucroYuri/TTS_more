# GPT-SoVITS repo scripts

Copy this directory into a GPT-SoVITS checkout when deploying that repo
manually, or let `scripts/deploy-local-tts.*` copy it into `<repo>/tts-more/`.

Run from the copied `tts-more` directory:

```bash
bash tts-more-prepare.sh
```

PowerShell:

```powershell
.\tts-more-prepare.ps1
```

The script delegates to the upstream `install.sh` or `install.ps1` in the
GPT-SoVITS repo root and accepts the same device/source choices through
environment variables:

- `TTS_MORE_DEVICE`: `CU128`, `CU126`, `CPU`, `ROCM`, or `MPS`
- `TTS_MORE_MODEL_SOURCE`: `Auto`, `ModelScope`, `HF-Mirror`, or `HF`
