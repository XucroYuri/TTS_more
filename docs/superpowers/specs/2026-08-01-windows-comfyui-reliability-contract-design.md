# Windows ComfyUI TTS Reliability Contract Design

**Status:** Approved design baseline for implementation planning

**Scope:** TTS More + official ComfyUI + the XucroYuri TTS-Audio-Suite fork on Windows, using registered GPT-SoVITS, IndexTTS, and CosyVoice official-model runtimes

## Context

The current `master` baseline has already proved real, non-silent synthesis for
all three engines through TTS More, ComfyUI, and TTS-Audio-Suite. It also has
deterministic unit and integration coverage for workflow construction, queue
serialization, Bridge resources, runtime release, external runner containment,
and temporary-directory cleanup.

That evidence does not yet prove reliable cancellation, timeout convergence,
service-loss recovery, or repeated mixed-engine operation. Two current behaviors
are especially important:

1. TTS More marks a job cancelled but only checks cancellation between line
   dispatches. A synthesis already waiting on ComfyUI continues running.
2. TTS More stops polling when its timeout expires but does not cancel the
   submitted ComfyUI prompt. TTS-Audio-Suite's registered external engines wait
   on blocking subprocess communication, so a ComfyUI interrupt alone cannot
   guarantee prompt, runner, asset, and temporary-directory convergence.

This design adds a cross-layer reliability contract without turning TTS More
into a supervisor for official ComfyUI and without modifying official model
projects.

## Goals

- Make queued and in-flight user cancellation truthful and bounded.
- Make a TTS More timeout cancel the corresponding ComfyUI work instead of only
  abandoning the poll loop.
- Make all three registered external engine runners respond to a targeted
  ComfyUI interruption while retaining Windows Job Object containment.
- Preserve accurate job, queue-item, generation-history, and external-prompt
  state across cancellation races and service loss.
- Prevent cancelled, timed-out, or failed requests from leaving partial TTS More
  WAV files, uploaded reference assets, external runner trees, or request-temp
  directories.
- Prove recovery on the current Windows host with a deterministic 30-request
  mixed-engine run plus fault injection and post-fault synthesis.
- Produce atomic, reviewable evidence that distinguishes product success,
  expected injected failures, cleanup convergence, and test-harness failure.

## Non-goals

- TTS More will not install, update, start, stop, or repair official ComfyUI in
  normal product operation.
- Official ComfyUI source will not be changed.
- GPT-SoVITS, IndexTTS, and CosyVoice source projects will not be changed.
- The work will not add automatic synthesis retries. Retrying an ambiguous
  prompt can create duplicate work and hide a non-converged first attempt.
- The work will not claim an eight-hour soak, production throughput benchmark,
  formal audio-quality benchmark, or multi-GPU certification. Those remain a
  separate reliability phase after this contract passes.
- ComfyUI's durable output/history files are not classified as temporary
  leakage. The validation harness may remove only files that it created and
  positively owns after evidence has been captured.

## Ownership boundaries

### TTS More

TTS More owns job state, cancellation intent, prompt submission and polling,
targeted prompt cancellation, reference-asset lifecycle, downloaded output
publication, manifest history, and user-visible error classification.

### TTS-Audio-Suite fork

The plugin owns registered-resource resolution, Bridge runtime leases, external
one-shot runner creation, interrupt observation inside a running node, Windows
process-tree containment, and runner/request-temp cleanup.

### Official ComfyUI

Official ComfyUI remains the queue and workflow executor. The integration uses
its existing targeted `POST /interrupt` behavior and queued-item deletion via
`POST /queue`; no private patch is maintained.

### Official TTS projects and models

The three projects remain read-only inference dependencies addressed through
private `resources.yaml` paths. Their WebUIs are not required or launched.

## Reliability invariants

The implementation and live gate must preserve all of these invariants:

1. Every submitted ComfyUI prompt ID is recorded in TTS More before it can be
   cancelled, timed out, completed, or failed.
2. Cancellation is prompt-scoped. TTS More must never issue an untargeted global
   interrupt to cancel one job.
3. Once cancellation is accepted for a nonterminal job, no later successful
   generation version may be committed for that cancelled request.
4. A queued prompt is removed from the ComfyUI queue; a running prompt receives
   a targeted interrupt. Repeating either operation is safe.
5. User cancellation, business timeout, ComfyUI unavailability, engine failure,
   and cleanup failure remain distinct outcomes.
6. Uploaded reference audio is deleted only after the prompt is terminal or no
   longer present in ComfyUI's running/pending queues. An asset must not be
   deleted while its prompt may still read it.
7. A cancelled or failed output is never published at the final TTS More WAV
   path. Output publication uses a same-directory temporary file followed by an
   atomic replace after decode and non-silence validation.
8. The Windows external runner and every descendant are assigned to a kill-on-
   close Job Object before execution is resumed.
9. Runner interruption and timeout cleanup are bounded. Failure to prove tree
   exit is reported as a cleanup failure, not silently treated as success.
10. A service crash cannot leave a TTS More job indefinitely `running` or
    `cancelling`.
11. A recovery synthesis is a new prompt with a new job/version identity and a
    newly decoded, non-silent WAV; health or capabilities alone do not prove
    recovery.
12. The default single-GPU resource group remains `capacity=1`.

## Architecture

### 1. Cancellation state and request contract

`GenerationStatus` gains `cancelling`. A running job moves through
`running -> cancelling -> cancelled` only after its in-flight prompt has
converged. If convergence cannot be proved, it moves to `failed` with a cleanup
diagnostic instead of claiming cancellation succeeded.

`SynthesisRequest` gains an optional cancellation callback. The callback is
side-effect free and answers whether the owning job has entered `cancelling` or
`cancelled`. Existing service clients may ignore it; the ComfyUI client must
observe it while polling.

`GenerationJobManager.cancel()` behaves as follows:

- A job that has not dispatched work is cancelled immediately.
- Queued items are marked cancelled and are never dispatched.
- A job with an in-flight item becomes cancelling. That item remains nonterminal
  until the ComfyUI client reports successful convergence or a cleanup failure.
- Completed and failed jobs are unchanged by a later cancellation request.
- Repeated cancellation requests return the same current state.

If synthesis wins the race and returns after cancellation was accepted, the
queue discards the uncommitted result, removes its validation-owned partial
output, records a cancelled version, and does not publish it as completed.

### 2. Prompt-scoped ComfyUI control

`ComfyUIAPIClient` gains prompt queue inspection and a single idempotent
`cancel_prompt(prompt_id, max_wait)` operation:

1. Inspect `/queue` to classify the prompt as running, pending, or absent.
2. Send `POST /interrupt` with the exact `prompt_id` when running.
3. Send `POST /queue` with `{"delete": [prompt_id]}` when pending.
4. Poll queue and history until the prompt is absent from running/pending work
   and has either terminal history or confirmed dequeue.
5. Return a structured cancellation result containing prompt ID, initial state,
   requested actions, final state, duration, and sanitized diagnostics.

Already-terminal and already-absent prompts are successful idempotent outcomes.
HTTP failure or failure to converge within 30 seconds is a cleanup failure.

`poll_until_done()` checks the cancellation callback independently of the
normal history interval. When user cancellation is observed, it invokes
`cancel_prompt()` and raises a typed cancellation exception carrying the
structured result.

When the synthesis deadline expires, the same `cancel_prompt()` path runs
before a typed timeout exception is raised. The timeout remains the primary
outcome; any cancellation/cleanup defect is attached as secondary evidence.

### 3. Interruptible TTS-Audio-Suite external runners

The three registered engines currently wait on blocking `Popen.communicate()`.
They will use one shared interrupt-aware communication primitive owned by the
plugin's external-subprocess layer.

The primitive:

- waits in short bounded slices rather than for the full engine timeout;
- checks ComfyUI's processing-interrupted state between slices through an
  injectable callback;
- on interruption, terminates the Windows Job Object/process tree using the
  same bounded cleanup path used for engine timeout;
- captures stdout/stderr without losing the primary interruption or timeout;
- closes the Job Object only after exit is verified;
- preserves the original request-temp cleanup behavior; and
- works without importing ComfyUI when exercised by isolated unit tests.

GPT-SoVITS, IndexTTS, and CosyVoice must each call the shared primitive. Unit
tests must prove that no engine retains a direct full-duration blocking
`communicate()` path.

### 4. Truthful manifest and API outcomes

Typed internal outcomes map to product state as follows:

| Event | Job/item state | Generation version | Error semantics |
| --- | --- | --- | --- |
| Cancel before dispatch | `cancelled` | No attempted version | No error |
| Cancel in flight and converge | `cancelling -> cancelled` | `cancelled`, prompt metadata retained | No failure error; cancellation reason retained |
| Cancel requested but cleanup unproved | `failed` | `failed` | Primary cancellation plus cleanup diagnostic |
| Synthesis deadline exceeded and converge | `failed` | `failed` | Timeout code plus prompt cancellation evidence |
| ComfyUI connection lost | `failed` within the endpoint timeout | `failed` | Sanitized service-unavailable diagnostic |
| Engine execution error | `failed` | `failed` | Sanitized ComfyUI execution error |
| Successful synthesis | `completed` | `completed` | Final WAV metadata and prompt ID |

The frontend/API accepts and renders `cancelling` as a nonterminal state. It
must not offer a second cancel as a new operation, label the job completed, or
display a cancelled line as a synthesis failure.

### 5. Atomic output and reference-asset cleanup

Downloaded or transcoded audio is written to a unique temporary sibling of the
requested output. The file is decoded in full and must have a positive sample
rate, nonzero frames, finite samples, and a peak above the existing silence
threshold. Only then is it atomically moved to the final WAV path.

Cancellation, timeout, connection loss, decode failure, or manifest failure
removes the temporary sibling. A pre-existing final WAV is not overwritten
until the new output has passed validation.

Reference-asset deletion happens after prompt convergence. If primary synthesis
failed and asset deletion also fails, both outcomes are retained. Cleanup
failure must be visible in evidence and logs without replacing the primary
error category.

## Windows reliability validator

The validator is an opt-in, local-only harness. It is not part of ordinary
hosted CI and refuses non-loopback ComfyUI targets by default. It owns only the
processes and files it starts or creates.

### Preflight

Before changing runtime state, the validator records:

- full TTS More, TTS-Audio-Suite, and official ComfyUI commit IDs and porcelain
  states;
- the three redacted Bridge resource capabilities;
- Python, Node, Torch, CUDA, GPU, driver, FFmpeg, and ffprobe versions;
- listener identities on the selected frontend, backend, and ComfyUI ports;
- TTS More queue state, ComfyUI queue, Bridge runtime status, matching external
  runner processes, request-temp directories, and GPU memory;
- hashes of the private registry and validation reference assets without
  publishing their absolute paths.

Pre-existing dirty model checkouts are allowed but captured and required to be
byte/state identical after validation.

### Steady-state matrix

The primary run contains ten deterministic rounds. Each round executes
GPT-SoVITS, IndexTTS, then CosyVoice, for exactly 30 accepted synthesis
requests. Every request uses distinct text and a new TTS More job/version and
ComfyUI prompt ID.

For every request the validator requires:

- terminal TTS More status `completed`;
- terminal successful ComfyUI history;
- a fully decodable, finite, non-silent PCM WAV;
- engine-appropriate sample-rate evidence;
- no pending/running ComfyUI prompt after completion;
- no external one-shot runner or request-temp directory after its bounded
  cleanup window; and
- no unexpected modification to official checkouts, model assets, reference
  assets, or the private registry.

An idle non-busy Bridge runtime may exist between successful requests. After an
explicit final release, all Bridge runtimes must be absent.

### Fault matrix

The fault phase runs after the steady-state matrix:

1. Cancel a queued second line while the first line owns the single-GPU
   resource group. The second line must never receive a ComfyUI prompt.
2. Cancel an in-flight GPT-SoVITS prompt and prove cancellation convergence,
   child-tree exit, temp cleanup, truthful history, and a subsequent successful
   GPT-SoVITS request.
3. Repeat the in-flight cancellation and recovery requirement for IndexTTS.
4. Repeat the in-flight cancellation and recovery requirement for CosyVoice.
5. Force a one-second TTS More deadline for each engine. Each prompt must be
   cancelled, each job must fail as timeout rather than generic engine failure,
   and the next normal-timeout request for that engine must pass.
6. Terminate only the validator-owned ComfyUI process while an IndexTTS runner
   is active. Windows Job Object closure must eliminate the runner tree, TTS
   More must leave its nonterminal state within 30 seconds, and no partial WAV
   or request temp may remain.
7. Restart the same official ComfyUI checkout with the same registry, wait up to
   180 seconds for system stats, object info, and all three redacted resources,
   then synthesize successfully once with each engine.

The validator never kills a process based only on its executable name. PID,
creation time, command line, executable path, and parent/Job Object ownership
must match the process it started or observed for the current prompt.

### Resource and convergence gates

- Prompt cancellation convergence: at most 30 seconds.
- TTS More exit from `running`/`cancelling` after owned ComfyUI termination: at
  most 30 seconds.
- ComfyUI restart readiness: at most 180 seconds.
- Final Bridge release and GPU recovery: at most 30 seconds.
- Final GPU reserved/used memory: no more than 1,024 MiB above the recorded idle
  baseline, matching the existing Windows CUDA recovery tolerance.
- Final TTS More queue: zero queued and zero running/cancelling items.
- Final ComfyUI queue: zero running and zero pending prompts.
- Final external runners and validation request-temp directories: zero.

## Test strategy

### TTS More deterministic tests

- Prompt cancellation for running, pending, terminal, absent, and HTTP-failure
  states using `httpx.MockTransport`.
- Idempotent repeated cancellation and targeted-only interrupt assertions.
- Poll-loop cancellation and timeout convergence, including secondary cleanup
  failure preservation.
- Reference-asset deletion ordering and dual-error reporting.
- Atomic output publication, validation failure, cancellation race, and partial
  file cleanup.
- Job state transitions for queued cancellation, in-flight cancellation,
  cancellation/completion races, cancellation cleanup failure, and service
  loss.
- API/frontend rendering and persistence of `cancelling`, `cancelled`, timeout,
  and cleanup diagnostics.
- Regression coverage proving `capacity=1` serialization remains unchanged.

### TTS-Audio-Suite deterministic tests

- Shared communication helper exits normally and preserves output.
- Each engine observes an injected interrupt, invokes bounded tree cleanup, and
  removes request temp.
- Timeout and interrupt retain distinct primary diagnostics.
- Cleanup failure never becomes a false cancellation success.
- Windows Job Object tests verify a late-spawned descendant exits after
  interruption and after owner-process loss.
- Non-Windows and isolated-test imports do not require a live ComfyUI module.

### Hosted CI boundary

Linux and Windows hosted CI run deterministic tests only. Live tests remain
behind an explicit opt-in variable and require an already provisioned official
ComfyUI, private registry, compatible checkout interpreters, models, reference
audio, and CUDA host. Missing live fixtures skip rather than contacting the
default loopback URL.

## Evidence contract

The Windows validator writes one atomic run summary and per-case atomic evidence
under a private validation root. Each case contains:

- case ID, expected outcome, actual outcome, start/end timestamps, duration,
  engine, redacted resource ID, TTS More job/version, and ComfyUI prompt ID;
- output size, sample rate, frames, peak, RMS, and SHA-256 for successful WAVs;
- cancellation/timeout action and convergence records for expected failures;
- queue, runtime, runner, temp, process-identity, and GPU snapshots before,
  during, and after the case;
- raw command exit code plus stdout/stderr sidecars; and
- cleanup status independent from the primary case result.

The aggregate fails closed when any required case, sidecar, identity field,
audio field, cleanup observation, or final boundary comparison is missing. A
harness failure cannot be adjudicated as a product pass without separate raw
product evidence and an explicit non-authoritative label.

Published repository documentation contains only redacted summaries and
placeholder paths. Private model paths, registry content, access tokens,
machine usernames, and raw environment variables are not committed.

## Acceptance criteria

The implementation phase is complete only when all of the following are true:

1. TTS More and TTS-Audio-Suite deterministic suites pass on the final source.
2. Frontend tests and production build pass with the cancelling state included.
3. The 30-request matrix passes 10/10 requests for each engine.
4. Every fault case reaches its exact expected terminal state and cleanup gate.
5. Every fault case is followed by the required real, non-silent recovery
   synthesis.
6. Final release, queue, process, temp, GPU, repository, registry, model, and
   reference-asset boundaries pass.
7. Official ComfyUI and the three official TTS projects have no validation-
   caused source changes.
8. TTS-Audio-Suite changes pass their PR CI and merge before the dependent TTS
   More PR is finalized.
9. TTS More changes pass GitHub Linux/Windows backend and frontend CI, merge to
   `master`, and synchronize to the configured Gitee `master`.
10. The validation report states the remaining non-goals and does not call this
    phase a long-duration, throughput, audio-quality, or multi-GPU
    certification.

## Delivery sequence

1. Implement and review the plugin's interrupt-aware runner contract.
2. Implement TTS More cancellation, timeout, state, output, and evidence
   contracts against deterministic fakes.
3. Run focused and complete deterministic suites in both repositories.
4. Run Windows live preflight, steady-state matrix, fault matrix, and final
   boundary comparison.
5. Repair only evidence-backed defects, adding a failing regression before each
   product fix.
6. Merge the plugin PR, re-run product-equivalent smoke validation against the
   merged plugin main, then merge the TTS More PR.
7. Synchronize local, GitHub, and Gitee master references and remove only clean,
   merged, validation-owned worktrees and processes.

## Deferred follow-up

After this phase passes, a separate design may define an eight-hour or
100-request soak, latency/throughput budgets, multi-ComfyUI or multi-GPU
scheduling, upgrade compatibility matrices, and formal audio-quality scoring.
Those gates must consume this cancellation and recovery contract rather than
re-implementing it.
