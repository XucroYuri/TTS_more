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
