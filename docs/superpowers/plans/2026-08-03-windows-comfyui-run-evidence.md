# Windows ComfyUI + TTS-Audio-Suite Immutable Run Evidence Plan

> Execute with `superpowers:subagent-driven-development`. Every implementation task uses behavior-first RED/GREEN tests and an independent task review.

## Goal

Make TTS More + official ComfyUI + the forked TTS-Audio-Suite highly reliable on Windows for GPT-SoVITS, IndexTTS, and CosyVoice, with immutable per-run evidence, an atomic current pointer, real preflight/matrix proof, plugin-first delivery, and exact GitHub/Gitee synchronization.

## Global constraints

- TTS More starts from `72fdd803ad1bf62b340d2354660c217983aeaff7`, on `dev-xu/windows-comfyui-run-evidence`, then integrates the execution-time `github/master`.
- TTS-Audio-Suite starts from `ce443482cdddd883914f589705196415bb98e332` on an integration branch, then audits and selectively integrates current fork/upstream work.
- Do not merge the Fix11/Fix12 shared-root retirement, deletion, quarantine, rollback, or `rmtree` experiments.
- Official ComfyUI and official GPT-SoVITS, IndexTTS, and CosyVoice source must not be modified.
- TTS More and the forked TTS-Audio-Suite are the only product repositories that may change.
- `resource_id` is required. One GPU/resource group defaults to `capacity: 1`.
- Hosted CI runs deterministic tests only; real CUDA validation remains explicit opt-in.
- Private model paths, resource registry values, commands, tokens, and fixture contents never enter public pointer/terminal JSON or committed reports.
- Every product fix found by live validation requires a deterministic behavior RED before implementation.
- A live stage runs once. On the first nonzero result, preserve its run, make that failure current where the supervisor is capable of committing it, and stop without blind rerun.
- Existing legacy evidence bytes and mtimes are never changed, moved, deleted, archived, or quarantined.

## Public contracts

### Layout

```text
<output-root>/
  current-terminal.json
  .current-terminal.lock
  runs/
    <64-lowercase-run-key>/
      terminal.json
      supervisor.json
      run-result.json
      preflight.json
      failure.json
      reliability-summary.json
      cases/
      audio/
      logs/
```

Only files applicable to a run's mode and outcome are present. A completed run has exact membership; any extra, missing, replaced, renamed, non-regular, or reparse member fails closed.

### Current pointer

`current-terminal.json` has exactly these fields:

- `schema_version=1`
- `kind="reliability-current-terminal"`
- `run_key`
- `mode="preflight"|"matrix"`
- `outcome="passed"|"failed"`
- `terminal_size_bytes`
- `terminal_sha256`
- `previous_pointer_sha256=null|<sha256>`

The pointer carries no path. Readers derive `runs/<run-key>/terminal.json`. `run_key` is exactly 64 lowercase hexadecimal characters.

### Terminal

`terminal.json` strictly records:

- mode, outcome, failure source, and evidence completeness;
- signed Int32 launcher and validator exit codes, with fail-closed nullable validator rules;
- cleanup status;
- preflight, failure, summary, and case commitments;
- complete, uniquely named, ordered commitments for every remaining lifecycle, log, and audio file in the run.

Every committed artifact is first-write. A same-run same-path replay is allowed only when bytes are identical; different bytes are a conflict and never overwrite the original.

### CAS and legacy behavior

- The supervisor snapshots the pointer token before starting the launcher.
- Under a cross-process OS advisory lock, commit compares the expected token and atomically replaces only `current-terminal.json`.
- Two formal writers from the same token: first succeeds, second exits nonzero; both run directories remain.
- A crash before terminal or pointer leaves an unreferenced orphan run and preserves the previous current pointer.
- Pointer present but invalid: fail closed and do not read legacy evidence.
- Pointer absent: allow the existing strict legacy root reader in read-only mode only.
- New writers never create a legacy baseline and never mutate legacy evidence.

## Task 1: Establish the clean baselines and audit all remote capabilities

- Verify the isolated TTS More and plugin branches, exact bases, clean status, and current remotes.
- Audit every remote branch in both repositories after fresh fetches.
- Integrate only behavior-backed changes directly related to GPT-SoVITS, IndexTTS, CosyVoice, Bridge/API, Windows runner, FFmpeg/toolchain, or isolated runtime.
- Prefer bounded cherry-picks or reimplementation of a single behavior. Do not merge experimental branches wholesale.
- Record each relevant and unrelated branch with integrated, upstream-covered, deferred, or rejected reasoning. Explicitly cover OmniVoice, Echo, Higgs, Dots, Granite, TADA, and other discovered experimental engines.
- Commit this plan, a design supplement, and the remote branch audit report.

## Task 2: Implement the immutable evidence store

- Create a focused storage module for safe paths, bounded canonical reads, strict schemas, first-write artifacts, exact membership, SHA-256 verification, OS advisory locking, pointer snapshot, and pointer CAS.
- Add a separate evidence CLI with `snapshot-current`, `commit-run`, `verify-current`, and `verify-run`.
- Behavior tests must first fail for both pointer crash boundaries, CAS first-writer-wins, same-run conflict, pointer/terminal/artifact tampering, extra/missing/replaced/reparse members, invalid schemas, privacy leakage, and real Windows junction escape with outside sentinel preservation.
- Do not implement any legacy cleanup or quarantine.

## Task 3: Migrate validator and launcher failure context to run-key ownership

- Route all preflight, summary, case, audio, failure, run-result, and validator logs into the current run directory.
- Construct strict results for preflight success, matrix success, and failure; retain the formal 47-case summary schema.
- Add `launcher_failure_context` APIs and CLI behavior that read one explicit run-key and never infer the active case from mtime or a shared root.
- Keep root baseline/evaluate only for pointer-absent legacy audit.
- Pointer present and invalid must prohibit legacy fallback.
- Behavior tests cover contradictory legacy root, pointer-absent fallback, invalid-pointer no-fallback, wrong run-key isolation, exact 47 case IDs/order/timeouts/recovery, and valid non-silent WAV commitments.

## Task 4: Productize the formal Windows supervisor

- Add `scripts/run-windows-comfyui-reliability-supervised.ps1` as the only documented formal entry.
- Add a supervised `-RunId` to the inner launcher. The inner launcher writes only its run directory, starts once, completes cleanup and `run-result.json`, closes logs, and never commits current.
- Use `System.Diagnostics.Process` to prove one child start and precise signed Int32 exit propagation. Exit 0 returns 0; exit 7 returns 7; null, missing, or invalid exit fails closed.
- After the child is fully done, the supervisor writes its result, freezes exact run membership, first-writes terminal, and performs CAS.
- Launcher failure and cleanup failure may commit `outcome=failed`; supervisor crash does not advance the pointer.
- Behavior tests cover preflight success, matrix success, validator failure, launcher crash, cleanup failure, numeric exits, null/missing exits, and single launch.

## Task 5: Run independent review and the complete deterministic gate

- Put new evidence behavior tests in `backend/tests/test_comfyui_reliability_evidence.py` instead of further inflating the original validator test file.
- Independently review schema closure, privacy, Windows path/reparse handling, concurrency, and source boundaries.
- Run plugin unit tests twice consecutively and Python 3.12 `pip check`.
- Run TTS More focused reliability suites, full backend, frontend tests/build, governance, queue/process tests, compile, PowerShell 5.1 parse, formal supervisor tests, and `git diff --check`.
- Only the historically exact seven Windows host-capability failures with unchanged causes may be separately classified; any new node or changed cause stops the run.
- A separate reviewer must return READY for TTS More, plugin, official ComfyUI, all three model checkouts, private input boundaries, and runtime boundaries.

## Task 6: Perform the one-shot Windows behavior validation

- Use the existing private fixture and evidence root for one supervised `-PreflightOnly` run.
- Require formal numeric exit 0, current pointer to `preflight/passed`, complete schema/hash/membership verification, unchanged legacy bytes/mtimes, and zero owned port/process/temp residue.
- After an independent read-only READY review, run the full matrix once.
- Matrix contains exactly 47 cases: 30 steady, 8 fault, and 9 recovery. All 39 completed cases require valid non-silent WAV evidence; all 8 fault cases require the exact expected terminal outcome.
- Any nonzero result stops immediately. A failed run must be the current failure and must not be blindly rerun.
- Success atomically changes current to `matrix/passed` while preserving the preflight run and every legacy artifact.

## Task 7: Complete PR, merge, synchronization, and cleanup

- Submit the plugin PR first. Require fork/upstream integration, Windows/Linux CI, and target-engine behavior tests.
- After merge, point the ComfyUI Junction to merged plugin main and run one real GPT-SoVITS, IndexTTS, and CosyVoice smoke. If the merge tree differs materially from the validated tree, rerun deterministic gates, preflight, and matrix.
- Submit the TTS More PR, require all GitHub checks, and merge to `master`.
- Verify local `master`, `github/master`, and Gitee `origin/master` independently; fast-forward GitHub master to Gitee and require all three SHAs equal.
- Privately preserve patch/hash evidence for failed Fix11/Fix12 worktrees, then remove only exact, task-owned, clean/authorized worktrees. Preserve models, private configuration, runtimes, and unrelated dirt.
- Final report binds tests, run keys, pointer/terminal hashes, real audio, fault/recovery proof, PRs, CI, remote SHAs, and cleanup evidence. Missing evidence means the goal remains incomplete.

## Required behavior-test inventory

- Two crash boundaries before pointer, old current unchanged.
- CAS race from one token, first writer only.
- Pointer, terminal, summary, case, audio, and log extra/missing/replaced/reparse drift.
- Same run-key conflict never overwrites existing bytes.
- Contradictory legacy ignored when pointer exists; legacy read only when pointer is absent; invalid pointer forbids fallback.
- Real Windows junction outside the root is rejected and outside sentinel remains.
- Five terminal classes: preflight success, matrix success, validator failure, launcher crash, cleanup failure.
- Supervisor exit 0, exit 7, null, missing, and one child start.
- Exact 47-case IDs, order, timeout/recovery semantics, and WAV evidence.
