# Windows ComfyUI immutable-run evidence: design supplement

## Purpose

This supplement tightens the implementation boundaries of the approved
immutable-run plan.  It does not replace that plan and does not authorize any
legacy evidence deletion, quarantine, migration, or source modification in
official ComfyUI or the official GPT-SoVITS, IndexTTS, and CosyVoice projects.

## Repository and runtime ownership

| Owner | May change | Must not own |
|---|---|---|
| TTS More | evidence schemas, readers/writers, supervisor, validator, tests, public redacted reports | model checkout code, plugin internals, machine-local registry values |
| Forked TTS-Audio-Suite | API Bridge, target-engine adapters, Windows/isolated runner behavior, deterministic plugin tests | TTS More evidence pointer, official engine source, private model data |
| Official ComfyUI | runtime host only | product patches for this work |
| Official TTS checkouts | inference libraries/assets only | product patches for this work |

The plugin resolves a required `resource_id` through its machine-local registry.
TTS More treats `resource_id` as an opaque routing identifier.  Public evidence
may commit a digest of it when needed, but must never serialize the value or the
registry path.  A single GPU/resource group has `capacity=1`.

## Immutable run state machine

1. The supervisor snapshots the current-pointer token before launching work.
2. A unique 64-lowercase-hex run key names one new directory under `runs/`.
3. The launcher and validator first-write only artifacts owned by that run.
4. Cleanup completes and all owned streams are closed before terminal freeze.
5. The supervisor verifies exact membership and writes `terminal.json` once.
6. Under the OS advisory lock, pointer CAS compares the original token and
   atomically replaces only `current-terminal.json`.
7. A CAS loser returns nonzero and preserves its complete run directory.

No intermediate state is current.  A crash before terminal or pointer commit
leaves an unreferenced run and the previous pointer unchanged.  A formal
launcher/validator/cleanup failure may become current only after a complete,
strict failed terminal is committed.

## File-system safety rules

- Every run-relative path is derived from a validated run key and a fixed
  schema member; callers cannot provide arbitrary output paths.
- Reads are bounded before decoding, schemas reject unknown fields, and hashes
  cover the exact committed bytes.
- First-write means create-new semantics.  Repeating a same-run write succeeds
  only when the existing regular file has identical bytes.
- Exact membership rejects extra, missing, renamed, replaced, non-regular, or
  reparse members.  Windows junction/symlink escape is checked against the
  resolved root before any read or write.
- The current pointer never contains a filesystem path.  Readers derive the
  terminal location from the validated run key.
- Legacy root evidence is read-only and only visible when the pointer is absent.
  An invalid present pointer fails closed and forbids legacy fallback.

## Concurrency and durability

The pointer token is the SHA-256 of the complete prior pointer bytes, or an
explicit absent token.  Pointer comparison and atomic replace occur while the
same cross-process OS lock is held.  File flush and directory-entry replacement
must finish before success is reported.  The lock protects only current-pointer
publication; immutable run files never depend on a shared mutable baseline.

The design intentionally allows orphan runs: removing or reclassifying them is
outside this work.  It also preserves every legacy byte and mtime.  Fix11/Fix12
shared-root removal, quarantine, rollback, and recursive-deletion experiments
are explicitly excluded.

## Failure and privacy closure

Terminal outcomes are `passed` or `failed`; missing, null, out-of-range, or
non-integer exit information fails closed under the schema rules.  A terminal
commits the applicable run artifacts plus every remaining lifecycle, log, and
audio file.  Failure details are bounded and sanitized before public evidence is
written.

The following never appear in pointer/terminal/public report JSON:

- resource identifiers and registry locations;
- official checkout, model, reference-audio, or fixture paths;
- tokens, credentials, full private commands, or environment dumps;
- unbounded stderr/stdout or raw exception objects.

Tests use generated placeholder identifiers and temporary fixtures.  Committed
reports record only repository refs, public commit IDs, test names/counts, and
sanitized outcomes.

## Test separation

Deterministic hosted-CI tests cover schemas, first-write conflict, tampering,
exact membership, pointer CAS, crash boundaries, reparse escape, supervisor exit
propagation, one-child launch, exact matrix contracts, and legacy fallback
rules.  Real CUDA checks remain an explicit Windows opt-in and are never inferred
from `/health` alone.  A real proof requires the plugin resource to load, a
synthesis request to complete, a valid non-silent WAV, and the corresponding
ComfyUI/frontend history.

Any product defect discovered by live validation starts a new behavior test that
is observed RED before implementation.  Task 1 performs no speculative runtime
rewrite: remote capabilities are integrated only when behavior-backed and not
already covered by the selected fork/upstream baseline.

## Remote-integration rule

For each fetched remote branch, record the exact SHA and one of:

- **integrated**: its commit is an ancestor or a bounded behavior patch was
  selected;
- **upstream-covered**: the behavior already exists with commit/test evidence;
- **deferred**: plausibly useful but requires a separate behavior RED/GREEN or
  architectural decision;
- **rejected**: unrelated, experimental, failed, unsafe, or incompatible with
  the product boundary.

Patch equivalence is evidence only when `git cherry` and the current behavior
tests agree.  Branch names or commit messages alone are never treated as proof.

## Task 1 baseline conclusion

The selected TTS More base contains both fetched deployment baselines and the
formal Windows reliability contract.  The selected plugin base contains fork
main, the Bridge/API integration, isolated-runtime history, and patch-equivalent
FFmpeg/toolchain fixes.  The only newer upstream-main behavior is already
implemented and regression-tested in the fork, so replaying the release commit
would provide no behavior gain and would regress the fork-specific installer
profile.  The detailed ref-by-ref evidence is in the companion remote capability
audit.
