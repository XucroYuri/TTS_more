# Task 6 Report: Application Loop, Recovery And Evidence Completion

## Scope

Implemented the controller-owned application loop, isolated workstation Playwright
run, shared/distributed listener fault injection, exact service restart support, and
strict final LAN evidence validation. The implementation adapts the current Task 5
ownership model: only manager-recorded worker generations and the exact controller
`Popen` child can be stopped, while evidence collection and cleanup run on every
applicable failure path.

## TDD RED

1. Initial collection failed because `control_plane` and
   `LanEvidenceManifest` did not exist.
2. Node-manager RED reported three failures for missing selected-service restart and
   the absence of a native-Windows evidence publication path.
3. Application-loop RED showed that Playwright, fault recovery, and all-path evidence
   collection were not connected to the Task 5 orchestrator.
4. A control-plane race test failed when a process could satisfy the health probe and
   exit before ownership was confirmed. The context now verifies that its exact child
   remains live after readiness.
5. Remote evidence freshness RED failed because SCP publication time hid stale source
   logs. The strict remote snapshot now carries source modification time and the local
   atomic publisher preserves it for the final gate.
6. PR #10 portability tests exposed assumptions about `/usr/sbin/ioreg`, POSIX
   executable mode bits, and directory-file-descriptor support. Tests now inject the
   trusted controller executable, launcher assertions are platform-aware, and native
   Windows uses a contained atomic evidence publisher with replacement-attack tests.
7. Task 7 integration review exposed the stale short Playwright mode names. Coverage
   now requires `options.mode.value`, producing only `lan-shared` or
   `lan-distributed`.

## Controller Application Ownership

- Port 8000 must be bindable on loopback before launch. A pre-existing listener is a
  hard failure and is never inspected, adopted, or killed.
- Uvicorn starts with a fixed argument list, trusted Python executable, repository
  working directory, `shell=False`, and controlled run-local stdout/stderr files.
- Readiness is accepted only while the exact returned `Popen` child remains live.
  `finally` calls terminate/wait and, only after timeout, kill/wait on that same child.
- API credentials remain in the environment. Exceptions, public evidence, and
  subprocess arguments do not include the token.

## Playwright Isolation

The existing CUDA workstation Playwright entry point runs with a unique project ID,
run-local artifact directory, and run-local JUnit path. Port 5173 must also be free,
so an unrelated frontend process cannot be reused or terminated. The runner receives
the formal LAN mode and credentials through its environment. stdout and stderr remain
bounded controlled raw evidence instead of being copied into public summaries.

## Fault Recovery

- Shared mode first stops only the GPT listener, proves the application and the other
  services remain ready, restarts only that listener, and reruns the affected core
  case. It then stops all three exact listeners, measures a common maximum 15-second
  degradation window, proves application health, restarts the exact set, and reruns
  the complete core suite under `recovery/`.
- Distributed mode selects the configured public node alias or the first sorted
  worker, stops only its assigned listener, proves the other services and application
  remain ready, restarts only that owned service, and reruns the complete core suite.
- Partial failures write a fail-closed fault report and attempt restoration of only
  the listeners stopped by injection. Final Task 5 cleanup still reconciles pending
  and completed owned generations and never derives a process to kill from listener
  discovery.

## Strict Evidence

`LanEvidenceManifest` is a strict schema with formal service IDs, public topology
aliases, relative paths, deployment identity hashes, fault/Playwright/recovery
references, and an explicit `human_review_status: pending`. Merely finding the human
listening template cannot be interpreted as approval.

The manifest writer uses confined same-directory atomic replacement. Final validation
rejects traversal, symlinks/reparse points, non-regular files, empty or oversized
files, stale evidence, malformed reports, failed JUnit/core/fault results, missing GPU
CSV samples, and incomplete per-service logs. Raw host, IP, user, absolute path, UUID,
and token fields are not part of the public schema. GPU CSV and each service's worker
log are collected for every policy node even when an earlier orchestration stage
fails, followed by strict owned cleanup.

The POSIX evidence path retains directory-descriptor and no-follow guarantees. Native
Windows uses a unique same-volume staging directory, reparse checks, identity checks,
bounded copy hashing, destination revalidation, and atomic `os.replace`; tests inject
staging and destination-parent replacement attacks.

## Verification

- Tasks 1-6 focused suite: `584 passed, 3 skipped`.
- Owned Task 6 suite is included in that green run.
- Full backend suite: `1055 passed, 6 skipped`.
- `python -W error::DeprecationWarning -m compileall -q -f backend
  scripts/run-lan-validation.py`: passed.
- `git diff --check`: passed.

## Commit Coordination

The six owned implementation and test paths are isolated in `9f492f6`. The commit was
created with an explicit path-limited commit after verifying the before/after HEAD and
diff. No Task 7 or storage file was edited, staged, reset, amended, or included. This
report and progress update are committed separately as documentation.

## Remaining Integration Boundary

No live Windows/OpenSSH/CUDA LAN was available locally. Real GPU identity, model
loading, timing, audio/ASR quality, listener recovery, and human listening approval
remain fail-closed hardware-run gates rather than locally claimed results.
