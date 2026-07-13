# Task 4 Report: Windows Worker Lifecycle Manager

## Scope

Implemented `NodeProbe` and `WindowsLanNodeManager` worker inspection, checkout sync,
deployment, service start, GPU monitoring, bounded stop operations, and evidence
collection in `backend/app/lan_nodes.py`. Added focused lifecycle and security tests in
`backend/tests/test_lan_nodes.py`.

## RED

1. Initial test collection failed with `ModuleNotFoundError: app.lan_nodes`, proving the
   new lifecycle surface was absent before implementation.
2. A restart compatibility test failed because the first implementation rejected an
   existing service PID manifest. The implementation was changed to strictly validate
   the existing manifest and preserve the current worker CLI's append behavior.
3. A strict monitor-manifest test failed because PowerShell `ConvertFrom-Json` alone did
   not prove duplicate-key rejection. A fixed remote Python validator now rejects
   duplicate keys, extra fields, and invalid types before PID identity checks.

## GREEN

- Focused lifecycle and SSH safety suite:
  `.venv/bin/python -m pytest backend/tests/test_lan_nodes.py backend/tests/test_windows_ssh.py -q`
  -> `165 passed`.
- Full backend suite:
  `.venv/bin/python -m pytest backend/tests -q`
  -> `911 passed, 6 skipped`.
- Compilation:
  `.venv/bin/python -m compileall -q backend`
  -> exit 0.

## Security And Interface Notes

- Node names, run IDs, lowercase commits, formal service IDs, formal ports, Windows
  roots, local topology paths, and local evidence directories are validated before use.
- Probe, topology, repo confirmation, service PID manifest, and monitor PID manifest JSON
  reject duplicate keys and nonconforming structures.
- All remote scripts go through `WindowsSshExecutor.run_powershell`; that executor uses
  `powershell.exe -EncodedCommand` and `subprocess` argument arrays with `shell=False`.
- Deployment calls the current `deploy-local-tts.ps1` worker-node interface and requires
  `repo-paths.local.json` to exactly cover the service identities in `repo.lock.json`.
- Start calls the current `start-service-workers.ps1 -PidManifest` interface. Existing
  manifests are accepted only after strict path, module, service, and PID validation.
- Machine GUID and GPU UUID values are salted and hashed. The GPU monitor transforms the
  UUID field in memory and writes only `gpu_uuid_sha256` values to its CSV evidence.
- Evidence collection derives every remote path from validated roots/run IDs and formal
  service IDs; no fixture-provided log path is accepted.
- Monitor stop verifies PID, creation date, executable, and encoded-command identity.
  Service fault injection derives PIDs only from listeners on formal ports and verifies
  the expected worker module and exact port argument before stopping them.

## Concerns

- No live Windows/OpenSSH/CUDA worker was available in this task. PowerShell behavior and
  real NVIDIA output remain integration-test concerns for the later LAN end-to-end run.
- `ruff` and `black` are not installed in the project virtual environment, so those
  optional formatter/linter commands could not be run. Compilation, pytest, and diff
  whitespace checks are the available repository gates used here.

## Review Remediation: High 2 / Medium 3

### RED

1. The expanded review suite initially reported `15 failed, 30 passed`. Failures covered
   reserved/case-aliased Windows run IDs, one-snapshot PID termination, unowned service
   stopping, unbounded monitor output, weak manifest typing, and symlink/digest evidence
   escapes.
2. A focused adjacent-port-token test then failed because separate exact matches for
   `--port` and `9880` did not prove that they were one argument pair.

### GREEN

- Run IDs now require lowercase canonical spelling and reject reserved device basenames,
  reserved names with extensions, trailing dot/space aliases, and case aliases.
- Service starts use a fresh bounded manifest path for every generation. Service stops
  require the same manager's canonical remote root and manifest, consume one strict byte
  snapshot, and bind PID, creation time, canonical executable, project root, exact module,
  and adjacent exact `--port <port>` tokens.
- Monitor launch resolves the canonical PowerShell executable, rejects partial artifacts,
  atomically publishes a strict manifest, cleans the child through its owned `Process`
  object on failure, and reconciles only an exact pre-existing manifest/process after an
  ambiguous retry.
- The monitor child independently enforces a six-hour deadline, 200,000-row limit, and
  64 MiB CSV limit. GPU UUIDs remain salted hashes before persistence.
- Monitor and service termination open an owned process handle, validate a first CIM
  snapshot, validate a second snapshot immediately before `.Kill()`, and reject changes
  in creation time, executable, command/module, or port. No batch bare-PID termination is
  emitted by `lan_nodes.py`.
- Service and monitor manifest validators reject bool/float numeric substitutions,
  duplicate keys, extra fields, oversized files, and invalid exact field types. Consumers
  use the validated snapshot/digest output rather than reopening the mutable pathname.
- Evidence collection rejects symlink components, canonicalizes the output root, stages
  bounded regular non-reparse remote files under a fixed controlled snapshot path, checks
  size and SHA-256 after SCP, and publishes via a same-directory no-follow temporary plus
  directory-fd atomic replace and final containment check.

Review-focused verification:

- `.venv/bin/python -m pytest backend/tests/test_lan_nodes.py backend/tests/test_windows_ssh.py -q`
  -> `182 passed`.
- `.venv/bin/python -m pytest backend/tests -q`
  -> `928 passed, 6 skipped`.
- `.venv/bin/python -m compileall -q backend`
  -> exit 0.

### Remaining Concerns

- The controller has no `pwsh` executable and no live Windows/OpenSSH/CUDA worker was
  available. Generated PowerShell ordering and predicates are covered by tests, including
  the second-snapshot-before-handle-kill requirement, but execution against real CIM,
  reparse points, and NVIDIA output remains part of LAN end-to-end validation.

## Re-review Remediation: High 1 / Medium 2 / Medium 1 Narrowing

### RED

The re-review tests initially reported `6 failed, 48 passed`:

- service-start rollback still referenced the legacy bare-PID cleanup script;
- failed starts did not retain manager pending state or reuse the same manifest on retry;
- monitor launch rollback swallowed `Kill` / `WaitForExit` failures;
- SCP staging was not private and destination-parent replacement was not fd-anchored;
- the remote snapshot checked a pathname and then reopened it without final-handle proof.

### High 1 And Medium 2 Closure

- `start()` no longer references `cleanup-cuda-validation-processes.ps1` or emits
  `Stop-Process`. The strict bounded validator returns the exact process byte snapshot,
  including a digest and the complete topology-matched formal service set.
- Rollback consumes only that snapshot. Each live entry must match PID, creation time,
  canonical executable below the project root, exact worker-module token, adjacent exact
  `--port <port>` tokens, and the formal listener owner. It then opens a `Process` handle,
  performs a second CIM/listener snapshot, and immediately calls `.Kill()` on the retained
  handle. Entries are never validated as a batch and later killed by bare PID.
- A rejected manifest is never passed to a weaker consumer. It is retained and reported
  as a strict rejection.
- Rollback termination failure retains the ownership manifest, stores the same path in
  manager pending state, and reports an explicit rollback error. Retry reuses that path:
  an exact fully live process set is reconciled as success; a partial exact set is rolled
  back handle-by-handle before a fresh start; any ownership mismatch remains fail-closed.
- Monitor launch rollback now tests both `.Kill()` and `WaitForExit(10000)`. Confirmed
  termination is required before artifacts are removed. Failure republishes or retains
  the exact ownership manifest and all artifacts. Retry reconciles an exact live monitor,
  or cleans a strictly identified manifest whose process is confirmed absent.

### Medium 1 Narrowing And Residual

- SCP downloads now target a controller-created random private directory with mode 0700
  and a pre-created 0600 file. The retained staging directory fd and original file inode
  must still match after SCP; replacements are rejected before evidence publication.
- Destination publication starts from a retained evidence-root fd, opens every parent
  component relative to the previous fd with `O_DIRECTORY | O_NOFOLLOW`, and uses
  dirfd-relative `os.replace`. Root identity is rechecked after publication.
- Remote evidence opens the source once, obtains final path and reparse attributes through
  `SafeFileHandle` (`GetFinalPathNameByHandle` / `FileAttributeTagInfo`), and reads that same
  handle into the bounded snapshot.
- Cross-principal staging replacement is blocked by the random 0700 directory. A process
  running as the same controller principal can still replace the SCP pathname after it is
  handed to `scp`; the inode/no-follow checks detect this and prevent final publication,
  but cannot prevent the attempted SCP write from following that same-principal link.
  The race test intentionally demonstrates this pre-detection write. Fully closing it
  requires changing the owned `WindowsSshExecutor.copy_from` interface to an fd/stream
  sink or an equivalent descriptor-preserving transport, outside Task 4 ownership.

### GREEN

- `.venv/bin/python -m pytest backend/tests/test_lan_nodes.py backend/tests/test_windows_ssh.py -q`
  -> `188 passed`.
- `.venv/bin/python -m pytest backend -q`
  -> `934 passed, 6 skipped`.
- `.venv/bin/python -W error::DeprecationWarning -m compileall -q -f backend`
  -> exit 0.

## Second Re-review Medium 1: Incremental Service Manifest Recovery

### RED

- The incremental-manifest test modeled service one succeeding and service two failing,
  leaving one exact owned process in the manifest. The validator-focused run reported
  `1 failed, 1 passed`: the strict validator rejected the valid nonempty topology subset
  before retry reconciliation could reach rollback.
- A separate post-launch recovery test then reported `1 failed`: an incomplete snapshot
  returned after the launcher path did not yet enter handle-bound rollback.

### GREEN

- The start validator is now topology-bound and accepts only a nonempty, unique set of
  formal service identities that is a subset of the selected worker's nonempty, unique
  expected services. Empty, duplicate, unknown, and formal-but-not-expected identities
  remain fail-closed.
- The validated byte snapshot reports a validator-derived `complete` boolean and exact
  expected-service count. Existing-process reconciliation succeeds only when the snapshot
  is complete, every recorded process is exact and live, and both counts match.
- A valid partial snapshot always reaches the existing per-process owned-handle rollback.
  Successful rollback removes the partial manifest and proceeds to a fresh start on the
  pending retry. Rollback failure removes nothing; Python retains the same manifest path
  in pending manager state so the exact process set can be reconciled again.
- A launcher result that is incomplete without an explicit launch exception is treated
  the same way: handle-bound rollback is mandatory, successful cleanup requests a bounded
  retry, and failed cleanup retains ownership state.

Verification:

- `.venv/bin/python -m pytest backend/tests/test_lan_nodes.py -q`
  -> `56 passed`.
- `.venv/bin/python -m pytest backend/tests/test_lan_nodes.py backend/tests/test_windows_ssh.py -q`
  -> `190 passed`.
- `.venv/bin/python -W error::DeprecationWarning -m compileall -q -f backend`
  -> exit 0.

### Remaining Concern

- The controller still has no `pwsh` executable and no live Windows worker was available.
  The embedded validator is executed directly in tests and generated PowerShell control
  flow is checked for complete-only success and rollback ordering; live CIM/listener/
  process-handle execution remains part of LAN end-to-end validation.
