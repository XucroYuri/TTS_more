# Windows ComfyUI run-evidence remote capability audit (2026-08-03)

## Audit method

Both repositories were audited from their assigned isolated branches.  The
sequence was:

```text
git fetch --all --prune --tags
git for-each-ref --sort=refname refs/remotes
git merge-base --is-ancestor <remote-ref> HEAD
git rev-list --left-right --count HEAD...<remote-ref>
git log --right-only --cherry-pick --no-merges HEAD...<remote-ref>
git cherry -v HEAD <relevant-remote-ref>
```

Relevant commit patches, current file history, and deterministic tests were then
inspected.  Symbolic remote `HEAD` aliases are listed because they were visible
in `refs/remotes`; they are not counted as independent product branches.

Selection was restricted to GPT-SoVITS, IndexTTS, CosyVoice, Bridge/API,
Windows runner behavior, FFmpeg/toolchain discovery, and isolated runtimes.
Experimental engines are still recorded so that their rejection or existing
upstream coverage is explicit.

## TTS More refs

Audit base: `a47cce191807fc7dd10333300e024bf788f8b559`.

| Remote ref | SHA | Disposition | Reason |
|---|---|---|---|
| `github/HEAD` | `c28493bd971265cb21d0ef7048b25488f03cd8fe` | integrated | Symbolic alias of `github/master`; target is an ancestor of the audit base. |
| `github/master` | `c28493bd971265cb21d0ef7048b25488f03cd8fe` | integrated | Ancestor of the audit base; current GitHub deployment baseline is present. |
| `origin/master` | `8179d6fdd43af0095e2a215c12f6b6208b126bc1` | integrated | Ancestor of the audit base; the separately fetched Gitee baseline is present. |
| `github/chore/comfyui-cleanup` | `f88f995324b10ed27efb66a5bd4a95c25b7c61ae` | upstream-covered | Its resource template, ignored machine-local registry, developer variables, and removal of a machine-specific test path are superseded by `c535cfe`, `d530043`, and governance tests. |
| `github/dev-xu/comfyui-integration` | `739dbe1ddaf1f566e378b270cce2b6717b560682` | upstream-covered | Same old Bridge-template behavior as the cleanup branch; the selected base has the later Bridge contract, mandatory `resource_id`, and stronger docs/tests. |
| `github/dev-xu/windows-dual-portable-v2` | `bbe20cbac915d0303a210d36ecfa8f9ec5dd4252` | rejected | The ref is an ancestor, but the dual-portable delivery route is retired; no new legacy worker/package behavior is selected. |
| `origin/dev-xu/cuda-e2e-validation` | `fd1419369decca36799ce0eaffb92248208854ce` | upstream-covered | Ancestor of the selected base.  CUDA remains explicit opt-in and is not added to hosted CI. |

The two old ComfyUI branches have two/one patch-unique commits by patch ID, but
their useful behavior is present in the selected base through later independent
commits.  They are therefore not cherry-picked: replay would reintroduce stale
documentation/test assumptions without adding a missing contract.

## TTS-Audio-Suite refs

Audit base: `ce443482cdddd883914f589705196415bb98e332`.

| Remote ref | SHA | Disposition | Reason |
|---|---|---|---|
| `origin/HEAD` | `1d9e0f6c31309c9ad476da3d735b3aa91f61028f` | integrated | Alias of fork main; target is an ancestor of the base. |
| `origin/main` | `1d9e0f6c31309c9ad476da3d735b3aa91f61028f` | integrated | Fork main is already in the base, including Windows late-child stabilization. |
| `origin/codex/unified-voice-design-and-saving` | `1d52a512069dd086e0c5f32f06421dcf5ec937ab` | upstream-covered | Ancestor of base/upstream; unrelated to the target runtimes. |
| `origin/cosyvoice-continue` | `33643e4f7faebd12fa1505c837d210f9a198c7e3` | upstream-covered | Ancestor; later external-checkout CosyVoice behavior supersedes it. |
| `origin/debug/startup-timing-273` | `354e5ad523b7deb86a9a2db788e260c16b543c34` | deferred | Divergent generic diagnostics without target-runner behavior evidence. |
| `origin/dev-xu/comfyui-api-bridge` | `701a5351e0b2b345e8b3a12561764c5443537ef7` | integrated | Ancestor of the base; registry/route/node tests cover the Bridge contract. |
| `origin/echo-tts-integration` | `faf263d24295d6948f30d3e301d715baa32c19f8` | upstream-covered | Echo is in upstream/base history but outside the three target engines. |
| `origin/feat/omnivoice-builder` | `14fcba3bed1b709f92f82ff65be03c427c00e8a5` | upstream-covered | OmniVoice is in upstream/base history; no new integration is required. |
| `origin/feature/combined-granite-echo-testing` | `99ea0bc6f9b45278f94ca10d139720d98849c693` | upstream-covered | Granite/Echo work is an ancestor and outside target scope. |
| `origin/feature/dots-tts` | `3a817a25268b8fabf2bd8a0d859a3ae999e554e2` | upstream-covered | Dots is an ancestor and outside target scope. |
| `origin/feature/higgs-audio-transformers5-investigation` | `49baed441ce2d5aef210ef3f32ea2e671f12535e` | rejected | Divergent Higgs/Transformers 5 experiment; not a target engine. |
| `origin/feature/indextts2-engine-implementation` | `306be0ebb75856bc26feacd5ffeeb7ac9dcc42b8` | upstream-covered | Remaining patch is `git cherry` equivalent; current IndexTTS/Bridge implementation is stronger. |
| `origin/feature/isolated-engine-runtimes` | `667d12c37b0b1c4e0c6d089c3c34553b07370d91` | integrated | Ancestor of base/upstream and foundation for the current isolated subprocess behavior. |
| `origin/feature/seed-per-iteration-experimental` | `53421b28ac14b07bc8a4ff36dc74a36e6557d7dd` | rejected | Divergent experimental seed behavior; outside target scope. |
| `origin/fix-step-audio-editx-import-error-6139423650818070639` | `f44430b2414d2c9701269623211717f4b4190ced` | rejected | Step Audio EditX-only fix. |
| `origin/fix/ffmpeg-toolchain-and-tts-paths` | `4b8138daad4549451c1f5a06fb5f9a0c849ee200` | upstream-covered | Its two commits are patch-equivalent to fork `63b2b56` and `5dd7d0d`; `4537b42` adds tests. |
| `origin/gguf_failed_attempt` | `62eb5cadbb76d6b05dc77d067eb253680e7b9eb6` | rejected | Named failed attempt, divergent, and unrelated to target runtimes. |
| `origin/gpt-sovits-integration` | `7c0b73aa1401c5e688323f3a369c1cf5d49f15a7` | deferred | Commit-level decisions below; no wholesale merge. |
| `origin/jules-capabilities-doc-10075870564657213280` | `4bc6c558dfd2b678b3031a081d83d8dd6d0797b3` | rejected | Documentation-only branch. |
| `origin/pr-246` | `1c19fbbd752b96dda7ad3e90ce6d603ce24edb2b` | upstream-covered | Ancestor; unrelated voice-cache signal behavior. |
| `origin/resume/higgs-audio-v3-t5` | `03401c0b40921702abd71cf0be339099d4f4cb3e` | upstream-covered | Ancestor, but Higgs is outside scope. |
| `origin/temp-new-engine-guides-docs-20260513` | `3286ef815ee51c11f9c40a16b7a514ca9a9e57a6` | rejected | Divergent temporary engine documentation. |
| `origin/wip/kugel-transformers5` | `e477233832bee9f8d9ac18a88aa542c01e8285df` | rejected | WIP Kugel experiment. |
| `upstream/HEAD` | `2f587b22b32a42a8d1873ac0926136378c9fc44f` | upstream-covered | Alias of upstream main; only new behavior is already covered in the fork. |
| `upstream/main` | `2f587b22b32a42a8d1873ac0926136378c9fc44f` | upstream-covered | `2f587b2` nested-module probe behavior exists in `e349dc0` with a regression test; cherry-pick would regress fork installer-profile code. |
| `upstream/codex/add-voxcpm-engine` | `a2513527c94d5a2fae393cddb5ced7019bd172fb` | rejected | Divergent VoxCPM engine/training work. |
| `upstream/codex/audio8-engine` | `73a282406e6a34f6949cbcd5e8c0eaad8e1274e0` | rejected | Divergent Audio8 engine work. |
| `upstream/codex/dramabox-chatterbox-v3` | `517d11ed4c4e193155e3ecd39aa6d2627712a8e6` | upstream-covered | Ancestor; unrelated engines. |
| `upstream/codex/tada-engine` | `35cbcaca714faa3d34bfbfd8663ce87bf7cb86a5` | rejected | Divergent TADA engine/tooltips, outside scope and not on upstream main. |
| `upstream/codex/unified-voice-design-and-saving` | `1d52a512069dd086e0c5f32f06421dcf5ec937ab` | upstream-covered | Same covered SHA as fork ref. |
| `upstream/cosyvoice-continue` | `33643e4f7faebd12fa1505c837d210f9a198c7e3` | upstream-covered | Same covered CosyVoice SHA as fork ref. |
| `upstream/debug/startup-timing-273` | `354e5ad523b7deb86a9a2db788e260c16b543c34` | deferred | Same unproven generic diagnostics commit as fork ref. |
| `upstream/echo-tts-integration` | `faf263d24295d6948f30d3e301d715baa32c19f8` | upstream-covered | Same covered Echo SHA as fork ref. |
| `upstream/feat/omnivoice-builder` | `14fcba3bed1b709f92f82ff65be03c427c00e8a5` | upstream-covered | Same covered OmniVoice SHA as fork ref. |
| `upstream/feature/combined-granite-echo-testing` | `99ea0bc6f9b45278f94ca10d139720d98849c693` | upstream-covered | Same covered Granite/Echo SHA as fork ref. |
| `upstream/feature/dots-tts` | `3a817a25268b8fabf2bd8a0d859a3ae999e554e2` | upstream-covered | Same covered Dots SHA as fork ref. |
| `upstream/feature/higgs-audio-transformers5-investigation` | `49baed441ce2d5aef210ef3f32ea2e671f12535e` | rejected | Same rejected Higgs experiment. |
| `upstream/feature/indextts2-engine-implementation` | `306be0ebb75856bc26feacd5ffeeb7ac9dcc42b8` | upstream-covered | Same patch-equivalent old IndexTTS ref. |
| `upstream/feature/isolated-engine-runtimes` | `667d12c37b0b1c4e0c6d089c3c34553b07370d91` | integrated | Same covered isolated-runtime ancestor. |
| `upstream/feature/seed-per-iteration-experimental` | `53421b28ac14b07bc8a4ff36dc74a36e6557d7dd` | rejected | Same rejected seed experiment. |
| `upstream/fix-step-audio-editx-import-error-6139423650818070639` | `f44430b2414d2c9701269623211717f4b4190ced` | rejected | Same out-of-scope Step Audio EditX fix. |
| `upstream/gguf_failed_attempt` | `62eb5cadbb76d6b05dc77d067eb253680e7b9eb6` | rejected | Same named failed attempt. |
| `upstream/jules-capabilities-doc-10075870564657213280` | `4bc6c558dfd2b678b3031a081d83d8dd6d0797b3` | rejected | Same documentation-only ref. |
| `upstream/pr-246` | `1c19fbbd752b96dda7ad3e90ce6d603ce24edb2b` | upstream-covered | Same covered ancestor. |
| `upstream/resume/higgs-audio-v3-t5` | `03401c0b40921702abd71cf0be339099d4f4cb3e` | upstream-covered | Same covered Higgs ancestor; no new integration. |
| `upstream/temp-new-engine-guides-docs-20260513` | `3286ef815ee51c11f9c40a16b7a514ca9a9e57a6` | rejected | Same temporary documentation branch. |
| `upstream/wip/kugel-transformers5` | `e477233832bee9f8d9ac18a88aa542c01e8285df` | rejected | Same WIP Kugel experiment. |

## GPT-SoVITS divergent commit decisions

| Commit | Disposition | Evidence/reason |
|---|---|---|
| `5eb772159e965fa7c37bd6a9b5eaee05dced6ecc` | rejected | Superseded branch-local design; current architecture uses the machine-local registry and API Bridge. |
| `c598229e94baa2ad2267a94512346ada2c997573` | upstream-covered | Current GPT nodes/runtime are present; fork commits from `dccdda0` onward bind the official runtime and add deterministic behavior tests. |
| `8f2a881a5602cd65deea009ec11d114425309580` | upstream-covered | Checkout binding is covered by `59ced47`, `0d5c67f`, and later isolated registered-runtime commits. |
| `094264cfc15656ff84f205a27eeca4d18e16f3bb` | rejected | Plugin-owned downloader/model layout conflicts with external official checkouts and private registry ownership. |
| `94b6a8810f99ea57091fc8934bea854653378681` | deferred | Broad all-engine console rewrite has no focused Windows-encoding RED/GREEN behavior proof. |
| `7c0b73aa1401c5e688323f3a369c1cf5d49f15a7` | upstream-covered | IndexTTS/CosyVoice source-root behavior is covered by the registry, Bridge, and isolated subprocess tests. |

## Integrated commit evidence

No new remote product patch was selected in Task 1; avoiding a redundant or
regressive cherry-pick is the audit result.  Existing selected behavior is bound
to these commits:

| Capability | Existing commit evidence |
|---|---|
| Plugin Bridge/API | `701a5351e0b2b345e8b3a12561764c5443537ef7` and later fork commits |
| Side-effect-free installer probe | `e349dc005181873f1b6755ff73f4b0b50c0e0feb` |
| FFmpeg/FFprobe fail-closed discovery | `63b2b56440532700b1b9217171918349e11c8baa` |
| Dynamic ComfyUI TTS model paths | `5dd7d0d29bc16cf0857b7f43f740a8e62872f5e3` |
| Toolchain/path behavior tests | `4537b42aa6eddeb20ac01b0cb89d177a8a97a96b` |
| TTS More Audio Suite alignment | `c535cfe294ec198190394e53eaf468938929bc6f` |
| Single-GPU capacity default | `1e416b01674bfc45448b9922815cf4f21a5eeb87` |
| Bridge deployment guidance | `d5300431ab1ead1900a13d911502f871cda93377` |

## Exclusions and privacy

Fix11/Fix12 shared-root deletion, quarantine, rollback, and recursive-removal
experiments were not integrated.  Official source repositories were not
modified.  This report contains no machine-local registry value, model/fixture
path, token, private command, or private fixture content.  Hosted CI remains
deterministic; real CUDA validation remains opt-in.
