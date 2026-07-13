# Task 2 Report

## RED

- `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q`
  initially failed during collection with `ModuleNotFoundError: app.windows_ssh`.
- The added `UserKnownHostsFile=/` case then failed until directory paths were
  rejected.

## GREEN

- Focused adapter tests: `29 passed`.
- `compileall -q backend/app/windows_ssh.py`: passed.
- Related topology tests: `44 passed`.
- `git diff --check`: passed before staging; staged diff also had no whitespace
  errors.

## Commit

- SHA: `389d591`
- Message: `feat: add pinned Windows OpenSSH adapter`

## Files

- `backend/app/windows_ssh.py`
- `backend/tests/test_windows_ssh.py`

## Concerns

- Command behavior was verified with a fake runner; no live Windows host was
  contacted.
- Resolved targets are cached only after full policy validation, and aliases are
  revalidated before cache use.

## Review Fixes (2026-07-13)

### RED

- Critical policy/DNS/local-command group:
  `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q -k
  'binds_validated or prohibited_dns or mixed_valid or rebinding or
  local_command_and_proxy or forces_proxy'` -> `12 failed, 29 deselected`.
  The failures showed that no injectable resolver/address binding existed.
- Important/Minor hardening group:
  `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q -k
  'malformed_user or missing_known or known_hosts_directory or
  known_hosts_symlink or disguised_null or canonical_known or
  process_exceptions or forces_sftp or absolute_local_destination'` ->
  `10 failed, 4 passed, 46 deselected`. The failures covered missing canonical
  file checks, exception leakage, SFTP enforcement, and relative local paths.
- IPv6 SCP operand group:
  `.venv/bin/python -m pytest
  backend/tests/test_windows_ssh.py::test_scp_brackets_validated_ipv6_remote_operand
  -q` -> `1 failed`; the unbracketed IPv6 address was ambiguous to SCP.
- Strict hostname/symlink-parent group:
  `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q -k
  'unsafe_target_values or below_symlink_directory'` ->
  `6 failed, 9 passed, 52 deselected`.

### GREEN

- `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q` ->
  `67 passed in 0.06s`.
- `.venv/bin/python -m pytest backend/tests/test_lan_topology.py -q` ->
  `44 passed in 0.09s`.
- `.venv/bin/python -m compileall -q backend/app/windows_ssh.py
  backend/tests/test_windows_ssh.py` -> passed.
- `git diff --check` -> passed.
- Local OpenSSH option parse check using `ssh -G -F /dev/null` with every forced
  `-o` option -> exit 0.

### Review Checklist Closure

1. Critical, validated policy binding: closed. The cache was removed and every
   SSH/SCP argv now pins the validated numeric HostName, HostKeyAlias, User,
   BatchMode, IdentitiesOnly, StrictHostKeyChecking, canonical
   UserKnownHostsFile, and disabled global known hosts.
2. Critical, DNS/rebinding: closed. All resolver answers are parsed and checked;
   any loopback, unspecified, multicast, or malformed answer rejects the target.
   Each operation resolves afresh and connects to the selected numeric address.
3. Critical, OpenSSH shell-capable configuration: closed. ProxyCommand,
   ProxyJump, PermitLocalCommand, and LocalCommand are rejected during resolve
   and explicitly forced off in actual SSH/SCP argv.
4. Important, known-host pinning: closed. The path must exist, be a canonical
   regular file, contain no symlink component, and cannot resolve to a null
   device/directory; GlobalKnownHostsFile is forced to `none`.
5. Important, exception redaction: closed. TimeoutExpired and OSError are caught
   and replaced with stable operation/alias messages without command or output.
6. Important, alias injection: closed. Aliases require an alphanumeric first
   character and reject option-like and dot-only names before runner invocation.
7. Important, SCP remote shell: closed. Both transfer directions force SFTP with
   `scp -s`; IPv6 remote operands are bracketed.
8. Minor, user validation: closed. Users use a conservative alphanumeric-first
   ASCII grammar, and malformed `ssh -G` records are rejected.
9. Minor, local path option injection: closed. Upload sources and download
   destinations are expanded and resolved to absolute paths before argv use.

### Concerns

- Tests use fake process/DNS adapters; no live Windows host or transfer was
  contacted.
- DNS answers are intentionally not cached. A single operation is bound to one
  validated numeric address, while a later operation performs fresh validation.
- The known-hosts file is validated immediately before command construction;
  OpenSSH still opens the canonical path itself, so filesystem mutation by a
  separately privileged actor remains outside this adapter's process boundary.
