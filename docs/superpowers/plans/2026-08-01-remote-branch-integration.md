# Remote Branch Integration and Release Plan

> **Execution rule:** Keep the two repository histories separate. Complete and merge the TTS-Audio-Suite PR before finalizing the TTS More PR.

**Goal:** Integrate upstream TTS-Audio-Suite 5.6.2 and the two fork fixes without regressing the validated GPT-SoVITS, IndexTTS, and CosyVoice flow, selectively absorb useful TTS More PR #27 work, then merge both repositories through PRs and synchronize TTS More GitHub/Gitee master.

**Architecture:** Official ComfyUI and the three model projects remain read-only dependencies. TTS-Audio-Suite owns model/runtime compatibility and the API Bridge. TTS More owns orchestration, stable resource IDs, queue capacity, application state, and public deployment documentation.

---

## Task 1: Commit the approved integration baseline

**Files:**
- `docs/superpowers/specs/2026-08-01-remote-branch-integration-design.md`
- `docs/superpowers/plans/2026-08-01-remote-branch-integration.md`

- [ ] Verify both files match the approved stable-integration scope.
- [ ] Commit the design and plan on `dev-xu/comfyui-live-validation`.

## Task 2: Create the plugin integration branch and capture baselines

**Repository:** `F:\Code\Github\TTS-Audio-Suite`

- [ ] Confirm local `main`, `origin/main`, `upstream/main`, and `origin/fix/ffmpeg-toolchain-and-tts-paths` SHAs.
- [ ] Create `dev-xu/upstream-5.6.2-tts-more-integration` from the validated local `main`.
- [ ] Record current unit-test results before merging upstream.
- [ ] Confirm the ComfyUI and three model repositories are clean or only contain pre-existing user changes.

## Task 3: Merge upstream 5.6.2 into the plugin

**Expected overlap:**
- `README.md`, `__init__.py`, `install.py`, `nodes.py`, `requirements.txt`
- `engines/adapters/__init__.py`
- `nodes/unified/tts_srt_node.py`, `nodes/unified/tts_text_node.py`
- `utils/audio/cache.py`
- `utils/models/engine_registry.py`, `utils/models/unified_model_interface.py`
- `utils/text/segment_parameters.py`

- [ ] Merge `upstream/main` without committing the unresolved result.
- [ ] Use upstream 5.6.2 registration and optional-engine structure as the base.
- [ ] Reapply the API Bridge nodes, external adapters, resource registry, and `tts_more_targets` profile.
- [ ] Keep new optional engines out of the target profile and protect their imports behind dependency checks.
- [ ] Run syntax/import tests and the focused Bridge suite.
- [ ] Commit the upstream integration separately.

## Task 4: Port the fork toolchain and model-path fixes with tests

**Source commits:**
- `ba9ce7a` — FFmpeg/FFprobe detection
- `4b8138d` — ComfyUI TTS model-path discovery

**Expected files:**
- `utils/ffmpeg_utils.py`
- `engines/f5tts/f5tts.py`
- `utils/models/tts_paths.py`
- `utils/models/f5tts_manager.py`
- focused files under `tests/unit/`

- [ ] Add failing tests for executable discovery, PATH repair, missing-tool diagnostics, and ComfyUI TTS roots.
- [ ] Port the changes onto the integrated structure.
- [ ] Avoid permanently caching model roots if ComfyUI may add model paths after import; prove the selected behavior with a test.
- [ ] Run focused tests, Bridge tests, and relevant upstream engine-manager tests.
- [ ] Commit the two fixes as an independently reviewable change.

## Task 5: Validate the integrated plugin

- [ ] Run the complete available plugin unit suite in the ComfyUI Python environment.
- [ ] Run `install.py` with `TTS_AUDIO_SUITE_INSTALL_PROFILE=tts_more_targets` and then `pip check`.
- [ ] Start official ComfyUI with the private resource registry.
- [ ] Verify `/system_stats`, `/object_info`, and `/api/tts-audio-suite/v1/capabilities`.
- [ ] Run GPT-SoVITS, IndexTTS, and CosyVoice twice each through the existing live validation runner, including unload between attempts.
- [ ] Preserve exact logs and evidence for any regression; fix only in TTS-Audio-Suite when ownership is proven.
- [ ] Confirm official ComfyUI and model repositories were not modified.

## Task 6: Publish and merge the plugin PR

- [ ] Push `dev-xu/upstream-5.6.2-tts-more-integration` to `XucroYuri/TTS-Audio-Suite`.
- [ ] Create a ready PR to `main` describing included upstream work, preserved Bridge behavior, fork fixes, tests, and deferred Draft engines.
- [ ] Inspect all hosted checks and review state.
- [ ] Fix failures on the same branch and repeat local gates.
- [ ] Merge only after required checks pass or the repository has no configured checks and the full local gate is recorded.
- [ ] Fast-forward local plugin `main` and verify it equals `origin/main`.

## Task 7: Selectively absorb TTS More PR #27

**Candidate files:**
- `.gitignore`
- `README.md`
- `deployment/tts-repos/resources.yaml.example`
- `docs/comfyui-integration.md`
- `backend/tests/test_comfyui_adversarial_audit.py`

- [ ] Port only current-architecture content; do not merge the branch wholesale.
- [ ] Keep private paths out of the repository and use symbolic placeholders in examples.
- [ ] Keep `resource_id` mandatory, single-GPU `capacity=1`, and local bind defaults.
- [ ] Ensure deterministic CI does not require a live ComfyUI endpoint; gate live tests behind an explicit environment switch.
- [ ] Add or update focused tests before implementation where behavior changes.
- [ ] Record PR #27 as superseded after the integrated PR merges.

## Task 8: Run final TTS More validation

- [ ] Update the validation report with the merged plugin SHA and remote-branch assessment.
- [ ] Run backend full pytest with the supported Python 3.11 environment.
- [ ] Run frontend tests and production build.
- [ ] Run release-governance and hardcoded-path checks.
- [ ] Restart from the merged plugin `main` and rerun all three real engine flows.
- [ ] Verify TTS More-visible output/history, non-silent WAV, unload, and a second request per engine.
- [ ] Confirm the TTS More worktree contains only intentional changes.

## Task 9: Publish and merge the TTS More PR

- [ ] Push `dev-xu/comfyui-live-validation` to GitHub.
- [ ] Create a ready PR to `master` with architecture, plugin dependency, test, live evidence, and known-limit summaries.
- [ ] Monitor and repair GitHub Actions failures without weakening tests.
- [ ] Merge the PR after required gates pass.
- [ ] Close the conflicting PR #27 as superseded with a link to the merged PR.

## Task 10: Synchronize remotes and close the loop

- [ ] Fetch all TTS More remotes with pruning.
- [ ] Fast-forward local `master` to GitHub `master`.
- [ ] Push the merged GitHub `master` to Gitee `master`.
- [ ] Verify full SHA equality for local `master`, `github/master`, and `origin/master` separately.
- [ ] Verify no unexpected open integration PRs remain.
- [ ] Remove only owned temporary worktrees/branches after checking for uncommitted changes.
- [ ] Report merged PR URLs, final SHAs, test counts, live evidence, deferred engines, and any residual limitation.
