# Windows ComfyUI Reliability Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make TTS More + official ComfyUI + the TTS-Audio-Suite fork reliably cancel, time out, recover, and repeatedly synthesize with GPT-SoVITS, IndexTTS, and CosyVoice on Windows.

**Architecture:** TTS More propagates a side-effect-free cancellation check from its job state machine into a prompt-scoped ComfyUI controller, and publishes audio only after atomic validation. TTS-Audio-Suite converts its three registered external runners from one long blocking wait to one shared interrupt-aware wait that retains Windows Job Object containment. A committed opt-in validator proves 30 successful mixed-engine requests plus cancellation, timeout, ComfyUI-loss, restart, cleanup, and recovery gates without modifying official ComfyUI or the three official TTS projects.

**Tech Stack:** Python 3.11 FastAPI/Pydantic/httpx/soundfile/pytest, Python 3.12 TTS-Audio-Suite/pytest/psutil/Windows Job Objects, TypeScript/React/Vitest, PowerShell 7, official ComfyUI HTTP API, CUDA 12.8 host tooling.

## Global Constraints

- Authoritative design: `docs/superpowers/specs/2026-08-01-windows-comfyui-reliability-contract-design.md` at commit `1d02bc1a50983ce8c8982dcf9ff98a77f480d174`.
- TTS More requires Python `>=3.11,<3.12`; frontend requires Node `>=20` and pnpm `>=9`.
- TTS-Audio-Suite remains version `5.6.2` and its hosted matrix remains Python 3.12/3.13 on Ubuntu and Windows.
- Official ComfyUI source and GPT-SoVITS, IndexTTS, and CosyVoice source projects must not be edited.
- The three model/check-out environments are private registered resources; no model path or registry content may be committed.
- ComfyUI cancellation must always include the exact `prompt_id`; never issue a global interrupt for one TTS More job.
- The single-GPU resource group remains `capacity=1`.
- Cancellation convergence is at most 30 seconds; ComfyUI restart readiness is at most 180 seconds; final release/GPU recovery is at most 30 seconds.
- Final GPU memory must be no more than 1,024 MiB above the recorded idle baseline.
- Live inference remains opt-in and separate from deterministic hosted CI.
- Every product fix discovered by live validation requires a failing deterministic regression before implementation.
- Use `apply_patch` for source/document edits and preserve unrelated dirty files and model checkout changes.

## Repository and file map

The plan spans two repositories because the approved prompt-cancellation contract crosses the HTTP/prompt boundary. The plugin tasks and TTS More tasks remain independently testable, but neither alone can prove in-flight cancellation of an external model runner.

### TTS-Audio-Suite repository (`F:\Code\Github\TTS-Audio-Suite`)

- `engines/index_tts/external_subprocess.py`: shared Windows Job Object, tree termination, bounded cleanup, and new interrupt-aware communication primitive.
- `engines/gpt_sovits/external_subprocess.py`: GPT runner must consume the shared primitive.
- `engines/cosyvoice/external_subprocess.py`: CosyVoice runner must consume the shared primitive.
- `tests/unit/test_api_bridge_engine_nodes.py`: external runner, timeout, cleanup, late-child, and per-engine interrupt regressions.
- `docs/api-bridge.md`: Bridge cancellation/runner convergence contract.

### TTS More repository (`F:\Code\Github\TTS_more`)

- `backend/app/adapters/base.py`: cancellation callback and typed synthesis-control outcomes.
- `backend/app/comfyui/client.py`: prompt queue inspection, targeted cancellation, convergence, and cancel-aware polling.
- `backend/app/comfyui/output.py`: new focused atomic WAV validation/publication unit.
- `backend/app/services.py`: ComfyUI request lifecycle, typed error preservation, asset cleanup ordering, and atomic output use.
- `backend/app/models.py`: add the nonterminal `cancelling` generation status.
- `backend/app/queue.py`: propagate cancellation, commit truthful cancelled versions, and resolve races.
- `backend/app/comfyui/reliability_validation.py`: reusable validator models, probes, evidence aggregation, and acceptance evaluation.
- `backend/tests/test_comfyui_client.py`: prompt and service lifecycle regressions.
- `backend/tests/test_comfyui_output.py`: new atomic WAV tests.
- `backend/tests/test_service_queue.py`: job/item/manifest cancellation state-machine tests.
- `backend/tests/test_comfyui_reliability_validation.py`: new evidence and fail-closed validator tests.
- `backend/tests/test_api.py`: cancellation endpoint integration and serialized status tests.
- `frontend/src/types.ts`: add `cancelling` to generation and queue status unions.
- `frontend/src/lib/generationStatus.ts`: new focused terminal/tone/label helpers.
- `frontend/src/lib/generationStatus.test.ts`: status semantics regressions.
- `frontend/src/App.tsx`: use shared status helpers and separate cancelled from failed counts.
- `frontend/src/App.css`: cancelling and cancelled presentation.
- `frontend/src/i18n.ts`: Chinese/English cancelling labels and queue counts.
- `scripts/run-windows-comfyui-reliability.ps1`: owned-process orchestration and live gate entrypoint.
- `deployment/tts-repos/windows-reliability-fixture.example.json`: redacted fixture schema.
- `docs/comfyui-windows-reliability.md`: operator runbook.
- `docs/comfyui-windows-reliability-report.md`: final redacted evidence summary.

## Execution setup

At execution time, invoke `superpowers:using-git-worktrees` before editing product source. Create or select clean isolated branches from these exact baselines:

```powershell
git -C F:\Code\Github\TTS_more rev-parse HEAD
# expected ancestor: 1d02bc1a50983ce8c8982dcf9ff98a77f480d174
git -C F:\Code\Github\TTS-Audio-Suite rev-parse main origin/main
# both expected: 1d9e0f6c31309c9ad476da3d735b3aa91f61028f
```

Use implementation branches `dev-xu/windows-comfyui-reliability` and `dev-xu/windows-runner-interrupt`. Bootstrap deterministic environments without changing machine-global Python:

```powershell
uv sync --project backend --python 3.11 --extra dev
$env:COMFYUI_TESTING = '1'
& F:\TTS-More\runtime\py312\python.exe -c "import pytest, psutil, soundfile; print('plugin test runtime ready')"
pnpm --dir frontend install --frozen-lockfile
```

---

### Task 1: Add one shared interrupt-aware external runner wait

**Files:**
- Modify: `TTS-Audio-Suite/engines/index_tts/external_subprocess.py:1-700`
- Test: `TTS-Audio-Suite/tests/unit/test_api_bridge_engine_nodes.py:662-1478`

**Interfaces:**
- Consumes: existing `_start_process()`, `_cleanup_timed_out_process()`, and `_close_windows_job()` methods.
- Produces: `InterruptCheck = Callable[[], bool]`, `_comfyui_interrupt_requested() -> bool`, `_raise_processing_interrupted()`, `_clear_processing_interrupt()`, the Index base constructor field `interrupt_check`, and inherited `_communicate_with_control(process, engine_label) -> tuple[str, str]` behavior.

- [ ] **Step 1: Write failing normal, interrupt, and timeout tests**

Add tests that instantiate the proxy without running model inference and use a fake `Popen` object:

```python
def test_external_wait_returns_output_without_interrupt(monkeypatch):
    module = _load_external_index_subprocess_module()
    proxy = object.__new__(module.ExternalIndexTTSSubprocessProxy)
    proxy.timeout_seconds = 1.0
    proxy.termination_grace_seconds = 0.2
    proxy.interrupt_check = lambda: False

    class Finished:
        returncode = 0
        def communicate(self, timeout=None):
            return "ok", ""

    assert proxy._communicate_with_control(Finished(), "IndexTTS") == ("ok", "")


def test_external_wait_interrupts_and_cleans_tree(monkeypatch):
    module = _load_external_index_subprocess_module()
    proxy = object.__new__(module.ExternalIndexTTSSubprocessProxy)
    proxy.timeout_seconds = 10.0
    proxy.termination_grace_seconds = 0.2
    checks = iter((False, True))
    proxy.interrupt_check = lambda: next(checks, True)
    cleaned = []

    class Running:
        returncode = None
        def communicate(self, timeout=None):
            raise module.subprocess.TimeoutExpired("runner", timeout)
        def poll(self):
            return self.returncode

    def cleanup(process):
        process.returncode = -9
        cleaned.append(process)
        return "partial", "", "tree exited"

    monkeypatch.setattr(proxy, "_cleanup_timed_out_process", cleanup)
    process = Running()
    with pytest.raises(InterruptedError, match="IndexTTS external subprocess interrupted"):
        proxy._communicate_with_control(process, "IndexTTS")
    assert cleaned == [process]


def test_external_wait_reports_cleanup_failure_instead_of_false_interrupt_success(monkeypatch):
    module = _load_external_index_subprocess_module()
    proxy = object.__new__(module.ExternalIndexTTSSubprocessProxy)
    proxy.timeout_seconds = 10.0
    proxy.termination_grace_seconds = 0.01
    proxy.interrupt_check = lambda: True

    class Stuck:
        returncode = None
        def communicate(self, timeout=None):
            raise module.subprocess.TimeoutExpired("runner", timeout)
        def poll(self):
            return None

    monkeypatch.setattr(
        proxy,
        "_cleanup_timed_out_process",
        lambda process: ("", "", "process exit could not be verified"),
    )
    with pytest.raises(RuntimeError, match="interruption cleanup failed"):
        proxy._communicate_with_control(Stuck(), "IndexTTS")


def test_external_wait_preserves_timeout_category(monkeypatch):
    module = _load_external_index_subprocess_module()
    proxy = object.__new__(module.ExternalIndexTTSSubprocessProxy)
    proxy.timeout_seconds = 0.01
    proxy.termination_grace_seconds = 0.2
    proxy.interrupt_check = lambda: False

    class Running:
        returncode = None
        def communicate(self, timeout=None):
            raise module.subprocess.TimeoutExpired("runner", timeout)

    monkeypatch.setattr(proxy, "_cleanup_timed_out_process", lambda process: ("", "slow", "tree exited"))
    with pytest.raises(TimeoutError, match="exceeded 0.01s"):
        proxy._communicate_with_control(Running(), "IndexTTS")
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
$env:COMFYUI_TESTING = '1'
& F:\TTS-More\runtime\py312\python.exe -m pytest tests/unit/test_api_bridge_engine_nodes.py -k 'external_wait' -q
```

Expected: all four new tests fail because `_communicate_with_control` and `interrupt_check` do not exist.

- [ ] **Step 3: Implement the default interrupt probe and sliced wait**

Add this contract to `ExternalIndexTTSSubprocessProxy` and pass the optional callback through its constructor:

```python
from collections.abc import Callable

InterruptCheck = Callable[[], bool]


def _comfyui_interrupt_requested() -> bool:
    try:
        from comfy.model_management import processing_interrupted
    except ImportError:
        return False
    return bool(processing_interrupted())


def _raise_processing_interrupted(engine_label: str, diagnostic: str) -> None:
    try:
        from comfy.model_management import throw_exception_if_processing_interrupted
    except ImportError:
        pass
    else:
        throw_exception_if_processing_interrupted()
    raise InterruptedError(f"{engine_label} external subprocess interrupted: {diagnostic}")


def _clear_processing_interrupt() -> None:
    try:
        from comfy.model_management import interrupt_current_processing
    except ImportError:
        return
    interrupt_current_processing(False)


def _communicate_with_control(self, process, engine_label: str) -> tuple[str, str]:
    deadline = time.monotonic() + self.timeout_seconds
    timeout_error: subprocess.TimeoutExpired | None = None
    while True:
        if self.interrupt_check():
            stdout, stderr, cleanup = self._cleanup_timed_out_process(process)
            diagnostic = (stderr or stdout or "interrupted").strip()
            if cleanup:
                diagnostic = f"{diagnostic}; cleanup: {cleanup}"
            try:
                exit_verified = process.poll() is not None
            except Exception as exc:
                exit_verified = False
                diagnostic = f"{diagnostic}; final status check failed: {exc}"
            if not exit_verified:
                _clear_processing_interrupt()
                raise RuntimeError(
                    f"{engine_label} interruption cleanup failed: {diagnostic}"
                )
            _raise_processing_interrupted(engine_label, diagnostic)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stdout, stderr, cleanup = self._cleanup_timed_out_process(process)
            diagnostic = (stderr or stdout or str(timeout_error or "deadline exceeded")).strip()
            if cleanup:
                diagnostic = f"{diagnostic}; cleanup: {cleanup}"
            raise TimeoutError(
                f"External {engine_label} subprocess exceeded {self.timeout_seconds:g}s: {diagnostic}"
            ) from timeout_error
        try:
            return process.communicate(timeout=min(0.25, remaining))
        except subprocess.TimeoutExpired as exc:
            timeout_error = exc
```

Set `self.interrupt_check = interrupt_check or _comfyui_interrupt_requested` in the constructor. A real ComfyUI interrupt must propagate its native `InterruptProcessingException` after tree exit is verified; an injected isolated-test interrupt falls back to `InterruptedError`. Cleanup failure clears the ComfyUI interrupt flag before raising `RuntimeError`, so it cannot poison the next prompt. Do not change Job Object creation or cleanup deadlines.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the command from Step 2. Expected: the four new tests pass and existing timeout tests still pass.

- [ ] **Step 5: Run the complete external runner test slice**

Run:

```powershell
$env:COMFYUI_TESTING = '1'
& F:\TTS-More\runtime\py312\python.exe -m pytest tests/unit/test_api_bridge_engine_nodes.py -k 'external or windows_tree or windows_job' -q
```

Expected: all selected tests pass; Windows-only real process tests run on this host.

- [ ] **Step 6: Commit Task 1**

```powershell
git add engines/index_tts/external_subprocess.py tests/unit/test_api_bridge_engine_nodes.py
git commit -m "feat: make external runner waits interruptible"
```

### Task 2: Route GPT-SoVITS, IndexTTS, and CosyVoice through the shared wait

**Files:**
- Modify: `TTS-Audio-Suite/engines/index_tts/external_subprocess.py:230-266`
- Modify: `TTS-Audio-Suite/engines/gpt_sovits/external_subprocess.py:140-174`
- Modify: `TTS-Audio-Suite/engines/cosyvoice/external_subprocess.py:230-263`
- Modify: `TTS-Audio-Suite/docs/api-bridge.md`
- Test: `TTS-Audio-Suite/tests/unit/test_api_bridge_engine_nodes.py`

**Interfaces:**
- Consumes: `_communicate_with_control(process, engine_label)` from Task 1.
- Produces: all three registered engine constructors initialize `interrupt_check`, all three subprocess paths have identical interruption/timeout semantics, and none retains a full-duration direct `communicate()` call.

- [ ] **Step 1: Write one failing interrupt-behavior test per engine**

Use the existing `_prepare_external_index_runtime()`, GPT-SoVITS runtime, and CosyVoice runtime helpers to construct each real proxy with an injected `interrupt_check` that returns `False` once and then `True`. Replace only `subprocess.Popen` with a process double whose `communicate(timeout=...)` records every timeout, raises `TimeoutExpired` while running, and returns after the existing tree-termination path sets `returncode=-9`. Invoke the real public inference path for each engine and assert:

```python
with pytest.raises(InterruptedError, match=engine_label):
    invoke_real_engine(proxy)

assert communicate_timeouts
assert all(0 < timeout <= 0.25 for timeout in communicate_timeouts)
assert terminated_processes == [created_process]
assert created_process.returncode == -9
assert not list(temp_root.iterdir())
```

Name the three tests `test_registered_index_interrupts_during_sliced_wait`, `test_registered_gpt_sovits_interrupts_during_sliced_wait`, and `test_registered_cosyvoice_interrupts_during_sliced_wait`. The doubles isolate the external interpreter only; assertions target the real proxy's exception category, bounded wait slices, process-tree cleanup, and request-temp cleanup. Do not inspect or grep source text.

- [ ] **Step 2: Run the delegation test and verify RED**

```powershell
$env:COMFYUI_TESTING = '1'
& F:\TTS-More\runtime\py312\python.exe -m pytest tests/unit/test_api_bridge_engine_nodes.py -k 'registered_ and sliced_wait' -q
```

Expected: all three cases fail because the old direct waits ignore `interrupt_check`, use the full engine timeout, and raise `TimeoutError` instead of an interruption outcome.

- [ ] **Step 3: Initialize each subclass callback and replace each direct wait**

The Index constructor is completed in Task 1. In both subclass modules, expand the base import to include `InterruptCheck` and `_comfyui_interrupt_requested`, add `interrupt_check: InterruptCheck | None = None` after `temp_root`, and assign:

```python
self.interrupt_check = interrupt_check or _comfyui_interrupt_requested
```

Then, for each engine, replace the local `try/except subprocess.TimeoutExpired` block with:

```python
stdout, stderr = self._communicate_with_control(process, "GPT-SoVITS")
```

Use labels `IndexTTS`, `GPT-SoVITS`, and `CosyVoice` exactly. Keep `_close_windows_job(process)` and return-code/output validation after the wait.

- [ ] **Step 4: Verify all three per-engine interrupt tests**

Run Step 2 and the focused external runner slice from Task 1. Expected: pass.

- [ ] **Step 5: Document the Bridge interruption contract**

Add a short section to `docs/api-bridge.md` stating that targeted ComfyUI interruption is observed in at most 250 ms while an external runner is active, invokes bounded tree cleanup, and reports cleanup failure rather than false success.

- [ ] **Step 6: Run the full plugin suite and dependency integrity gate**

```powershell
$env:COMFYUI_TESTING = '1'
& F:\TTS-More\runtime\py312\python.exe -m pytest tests/unit -q
& F:\TTS-More\runtime\py312\python.exe -m pip check
```

Expected: complete suite passes, existing platform skips remain explicit, and `pip check` reports no broken requirements.

- [ ] **Step 7: Commit Task 2**

```powershell
git add engines/index_tts/external_subprocess.py engines/gpt_sovits/external_subprocess.py engines/cosyvoice/external_subprocess.py docs/api-bridge.md tests/unit/test_api_bridge_engine_nodes.py
git commit -m "feat: interrupt all registered TTS runners"
```

### Task 3: Define typed synthesis control outcomes

**Files:**
- Modify: `backend/app/adapters/base.py:1-26`
- Test: `backend/tests/test_comfyui_client.py`

**Interfaces:**
- Consumes: existing `SynthesisRequest` and `SynthesisResult` dataclasses.
- Produces: `SynthesisCancelCheck`, `SynthesisControlError`, `SynthesisCancelled`, `SynthesisTimeout`, and `SynthesisRequest.cancel_check`.

- [ ] **Step 1: Write a failing request/error contract test**

```python
def test_synthesis_request_carries_cancel_check_and_control_details(tmp_path):
    from app.adapters.base import SynthesisCancelled, SynthesisRequest

    request = SynthesisRequest(
        line=ScriptLine(id="l1", character_id="c1", text="hello"),
        profile="voice",
        output_path=tmp_path / "out.wav",
        cancel_check=lambda: True,
    )
    error = SynthesisCancelled("cancelled", details={"prompt_id": "p1"})
    assert request.cancel_check is not None and request.cancel_check()
    assert error.code == "cancelled"
    assert error.details == {"prompt_id": "p1"}
```

- [ ] **Step 2: Run the test and verify RED**

```powershell
& backend\.venv\Scripts\python.exe -m pytest backend/tests/test_comfyui_client.py -k 'request_carries_cancel' -q
```

Expected: import or constructor failure for the new types/field.

- [ ] **Step 3: Implement the shared types**

```python
SynthesisCancelCheck = Callable[[], bool]


class SynthesisControlError(RuntimeError):
    code = "control_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class SynthesisCancelled(SynthesisControlError):
    code = "cancelled"


class SynthesisTimeout(SynthesisControlError):
    code = "timeout"
```

Add `cancel_check: SynthesisCancelCheck | None = None` after `progress_callback` in `SynthesisRequest`.

- [ ] **Step 4: Run the test and all current ComfyUI client tests**

```powershell
& backend\.venv\Scripts\python.exe -m pytest backend/tests/test_comfyui_client.py -q
```

Expected: pass; existing `SynthesisRequest` call sites remain compatible through the default `None`.

- [ ] **Step 5: Commit Task 3**

```powershell
git add backend/app/adapters/base.py backend/tests/test_comfyui_client.py
git commit -m "feat: define synthesis cancellation outcomes"
```

### Task 4: Add prompt-scoped ComfyUI cancellation and convergence

**Files:**
- Modify: `backend/app/comfyui/client.py:1-166`
- Test: `backend/tests/test_comfyui_client.py:1-534`

**Interfaces:**
- Consumes: `SynthesisCancelCheck`, `SynthesisCancelled`, and `SynthesisTimeout` from Task 3.
- Produces: `PromptCancellationResult`, `get_queue()`, `cancel_prompt(prompt_id, max_wait=30.0)`, and cancel-aware `poll_until_done()`.

- [ ] **Step 1: Write failing running/pending/idempotent cancellation tests**

Use `httpx.MockTransport` with mutable queue state. The running case must observe only a targeted interrupt:

```python
def test_cancel_prompt_interrupts_only_the_targeted_running_prompt():
    state = {"running": True}
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, request.content))
        if request.url.path == "/queue":
            running = [[1, "prompt-1", {}, {}, []]] if state["running"] else []
            return httpx.Response(200, json={"queue_running": running, "queue_pending": []})
        if request.url.path == "/interrupt":
            assert json.loads(request.content) == {"prompt_id": "prompt-1"}
            state["running"] = False
            return httpx.Response(200, json={})
        if request.url.path == "/history/prompt-1":
            return httpx.Response(200, json={})
        raise AssertionError(request.url)

    client = ComfyUIAPIClient("http://127.0.0.1:8188", transport=httpx.MockTransport(handler))
    result = client.cancel_prompt("prompt-1", max_wait=1.0)
    assert result.converged is True
    assert result.actions == ("interrupt",)
    assert all(body != b"{}" for method, path, body in requests if path == "/interrupt")
```

Add pending, already-absent, already-terminal, and 30-second convergence-failure cases. The pending case must assert `{"delete": [prompt_id]}` and no interrupt. Add a running-prompt case whose post-interrupt history contains `execution_error` with `exception_message="IndexTTS interruption cleanup failed: process exit could not be verified"`; it must return `final_state="error"`, `converged=False`, and a sanitized diagnostic instead of a false cancellation success.

- [ ] **Step 2: Run the cancellation tests and verify RED**

```powershell
& backend\.venv\Scripts\python.exe -m pytest backend/tests/test_comfyui_client.py -k 'cancel_prompt' -q
```

Expected: `cancel_prompt` missing.

- [ ] **Step 3: Implement queue inspection and structured convergence**

Add:

```python
@dataclass(frozen=True)
class PromptCancellationResult:
    prompt_id: str
    initial_state: str
    final_state: str
    actions: tuple[str, ...]
    duration_seconds: float
    converged: bool
    diagnostic: str | None = None
```

`cancel_prompt()` must inspect `/queue`, issue exactly one targeted action for the observed state, then poll `/queue` and `/history/{prompt_id}` until absence/terminal. Scrub diagnostics with the existing `scrub_error` boundary. Treat repeated absent/terminal calls as converged. After this call sends an interrupt, `execution_interrupted` is successful convergence, while `execution_error` is terminal but `converged=False`; preserve its sanitized cleanup diagnostic. An already-terminal prompt found before any action remains an idempotent converged outcome.

- [ ] **Step 4: Write failing poll cancellation and timeout tests**

```python
def test_poll_until_done_cancels_prompt_when_requested(monkeypatch):
    client = ComfyUIAPIClient("http://127.0.0.1:8188")
    monkeypatch.setattr(client, "get_history", lambda prompt_id: {})
    monkeypatch.setattr(
        client,
        "cancel_prompt",
        lambda prompt_id, max_wait=30.0: PromptCancellationResult(
            prompt_id, "running", "absent", ("interrupt",), 0.1, True
        ),
    )
    with pytest.raises(SynthesisCancelled) as caught:
        client.poll_until_done("p1", poll_interval=0.01, max_wait=1.0, cancel_check=lambda: True)
    assert caught.value.details["prompt_id"] == "p1"


def test_poll_timeout_cancels_before_raising_timeout(monkeypatch):
    client = ComfyUIAPIClient("http://127.0.0.1:8188")
    monkeypatch.setattr(client, "get_history", lambda prompt_id: {})
    cancelled = []
    monkeypatch.setattr(
        client,
        "cancel_prompt",
        lambda prompt_id, max_wait=30.0: cancelled.append(prompt_id) or PromptCancellationResult(
            prompt_id, "running", "absent", ("interrupt",), 0.1, True
        ),
    )
    with pytest.raises(SynthesisTimeout):
        client.poll_until_done("p2", poll_interval=0.001, max_wait=0.001)
    assert cancelled == ["p2"]
```

- [ ] **Step 5: Implement cancel-aware polling and verify GREEN**

Add `cancel_check` and `cancel_wait=30.0` keyword parameters to `poll_until_done()`. Check cancellation before every history call and during sleeps at slices no longer than 250 ms. On user cancellation, always raise `SynthesisCancelled` with the serialized `PromptCancellationResult`, including its `converged` value, so the queue can distinguish successful cancellation from cleanup failure. On timeout, call the same cancellation path before raising `SynthesisTimeout`; timeout remains primary and the cancellation result, including any cleanup defect, is attached to `details`.

Run:

```powershell
& backend\.venv\Scripts\python.exe -m pytest backend/tests/test_comfyui_client.py -q
```

Expected: all ComfyUI client tests pass.

- [ ] **Step 6: Commit Task 4**

```powershell
git add backend/app/comfyui/client.py backend/tests/test_comfyui_client.py
git commit -m "feat: cancel ComfyUI prompts with convergence proof"
```

### Task 5: Publish validated ComfyUI WAV output atomically

**Files:**
- Create: `backend/app/comfyui/output.py`
- Create: `backend/tests/test_comfyui_output.py`
- Modify: `backend/app/services.py:1867-2000`
- Modify: `backend/tests/test_comfyui_client.py`

**Interfaces:**
- Consumes: raw ComfyUI output bytes and a requested final `Path`.
- Produces: `publish_wav_atomic(output_path: Path, audio_bytes: bytes) -> dict[str, int | float]` and failure-safe ComfyUI output publication.

- [ ] **Step 1: Write failing atomic publication tests**

```python
def test_publish_wav_atomic_preserves_existing_output_on_invalid_audio(tmp_path):
    output = tmp_path / "voice.wav"
    output.write_bytes(b"existing")
    with pytest.raises(ValueError, match="decode"):
        publish_wav_atomic(output, b"not-a-wave")
    assert output.read_bytes() == b"existing"
    assert list(tmp_path.glob(".voice.wav.*.tmp")) == []


def test_publish_wav_atomic_rejects_silence_and_replaces_after_validation(tmp_path):
    silent = io.BytesIO()
    soundfile.write(silent, [0.0] * 1600, 16000, format="WAV", subtype="PCM_16")
    with pytest.raises(ValueError, match="silent"):
        publish_wav_atomic(tmp_path / "silent.wav", silent.getvalue())

    voiced = io.BytesIO()
    soundfile.write(voiced, [0.2] * 1600, 16000, format="WAV", subtype="PCM_16")
    metadata = publish_wav_atomic(tmp_path / "voiced.wav", voiced.getvalue())
    assert metadata["sample_rate"] == 16000
    assert metadata["frames"] == 1600
    assert metadata["peak"] > 0.1
```

- [ ] **Step 2: Run and verify RED**

```powershell
& backend\.venv\Scripts\python.exe -m pytest backend/tests/test_comfyui_output.py -q
```

Expected: module missing.

- [ ] **Step 3: Implement atomic decode/validation/replace**

The new module must create the same-directory name `f".{output_path.name}.{uuid4().hex}.tmp"`, write or transcode into it, decode the entire file with `soundfile.read(always_2d=True)`, require positive sample rate/frames, finite min/max, and peak `> 1e-5`, then call `temporary.replace(output_path)` in one filesystem operation. Always unlink the temporary file in `finally` if it still exists.

- [ ] **Step 4: Use atomic publication in `ComfyUITTSClient`**

Replace `_write_wav(request.output_path, audio_bytes)` with:

```python
audio_metadata = publish_wav_atomic(request.output_path, audio_bytes)
```

Merge `audio_metadata` into the returned result metadata. Remove the old private `_write_wav` helper after all tests point at the focused module.

- [ ] **Step 5: Verify output tests and ComfyUI client tests**

```powershell
& backend\.venv\Scripts\python.exe -m pytest backend/tests/test_comfyui_output.py backend/tests/test_comfyui_client.py -q
```

Expected: pass; invalid/cancelled downloads never replace an existing final WAV.

- [ ] **Step 6: Commit Task 5**

```powershell
git add backend/app/comfyui/output.py backend/app/services.py backend/tests/test_comfyui_output.py backend/tests/test_comfyui_client.py
git commit -m "feat: publish ComfyUI audio atomically"
```

### Task 6: Preserve typed errors and clean reference assets after convergence

**Files:**
- Modify: `backend/app/services.py:1867-1985`
- Test: `backend/tests/test_comfyui_client.py`

**Interfaces:**
- Consumes: Task 3 control errors, Task 4 prompt convergence, Task 5 atomic publisher.
- Produces: cancellation/timeout type preservation, asset deletion after convergence, and dual primary/cleanup evidence.

- [ ] **Step 1: Write failing type and cleanup-order tests**

Create a fake API that records `poll`, `cancel`, and `delete` events. Assert the order and error details:

```python
def test_comfy_service_preserves_cancel_type_and_deletes_asset_after_convergence(tmp_path):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(_audio_bytes())
    request = SynthesisRequest(
        line=ScriptLine(id="line-1", character_id="role", text="cancel me"),
        profile="cosy-main",
        output_path=tmp_path / "cancelled.wav",
        parameters={
            "engine": "cosyvoice",
            "resource_id": "cosy-main",
            "reference_audio": str(reference),
        },
        cancel_check=lambda: True,
    )
    client = ComfyUITTSClient(_cosyvoice_audio_suite_endpoint())
    client._build_workflow = lambda _engine, _params: {
        "1": {"class_type": "TestNode", "inputs": {}}
    }
    events: list[str] = []
    client.api.upload_audio = lambda path: {"asset_id": "asset-1"}
    client.api.submit_workflow = lambda workflow: "prompt-1"

    def cancelled(*args, **kwargs):
        events.append("converged")
        raise SynthesisCancelled("cancelled", details={"prompt_id": "prompt-1", "converged": True})

    client.api.poll_until_done = cancelled
    client.api.delete_audio = lambda asset_id: events.append(f"delete:{asset_id}")

    with pytest.raises(SynthesisCancelled):
        client.synthesize(request)
    assert events == ["converged", "delete:asset-1"]
```

Add a timeout + asset-delete failure case that asserts `details["cleanup_error"]` exists while `code == "timeout"` remains primary.

- [ ] **Step 2: Run and verify RED**

```powershell
& backend\.venv\Scripts\python.exe -m pytest backend/tests/test_comfyui_client.py -k 'preserves_cancel_type or cleanup_order' -q
```

Expected: old `synthesize()` wraps the typed exception in plain `RuntimeError` or drops cleanup detail.

- [ ] **Step 3: Implement lifecycle ordering and typed preservation**

Pass `request.cancel_check` into `poll_until_done()`. In `synthesize()`, re-raise `SynthesisControlError` unchanged. Attach asset cleanup failure to its `details`; for non-control exceptions, retain the current scrubbed primary diagnostic plus cleanup diagnostic. Emit external status `cancelling`, `cancelled`, or `timeout` through the progress callback when applicable.

- [ ] **Step 4: Run service/client regression suites**

```powershell
& backend\.venv\Scripts\python.exe -m pytest backend/tests/test_comfyui_client.py backend/tests/test_services.py -q
```

Expected: pass.

- [ ] **Step 5: Commit Task 6**

```powershell
git add backend/app/services.py backend/tests/test_comfyui_client.py
git commit -m "fix: converge prompts before ComfyUI asset cleanup"
```

### Task 7: Implement truthful job and manifest cancellation state

**Files:**
- Modify: `backend/app/models.py:486-538`
- Modify: `backend/app/queue.py:32-750`
- Modify: `backend/tests/test_service_queue.py:1-639`
- Modify: `backend/tests/test_api.py:1175-1200,2400-2500`

**Interfaces:**
- Consumes: `SynthesisRequest.cancel_check` and `SynthesisCancelled` from Task 3.
- Produces: nonterminal `cancelling`, cancelled in-flight generation versions, failed cancellation-cleanup/timeout versions with typed evidence, race-safe output discard, and truthful final job state.

- [ ] **Step 1: Write failing queued and in-flight transition tests**

```python
def test_generation_cancel_transitions_inflight_item_through_cancelling(tmp_path):
    started = threading.Event()
    release = threading.Event()

    class CancelAwareClient(RecordingServiceClient):
        def synthesize(self, request):
            started.set()
            assert request.cancel_check is not None
            assert release.wait(timeout=3)
            if request.cancel_check():
                raise SynthesisCancelled("cancelled", details={"prompt_id": "p1", "converged": True})
            return super().synthesize(request)

    service_endpoint = endpoint("local-gpt", EngineName.GPT_SOVITS, "local-gpu-0")
    client = CancelAwareClient(service_endpoint)
    manager = GenerationJobManager(
        ServiceGenerationQueue(StaticRouter({"local-gpt": client})),
        MemoryStore(tmp_path),
    )
    job = manager.submit("demo", [gpt_task("line-1", "reference.wav")])
    assert started.wait(timeout=3)
    cancelling = manager.cancel(job.job_id)
    assert cancelling.status == "cancelling"
    assert cancelling.items[0].status == "cancelling"
    release.set()
    final = _wait_for_manager_job(manager, job.job_id)
    assert final.status == "cancelled"
    assert final.items[0].status == "cancelled"
```

Add tests for cancel-before-dispatch, repeated cancel, completed-job cancel, and an in-flight `SynthesisCancelled(details={"converged": False, "diagnostic": "process exit could not be verified"})` case. The latter must assert job/item/version `failed`, `metadata["control_code"] == "cancelled"`, `metadata["control_details"]["converged"] is False`, and no final audio path.

- [ ] **Step 2: Write a failing completion-race test**

The fake client writes an output then blocks before returning. Cancel during the block; after return, assert the final output is removed/not committed and the version status is `cancelled`.

- [ ] **Step 3: Run and verify RED**

```powershell
& backend\.venv\Scripts\python.exe -m pytest backend/tests/test_service_queue.py -k 'cancel' -q
```

Expected: old job moves immediately to cancelled and the in-flight request has no cancel callback.

- [ ] **Step 4: Add `cancelling` and propagate the callback**

Extend both Python generation status uses with `cancelling`. Thread `cancel_check` through `_run_resource_clusters()`, `_run_service_cluster()`, and `_run_task()`, then construct:

```python
SynthesisRequest(
    line=task.line,
    profile=task.profile,
    output_path=output_path,
    parameters=task.parameters,
    progress_callback=progress_callback,
    cancel_check=cancel_check,
)
```

Treat `cancelling` and `cancelled` as cancellation requested. Do not allow later progress updates to change a cancelling item back to running, but continue capturing `external_job_id` and `external_status`.

- [ ] **Step 5: Record a cancelled in-flight version**

Add `_append_cancelled_version()` mirroring the failed-version metadata boundary, with `status="cancelled"`, no `audio_path`, the prompt/cancellation details, and `failure_stage` absent. Catch `SynthesisCancelled` separately and first remove any uncommitted output. When `details["converged"] is True`, append the cancelled version and emit cancelled. Otherwise append a failed version with `failure_stage="cancellation_cleanup"`, `control_code="cancelled"`, and the full sanitized `control_details`, then emit failed. Catch `SynthesisTimeout` separately as failed with `failure_stage="timeout"`, `control_code="timeout"`, and its prompt cancellation/cleanup evidence; never collapse it to a generic engine error.

- [ ] **Step 6: Resolve final job state deterministically**

`cancel()` marks queued items cancelled and active items cancelling. `_finish_job()` applies this order:

```python
if any(item.status == "failed" for item in job.items):
    job.status = "failed"
elif job.status == "cancelling" or any(item.status == "cancelled" for item in job.items):
    job.status = "cancelled"
else:
    job.status = "completed"
```

Set terminal progress to `1.0`. A generic exception during cancelling is failed, not a false cancelled success.

- [ ] **Step 7: Add API serialization coverage**

Post cancellation through `/api/jobs/{job_id}/cancel`, assert the immediate response is `cancelling` for active work, poll until `cancelled`, and verify the manifest contains the cancelled version and prompt ID.

- [ ] **Step 8: Run queue and API suites**

```powershell
& backend\.venv\Scripts\python.exe -m pytest backend/tests/test_service_queue.py backend/tests/test_api.py -q
```

Expected: pass.

- [ ] **Step 9: Commit Task 7**

```powershell
git add backend/app/models.py backend/app/queue.py backend/tests/test_service_queue.py backend/tests/test_api.py
git commit -m "feat: make generation cancellation truthful"
```

### Task 8: Render cancelling and cancelled without calling them failures

**Files:**
- Create: `frontend/src/lib/generationStatus.ts`
- Create: `frontend/src/lib/generationStatus.test.ts`
- Modify: `frontend/src/types.ts:1-10,570-600`
- Modify: `frontend/src/App.tsx:700-735,2335-2375,4595-4740,4900-4920`
- Modify: `frontend/src/App.css:5354-5390`
- Modify: `frontend/src/i18n.ts:650-690,1530-1570`

**Interfaces:**
- Consumes: backend statuses `cancelling` and `cancelled` from Task 7.
- Produces: `isTerminalGenerationStatus()`, `generationStatusTone()`, `generationStatusKey()`, separate cancelled count, and nonterminal polling for cancelling jobs.

- [ ] **Step 1: Write failing frontend status tests**

```typescript
import { describe, expect, it } from "vitest";
import { generationStatusKey, generationStatusTone, isTerminalGenerationStatus } from "./generationStatus";

describe("generation status semantics", () => {
  it("keeps cancelling active and cancelled non-failure", () => {
    expect(isTerminalGenerationStatus("cancelling")).toBe(false);
    expect(isTerminalGenerationStatus("cancelled")).toBe(true);
    expect(generationStatusTone("cancelling")).toBe("running");
    expect(generationStatusTone("cancelled")).toBe("cancelled");
    expect(generationStatusKey("cancelling")).toBe("status.cancelling");
  });
});
```

- [ ] **Step 2: Run and verify RED**

```powershell
pnpm --dir frontend exec vitest run src/lib/generationStatus.test.ts
```

Expected: module missing.

- [ ] **Step 3: Implement the shared helpers and type unions**

```typescript
export const terminalGenerationStatuses = new Set<GenerationStatus>(["completed", "failed", "cancelled"]);

export function isTerminalGenerationStatus(status: GenerationStatus): boolean {
  return terminalGenerationStatuses.has(status);
}

export function generationStatusTone(status: GenerationStatus): "idle" | "queued" | "running" | "completed" | "failed" | "cancelled" {
  if (status === "completed") return "completed";
  if (status === "failed") return "failed";
  if (status === "cancelled") return "cancelled";
  if (status === "queued") return "queued";
  if (["loading", "running", "finalizing", "cancelling"].includes(status)) return "running";
  return "idle";
}
```

Add `cancelling` to both status unions and return `status.cancelling` from the key helper.

- [ ] **Step 4: Replace local status arrays/helpers in `App.tsx`**

Use `isTerminalGenerationStatus` for active job selection and poll termination. Count failed and cancelled items separately; include both in processed progress. Add a fifth queue count with `status.cancelled`. A cancelling job remains visible and the cancel action stays disabled after the first accepted request.

- [ ] **Step 5: Add labels and tones**

Add Chinese `取消中` and English `Cancelling`; style `.status-pill.cancelling` as running and `.status-pill.cancelled` as neutral/grey rather than red.

- [ ] **Step 6: Run focused and complete frontend gates**

```powershell
pnpm --dir frontend exec vitest run src/lib/generationStatus.test.ts src/i18n.test.ts
pnpm --dir frontend test
pnpm --dir frontend build
```

Expected: focused tests, complete Vitest suite, TypeScript, and Vite build pass.

- [ ] **Step 7: Commit Task 8**

```powershell
git add frontend/src/types.ts frontend/src/lib/generationStatus.ts frontend/src/lib/generationStatus.test.ts frontend/src/App.tsx frontend/src/App.css frontend/src/i18n.ts
git commit -m "feat: show generation cancellation convergence"
```

### Task 9: Build the fail-closed reliability evidence core

**Files:**
- Create: `backend/app/comfyui/reliability_validation.py`
- Create: `backend/tests/test_comfyui_reliability_validation.py`
- Create: `deployment/tts-repos/windows-reliability-fixture.example.json`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: TTS More job/manifest APIs, ComfyUI queue/history, Bridge capabilities/runtime routes, local WAV/process/GPU observations.
- Produces: `ReliabilityFixture`, `CaseEvidence`, `ReliabilityRunSummary`, `write_atomic_json()`, `validate_case()`, and `finalize_run()`.

- [ ] **Step 1: Write failing evidence-model and atomic-write tests**

```python
def _valid_fixture_document():
    return {
        "version": 1,
        "base_urls": {
            "tts_more": "http://127.0.0.1:8000",
            "comfyui": "http://127.0.0.1:8188",
        },
        "resources": {
            "gpt-sovits": {
                "resource_id": "gpt-main",
                "reference_audio": "fixtures/gpt-reference.wav",
                "reference_text": "GPT reference text",
            },
            "indextts": {
                "resource_id": "index-main",
                "reference_audio": "fixtures/index-reference.wav",
                "reference_text": "Index reference text",
            },
            "cosyvoice": {
                "resource_id": "cosy-main",
                "reference_audio": "fixtures/cosy-reference.wav",
                "reference_text": "CosyVoice reference text",
            },
        },
        "rounds": 10,
    }


def test_finalize_run_fails_closed_when_required_case_or_cleanup_is_missing(tmp_path):
    fixture = ReliabilityFixture.model_validate(_valid_fixture_document())
    cases = [
        CaseEvidence(case_id="steady-gpt-01", engine="gpt-sovits", expected="completed", actual="completed", cleanup_ok=True),
        CaseEvidence(case_id="cancel-index", engine="indextts", expected="cancelled", actual="cancelled", cleanup_ok=False),
    ]
    summary = finalize_run(fixture, cases, required_case_ids={"steady-gpt-01", "cancel-index", "restart-cosy"})
    assert summary.status == "failed"
    assert summary.missing_cases == ["restart-cosy"]
    assert summary.cleanup_failures == ["cancel-index"]


def test_atomic_evidence_never_leaves_partial_file(tmp_path, monkeypatch):
    target = tmp_path / "summary.json"
    write_atomic_json(target, {"status": "passed"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "passed"}
    assert list(tmp_path.glob(".summary.json.*.tmp")) == []
```

- [ ] **Step 2: Run and verify RED**

```powershell
& backend\.venv\Scripts\python.exe -m pytest backend/tests/test_comfyui_reliability_validation.py -q
```

Expected: module missing.

- [ ] **Step 3: Implement strict Pydantic evidence models**

Use `extra="forbid"`, explicit expected/actual outcome literals, UTC timestamps, prompt/job/version identities, audio proof, cleanup proof, process identities, queue snapshots, and GPU snapshots. `finalize_run()` must require exactly 10 successful `steady` cases per engine, then independently require every named fault and recovery case; recovery successes do not count toward the 10-per-engine steady matrix.

- [ ] **Step 4: Implement WAV and boundary validation**

Reuse the finite/non-silent rules from `comfyui.output` for evidence reads. Compare before/after repository HEAD/branch/status, private registry hash, reference hashes, and model checkout porcelain. Do not store absolute private paths in committed summaries.

- [ ] **Step 5: Add the redacted fixture schema and ignore the private fixture**

The example contains stable keys and empty neutral values:

```json
{
  "version": 1,
  "base_urls": {
    "tts_more": "http://127.0.0.1:8000",
    "comfyui": "http://127.0.0.1:8188"
  },
  "resources": {
    "gpt-sovits": {"resource_id": "", "reference_audio": "", "reference_text": ""},
    "indextts": {"resource_id": "", "reference_audio": "", "reference_text": ""},
    "cosyvoice": {"resource_id": "", "reference_audio": "", "reference_text": ""}
  },
  "rounds": 10
}
```

Ignore `data/local/windows-reliability-fixture.json` and validation output roots.

- [ ] **Step 6: Run validator and release-governance tests**

```powershell
& backend\.venv\Scripts\python.exe -m pytest backend/tests/test_comfyui_reliability_validation.py backend/tests/test_release_governance.py -q
```

Expected: pass; the example has no machine-specific path.

- [ ] **Step 7: Commit Task 9**

```powershell
git add .gitignore backend/app/comfyui/reliability_validation.py backend/tests/test_comfyui_reliability_validation.py deployment/tts-repos/windows-reliability-fixture.example.json
git commit -m "test: define Windows ComfyUI reliability evidence"
```

### Task 10: Add the owned-process Windows live validator

**Files:**
- Create: `scripts/run-windows-comfyui-reliability.ps1`
- Modify: `backend/app/comfyui/reliability_validation.py`
- Modify: `backend/tests/test_comfyui_reliability_validation.py`
- Create: `docs/comfyui-windows-reliability.md`

**Interfaces:**
- Consumes: Task 9 fixture/evidence core, official ComfyUI CLI, TTS More API, Bridge API, and exact process identities.
- Produces: one opt-in command that executes preflight, 30 steady requests, fault matrix, final release, and boundary comparison.

- [ ] **Step 1: Write failing scenario-plan tests**

```python
def test_reliability_plan_contains_exact_steady_and_fault_cases():
    plan = build_case_plan(rounds=10)
    steady = [case for case in plan if case.phase == "steady"]
    assert [case.engine for case in steady] == ["gpt-sovits", "indextts", "cosyvoice"] * 10
    assert len({case.case_id for case in plan}) == len(plan)
    assert {case.case_id for case in plan if case.phase == "fault"} == {
        "cancel-queued",
        "cancel-running-gpt-sovits",
        "recover-cancel-gpt-sovits",
        "cancel-running-indextts",
        "recover-cancel-indextts",
        "cancel-running-cosyvoice",
        "recover-cancel-cosyvoice",
        "timeout-gpt-sovits",
        "recover-timeout-gpt-sovits",
        "timeout-indextts",
        "recover-timeout-indextts",
        "timeout-cosyvoice",
        "recover-timeout-cosyvoice",
        "terminate-comfyui-indextts",
        "restart-gpt-sovits",
        "restart-indextts",
        "restart-cosyvoice",
    }
```

- [ ] **Step 2: Run and verify RED**

```powershell
& backend\.venv\Scripts\python.exe -m pytest backend/tests/test_comfyui_reliability_validation.py -k 'scenario_plan' -q
```

Expected: `build_case_plan` missing.

- [ ] **Step 3: Implement the exact case planner and probes**

Add typed cases for successful synthesis, queued cancellation, in-flight cancellation, one-second timeout, owned ComfyUI termination, restart readiness, and post-fault recovery. Add HTTP probes for TTS More job/manifest/queue and ComfyUI queue/history/capabilities/runtime. Add host probes for matching runner processes, request temp directories, and `nvidia-smi` memory.

- [ ] **Step 4: Implement PowerShell process ownership**

The script accepts mandatory `-Fixture`, `-OutputRoot`, `-ComfyUiRoot`, `-ComfyPython`, and `-TtsMoreRoot`. Before recursive cleanup or process termination, resolve and compare exact absolute paths. Record PID, creation time, executable, command line, and parent. Use `Start-Process -WindowStyle Hidden -PassThru`; stop only recorded identities whose current identity still matches.

- [ ] **Step 5: Implement preflight and fail-safe cleanup**

Preflight refuses dirty changes caused after its baseline, non-loopback endpoints unless `-AllowLan` is explicit, missing three ready resources, occupied ports not owned by the configured processes, or a non-idle initial queue. A `finally` block writes evidence first, then stops only validator-owned backend/frontend/ComfyUI processes and removes only validation-owned temp/output files.

- [ ] **Step 6: Implement the live execution call**

The PowerShell script invokes the Python module with the private fixture:

```powershell
& backend\.venv\Scripts\python.exe -m app.comfyui.reliability_validation `
  --fixture $Fixture `
  --output-root $OutputRoot `
  --comfyui-pid $comfyProcess.Id `
  --tts-more-pid $backendProcess.Id
if ($LASTEXITCODE -ne 0) { throw "Windows ComfyUI reliability gate failed" }
```

- [ ] **Step 7: Write the runbook with exact acceptance gates**

Document private fixture creation, environment startup, command invocation, expected duration, evidence layout, 30/180-second gates, 1,024 MiB memory tolerance, failure triage, and safe cleanup. State that live success is not an eight-hour, throughput, audio-quality, or multi-GPU certification.

- [ ] **Step 8: Run deterministic validator tests and PowerShell syntax parse**

```powershell
& backend\.venv\Scripts\python.exe -m pytest backend/tests/test_comfyui_reliability_validation.py -q
$null = [scriptblock]::Create((Get-Content -Raw scripts\run-windows-comfyui-reliability.ps1))
```

Expected: tests pass and PowerShell parses without exception.

- [ ] **Step 9: Commit Task 10**

```powershell
git add backend/app/comfyui/reliability_validation.py backend/tests/test_comfyui_reliability_validation.py scripts/run-windows-comfyui-reliability.ps1 docs/comfyui-windows-reliability.md
git commit -m "test: automate Windows ComfyUI reliability validation"
```

### Task 11: Run complete deterministic gates before live inference

**Files:**
- Modify only if a deterministic test exposes an evidence-backed regression; add its failing test before its fix.

**Interfaces:**
- Consumes: Tasks 1-10 final source.
- Produces: clean deterministic baseline eligible for expensive live validation.

- [ ] **Step 1: Run the complete plugin suite twice**

```powershell
$env:COMFYUI_TESTING = '1'
1..2 | ForEach-Object {
  & F:\TTS-More\runtime\py312\python.exe -m pytest tests/unit -q
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& F:\TTS-More\runtime\py312\python.exe -m pip check
```

Expected: two consecutive full passes and clean dependency integrity.

- [ ] **Step 2: Run focused TTS More reliability suites**

```powershell
& backend\.venv\Scripts\python.exe -m pytest `
  backend/tests/test_comfyui_client.py `
  backend/tests/test_comfyui_output.py `
  backend/tests/test_service_queue.py `
  backend/tests/test_comfyui_reliability_validation.py `
  backend/tests/test_api.py -q
```

Expected: pass.

- [ ] **Step 3: Run complete TTS More hosted-CI-equivalent gates**

```powershell
$env:TTS_MORE_SKIP_LEGACY_PORTABLE = '1'
Remove-Item Env:HTTP_PROXY,Env:HTTPS_PROXY,Env:ALL_PROXY -ErrorAction SilentlyContinue
& backend\.venv\Scripts\python.exe -m pytest backend -q
pnpm --dir frontend test
pnpm --dir frontend build
```

Expected: backend, frontend tests, and build pass. Record any Windows host-capability-only skip/failure separately; do not modify unrelated legacy portable behavior to force a local pass.

- [ ] **Step 4: Verify repository boundaries before live work**

```powershell
git -C F:\Code\Github\TTS_more status --short --branch
git -C F:\Code\Github\TTS-Audio-Suite status --short --branch
git -C F:\Code\Github\ComfyUI status --short --branch
```

Expected: both feature branches clean; official ComfyUI clean.

### Task 12: Execute the Windows steady-state and fault matrix

**Files:**
- Create: private evidence under `F:\TTS-More\validation\2026-08-01-windows-reliability-contract`
- Create after pass: `docs/comfyui-windows-reliability-report.md`

**Interfaces:**
- Consumes: the private fixture, compatible runtimes/models, Tasks 1-11 final source.
- Produces: authoritative live evidence for every acceptance criterion and a redacted repository report.

- [ ] **Step 1: Capture immutable preflight evidence**

Copy the example fixture to `data/local/windows-reliability-fixture.json`, fill only local private values, then run the validator with `-PreflightOnly`. Verify three exact SHAs/status baselines, three ready resource IDs, idle queues, no unexpected matching runners/temp directories, and the idle GPU baseline.

- [ ] **Step 2: Run the complete reliability gate**

```powershell
pwsh -NoProfile -File scripts\run-windows-comfyui-reliability.ps1 `
  -Fixture (Resolve-Path data\local\windows-reliability-fixture.json).Path `
  -OutputRoot 'F:\TTS-More\validation\2026-08-01-windows-reliability-contract' `
  -ComfyUiRoot 'F:\Code\Github\ComfyUI' `
  -ComfyPython 'F:\TTS-More\runtime\py312\python.exe' `
  -TtsMoreRoot (Resolve-Path .).Path
```

Expected: exit 0 only after 30/30 steady syntheses, every expected fault outcome, every recovery synthesis, final release, final idle queues, zero runners/temp directories, memory recovery, and boundary preservation.

- [ ] **Step 3: Stop on the first product failure and apply the debugging gate**

If the command exits nonzero, do not rerun blindly. Preserve the case evidence and logs, invoke `superpowers:systematic-debugging`, reproduce the smallest failing boundary, add a deterministic RED regression to the owning repository, implement one root-cause fix, rerun the focused GREEN test, then restart Task 11. A harness-only defect must be labeled non-authoritative and fixed without adjudicating the product case as passed.

- [ ] **Step 4: Independently re-read final evidence**

Use a separate verification command/process that does not submit inference or restart services. Require all case IDs, expected outcome matches, audio hashes/metrics, cleanup records, final queue/runtime/process/temp/memory state, and before/after boundary comparison.

- [ ] **Step 5: Write the redacted report**

Summarize exact request counts, engine success counts, fault/recovery outcomes, timing gates, memory result, repository SHAs, tests, known non-goals, and evidence root. Do not include private paths beyond the designated validation root or private registry values.

- [ ] **Step 6: Commit the report and any final deterministic report tests**

```powershell
git add docs/comfyui-windows-reliability-report.md backend/tests/test_release_governance.py
git commit -m "docs: certify Windows ComfyUI cancellation recovery"
```

### Task 13: Complete plugin-first PR, merged-source smoke, TTS More PR, and dual-remote sync

**Files:**
- No new product files unless CI exposes an evidence-backed regression.

**Interfaces:**
- Consumes: clean feature branches, deterministic gates, and authoritative live evidence.
- Produces: merged plugin `main`, merged TTS More `master`, synchronized GitHub/Gitee refs, stopped validation services, and removed clean merged worktrees.

- [ ] **Step 1: Push and open the plugin PR**

```powershell
git -C F:\Code\Github\TTS-Audio-Suite push -u origin dev-xu/windows-runner-interrupt
gh pr create --repo XucroYuri/TTS-Audio-Suite --base main --head dev-xu/windows-runner-interrupt --title "Make registered TTS runners interruptible on Windows" --body-file F:\Code\Github\TTS_more\data\local\pr-bodies\windows-runner-interrupt.md
```

Before running the command, create the PR body under the ignored private `data/local/pr-bodies` folder. It must list deterministic tests, Windows Job Object proof, live cancellation evidence, and official-source preservation. Do not place memory citations in the PR body.

- [ ] **Step 2: Wait for and repair plugin CI**

Require all Ubuntu/Windows Python 3.12/3.13 jobs to pass. For a failure, inspect `gh run view --log-failed`, invoke systematic debugging, add a failing regression, push the fix, and wait for the replacement run.

- [ ] **Step 3: Merge the plugin PR and update the active custom node**

Merge only when the head SHA, mergeability, and all checks are verified. Fast-forward local plugin `main` to `origin/main`. Verify `F:\Code\Github\ComfyUI\custom_nodes\TTS-Audio-Suite` points to the merged plugin checkout before smoke testing.

- [ ] **Step 4: Run product-equivalent merged-plugin smoke**

Run one new GPT-SoVITS, IndexTTS, and CosyVoice request through the TTS More feature branch against merged plugin main. Require unique prompts, valid non-silent WAVs, terminal success, cleanup convergence, and unchanged official/model checkouts.

- [ ] **Step 5: Push and open the TTS More PR**

```powershell
git -C F:\Code\Github\TTS_more push -u github dev-xu/windows-comfyui-reliability
gh pr create --repo XucroYuri/TTS_more --base master --head dev-xu/windows-comfyui-reliability --title "Harden Windows ComfyUI cancellation and recovery" --body-file F:\Code\Github\TTS_more\data\local\pr-bodies\windows-comfyui-reliability.md
```

The private PR body must distinguish deterministic CI, live CUDA evidence, injected expected failures, recovery proof, and deferred soak/throughput scope.

- [ ] **Step 6: Require GitHub CI and merge TTS More**

Require Ubuntu backend, Windows backend, frontend tests/build, and every configured required check to pass. Repair only root-caused failures with new regressions. Merge the PR and delete the remote feature branch.

- [ ] **Step 7: Synchronize local, GitHub, and Gitee master exactly**

```powershell
git -C F:\Code\Github\TTS_more fetch github --prune
git -C F:\Code\Github\TTS_more fetch origin --prune
git -C F:\Code\Github\TTS_more switch master
git -C F:\Code\Github\TTS_more merge --ff-only github/master
git -C F:\Code\Github\TTS_more push origin master:master
git -C F:\Code\Github\TTS_more fetch origin master:refs/remotes/origin/master
git -C F:\Code\Github\TTS_more rev-parse master github/master origin/master
```

Expected: all three printed SHAs are identical. Also require plugin local `main`, tracking `origin/main`, and live `ls-remote origin refs/heads/main` to match.

- [ ] **Step 8: Clean only owned merged artifacts and verify final state**

Stop only validation-owned processes after revalidating PID identity. Verify ports 5173/8000/8188 are not held by those processes, queues/runtimes are idle, both feature worktrees are clean and merged, then remove those exact worktrees/branches. Preserve all model environments, model artifacts, private configuration, and pre-existing checkout dirt.

- [ ] **Step 9: Run the final completion audit**

Check every acceptance criterion in the design against current files, final test outputs, live evidence, PR states, merged SHAs, remote refs, ports, processes, queues, and repository statuses. Do not mark the persistent goal complete if any evidence is missing, indirect, stale, or contradictory.

## Plan self-review checklist

- Every design goal is owned by Tasks 1-13.
- Plugin interruption is implemented before TTS More relies on targeted prompt interruption.
- `SynthesisRequest.cancel_check`, `SynthesisControlError.details`, `PromptCancellationResult`, and `GenerationStatus.cancelling` names are consistent across producer and consumer tasks.
- Cancellation-before-dispatch, in-flight cancellation, timeout, connection loss, completion race, cleanup failure, and recovery each have deterministic or live proof.
- The 30-request matrix is exactly 10 round-robin requests per engine.
- Official ComfyUI and official TTS projects remain unchanged.
- Hosted CI never attempts private live inference.
- Delivery ends only after plugin-first merge, merged-plugin smoke, TTS More merge, GitHub/Gitee equality, and owned cleanup.
