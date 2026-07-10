# TTS More app deployment assets

This directory is for the TTS More application itself. Files here are used by
the app-side one-click deployment workflow and are intentionally separate from
the scripts copied into upstream TTS repositories.

## Repo path confirmation

Copy `repo-paths.example.json` to `repo-paths.local.json` when the local checkout
paths differ from `repo.lock.json`, then edit the paths for this machine:

```bash
cp deployment/app/repo-paths.example.json deployment/app/repo-paths.local.json
```

Pass the file explicitly:

```bash
scripts/deploy-local-tts.sh --repo-paths deployment/app/repo-paths.local.json
```

The default workflow follows `default_selected` in `repo.lock.json` and deploys
GPT-SoVITS main, IndexTTS, and CosyVoice. Use `--targets dev` for the GPT-SoVITS
regression checkout or `--targets all` for every locked repository. The example
path file contains all entries so the same file can also support explicit
regression deployments.

For managed local workers, paths must stay inside the TTS More project root.
Use the per-repo scripts under `deployment/tts-repos/` for manual deployments in
external TTS repo directories.
