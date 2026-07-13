# Task 5 Report: Cross-Platform LAN CUDA Orchestrator

## Scope

Implemented the macOS controller orchestration module and thin Python, POSIX shell,
and PowerShell launchers. The controller now enforces an explicit clean or release
deployment, binds controller/SSH/DNS/node identities, runs Tasks 1-4 in a fixed order,
writes schema-v2 preflight evidence, invokes the in-process CUDA core, and performs
owned monitor/service cleanup and evidence collection on both success and failure.

## TDD RED

1. Initial collection failed with `ModuleNotFoundError: app.lan_orchestration` after
   adding CLI, deployment gate, path, controller identity, DNS, and probe tests.
2. The state-machine batch failed import because `LanOrchestrator` and the helper
   surface did not exist. After the first implementation, the focused run exposed an
   over-restricted token contract (`1 failed, 23 passed`).
3. Launcher/path RED reported `2 failed, 24 passed`: `Path.resolve()` followed and
   hid a fixture symlink, and all three launchers were absent.
4. Security review RED reported `3 failed, 25 passed`: SSH addresses were not bound
   to topology DNS, probe digest formats were not closed, and network validation ran
   before SSH resolution.
5. Current CUDA API review RED reported `2 failed, 26 passed`: schema-v2 stored the
   salted controller identity directly even though the current core hashes the
   supplied identity provider, and the core helper had no in-memory identity input.
6. Final fail-closed RED reported `5 failed, 28 passed`: direct non-Path option values
   raised `AttributeError`, and duplicate probe nodes were silently overwritten.

## Current API Adaptation

- `WindowsLanNodeManager.deploy()` remains the only deployment implementation. It
  copies and hashes the topology, requires `repo-paths.local.json` to confirm the full
  `repo.lock.json` repository set, invokes trusted worker deployment tooling, and
  receives `clean=True` only for clean certification.
- Controller Git and IORegistry calls use fixed `/usr/bin/git` and `/usr/sbin/ioreg`
  argument arrays with `shell=False`, bounded time/output, and generic errors. Git
  confirms the exact repository root, commit, and clean tracked/untracked state.
- SSH aliases are fully resolved before mutation. Their pinned target addresses must
  belong to the corresponding topology DNS answer set, and every controller/worker
  address, machine hash, host-key hash, and distributed GPU hash must be distinct.
- The raw macOS platform UUID never leaves `controller_id_sha256()`. The salted digest
  is the in-memory CUDA controller identity; schema-v2 stores only SHA-256 of that
  digest, matching the current `CUDAValidationRunner` identity-provider contract.
- The one-time orchestration token is passed only in memory, represented in preflight
  only by SHA-256, omitted from subprocess arguments/logs/blocker text, and removed
  after monitor stop, worker evidence collection, and owned service cleanup.
- The launchers contain no deployment, CUDA core, cleanup, or legacy Windows gate
  logic. `scripts/run-cuda-validation.ps1` was not modified.

## Required Order And Failure Behavior

The tested stage order is controller commit/identity, SSH plus DNS identity gate,
all-node checkout sync, all-node deploy (including topology copy/repo confirmation),
all-node GPU monitor start, all-node worker start, node inspection, app-only external
service render/readiness, probe identity gate, schema-v2 preflight, CUDA core, monitor
stop, evidence collection, owned service stop, and token removal.

Failures return nonzero after all applicable owned cleanup attempts. Blocker evidence
uses the existing CUDA evidence writer with a bounded generic error type/count; raw
exception text is intentionally excluded so tokens, hosts, and absolute paths cannot
be copied into logs or evidence.

## GREEN Verification

- Task 5 focused suite: `34 passed`.
- Task 5 plus Tasks 1-4 integration suite: `397 passed, 2 skipped`.
- Full backend suite after the final code cleanup: `970 passed, 6 skipped`.
- `python -W error::DeprecationWarning -m compileall -q -f backend
  scripts/run-lan-validation.py`: passed.
- Python launcher `--help`, POSIX launcher `--help`, and `bash -n` checks: passed.
- New-file whitespace checks and line-width scan: passed.

## Remaining Integration Boundary

No live Windows/OpenSSH/CUDA nodes were available. The current machine also has no
`pwsh`, so the new PowerShell launcher was checked statically as a thin argument
forwarder; its delegated worker PowerShell behavior remains covered by the existing
Task 4 tests and must be exercised by the real LAN validation run.
