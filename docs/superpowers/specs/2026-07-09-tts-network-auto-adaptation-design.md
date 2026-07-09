# TTS Network Auto Adaptation Deployment Design

## Summary

Extend the TTS More local deployment tooling with an automatic network profile that chooses fast, reliable installation and model-download sources for the current machine. The default behavior should favor China-accessible sources when they are healthy, then fall back to global sources when local probes fail or are slower. The deployment should also reduce first-time setup time through shallow repository checkout, shared caches, and resumable model downloads.

The accepted approach is a centralized `Auto` network profile implemented in the TTS More deployment layer. Upstream TTS repositories remain unmodified.

## Goals

- Make `scripts/prepare-tts-repos.ps1` and `scripts/prepare-tts-repos.sh` default to `Auto` instead of a hard-coded source.
- Detect source availability and relative speed from the local network before install/download work begins.
- Prefer domestic-friendly routes for China mainland users when they are usable.
- Fall back to slower global routes automatically when domestic routes fail.
- Reuse package and model caches across GPT-SoVITS branches, IndexTTS, and CosyVoice.
- Keep app-only, worker-node, and local-all deployment profiles compatible with distributed deployments.
- Keep default model setup full-quality and full-featured.

## Non-Goals

- Do not modify upstream TTS repositories unless an upstream script cannot be controlled from the outer deployment layer.
- Do not create a full offline artifact mirror in this phase.
- Do not include quantized, distilled, simplified, small, low-memory, or quality-reduced models in the recommended base setup.
- Do not silently replace full-quality models with smaller alternatives based on hardware limits.

## Model Baseline Policy

The recommended deployment path installs only the full-function, full-quality baseline models required by each TTS service.

Model variants that trade quality, features, or fidelity for speed, size, lower VRAM, or easier installation are treated as manual advanced options. The automation may document that such variants exist, but it must not download, select, recommend, or configure them as part of the default install.

If a machine cannot run the full-quality model set, `doctor` should report the limitation and point the user to manual downgrade instructions. The default installer should not auto-downgrade.

## Architecture

### Deployment Helper

Add network-profile support to `scripts/tts_more_deploy.py`.

New command:

```text
probe-network
```

Responsibilities:

- Probe a small candidate set with short timeouts.
- Rank available endpoints by success and latency.
- Choose a model source, Hugging Face endpoint, PyPI index, PyTorch wheel index policy, and cache directories.
- Write an ignored local profile to `data/local/network-profile.json`.
- Print the resolved profile as JSON for shell wrappers.

The command should support:

- `--mode auto|china|global`
- `--write`
- `--force`
- `--timeout-seconds`
- `--ttl-hours`
- `--output`

Environment overrides:

- `TTS_MORE_NETWORK_PROFILE=auto|china|global`
- `TTS_MORE_MODEL_SOURCE=Auto|ModelScope|HF-Mirror|HF`
- `TTS_MORE_PIP_INDEX_URL`
- `TTS_MORE_EXTRA_PIP_INDEX_URL`
- `TTS_MORE_HF_ENDPOINT`
- `TTS_MORE_CACHE_ROOT`

### Prepare Scripts

Change both wrappers to accept `Auto`:

- PowerShell: `[ValidateSet("Auto", "ModelScope", "HF", "HF-Mirror")]`
- Bash: `--source Auto` by default

When `Source=Auto`, the wrapper calls `probe-network --write` before installing dependencies. It then exports the selected environment variables for the child installer process.

Manual source selection remains available and bypasses automatic model-source choice while still applying shared cache paths.

### Source Candidates

Domestic-friendly candidates:

- ModelScope: `https://www.modelscope.cn`
- HF Mirror: `https://hf-mirror.com`
- PyPI mirrors such as Aliyun and Tsinghua
- Existing Gitee mirror route for IndexTTS BigVGAN resources where applicable

Global fallback candidates:

- Hugging Face: `https://huggingface.co`
- PyPI: `https://pypi.org/simple`
- PyTorch wheel indexes under `https://download.pytorch.org/whl/`

The probe should prefer domestic candidates when they succeed within the healthy latency threshold. If all domestic candidates fail or are materially slower than the global source, the profile may choose global.

### Repository Sync Speed

Update `sync-repos` to use faster clone defaults:

```text
git clone --depth 1 --filter=blob:none --branch <branch> --single-branch <remote> <path>
```

If partial clone is unsupported, retry without `--filter=blob:none`. If the locked commit is not present in the shallow clone, fetch the required branch or commit and then verify `HEAD`.

Existing commit checks remain mandatory.

### Shared Cache Layout

Use a repo-local ignored cache root by default:

```text
data/cache/
  pip/
  uv/
  huggingface/
  modelscope/
  torch/
  downloads/
```

Wrappers export:

- `PIP_CACHE_DIR`
- `UV_CACHE_DIR`
- `HF_HOME`
- `HUGGINGFACE_HUB_CACHE`
- `TRANSFORMERS_CACHE`
- `MODELSCOPE_CACHE`

This cache root can be overridden with `TTS_MORE_CACHE_ROOT`.

### Service-Specific Behavior

GPT-SoVITS main, dev, and xucroyuri/proplus-hc-dev:

- Continue to call each branch's official installer.
- Pass the resolved source as `HF`, `HF-Mirror`, or `ModelScope`.
- Share package and model cache environment where upstream scripts respect it.
- Do not change the selected model quality tier.

IndexTTS:

- Prefer the resolved source rather than relying on upstream `auto`.
- Use `modelscope` for `ModelScope`, `huggingface` for `HF` or `HF-Mirror`.
- Set `HF_ENDPOINT=https://hf-mirror.com` when the resolved source is `HF-Mirror`.
- Keep full IndexTTS model resources as the default.

CosyVoice:

- Use ModelScope when selected.
- Use Hugging Face or HF Mirror otherwise.
- Keep `CosyVoice-300M` as the configured baseline for this implementation.

## Data Flow

1. User runs `prepare-tts-repos` with default options.
2. Wrapper calls `tts_more_deploy.py probe-network --write`.
3. Probe returns a profile with selected source and cache env.
4. Wrapper exports env values and performs optional repo sync.
5. Wrapper installs dependencies for selected repos.
6. Wrapper downloads full-quality model resources from the selected source.
7. Wrapper renders `data/local/services.json`.
8. `doctor` reports repo state, selected network profile, and cache paths.

## Error Handling

- Probe failures must not abort setup when at least one fallback route is available.
- If no route works, fail early with a clear message listing failed candidates.
- If a chosen source fails during model download, retry with the next compatible source.
- If shallow clone fails due server capability, retry with a regular single-branch clone.
- If full-quality model resources cannot be downloaded, stop and report the missing resource. Do not substitute a reduced model.
- If cache directories cannot be created, fall back to normal tool defaults and warn.

## Testing

Follow TDD for implementation.

Add focused tests for:

- Auto selects ModelScope or HF Mirror when domestic probes are healthy.
- Auto falls back to global Hugging Face when domestic probes fail.
- Manual `Source` bypasses source selection but still gets cache env.
- `probe-network --write` produces the expected JSON shape.
- `doctor` includes network profile and cache diagnostics.
- `sync-repos --dry-run` emits shallow/partial clone arguments.
- Clone command generation retries without partial clone when needed.
- Prepare script dry-runs show `Auto` defaults and selected source propagation.
- Model baseline policy is documented and no default path references quantized or reduced model variants.

## Documentation

Update:

- `README.md`
- `docs/deployment.md`
- `docs/open-source-tts-services.md`
- `.env.example`

Document:

- Default `Auto` source behavior.
- China-first, fallback-to-global strategy.
- Cache locations and overrides.
- How to force `ModelScope`, `HF-Mirror`, or `HF`.
- Full-quality baseline policy and manual-only downgrade variants.

## Acceptance Criteria

- A fresh user can run one prepare command without manually choosing a source.
- China-friendly sources are tried first when reachable and healthy.
- Global fallback works when domestic candidates fail.
- Repeated installs reuse shared caches.
- Repository clone is faster by default while still verifying locked commits.
- Default install does not select quantized, simplified, distilled, small, or quality-reduced models.
- Existing service rendering and worker start commands remain compatible.
