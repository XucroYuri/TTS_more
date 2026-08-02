# Windows ComfyUI reliability validator

This validator is an opt-in, single-GPU acceptance gate for TTS More with ComfyUI and TTS-Audio-Suite. It runs 30 steady syntheses in round-major order, the cancellation/timeout/termination matrix, recovery syntheses, runtime release, queue convergence, GPU recovery, runner cleanup, and final repository/model boundary comparison.

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

Use `-PreflightOnly` first. The wrapper refuses missing or non-exact paths, occupied ports 8000/8188, PID identity changes, non-loopback fixture endpoints without explicit `-AllowLan`, anything other than the exact three ready resource IDs, non-idle TTS More or ComfyUI queues, incomplete repository/model boundaries, and unexpected validation temp residue.

```powershell
powershell.exe -NoProfile -File scripts/run-windows-comfyui-reliability.ps1 `
  -Fixture '<private-fixture-json>' `
  -OutputRoot '<private-evidence-root>' `
  -ComfyUiRoot '<comfyui-checkout>' `
  -ComfyPython '<comfyui-python>' `
  -TtsMoreRoot (Resolve-Path .).Path `
  -PreflightOnly
```

Success writes `preflight.json`. It performs no synthesis request. `-AllowLan` is the only way to permit LAN fixture URLs; it also binds the validator-owned services to all interfaces, so use it only on a trusted network with host firewall rules in place.

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

## Evidence and cleanup

The evidence root contains `reliability-summary.json`, one public JSON document under `cases/` for each case, generated audio retained by the normal TTS More project output, and `failure.json` when the gate fails. Public evidence contains neutral IDs, hashes, timestamps, metrics, and executable basenames—not source paths, model paths, registry values, command lines, credentials, or tokens. A current failed case is explicitly `schema_version: 2` and must carry a non-null strict partial observation: request/service/resource/job/prompt/version values are SHA-256 commitments only; event times, poll count, bounded terminal/control status, queue commitment, and any already-observed WAV hash/size are retained. If detailed observation validation or publication fails, the validator retries one versioned, schema-valid minimal observation with only a hash of the secondary evidence error; the original case failure code and stage remain primary. The formal reader rejects JSON larger than 4 MiB before parsing and reports typed validation locations/codes without retaining raw invalid input in exception renderings. It retains a separate versionless legacy branch only for the exact historical field shape; legacy documents cannot carry current-only fields or bypass current bounds. Both branches accept only canonical UTC `Z` timestamps, ordered lifecycles, strict bounded scalar/collection values, and no unknown fields.

The wrapper creates a private host manifest and one run-owned temp base beneath the output root. TTS More inherits an owned system temp directory through child-only `TEMP`/`TMP` values, while ComfyUI receives an owned `--temp-directory` base; the manifest monitors the actual runner root and ComfyUI's resulting `<base>\temp` root. The wrapper restores its own environment immediately after each child launch.

Every `Start-Process` result is tracked provisionally before CIM identity capture. The wrapper atomically persists and revalidates the run-ID-bound companion state before waiting for either port or proceeding to later operations. If capture or persistence fails, it stops the just-started PID only when its executable, creation window, parent PID, and parent creation time still match. Python writes evidence before wrapper cleanup. In `finally`, cleanup re-reads the companion state, compares PID, creation time, executable, command line, parent PID, and parent creation time, and stops only matching validator-owned processes and descendants. A mismatched or reused PID is preserved with a warning. Recursive deletion is limited to the resolved temp base under the exact output root and requires its matching owner marker. Public evidence is preserved; model environments, checkouts, user configuration, and pre-existing processes are never deleted.

When the formal Python validator returns nonzero, the launcher first atomically writes `launcher-failure-lifecycle-<run-sha256>.json` before the first process stop. Its strict version-1 schema contains only the failed primary code/stage, hashed run/case and ownership commitments, bounded role/PID/UTC parent-edge observations, and cleanup transaction state; executable paths, command lines, private IDs, model/resource values, headers, tokens, and raw diagnostics are never published. Keys are case-sensitive, JSON collections must remain arrays after round-trip, and cleanup flags, timestamps, diagnostics, and raw dispositions must describe one consistent `snapshot-written`, `cleanup-unproven`, or `cleanup-proven` state. Exact forest stop and a non-destructive owner/temp eligibility check then run. Raw temp/owner/control/manifest deletion is authorized only after a second atomic lifecycle write records `cleanup-proven` and `removal-committed`; this is a transaction commitment, not a premature claim that deletion finished. Either lifecycle write failure or unproved cleanup preserves all still-existing raw recovery records while process convergence remains attempted. A later precise-deletion failure is hash-only secondary evidence and preserves the original validator failure. Actual post-commit absence remains a separate host/evidence audit. Successful runs publish no failure lifecycle and retain the normal exact cleanup path.

The launcher snapshots the existing `failure.json`, `reliability-summary.json`, and case-file stamps immediately before invoking the formal validator. Failure context is accepted only when the validator rewrites the artifact inside the current invocation window. Optional case context additionally requires a current failed summary whose embedded case is exactly equal to the current case artifact and whose case timestamps fall inside that invocation. Unchanged stale markers, stale cases, malformed summaries, and mismatched case artifacts therefore fall back to the neutral launcher classification or an omitted case commitment instead of being attributed to the new run.

## Failure triage

Stop after the first nonzero run. Preserve `reliability-summary.json`, `failure.json`, case evidence, and service logs. Do not rerun blindly or label a health check as synthesis proof. Classify the failure as harness, TTS More, bridge, engine/runtime, boundary drift, or host ownership. Reproduce the smallest failing case, add a deterministic regression test in the owning repository, fix the root cause, rerun deterministic gates, then begin a fresh preflight and full matrix. A harness defect makes that run non-authoritative.
