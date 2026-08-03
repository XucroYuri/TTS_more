# Windows ComfyUI formal reliability supervisor

This supervised validator is an opt-in, single-GPU acceptance gate for TTS More with ComfyUI and TTS-Audio-Suite. It runs 30 steady syntheses in round-major order, the cancellation/timeout/termination matrix, recovery syntheses, runtime release, queue convergence, GPU recovery, runner cleanup, and final repository/model boundary comparison.

It does not run in CI and does not modify the official GPT-SoVITS, IndexTTS, CosyVoice, or ComfyUI checkouts. A pass is not an eight-hour soak, throughput benchmark, subjective audio-quality certification, or multi-GPU certification.

## Private inputs

Copy `deployment/tts-repos/windows-reliability-fixture.example.json` to the ignored `data/local` area. Keep both endpoints on `127.0.0.1` unless LAN access is intentional, fill exactly one ready `resource_id` per engine, and set each `reference_audio` to a path relative to the private fixture. Keep `rounds` at `10`.

`normal_request_timeout_seconds` is the public, engine-specific allowance for a normal synthesis, including Windows cold model startup. Its deterministic defaults are 120 seconds for GPT-SoVITS, 240 seconds for IndexTTS, and 180 seconds for CosyVoice. An older private fixture that omits this field receives those defaults without modification. An explicit map must contain all and only the three engines, use finite JSON floating-point values no lower than the defaults, and remain at or below the 600-second TTS More public request ceiling. These are acceptance ceilings, not latency targets.

The three configured TTS More services must point to the same ComfyUI instance, use the TTS-Audio-Suite v1 bridge contract, share one GPU resource group, and keep `capacity: 1`.

Set the private checkout and registry locations in the current PowerShell session:

```powershell
$env:TTS_MORE_RELIABILITY_GPT_SOVITS_ROOT = '<gpt-sovits-checkout>'
$env:TTS_MORE_RELIABILITY_INDEXTTS_ROOT = '<indextts-checkout>'
$env:TTS_MORE_RELIABILITY_COSYVOICE_ROOT = '<cosyvoice-checkout>'
$env:TTS_AUDIO_SUITE_RESOURCES = '<private-resources-yaml>'
```

The script derives TTS-Audio-Suite from `custom_nodes/TTS-Audio-Suite` under the supplied ComfyUI root. All six repositories must be Git worktrees. Reference files and the private registry are hashed, but their paths and contents are never written to public JSON evidence.

## Preflight

Use `-PreflightOnly` first. `scripts/run-windows-comfyui-reliability-supervised.ps1` is the only formal entry point. The similarly named inner script is a non-authoritative implementation detail: invoking it directly cannot create `terminal.json`, advance `current-terminal.json`, or certify a run.

The supervisor refuses missing or non-exact paths, occupied ports 8000/8188, PID identity changes, non-loopback fixture endpoints without explicit `-AllowLan`, anything other than the exact three ready resource IDs, non-idle TTS More or ComfyUI queues, incomplete repository/model boundaries, and unexpected validation temp residue.

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File scripts/run-windows-comfyui-reliability-supervised.ps1 `
  -Fixture '<private-fixture-json>' `
  -OutputRoot '<private-evidence-root>' `
  -ComfyUiRoot '<comfyui-checkout>' `
  -ComfyPython '<comfyui-python>' `
  -TtsMoreRoot (Resolve-Path .).Path `
  -PreflightOnly
```

Success performs no synthesis request. It returns zero only after a `preflight/passed` run is frozen, verified, atomically published as current, and verified again through the current pointer. `-AllowLan` is the only way to permit LAN fixture URLs; it also binds the validator-owned services to all interfaces, so use it only on a trusted network with host firewall rules in place.

## Full gate

Run the same command without `-PreflightOnly`. A typical run may take roughly 30–90 minutes depending on model load times. The default 47-case plan has an auditable 8,583-second case-level phase ceiling (143.05 minutes): 6,300 seconds for 30 steady cases, 60 for queued cancellation, 180 for three running cancellations, 630 for their three recoveries, 93 for three intentional timeouts, 630 for their three recoveries, 60 for owned-ComfyUI termination, and 630 for three post-restart syntheses. These eight disjoint groups cover all 47 cases without double-counting. The total excludes the separately bounded restart-readiness and fixed HTTP/host operations. The command exits zero only after all 47 unique cases pass:

- 30/30 steady cases: `gpt-sovits`, `indextts`, `cosyvoice`, repeated ten times in that order;
- queued cancellation, one running cancellation per engine, and a completed recovery after each running cancellation;
- one-second timeout injection per engine and a completed recovery after each timeout;
- validator-owned ComfyUI termination during IndexTTS, followed by completed restart/readiness synthesis for all three engines;
- unique TTS More job IDs for every case and unique ComfyUI prompt/manifest version IDs for every dispatched case; queued cancellation must instead prove both IDs remained absent;
- non-silent WAV proof for every completed case and no WAV proof for expected non-completed cases;
- empty TTS More and ComfyUI queues, present terminal ComfyUI history for ordinary dispatched cases, truthful endpoint-unavailable/TTS-failure evidence for owned ComfyUI termination, runtime release plus `/free`, no runner/temp residue, and GPU memory within 1,024 MiB of the pre-case baseline;
- identical before/after Git HEAD, branch, porcelain hash, private registry hash, and reference hashes.

Normal steady, recovery, and post-restart synthesis requests use the fixture's engine-specific request allowance. After that request window, terminal and queue observation receives one separate window of at most 30 seconds. The intentional timeout request itself remains exactly one second followed by at most 30 seconds of cancellation convergence. Running-cancel and owned-ComfyUI-termination cases must observe and issue their action within the fault case's 30-second request phase, then receive one fresh terminal window of at most 30 seconds measured from action issuance. Queued cancellation shares one 30-second admission phase across blocker admission and target queue admission, then receives one fresh at-most-30-second settlement window measured from target cancellation issuance. ComfyUI restart readiness remains an independent operation bounded at 180 seconds. Exceeding any deadline fails the run; cancellation is never converted into a successful synthesis, and no window is retried or extended indefinitely.

## Immutable evidence and publication

Each supervised attempt receives a fresh unpredictable run identity. The public run key is its 64-character lowercase SHA-256 commitment. The supervisor derives every path; no argument can select an individual artifact, terminal, pointer, log, or temporary path.

```text
<evidence-root>/
  current-terminal.json
  runs/
    <64-lowercase-run-key>/
      terminal.json
      supervisor.json
      run-result.json
      preflight.json
      failure.json                 # failed runs only
      reliability-summary.json    # applicable matrix runs only
      cases/                       # applicable matrix runs only
      audio/                       # completed synthesis proofs only
      logs/                        # hash-only stream commitments
```

Only artifacts applicable to that run may exist. `terminal.json` commits the exact file membership, sizes, and SHA-256 values; extra, missing, renamed, replaced, non-regular, symlink, junction, mount-point, or other reparse members fail closed. `supervisor.json` and `run-result.json` are mandatory. Passing matrix runs retain exactly 47 cases and 39 WAV commitments. `terminal.json` is first-write and is never one of its own commitments.

The supervisor performs publication in this order:

1. Snapshot the exact `current-terminal.json` token before starting the child.
2. Prepare a new empty `runs/<run-key>` and start the inner launcher exactly once through `System.Diagnostics.Process`.
3. Fully drain both child streams into byte-bounded captures, reject overflow or invalid UTF-8 before publication, close the process/stream handles, and commit only size/SHA-256 stream documents—not raw child output.
4. Read the strict inner result, finalize `supervisor.json`, enumerate exact membership, first-write `terminal.json`, and verify the frozen run.
5. Compare-and-swap only `current-terminal.json` using the original snapshot token. A CAS mismatch is never retried with a new token.
6. Verify the new current pointer and its run before returning the child's exact signed Int32 exit code.

Two supervisors starting from the same token keep distinct immutable run directories; only the first CAS succeeds. A crash before terminal or before CAS leaves an unreferenced orphan and preserves the previous current pointer byte-for-byte.

All public schemas forbid unknown fields and implicit coercion, impose collection/string/integer bounds, and use canonical compact sorted UTF-8 JSON without a BOM. Public evidence contains neutral IDs, hashes, timestamps, metrics, and bounded process observations—not checkout/model/reference paths, registry values, resource IDs, commands, credentials, tokens, URI userinfo, environment dumps, or raw exceptions. A current failed case uses the strict versioned partial-observation schema; identifiers are commitments only. If detailed case observation cannot be validated, the original primary failure is retained with a single hash-only secondary commitment.

### Current pointer and legacy reads

New supervised writers never create or update root-level legacy evidence. If `current-terminal.json` is present, it must validate canonically and bind a verified immutable run; an invalid present pointer fails closed and never falls back to legacy evidence. Only when the pointer is absent may the existing strict legacy reader inspect historical root-level evidence in read-only mode. Legacy bytes and mtimes are never changed, moved, deleted, archived, or quarantined.

## Cleanup boundary

The inner launcher owns one exact private temp/recovery set inside its selected run and never writes the terminal or current pointer. Service starts are tracked provisionally before full CIM capture. Cleanup revalidates PID, creation time, executable, command line, parent PID, parent creation time, descendant edges, and exact port ownership; a mismatched or reused PID is preserved. Temp deletion requires the exact run-owned `.p` root, `.o` owner marker, raw run ID, and exact derived runner/ComfyUI temp paths. Private identity removal accepts only that run's exact `.h` and `.c` pair. Model environments, checkouts, configuration, and pre-existing processes are outside the deletion boundary.

On validator failure, the launcher reads failure context only through `launcher_failure_context evaluate-run --output-root <root> --run-key <run-key>`. It does not create a legacy snapshot or infer evidence from mtimes. Run-owned raw service/lifecycle streams use fixed private short names and are converted by the supervisor to bounded hash-only log commitments before exact-membership freeze.

A cleanup failure is current-eligible only when the child has completed and no mutable private recovery members remain in the run. If cleanup is unproven and `.p`, `.o`, `.h`, or `.c` must be preserved for safe recovery, the supervisor fails closed: it does not delete or relocate them, does not first-write `terminal.json`, and does not advance the previous current pointer. That preserved attempt remains an orphan for diagnosis rather than a falsely immutable current run.

## Failure triage

Stop after the first nonzero run. Preserve `reliability-summary.json`, `failure.json`, case evidence, and service logs. Do not rerun blindly or label a health check as synthesis proof. Classify the failure as harness, TTS More, bridge, engine/runtime, boundary drift, or host ownership. Reproduce the smallest failing case, add a deterministic regression test in the owning repository, fix the root cause, rerun deterministic gates, then begin a fresh preflight and full matrix. A harness defect makes that run non-authoritative.
