# ComfyUI local TTS validation report

> Current final status (2026-08-01): the original Task 9 deterministic/direct/
> browser validation and the later TTS-Audio-Suite upstream 5.6.2 integration
> refresh are complete. The refresh adds six fresh direct runs and a merged,
> four-platform-green plugin PR. This is a functional and lifecycle validation,
> subject to the limitations at the end of this report.

## Result boundary

The final checked source successfully synthesized GPT-SoVITS, IndexTTS, and
CosyVoice in sequence through this path:

```text
TTS More live-validation CLI
  -> official ComfyUI on 127.0.0.1:8188
  -> TTS-Audio-Suite API Bridge
  -> registered one-shot engine runtime
  -> non-silent WAV
```

Each Task 9 request created a fresh ComfyUI prompt, completed with history
`status_str=success`, reported `execution_cached.nodes=[]`, produced a decoded
non-silent WAV, and converged to an empty ComfyUI queue, zero target-engine
runtimes, zero external runners, and zero request temporary directories. A
pre-existing idle CosyVoice runtime remained during the GPT-SoVITS and IndexTTS
checks; the full three-engine sequence ended with zero Bridge runtimes. The
official ComfyUI and model repositories were not cleaned, patched, or updated
during the rerun.

This proves the direct Bridge path on the recorded machine. A subsequent fresh
visible TTS More browser workflow on the same product source also completed all
three engines and a CosyVoice rerun after an observed empty-runtime release;
its application/job/history/audio evidence is recorded below.

## Validated source and runtime

| Component | Revision / runtime | Final state |
| --- | --- | --- |
| TTS More | `9cc86ceaa78d4e6f2133143af84bb40662722ba0` | upstream-5.6.2 refresh source; prior browser validation used `e546f8d`; report/governance changes followed |
| TTS-Audio-Suite | `107213edf6fcbebabfa56a3e0284fec8f2cf9b55` | merged `main` from PR #3; live synthesis used product-equivalent `4537b42` before the final CI/test-only commits |
| official ComfyUI | `5cc026f5b81b3f01fe7a1438a0fd4131d2ebda25` | clean; local tracking divergence `0/0` |
| official GPT-SoVITS checkout | `f8a5865c472c0d21c204965a9bb6e002aceb36fe` | exactly two pre-existing metadata-dirty files preserved; 64 behind the existing tracking ref |
| official IndexTTS checkout | `3c7c3dee516800511bb9ea3dccef22a1e710c05b` | pre-existing untracked configuration/data/reference paths preserved; 66 behind |
| official CosyVoice checkout | `7ebe0f3444d724d25d7111f93fa02c8223511e1b` | pre-existing untracked virtual environment preserved; 64 behind |

Remote refs were not fetched during the final boundary capture; ahead/behind
values above describe the already-present local tracking refs, not a claim
about current remote tips.

## Upstream 5.6.2 integration refresh

The plugin refresh merged upstream `Version 5.6.2` while retaining the TTS More
three-engine resource bridge, the target install profile, the FFmpeg/FFprobe
preflight, and dynamic ComfyUI `TTS` model-path discovery. The merged pull
request is `XucroYuri/TTS-Audio-Suite#3`, merge commit `107213e`. Official
ComfyUI and the three official TTS checkouts were not modified.

The exact product code at plugin commit `4537b42` completed two fresh requests
per engine. The later merge commit only adds CI dependency coverage and
cross-platform test corrections, so these results apply to the merged product
code without claiming that test-only changes were separately synthesized.

| Engine / run | Prompt | Result |
| --- | --- | --- |
| GPT-SoVITS 1 | `16dbbb0a-b1e7-4b34-99a5-6623aa90a6e7` | 352,044-byte WAV; 32,000 Hz; 176,000 frames; peak 0.89746; 39.75 s |
| GPT-SoVITS 2 | `5b2db6e4-da17-4e45-8d1f-3d797458f1d1` | 410,924-byte WAV; 32,000 Hz; 205,440 frames; peak 0.90137; 39.64 s |
| IndexTTS 1 | `ed9cee0b-c6f9-4a66-8154-bf98cf059ee3` | 356,396-byte WAV; 22,050 Hz; 178,176 frames; peak 0.76141; 134.80 s |
| IndexTTS 2 | `d78e34ad-0192-4ba8-b90c-309b7ada5a7f` | 292,396-byte WAV; 22,050 Hz; 146,176 frames; peak 0.82773; 47.30 s |
| CosyVoice 1 | `74327325-b5e4-4963-911d-cc3e0a9170f6` | 318,252-byte WAV; 24,000 Hz; 159,104 frames; peak 0.57471; 104.54 s |
| CosyVoice 2 | `b80941ec-4361-41e1-bd81-b136f63e7933` | 260,852-byte WAV; 24,000 Hz; 130,404 frames; peak 0.52350; 32.05 s |

All six evidence files report `status=passed` and `cleanup_error=null`; the
queue and Bridge runtime registry were empty afterward. Evidence remains under
the machine-private `2026-08-01-remote-integration` validation directory and is
not committed.

Additional issues found during the refresh:

- The existing portable ComfyUI runtime failed under `PYTHONNOUSERSITE=1` with
  missing SQLAlchemy. It is therefore not considered self-contained. The
  authoritative runs used the isolated ComfyUI venv, whose target-profile
  install and `pip check` passed.
- An initial GPT wrapper timed out before its outer deadline, although ComfyUI
  later completed the prompt. It was excluded; the two tabled GPT runs used a
  corrected outer timeout.
- Minimal CI initially omitted the declared `psutil` process-cleanup
  dependency, hiding Qwen3/RVC node registration. After installing it, CI
  exposed Windows-only interpreter fixtures and an exact floating-point timing
  assertion. CI now installs the core dependency, uses platform-native venv
  paths, and checks a bounded grace interval.
- The final plugin gate is 326 passed / 2 skipped locally, and the PR passed
  Ubuntu and Windows on Python 3.12 and 3.13.
- Draft Audio8/VoxCPM work found on other remote branches was deliberately not
  merged: it is outside the validated three-engine bridge and needs a separate
  architecture and model-runtime acceptance cycle.

The validated host used an NVIDIA GeForce RTX 4060 Ti with driver 591.86.
ComfyUI used Python 3.12.3, PyTorch 2.7.1+cu128, and CUDA 12.8. TTS More and the
IndexTTS/CosyVoice one-shot runtimes used their Python 3.11 environments. The
registered GPT-SoVITS resource used a separately configured compatible Python
3.12 portable interpreter. All application listeners remained loopback-only:
frontend 5173, backend 8000, and ComfyUI 8188.

## Reproducible private setup

Keep machine-specific paths out of source control. Define local variables such
as these in the validation shell:

```powershell
$TTS_MORE_ROOT = '<tts-more-checkout>'
$COMFYUI_ROOT = '<official-comfyui-checkout>'
$TTS_AUDIO_SUITE_ROOT = '<tts-audio-suite-checkout>'
$GPT_ROOT = '<official-gpt-sovits-checkout>'
$INDEX_ROOT = '<official-indextts-checkout>'
$COSY_ROOT = '<official-cosyvoice-checkout>'
$PRIVATE_ROOT = '<private-tts-more-state>'
$VALIDATION_ROOT = Join-Path $PRIVATE_ROOT 'validation/comfyui-live'
$REGISTRY = Join-Path $PRIVATE_ROOT 'config/tts-audio-suite-resources.yaml'
$COMFYUI_PY = '<isolated-comfyui-python>'
$BACKEND_PY = Join-Path $TTS_MORE_ROOT '.venv/Scripts/python.exe'
```

The private registry must contain exactly one ready entry for each resource ID
`gpt-sovits-local`, `indextts-local`, and `cosyvoice-local`. Each entry binds
its official checkout/model path; the GPT entry may also bind a compatible
private `python_executable`. Do not expose private paths through public
capabilities or commit the registry. For a single GPU/resource group, use
`capacity: 1` and a shared group such as `comfyui-local-0`.

Install official ComfyUI requirements in the isolated ComfyUI environment,
link `TTS-Audio-Suite` as a ComfyUI custom node, and install the plugin's
configuration-only target profile:

```powershell
& $COMFYUI_PY -m pip install -r (Join-Path $COMFYUI_ROOT 'requirements.txt')
$env:TTS_AUDIO_SUITE_INSTALL_PROFILE = 'tts_more_targets'
$env:PYTHONNOUSERSITE = '1'
Push-Location $TTS_AUDIO_SUITE_ROOT
& $COMFYUI_PY install.py
& $COMFYUI_PY -m pip check
Pop-Location
```

Start official ComfyUI on the mandated loopback endpoint:

```powershell
$env:TTS_AUDIO_SUITE_RESOURCES = $REGISTRY
$env:TTS_AUDIO_SUITE_INSTALL_PROFILE = 'tts_more_targets'
$env:PYTHONNOUSERSITE = '1'
Push-Location $COMFYUI_ROOT
& $COMFYUI_PY main.py --listen 127.0.0.1 --port 8188
Pop-Location
```

Before synthesis, require `/system_stats`, Bridge capabilities, and
`/object_info` to show all three ready resources plus
`TTSExternalGPTSovitsEngine`, `TTSExternalIndexTTSEngine`,
`TTSExternalCosyVoiceEngine`, `TTSExternalAudioAsset`, `UnifiedTTSTextNode`,
and `SaveAudio`. Configuration readiness alone is not synthesis proof.

Run one engine at a time. A representative direct command is:

```powershell
Push-Location (Join-Path $TTS_MORE_ROOT 'backend')
& $BACKEND_PY -m app.comfyui.live_validation `
  --engine '<gpt-sovits|indextts|cosyvoice>' `
  --resource-id '<registered-resource-id>' `
  --base-url 'http://127.0.0.1:8188' `
  --reference-audio '<local-reference-wav>' `
  --reference-text '<paired-text-or-empty>' `
  --text '<fresh-synthesis-text>' `
  --output (Join-Path $VALIDATION_ROOT 'outputs/<run>.wav') `
  --evidence (Join-Path $VALIDATION_ROOT 'evidence/<run>.json')
Pop-Location
```

After every request, verify the prompt is new, history is successful and
uncached, the WAV decodes with non-zero peak/RMS, and the runtime, queue,
runner, and request-temp counts all return to zero before starting the next
engine.

## Chronological issues and repairs

| Task | Observed issue | Resolution / current boundary |
| --- | --- | --- |
| 1 | A reusable real-request harness and evidence contract were missing; arbitrary ComfyUI targets would weaken the local proof. | Added the CLI, WAV/non-silence validation, atomic evidence, cleanup-on-failure, and exact `127.0.0.1:8188` enforcement. |
| 2 | Registry capability checks could be mistaken for synthesis readiness. | Captured an immutable machine baseline and a private three-resource registry; kept configuration-ready and inference-ready claims separate. |
| 3 | The original runtime inherited external site packages; broad plugin installation timed out and created incompatible multi-engine dependency metadata. | Rebuilt a recoverably isolated ComfyUI venv and introduced `tts_more_targets`, which installs and registers only the configuration/Bridge surface and passes `pip check`. |
| 4 | The rebuilt venv initially contained CPU-only Torch, and the target profile omitted `UnifiedTTSTextNode`. | Installed the matched cu128 Torch stack and added the dependency-clean text node to the target allowlist. Official ComfyUI remained unmodified. |
| 5 | IndexTTS ignored the registered checkout, ran in incompatible ComfyUI Python, hid terminal errors, could reuse cached waveforms, and needed bounded Windows descendant/temp cleanup. | Added checkout-local one-shot execution, prompt error propagation, registered-cache bypass, redirected caches, explicit cleanup, and Job Object containment with fallback. Two fresh post-cleanup requests passed. |
| 6 | A registered v1 CosyVoice model was sent through v3 download verification, hanging the ComfyUI process; a probe also exposed unsafe local-cache download behavior. | Bound the registered checkout/model, selected the v1 loader, forced offline local files, blocked sockets, used contained one-shot execution, resampled to 24 kHz, and bypassed the registered waveform cache. |
| 7 | GPT-SoVITS imported into ComfyUI Python and returned silence after construction failure; the checkout environment had an incompatible Torch/Torchaudio pair; later review found legacy/profile runtime-binding regressions. | Bound a private compatible interpreter through the registry, used offline read-only one-shot execution, surfaced terminal errors, bypassed registered caching, and preserved checkout/interpreter bindings during legacy/profile flows. |
| 8 | App defaults allowed capacity 3 on one GPU; browser QA found registry-backed GPT validation still demanded legacy weight paths and passed legacy `cut5` directly to a ComfyUI enum. | Defaulted to capacity 1, made GPT validation endpoint-aware, and normalized legacy cut methods at the workflow boundary. Task 8 browser generation then passed for all engines plus a post-release CosyVoice rerun. |
| 9 | A final-source deterministic, direct, and browser-visible evidence refresh was required. Two early GPT attempts exposed evidence-wrapper aggregation/exit-code defects after successful inference. | Fixed only the external evidence harness, preserved both adjudications, completed authoritative direct runs for all engines, then completed GPT v003, IndexTTS v002, CosyVoice v003, a successful release-all, and CosyVoice v004 in the browser. Product source was unchanged. |

## Deterministic final gates

All commands were rerun on the final checked source with raw stdout, stderr,
duration, and exit-code sidecars under the private validation root.

| Gate | Result |
| --- | --- |
| TTS More backend: `test_api.py`, `test_comfyui_client.py`, `test_service_queue.py`, `test_comfyui_live_validation.py` | 140 passed in 35.47 s; exit 0 |
| TTS More frontend test | 25 files / 151 tests passed; exit 0 |
| TTS More frontend build | TypeScript + Vite, 1,762 modules; built in 1.92 s; exit 0 |
| TTS-Audio-Suite Bridge: resource registry, engine nodes, routes, runtime registry | 122 passed, 1 warning in 3.08 s; exit 0 |

The authoritative aggregate is
`[validation-root]/task-9-final-rerun-deterministic-gates.json`.

## Final direct synthesis evidence

The three authoritative Task 9 runs were strictly sequential.

| Engine | Prompt | Output proof | Cleanup proof |
| --- | --- | --- | --- |
| GPT-SoVITS | `b8881d55-4c44-4a56-8651-7ae570f17275` | 720,684-byte PCM16 mono WAV; 32,000 Hz; 11.26 s; peak 0.9140625; RMS 0.13459224; SHA-256 `8515972DB37A70BB509EA031C8689466FEBB2370A5CE90865F123FF228E49FBB` | Compatible registered portable runner observed; history success/uncached; GPT runtime, queue, runner, and temp all 0 |
| IndexTTS | `44442dd9-f22d-4894-a384-b3ccfd4d6ef2` | 330,284-byte PCM16 mono WAV; 22,050 Hz; 7.488435 s; peak 0.65618896; RMS 0.09546190; SHA-256 `DD2DEFED0B0D1A21C37FF48125896116B201BE5FEE1F210A6DB31C52A4AD20B1` | Checkout-local Python 3.11 runner observed; history success/uncached; runtime, queue, runner, and temp all 0 |
| CosyVoice | `1c4c524b-282b-4915-9719-a7b88c38859c` | 355,032-byte PCM16 mono WAV; 24,000 Hz; 7.395583 s; peak 0.66201782; RMS 0.07329022; SHA-256 `3227B28EBF3A21408BCF9A0ABDBE5B5C367B767007EABB4DD0465C6EC8D5B279` | Checkout-local Python 3.11 runner observed; history success/uncached; runtime, queue, runner, and temp all 0 |

GPT used the Task 7 official paired reference WAV/transcription and the same
final accepted second-run synthesis text. IndexTTS and CosyVoice used their
final accepted Task 5/6 parameters. Matching the prior GPT and CosyVoice audio
hashes is expected for these deterministic repeated inputs; the fresh prompt
IDs, empty cached-node lists, observed new runners, and cleanup evidence prove
that these were new executions rather than copied artifacts.

Authoritative evidence names:

- `[validation-root]/task-9-final-rerun-live-summary.json`
- `[validation-root]/evidence/task-9-final-rerun-<engine>.json` (GPT uses the
  `gpt-sovits-fix2` suffix)
- `[validation-root]/task-9-final-rerun-<engine>-history.json`
- `[validation-root]/task-9-final-rerun-<engine>-monitor.json`
- `[validation-root]/task-9-final-rerun-<engine>-verification.json`
- `[validation-root]/outputs/task-9-final-rerun-<engine>.wav`

The first two GPT wrapper attempts are retained separately as
`gpt-sovits-harness-failure` and `gpt-sovits-fix1-harness-adjudication`.
Both underlying inference requests succeeded; neither is counted as the
authoritative Task 9 GPT result.

## Earlier repeated-request and browser evidence

Before this final rerun, Tasks 5-7 had already established two uncached,
post-cleanup direct requests per engine on their then-final source:

| Engine | Accepted pair | Key result |
| --- | --- | --- |
| IndexTTS | `task-5-fix2-run1`, `task-5-fix2-run2` | Distinct prompts/runners; 22.05 kHz non-silent WAVs; queue, runner, and temp returned to zero after each request. |
| CosyVoice | `task-6-fix1-run1`, `task-6-fix1-run2` | Distinct prompts/runners; 24 kHz non-silent WAVs; registered waveform cache bypassed; cleanup converged. |
| GPT-SoVITS | `task-7-fix3-final-run1`, `task-7-fix3-final-run2` | Distinct prompts/runners/hashes; 32 kHz non-silent WAVs; assets and pre-existing checkout changes remained byte-identical. |

Task 8 also completed a visible application pass on the same TTS More commit:
GPT-SoVITS v002, IndexTTS v001, CosyVoice v001, and a CosyVoice v002 rerun after
a successful runtime release all had distinct TTS More jobs and ComfyUI prompts,
successful histories, and playable local WAVs. The final viewport showed all
three lines completed and a visible waveform/player, with zero browser-console
warnings or errors after reload.

That Task 8 evidence remains useful historical proof. Two Task 8 full-page
captures were blank because of the browser capture tool and are excluded; only
the validated viewport capture and structured application/history/audio
evidence count.

## Final browser validation

The Task 9 browser pass generated one line at a time on TTS More product source
`e546f8da5bb000b160084386cc1ee2933a8e6c28`. All four TTS More jobs and
manifest versions completed, their ComfyUI histories reported success, and
their local outputs strictly matched the expected RIFF/WAVE PCM16 metadata.

| Engine / version | TTS More job | ComfyUI prompt | Validated WAV |
| --- | --- | --- | --- |
| GPT-SoVITS v003 | `job-b19f5885ff27` | `9df93316-dc71-40b1-98dc-c35ea107cb78` | 454,444 bytes; mono 32,000 Hz; 7.100000 s; SHA-256 `8008E64F501D9B036C31AC718BA83F403800B363B9099E614BB832B15332D3DF` |
| IndexTTS v002 | `job-e2637aaabc32` | `3924b634-ffa8-4de1-ae48-a70cd9f3d7a0` | 391,724 bytes; mono 22,050 Hz; 8.881633 s; SHA-256 `37BD417B202CBC175DB6334B81D2B2100F3FFB88935FC528A1F4B5A5D406A139` |
| CosyVoice v003 | `job-4fdbab397be6` | `7f7b62d4-2bd1-4e01-84e5-035eae6df26e` | 345,558 bytes; mono 24,000 Hz; 7.198208 s; SHA-256 `4C3A559C5A2E08630C37ABDAAF174578209F3300DBD6184EF30002364CFC414E` |
| CosyVoice v004, after release | `job-26011570d8b1` | `8cb218a4-48a0-4c9e-af5b-fd97d87f0438` | 345,558 bytes; mono 24,000 Hz; 7.198208 s; SHA-256 `4C3A559C5A2E08630C37ABDAAF174578209F3300DBD6184EF30002364CFC414E` |

Every WAV was decoded in full and was non-silent. The two CosyVoice outputs are
byte-identical for the same deterministic input, but they belong to distinct
TTS More jobs, manifest versions, ComfyUI prompts, output paths, and successful
history records.

The raw lifecycle proof
`[validation-root]/task-9-browser-release-proof.json` has SHA-256
`83E415115E5F23FDCAA38746DD4460CB9BAE5201CC599C90907033FB37DF096A`.
It records HTTP 200, exactly one released CosyVoice runtime, empty `busy` and
`errors`, `runtime_after.runtimes=[]`, and an empty ComfyUI queue. Its timestamp
falls after v003 completed and before v004 was created.

The v004 ComfyUI history reports cached node `1`, which is
`TTSExternalCosyVoiceEngine`, the engine-configuration node. The synthesis node
`3` and `SaveAudio` were not cached. The final runtime's `loaded_at` is after
the empty-runtime release proof, so the single final idle CosyVoice runtime was
recreated by v004; this report does not call the entire v004 workflow uncached.

Final application/runtime state:

- TTS More has eight historical completed jobs total, zero queued, and zero
  running; the four Task 9 jobs above are the newest successful versions.
- ComfyUI queue is `0/0`.
- The final Bridge status contains one expected idle, non-busy CosyVoice
  runtime created by the post-release v004 request.
- Exactly three application services remain ready, each at capacity one in
  `comfyui-local-0`; capabilities contain exactly the three ready registered
  resources.
- `task-9-browser-final-viewport.jpg` is a 1280x720 JPEG/JFIF, 83,166 bytes,
  SHA-256 `ED4337A1C9A0BED0FD992C053DDF50425FD1D6ACC31CB382F0CFC8139452E164`.
  It shows project `ComfyUI 本机验证`, all three lines completed and marked
  latest/playable, version badges 3/2/4, and the GPT v003 player/waveform.
- The browser console inspection after the final state reported zero warnings
  and zero errors.

Authoritative Task 9 browser evidence:

- `[validation-root]/task-9-browser-final-evidence.json`
- `[validation-root]/task-9-browser-final-audio-validation.json`
- `[validation-root]/task-9-browser-final-queue.json`
- `[validation-root]/task-9-browser-final-manifest.json`
- `[validation-root]/task-9-browser-final-prompt-histories.json`
- `[validation-root]/task-9-browser-final-runtime.json`
- `[validation-root]/task-9-browser-final-capabilities.json`
- `[validation-root]/task-9-browser-final-services.json`
- `[validation-root]/task-9-browser-final-comfy-queue.json`
- `[validation-root]/task-9-browser-release-proof.json`
- `[validation-root]/task-9-browser-final-viewport.jpg`

## Boundary and preservation proof

The final before/after comparison reports `all_preserved=true`:

- all six repository HEADs, branches, tracking divergence, and porcelain
  entries are unchanged across direct validation;
- the GPT checkout retains exactly its two Task 7 baseline modifications, with
  matching length, mtime, and worktree blob;
- all nine registered GPT/reference assets, the private registry, and the
  portable interpreter match their baseline hashes;
- listener identities on 5173, 8000, and 8188 are unchanged; and
- ComfyUI queue is `0/0`, Bridge runtime count is 0, external runner count is
  0, and request-temp count is 0.

Evidence:

- `[validation-root]/task-9-final-rerun-boundary-before.json`
- `[validation-root]/task-9-final-rerun-boundary-after.json`
- `[validation-root]/task-9-final-rerun-boundary-comparison.json`

## Remaining limitations

- This validation is functional and lifecycle-oriented, not a formal latency,
  throughput, concurrency, audio-quality, or long-duration stability benchmark.
- GPT-SoVITS relies on a private compatible interpreter whose Torch,
  Torchaudio, source, and model compatibility must remain aligned.
- IndexTTS and CosyVoice official checkouts intentionally retain pre-existing
  local runtime/model artifacts and are behind their existing tracking refs.
- Optional ComfyUI CUDA optimization warnings do not affect the validated cu128
  path, but upgrades require a fresh compatibility pass.
- The target install profile proves Bridge configuration imports and dependency
  integrity; only real inference proves model/runtime readiness.
- Browser console counts and visual observations come from the main-agent
  browser inspection; final evidence consolidation independently re-read the
  queue, manifest, histories, audio, runtime, capabilities, and services without
  operating the browser, submitting inference, or restarting services.
- The final idle CosyVoice runtime is expected post-rerun state, not a claim of
  full runtime unload after the last request. The earlier raw release proof is
  the authoritative observation of the empty state between v003 and v004.
- No official checkout cleanup, source rewrite, remote fetch, or service-process
  operation was performed during final evidence consolidation.
