# Windows ComfyUI reliability validator

This validator is an opt-in, single-GPU acceptance gate for TTS More with ComfyUI and TTS-Audio-Suite. It runs 30 steady syntheses in round-major order, the cancellation/timeout/termination matrix, recovery syntheses, runtime release, queue convergence, GPU recovery, runner cleanup, and final repository/model boundary comparison.

It does not run in CI and does not modify the official GPT-SoVITS, IndexTTS, CosyVoice, or ComfyUI checkouts. A pass is not an eight-hour soak, throughput benchmark, subjective audio-quality certification, or multi-GPU certification.

## Private inputs

Copy `deployment/tts-repos/windows-reliability-fixture.example.json` to the ignored `data/local` area. Keep both endpoints on `127.0.0.1` unless LAN access is intentional, fill exactly one ready `resource_id` per engine, and set each `reference_audio` to a path relative to the private fixture. Keep `rounds` at `10`.

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

Run the same command without `-PreflightOnly`. Allow roughly 30–90 minutes depending on model load times. The command exits zero only after all 47 unique cases pass:

- 30/30 steady cases: `gpt-sovits`, `indextts`, `cosyvoice`, repeated ten times in that order;
- queued cancellation, one running cancellation per engine, and a completed recovery after each running cancellation;
- one-second timeout injection per engine and a completed recovery after each timeout;
- validator-owned ComfyUI termination during IndexTTS, followed by completed restart/readiness synthesis for all three engines;
- unique TTS More job IDs for every case and unique ComfyUI prompt/manifest version IDs for every dispatched case; queued cancellation must instead prove both IDs remained absent;
- non-silent WAV proof for every completed case and no WAV proof for expected non-completed cases;
- empty TTS More and ComfyUI queues, present terminal ComfyUI history for ordinary dispatched cases, truthful endpoint-unavailable/TTS-failure evidence for owned ComfyUI termination, runtime release plus `/free`, no runner/temp residue, and GPU memory within 1,024 MiB of the pre-case baseline;
- identical before/after Git HEAD, branch, porcelain hash, private registry hash, and reference hashes.

Cancellation and ordinary case cleanup must converge within 30 seconds. Restart/readiness and recovery cases have 180 seconds. The timeout request itself uses one second. Exceeding a deadline fails the run; it is never converted into a pass.

## Evidence and cleanup

The evidence root contains `reliability-summary.json`, one public JSON document under `cases/` for each case, generated audio retained by the normal TTS More project output, and `failure.json` when the gate fails. Public evidence contains neutral IDs, hashes, timestamps, metrics, and executable basenames—not source paths, model paths, registry values, command lines, credentials, or tokens.

The wrapper creates a private host manifest and one run-owned temp base beneath the output root. TTS More inherits an owned system temp directory through child-only `TEMP`/`TMP` values, while ComfyUI receives an owned `--temp-directory` base; the manifest monitors the actual runner root and ComfyUI's resulting `<base>\temp` root. The wrapper restores its own environment immediately after each child launch.

Every `Start-Process` result is tracked provisionally before CIM identity capture. The wrapper atomically persists and revalidates the run-ID-bound companion state before waiting for either port or proceeding to later operations. If capture or persistence fails, it stops the just-started PID only when its executable, creation window, parent PID, and parent creation time still match. Python writes evidence before wrapper cleanup. In `finally`, cleanup re-reads the companion state, compares PID, creation time, executable, command line, parent PID, and parent creation time, and stops only matching validator-owned processes and descendants. A mismatched or reused PID is preserved with a warning. Recursive deletion is limited to the resolved temp base under the exact output root and requires its matching owner marker. Public evidence is preserved; model environments, checkouts, user configuration, and pre-existing processes are never deleted.

## Failure triage

Stop after the first nonzero run. Preserve `reliability-summary.json`, `failure.json`, case evidence, and service logs. Do not rerun blindly or label a health check as synthesis proof. Classify the failure as harness, TTS More, bridge, engine/runtime, boundary drift, or host ownership. Reproduce the smallest failing case, add a deterministic regression test in the owning repository, fix the root cause, rerun deterministic gates, then begin a fresh preflight and full matrix. A harness defect makes that run non-authoritative.
