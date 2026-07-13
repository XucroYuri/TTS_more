# TTS More app deployment assets

This directory is for the TTS More application itself. Files here are used by
the app-side one-click deployment workflow and are intentionally separate from
the scripts copied into upstream TTS repositories.

## Repo path confirmation

Copy `repo-paths.example.json` to `repo-paths.local.json` and confirm every
selected service path, including paths that are unchanged from `repo.lock.json`:

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

The confirmation object accepts only unique formal `service_id` keys and must
contain every selected service. Managed local worker paths must stay inside the
dedicated `<TTS More>/repo/` area, and existing Git origins must match the lock.
Managed checkouts must use a checkout-local `.git` directory; symlink/reparse
metadata and `gitdir:` files used by worktrees/submodules are rejected.
Use the per-repo scripts under `deployment/tts-repos/` for manual deployments in
external TTS repo directories.

The standalone updater is a four-file bundle: `tts-more-update.sh`,
`tts-more-update.ps1`, `tts-more-update.py`, and `tts-more-update.json`.
Repositories with submodules do not receive it and must be updated through the
TTS More managed `sync-repos` workflow.
