# ComfyUI + TTS-Audio-Suite Local Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update the official source ComfyUI runtime, install and validate the XucroYuri TTS-Audio-Suite fork, reuse local official-model assets for GPT-SoVITS, IndexTTS, and CosyVoice, and produce an evidence-backed TTS More end-to-end solution.

**Architecture:** TTS More remains the application and orchestration layer. Official ComfyUI remains an unmodified HTTP workflow runtime, while the XucroYuri TTS-Audio-Suite fork owns resource registration and model compatibility. All three TTS repositories are read-only model/code-structure sources; their WebUIs and legacy TTS More workers stay stopped.

**Tech Stack:** Windows PowerShell, Python 3.11 for TTS More, Python 3.12 + PyTorch 2.7.1 CUDA 12.8 for ComfyUI, FastAPI, httpx, soundfile, ComfyUI HTTP API, TTS-Audio-Suite API Bridge, React/Vite.

## Global Constraints

- Only the linked TTS More worktree at `F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation` and `F:\Code\Github\TTS-Audio-Suite` may receive source changes.
- Run all TTS More source, test, and application commands from `F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation`. Keep model assets under the existing primary checkout at `F:\Code\Github\TTS_more\repo`.
- `F:\Code\Github\ComfyUI` must track official `Comfy-Org/ComfyUI` `master` without local patches or commits.
- GPT-SoVITS, IndexTTS, and CosyVoice repositories must not be modified or started as WebUIs/workers.
- ComfyUI must bind to `127.0.0.1:8188`.
- All three logical endpoints share `resource_group=comfyui-local-0` and use `capacity=1`.
- Private absolute model paths belong only in `F:\TTS-More\config\tts-audio-suite-resources.yaml`.
- Raw logs, evidence JSON, and generated audio belong under `F:\TTS-More\validation\2026-07-31-comfyui-live` and must not be committed.
- Record the original failure before changing code or dependencies.
- A health response, capabilities response, or successful node import is not a real TTS pass.
- A real engine pass requires a completed prompt, a decodable non-silent audio file, TTS More-visible status/result, cleanup, and a successful second request after cleanup.

---

## File Map

### TTS More source changes

- Create `backend/app/comfyui/live_validation.py`: reusable live-engine validation CLI and evidence writer.
- Create `backend/tests/test_comfyui_live_validation.py`: deterministic unit tests for endpoint construction, audio validation, evidence serialization, and cleanup.
- Modify `backend/app/open_source_tts.py`: change the ComfyUI endpoint capacity default from `3` to `1`.
- Modify `backend/tests/test_api.py`: lock API-created ComfyUI endpoints to the single-GPU default.
- Modify `docs/comfyui-integration.md`: make all single-GPU examples and guidance use `capacity=1`.
- Create `docs/comfyui-local-validation-report.md`: sanitized final problem matrix, confirmed commands, results, and remaining limitations.

### TTS-Audio-Suite source changes, only when a reproduced failure requires them

- `api_bridge/resource_registry.py`: resource schema/path validation findings.
- `nodes/api_bridge/resource_engine_nodes.py`: external engine node input or mapping findings.
- `api_bridge/routes.py`: capabilities, asset, or runtime-release API findings.
- `engines/adapters/gpt_sovits_adapter.py`: GPT-SoVITS official-model compatibility findings.
- `engines/adapters/index_tts_adapter.py`: IndexTTS official-model compatibility findings.
- `engines/adapters/cosyvoice_adapter.py`: CosyVoice official-model compatibility findings.
- `tests/unit/test_api_bridge_resource_registry.py`, `tests/unit/test_api_bridge_engine_nodes.py`, or `tests/unit/test_api_bridge_routes.py`: regression test beside every plugin fix.

### Local-only files

- `F:\TTS-More\config\tts-audio-suite-resources.yaml`
- `F:\TTS-More\validation\2026-07-31-comfyui-live\baseline.txt`
- `F:\TTS-More\validation\2026-07-31-comfyui-live\comfyui.stdout.log`
- `F:\TTS-More\validation\2026-07-31-comfyui-live\comfyui.stderr.log`
- `F:\TTS-More\validation\2026-07-31-comfyui-live\evidence\*.json`
- `F:\TTS-More\validation\2026-07-31-comfyui-live\outputs\*.wav`

---

### Task 1: Add a reusable live ComfyUI validation runner

**Files:**
- Create: `backend/app/comfyui/live_validation.py`
- Create: `backend/tests/test_comfyui_live_validation.py`

**Interfaces:**
- Produces: `LiveValidationConfig`, `LiveValidationResult`, `build_live_endpoint()`, `validate_audio_file()`, `validate_live_engine()`, and `main()`.
- Consumes: `TTSServiceEndpoint`, `SynthesisRequest`, `ScriptLine`, `build_service_client()`, and `soundfile`.
- CLI entry: `python -m app.comfyui.live_validation`.

- [ ] **Step 1: Create the supported TTS More Python 3.11 environment**

```powershell
if (-not (Test-Path F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\.venv\Scripts\python.exe)) { py -3.11 -m venv F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\.venv }
F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\.venv\Scripts\python.exe -m pip install -e 'F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\backend[dev]'
```

- [ ] **Step 2: Write failing endpoint and audio-validation tests**

```python
def test_build_live_endpoint_uses_audio_suite_and_capacity_one(tmp_path):
    config = LiveValidationConfig(
        engine="indextts",
        resource_id="indextts-local",
        base_url="http://127.0.0.1:8188",
        reference_audio=tmp_path / "voice.wav",
        reference_text="",
        text="这是 IndexTTS 的真实验证。",
        output_path=tmp_path / "out.wav",
        evidence_path=tmp_path / "evidence.json",
    )
    endpoint = build_live_endpoint(config)
    assert endpoint.api_contract == "comfyui-tts-audio-suite-v1"
    assert endpoint.engine == EngineName.INDEX_TTS
    assert endpoint.capacity == 1
    assert endpoint.resource_group == "comfyui-local-0"
    assert endpoint.default_params["resource_id"] == "indextts-local"


def test_validate_audio_file_rejects_silence(tmp_path):
    path = tmp_path / "silent.wav"
    soundfile.write(path, [0.0] * 16000, 16000)
    with pytest.raises(ValueError, match="silent"):
        validate_audio_file(path)
```

- [ ] **Step 3: Run the focused tests and confirm they fail**

Run from `F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\backend`:

```powershell
F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\.venv\Scripts\python.exe -m pytest tests\test_comfyui_live_validation.py -q
```

Expected: collection fails because `app.comfyui.live_validation` does not exist.

- [ ] **Step 4: Implement the configuration, result, endpoint, and audio contracts**

```python
@dataclass(frozen=True)
class LiveValidationConfig:
    engine: Literal["gpt-sovits", "indextts", "cosyvoice"]
    resource_id: str
    base_url: str
    reference_audio: Path
    reference_text: str
    text: str
    output_path: Path
    evidence_path: Path
    timeout_seconds: float = 900.0


@dataclass
class LiveValidationResult:
    engine: str
    resource_id: str
    status: Literal["passed", "failed"]
    started_at: str
    duration_seconds: float
    output_path: str | None
    output_size: int
    sample_rate: int
    frames: int
    peak: float
    metadata: dict[str, Any]
    progress: list[dict[str, Any]]
    error: str | None
    cleanup_error: str | None


def validate_audio_file(path: Path) -> dict[str, int | float]:
    samples, sample_rate = soundfile.read(path, dtype="float32", always_2d=True)
    peak = float(abs(samples).max()) if samples.size else 0.0
    if sample_rate <= 0 or samples.shape[0] == 0:
        raise ValueError("generated audio is empty")
    if peak <= 1e-5:
        raise ValueError("generated audio is silent")
    return {"sample_rate": int(sample_rate), "frames": int(samples.shape[0]), "peak": peak}
```

`build_live_endpoint()` must map `gpt-sovits`, `indextts`, and `cosyvoice` to their existing enums, set `api_contract="comfyui-tts-audio-suite-v1"`, `mode="external"`, `resource_group="comfyui-local-0"`, `capacity=1`, and store `engine`, `resource_id`, `poll_interval=2.0`, and the configured timeout in `default_params`.

- [ ] **Step 5: Write a failing cleanup/evidence test with a fake client**

```python
def test_validate_live_engine_writes_failure_evidence_and_unloads(tmp_path):
    fake = FakeLiveClient(fail_with=RuntimeError("model load failed"))
    config = make_config(tmp_path)
    result = validate_live_engine(config, client_factory=lambda endpoint: fake)
    assert result.status == "failed"
    assert result.error == "model load failed"
    assert fake.unload_calls == 1
    payload = json.loads(config.evidence_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["resource_id"] == "indextts-local"
```

- [ ] **Step 6: Implement live execution and atomic evidence output**

`validate_live_engine()` must:

1. call `health()` and require `ready=True`;
2. call `capabilities()` and require the requested `resource_id`;
3. create a `SynthesisRequest` with the reference audio and reference text;
4. collect prompt progress callbacks;
5. call `synthesize()`;
6. validate the resulting WAV;
7. call `unload()` in `finally`;
8. write the result through a temporary JSON file followed by `Path.replace()`;
9. return a failed result rather than losing the original exception.

- [ ] **Step 7: Implement the CLI**

Required arguments:

```text
--engine
--resource-id
--base-url
--reference-audio
--reference-text
--text
--output
--evidence
--timeout-seconds
```

Exit `0` only for `status=passed`; print the evidence JSON path and exit `1` for a recorded failure.

- [ ] **Step 8: Run deterministic tests**

```powershell
F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\.venv\Scripts\python.exe -m pytest tests\test_comfyui_live_validation.py tests\test_comfyui_client.py -q
```

Expected: all tests pass and no live ComfyUI connection is attempted.

- [ ] **Step 9: Commit the validation runner**

```powershell
git add backend/app/comfyui/live_validation.py backend/tests/test_comfyui_live_validation.py
git commit -m "test: add live ComfyUI validation runner"
```

---

### Task 2: Capture the immutable baseline and create the private resource registry

**Files:**
- Create locally: `F:\TTS-More\config\tts-audio-suite-resources.yaml`
- Create locally: `F:\TTS-More\validation\2026-07-31-comfyui-live\baseline.txt`

**Interfaces:**
- Produces: three stable resource IDs consumed by ComfyUI and TTS More.
- Consumes: existing local model directories only.

- [ ] **Step 1: Create the evidence directories**

```powershell
New-Item -ItemType Directory -Force -Path 'F:\TTS-More\config','F:\TTS-More\validation\2026-07-31-comfyui-live\evidence','F:\TTS-More\validation\2026-07-31-comfyui-live\outputs' | Out-Null
```

- [ ] **Step 2: Record repository, runtime, GPU, port, and model-file baseline**

Capture these commands in `baseline.txt`:

```powershell
git -C F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation status --short --branch
git -C F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation rev-parse HEAD
git -C F:\Code\Github\ComfyUI status --short --branch
git -C F:\Code\Github\ComfyUI rev-parse HEAD
git -C F:\Code\Github\TTS-Audio-Suite status --short --branch
git -C F:\Code\Github\TTS-Audio-Suite rev-parse HEAD
F:\venvs\comfyui-tts\Scripts\python.exe -c "import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.get_device_name(0)); print(torch.cuda.get_device_properties(0).total_memory)"
nvidia-smi --query-gpu=name,driver_version,memory.total,memory.free --format=csv,noheader
Get-NetTCPConnection -State Listen -LocalPort 8188 -ErrorAction SilentlyContinue
```

Also record `Get-Item` results for the two GPT-SoVITS weights, the IndexTTS checkpoint directory, the CosyVoice model directory, and the three reference-audio files used by later tasks.

- [ ] **Step 3: Create the exact resource registry**

```yaml
version: 1
resources:
  gpt-sovits-local:
    engine: gpt_sovits
    source_root: F:/Code/Github/TTS_more/repo/GPT-SoVITS-main
    gpt_weight: F:/Code/Github/TTS_more/repo/GPT-SoVITS-main/GPT_SoVITS/pretrained_models/s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt
    sovits_weight: F:/Code/Github/TTS_more/repo/GPT-SoVITS-main/GPT_SoVITS/pretrained_models/s2G488k.pth
    bert_path: F:/Code/Github/TTS_more/repo/GPT-SoVITS-main/GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large
    cnhubert_path: F:/Code/Github/TTS_more/repo/GPT-SoVITS-main/GPT_SoVITS/pretrained_models/chinese-hubert-base
    version: v2
  indextts-local:
    engine: index_tts
    source_root: F:/Code/Github/TTS_more/repo/index-tts
    model_dir: F:/Code/Github/TTS_more/repo/index-tts/checkpoints
  cosyvoice-local:
    engine: cosyvoice
    source_root: F:/Code/Github/TTS_more/repo/CosyVoice
    model_dir: F:/Code/Github/TTS_more/repo/CosyVoice/pretrained_models/CosyVoice-300M
```

- [ ] **Step 4: Validate the registry without starting ComfyUI**

Run from `F:\Code\Github\TTS-Audio-Suite`:

```powershell
$env:PYTHONPATH='F:\Code\Github\TTS-Audio-Suite'
F:\venvs\comfyui-tts\Scripts\python.exe -c "from pathlib import Path; from api_bridge.resource_registry import ResourceRegistry; r=ResourceRegistry.load(Path(r'F:\TTS-More\config\tts-audio-suite-resources.yaml')); print(r.capabilities())"
```

Expected: three ready resources with IDs `gpt-sovits-local`, `indextts-local`, and `cosyvoice-local`.

- [ ] **Step 5: Record any missing file as the first issue before changing a model path**

Append a JSON object to the external issue evidence with stage `resource_registry`, exact missing label, exact command, repository SHAs, and `status=unresolved`. Do not modify a TTS repository to satisfy the registry.

---

### Task 3: Update official ComfyUI and install the TTS-Audio-Suite fork

**Files:**
- No source files are expected to change in official ComfyUI.
- TTS-Audio-Suite source changes are allowed only after a reproduced plugin failure.

**Interfaces:**
- Produces: an updated official ComfyUI runtime and a dependency-complete plugin environment.
- Consumes: `F:\venvs\comfyui-tts`.

- [ ] **Step 1: Prove both worktrees are clean**

```powershell
git -C F:\Code\Github\ComfyUI status --porcelain
git -C F:\Code\Github\TTS-Audio-Suite status --porcelain
```

Expected: no output. Stop if either repository contains unrelated local changes.

- [ ] **Step 2: Fast-forward official ComfyUI**

```powershell
git -C F:\Code\Github\ComfyUI fetch --prune origin
git -C F:\Code\Github\ComfyUI pull --ff-only origin master
git -C F:\Code\Github\ComfyUI rev-list --left-right --count HEAD...origin/master
```

Expected final divergence: `0  0`. Do not commit in this repository.

- [ ] **Step 3: Fast-forward the TTS-Audio-Suite fork**

```powershell
git -C F:\Code\Github\TTS-Audio-Suite fetch --prune origin
git -C F:\Code\Github\TTS-Audio-Suite pull --ff-only origin main
git -C F:\Code\Github\TTS-Audio-Suite rev-list --left-right --count HEAD...origin/main
```

Expected final divergence: `0  0`.

- [ ] **Step 4: Synchronize official ComfyUI requirements**

```powershell
F:\venvs\comfyui-tts\Scripts\python.exe -m pip install -r F:\Code\Github\ComfyUI\requirements.txt
```

- [ ] **Step 5: Run the plugin's official installer in the same Python environment**

```powershell
Set-Location F:\Code\Github\TTS-Audio-Suite
F:\venvs\comfyui-tts\Scripts\python.exe install.py
F:\venvs\comfyui-tts\Scripts\python.exe -m pip check
```

Expected: installer exits `0`; `pip check` reports no broken requirements.

- [ ] **Step 6: Run the API Bridge unit suite**

```powershell
Set-Location F:\Code\Github\TTS-Audio-Suite
F:\venvs\comfyui-tts\Scripts\python.exe -m pytest tests\unit\test_api_bridge_resource_registry.py tests\unit\test_api_bridge_engine_nodes.py tests\unit\test_api_bridge_routes.py tests\unit\test_api_bridge_runtime_registry.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Record installer or test failures before repair**

Store the command, exit code, failing test names, exception summary, and full log path in an evidence JSON file. A plugin dependency or Bridge failure is owned by the TTS-Audio-Suite fork; an official ComfyUI requirement failure is an environment issue unless a reproducible plugin constraint conflict proves otherwise.

---

### Task 4: Start ComfyUI and validate the Bridge contract

**Files:**
- Local logs only.
- Plugin source/test files only if the recorded startup failure belongs to the fork.

**Interfaces:**
- Produces: live `http://127.0.0.1:8188` with Bridge capabilities and external nodes.

- [ ] **Step 1: Start official ComfyUI with the private registry**

```powershell
$env:TTS_AUDIO_SUITE_RESOURCES='F:\TTS-More\config\tts-audio-suite-resources.yaml'
$process = Start-Process `
  -FilePath 'F:\venvs\comfyui-tts\Scripts\python.exe' `
  -ArgumentList @('main.py','--listen','127.0.0.1','--port','8188') `
  -WorkingDirectory 'F:\Code\Github\ComfyUI' `
  -RedirectStandardOutput 'F:\TTS-More\validation\2026-07-31-comfyui-live\comfyui.stdout.log' `
  -RedirectStandardError 'F:\TTS-More\validation\2026-07-31-comfyui-live\comfyui.stderr.log' `
  -WindowStyle Hidden `
  -PassThru
$process.Id
```

- [ ] **Step 2: Poll readiness with a 60-second bounded loop**

```powershell
$ready = $false
for ($attempt = 1; $attempt -le 60; $attempt++) {
  try {
    $response = Invoke-RestMethod -Uri 'http://127.0.0.1:8188/system_stats' -TimeoutSec 2
    if ($response) {
      $ready = $true
      break
    }
  } catch {}
  Start-Sleep -Seconds 1
}
if (-not $ready) {
  throw 'ComfyUI did not become ready within 60 seconds'
}
```

If readiness is not reached, record both logs before changing anything.

- [ ] **Step 3: Verify capabilities and node registration**

```powershell
$capabilities = Invoke-RestMethod -Uri 'http://127.0.0.1:8188/api/tts-audio-suite/v1/capabilities' -TimeoutSec 10
$objects = Invoke-RestMethod -Uri 'http://127.0.0.1:8188/object_info' -TimeoutSec 30
$capabilities.resources
$objects.PSObject.Properties.Name | Where-Object {
  $_ -in @('TTSExternalGPTSovitsEngine','TTSExternalIndexTTSEngine','TTSExternalCosyVoiceEngine','TTSExternalAudioAsset','UnifiedTTSTextNode','SaveAudio')
}
```

Expected: three ready resources and all six required nodes.

- [ ] **Step 4: Fix only reproduced fork-owned startup failures**

For each fork-owned failure:

1. add the smallest failing unit test in the matching TTS-Audio-Suite test file;
2. run it and confirm the original failure;
3. make the minimum plugin change;
4. rerun the focused test and the four-file Bridge suite;
5. restart ComfyUI and repeat capabilities/node checks;
6. commit the plugin test and fix with a finding-specific message.

Do not patch official ComfyUI.

---

### Task 5: Validate IndexTTS directly through the Bridge

**Files:**
- Local evidence and output.
- TTS-Audio-Suite Index adapter/tests only if a reproduced adapter failure requires a fix.

**Interfaces:**
- Consumes: `indextts-local` and `repo\index-tts\examples\voice_01.wav`.
- Produces: `outputs\indextts.wav` and `evidence\indextts.json`.

- [ ] **Step 1: Run the first real synthesis attempt**

Run from `F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\backend`:

```powershell
F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\.venv\Scripts\python.exe -m app.comfyui.live_validation `
  --engine indextts `
  --resource-id indextts-local `
  --base-url http://127.0.0.1:8188 `
  --reference-audio F:\Code\Github\TTS_more\repo\index-tts\examples\voice_01.wav `
  --reference-text '' `
  --text '这是 IndexTTS 通过 TTS More、ComfyUI 和 TTS Audio Suite 生成的真实验证音频。' `
  --output F:\TTS-More\validation\2026-07-31-comfyui-live\outputs\indextts.wav `
  --evidence F:\TTS-More\validation\2026-07-31-comfyui-live\evidence\indextts.json `
  --timeout-seconds 900
```

- [ ] **Step 2: Inspect evidence, audio metrics, ComfyUI history, and GPU state**

Require `status=passed`, nonzero duration, peak above `1e-5`, prompt metadata, and a decodable WAV. Record `nvidia-smi` before and after `unload()`.

- [ ] **Step 3: If the attempt fails, preserve and classify the original failure**

Classify it as environment, registry, Bridge schema, Index adapter, model asset, TTS More client, or cleanup. Only Bridge/adapter failures may modify the plugin; only client/workflow/evidence failures may modify TTS More.

- [ ] **Step 4: Add a regression test before an allowed source fix**

Use the recorded error as the exact assertion in either `backend/tests/test_comfyui_client.py`, `backend/tests/test_comfyui_live_validation.py`, or `tests/unit/test_api_bridge_engine_nodes.py`. Confirm red, apply the minimum fix, confirm green, commit in the owning repository, restart ComfyUI, and rerun Step 1.

- [ ] **Step 5: Repeat after cleanup**

Run the same command a second time with output `indextts-second.wav` and evidence `indextts-second.json`. Both attempts must pass.

---

### Task 6: Validate CosyVoice directly through the Bridge

**Files:**
- Local evidence and output.
- TTS-Audio-Suite CosyVoice adapter/tests only after a reproduced failure.

**Interfaces:**
- Consumes: `cosyvoice-local` and the same neutral IndexTTS sample as reference audio.
- Produces: `outputs\cosyvoice.wav` and `evidence\cosyvoice.json`.

- [ ] **Step 1: Run the existing CosyVoice-300M model attempt**

```powershell
F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\.venv\Scripts\python.exe -m app.comfyui.live_validation `
  --engine cosyvoice `
  --resource-id cosyvoice-local `
  --base-url http://127.0.0.1:8188 `
  --reference-audio F:\Code\Github\TTS_more\repo\index-tts\examples\voice_01.wav `
  --reference-text '' `
  --text '这是 CosyVoice 通过统一 ComfyUI 运行环境生成的真实验证音频。' `
  --output F:\TTS-More\validation\2026-07-31-comfyui-live\outputs\cosyvoice.wav `
  --evidence F:\TTS-More\validation\2026-07-31-comfyui-live\evidence\cosyvoice.json `
  --timeout-seconds 900
```

- [ ] **Step 2: Preserve any model-architecture incompatibility before intervention**

Record the exact model loader exception, expected configuration keys, current `CosyVoice-300M` files, plugin adapter commit, and ComfyUI commit. Do not edit the CosyVoice repository.

- [ ] **Step 3: Apply a plugin-side compatibility fix only when the official model architecture is supported**

Add a failing CosyVoice adapter unit test, implement the minimum adapter/model-loader selection change, run the focused test and Bridge suite, commit in TTS-Audio-Suite, restart ComfyUI, and rerun Step 1.

If the plugin explicitly requires a different official CosyVoice generation and adaptation is not valid, stop this engine after recording the required official model ID and download size; do not silently replace the model.

- [ ] **Step 4: Repeat after cleanup**

On a pass, repeat with `cosyvoice-second.wav` and `cosyvoice-second.json`.

---

### Task 7: Validate GPT-SoVITS directly through the Bridge

**Files:**
- Local evidence and output.
- TTS-Audio-Suite GPT-SoVITS adapter/tests only after a reproduced failure.

**Interfaces:**
- Consumes: `gpt-sovits-local`, official v2 base weights, and a repository-provided paired reference WAV/text.
- Produces: `outputs\gpt-sovits.wav` and `evidence\gpt-sovits.json`.

- [ ] **Step 1: Run the official v2 base-weight attempt**

Use:

- reference WAV: `F:\Code\Github\TTS-Audio-Suite\voices_examples\Sophie_Anderson CC3.wav`
- reference text: `It was just brilliant because because people put me down in the past as well about my look as you have you heard it would be people want to put you down because it makes them feel secure.`

```powershell
F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\.venv\Scripts\python.exe -m app.comfyui.live_validation `
  --engine gpt-sovits `
  --resource-id gpt-sovits-local `
  --base-url http://127.0.0.1:8188 `
  --reference-audio 'F:\Code\Github\TTS-Audio-Suite\voices_examples\Sophie_Anderson CC3.wav' `
  --reference-text 'It was just brilliant because because people put me down in the past as well about my look as you have you heard it would be people want to put you down because it makes them feel secure.' `
  --text 'This is the real GPT SoVITS validation through TTS More and ComfyUI.' `
  --output F:\TTS-More\validation\2026-07-31-comfyui-live\outputs\gpt-sovits.wav `
  --evidence F:\TTS-More\validation\2026-07-31-comfyui-live\evidence\gpt-sovits.json `
  --timeout-seconds 900
```

- [ ] **Step 2: Preserve checkout/import/weight errors before intervention**

Record the bound checkout path, missing module, expected version, selected weight pair, reference asset ID lifecycle, and ComfyUI logs. Do not modify GPT-SoVITS.

- [ ] **Step 3: Fix an allowed adapter problem with a regression test**

Put checkout binding, import-path, version mapping, or official-weight compatibility fixes in TTS-Audio-Suite. Put workflow parameter or asset lifecycle fixes in TTS More. Confirm the failing test first, implement the minimum change, run both owning-repo focused suites, commit, restart, and rerun Step 1.

- [ ] **Step 4: Repeat after cleanup**

On a pass, repeat with `gpt-sovits-second.wav` and `gpt-sovits-second.json`.

---

### Task 8: Fix the single-GPU default and validate through the TTS More application

**Files:**
- Modify: `backend/app/open_source_tts.py:26-39`
- Modify: `backend/tests/test_api.py`
- Modify: `docs/comfyui-integration.md`
- Local: `data/local/services.json`

**Interfaces:**
- Produces: three application-visible ComfyUI endpoints with capacity one.
- Consumes: the already validated live Bridge.

- [ ] **Step 1: Write the failing capacity-default tests**

```python
def test_open_source_comfyui_request_defaults_to_single_gpu_capacity():
    request = OpenSourceTTSConfigureRequest(
        provider_type="indextts",
        base_url="http://127.0.0.1:8188",
        resource_id="indextts-local",
    )
    assert request.capacity == 1
```

Add an API test that posts a configuration without `capacity` and asserts the saved endpoint uses `capacity == 1` and `resource_group == "comfyui-local-0"`.

- [ ] **Step 2: Confirm the tests fail with capacity three**

```powershell
Set-Location F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\backend
F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\.venv\Scripts\python.exe -m pytest tests\test_api.py -k "open_source and capacity" -q
```

- [ ] **Step 3: Change the default and all single-GPU documentation examples**

Change `OpenSourceTTSConfigureRequest.capacity` to `Field(default=1, ge=1)`. Change ComfyUI single-instance JSON and explanatory text in `docs/comfyui-integration.md` from capacity three to capacity one. Preserve multi-instance scaling through separate resource groups.

- [ ] **Step 4: Run backend tests and commit**

```powershell
F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\.venv\Scripts\python.exe -m pytest tests\test_api.py tests\test_comfyui_client.py tests\test_service_queue.py tests\test_comfyui_live_validation.py -q
git add backend/app/open_source_tts.py backend/tests/test_api.py docs/comfyui-integration.md
git commit -m "fix: default ComfyUI endpoints to single GPU capacity"
```

- [ ] **Step 5: Verify the TTS More environment and start backend and frontend**

Refresh the editable backend installation created in Task 1:

```powershell
F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\.venv\Scripts\python.exe -m pip install -e 'F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\backend[dev]'
```

Start both processes hidden with separate logs:

```powershell
$backend = Start-Process -FilePath 'F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\.venv\Scripts\python.exe' -ArgumentList @('-m','uvicorn','app.main:create_app','--host','127.0.0.1','--port','8000') -WorkingDirectory 'F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\backend' -RedirectStandardOutput 'F:\TTS-More\validation\2026-07-31-comfyui-live\tts-more-backend.stdout.log' -RedirectStandardError 'F:\TTS-More\validation\2026-07-31-comfyui-live\tts-more-backend.stderr.log' -WindowStyle Hidden -PassThru
Set-Location F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\frontend
pnpm install
$frontend = Start-Process -FilePath 'C:\Users\xuyu_\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd' -ArgumentList @('dev','--','--host','127.0.0.1','--port','5173') -WorkingDirectory 'F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\frontend' -RedirectStandardOutput 'F:\TTS-More\validation\2026-07-31-comfyui-live\tts-more-frontend.stdout.log' -RedirectStandardError 'F:\TTS-More\validation\2026-07-31-comfyui-live\tts-more-frontend.stderr.log' -WindowStyle Hidden -PassThru
```

- [ ] **Step 6: Register the three endpoints through the application API**

POST each payload to `http://127.0.0.1:8000/api/open-source-tts/configure`:

```json
{"provider_type":"gpt-sovits","service_id":"comfyui-gpt-sovits","display_name":"ComfyUI GPT-SoVITS","source_profile":"local_endpoint","base_url":"http://127.0.0.1:8188","api_contract":"comfyui-tts-audio-suite-v1","enabled":true,"resource_group":"comfyui-local-0","capacity":1,"resource_id":"gpt-sovits-local"}
```

```json
{"provider_type":"indextts","service_id":"comfyui-indextts","display_name":"ComfyUI IndexTTS","source_profile":"local_endpoint","base_url":"http://127.0.0.1:8188","api_contract":"comfyui-tts-audio-suite-v1","enabled":true,"resource_group":"comfyui-local-0","capacity":1,"resource_id":"indextts-local"}
```

```json
{"provider_type":"cosyvoice","service_id":"comfyui-cosyvoice","display_name":"ComfyUI CosyVoice","source_profile":"local_endpoint","base_url":"http://127.0.0.1:8188","api_contract":"comfyui-tts-audio-suite-v1","enabled":true,"resource_group":"comfyui-local-0","capacity":1,"resource_id":"cosyvoice-local"}
```

Require `setup_state=ready` and the matching `resource_id` in every saved endpoint.

- [ ] **Step 7: Perform browser-visible end-to-end validation**

Open `http://127.0.0.1:5173`, verify all three service cards are ready, create project `ComfyUI 本机验证`, create one line per engine, bind the matching service/resource/reference audio, submit each line, and wait for terminal queue states.

For every engine already passing direct validation, require:

- an external ComfyUI prompt ID;
- completed status in TTS More;
- a generated history item;
- a browser-visible playable audio result;
- a nonempty local audio file.

Capture screenshots and application API responses in the external evidence directory.

- [ ] **Step 8: Run a second application request after cleanup**

Release the engine runtime, regenerate one previously successful line, and confirm TTS More and ComfyUI recover without restarting either service.

---

### Task 9: Consolidate findings into the supported solution

**Files:**
- Create: `docs/comfyui-local-validation-report.md`
- Modify only evidence-backed source/test/docs files in TTS More or TTS-Audio-Suite.

**Interfaces:**
- Produces: a sanitized, repeatable setup and an honest per-engine certification matrix.

- [ ] **Step 1: Build the report from actual evidence**

The report must contain:

1. final TTS More, ComfyUI, and TTS-Audio-Suite commit SHAs;
2. GPU, Python, PyTorch, CUDA, and port configuration;
3. sanitized resource registry example using symbolic root names rather than this machine's private absolute paths;
4. exact successful startup and validation commands;
5. chronological issue matrix with symptom, root cause, owning repository, fix, and rerun result;
6. per-engine results for first request, cleanup, and second request;
7. TTS More browser-visible evidence;
8. unresolved blockers and model downloads that were deliberately not performed.

- [ ] **Step 2: Run TTS More deterministic verification**

```powershell
Set-Location F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\backend
F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\.venv\Scripts\python.exe -m pytest tests\test_comfyui_client.py tests\test_comfyui_live_validation.py tests\test_service_queue.py tests\test_api.py -q
Set-Location F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation\frontend
pnpm test
pnpm build
```

- [ ] **Step 3: Run the TTS-Audio-Suite Bridge verification**

```powershell
Set-Location F:\Code\Github\TTS-Audio-Suite
F:\venvs\comfyui-tts\Scripts\python.exe -m pytest tests\unit\test_api_bridge_resource_registry.py tests\unit\test_api_bridge_engine_nodes.py tests\unit\test_api_bridge_routes.py tests\unit\test_api_bridge_runtime_registry.py -q
```

- [ ] **Step 4: Re-run every engine marked passed**

Re-run each successful live CLI command with a new output/evidence filename. Re-run the TTS More browser-visible line for every engine marked application-pass. Do not infer one engine's result from another.

- [ ] **Step 5: Verify repository boundaries**

```powershell
git -C F:\Code\Github\ComfyUI status --short --branch
git -C F:\Code\Github\TTS_more\.worktrees\comfyui-live-validation status --short --branch
git -C F:\Code\Github\TTS-Audio-Suite status --short --branch
git -C F:\Code\Github\TTS_more\repo\GPT-SoVITS-main status --short --branch
git -C F:\Code\Github\TTS_more\repo\index-tts status --short --branch
git -C F:\Code\Github\TTS_more\repo\CosyVoice status --short --branch
```

ComfyUI and the three TTS repositories must contain no new source changes. Only intentional commits in TTS More and TTS-Audio-Suite are allowed.

- [ ] **Step 6: Commit the final TTS More report**

```powershell
git add docs/comfyui-local-validation-report.md
git commit -m "docs: record ComfyUI TTS live validation"
```

- [ ] **Step 7: Report certification honestly**

Report each engine independently as `application-pass`, `bridge-only-pass`, or `blocked`. Include generated audio paths and evidence JSON paths for passes. Include the exact blocking error and next authorized action for blocks.
