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

## Second Review Fixes (2026-07-13)

### RED

- Config graph/TOCTOU group:
  `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q -k
  'match_exec or never_reuse_mutable'` -> `5 failed, 67 deselected`. Direct and
  recursively included `Match exec` reached the runner, and `ssh -G` received
  the mutable original config path.
- Multiplexing/host-key/authentication group:
  `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q -k
  'multiplexing or alternative_host_key or sharing_and_alternative or
  authentication_semantics'` -> `10 failed, 72 deselected`.
- `Match exec=...` syntax variant:
  `.venv/bin/python -m pytest
  backend/tests/test_windows_ssh.py::test_match_exec_in_root_config_is_rejected_before_runner
  -q` -> `1 failed, 3 passed` before criterion normalization was expanded.
- Relative Include semantics:
  `.venv/bin/python -m pytest
  backend/tests/test_windows_ssh.py::test_relative_include_uses_user_ssh_directory_like_openssh
  -q` -> `1 failed`; the parser incorrectly used the containing directory
  instead of OpenSSH's `~/.ssh` base.
- Authentication path validation:
  `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q -k
  'unresolved_or_missing_auth_files'` -> `3 failed, 88 deselected` for residual
  percent tokens and missing certificate files.

### GREEN

- `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q` ->
  `91 passed in 0.14s`.
- `.venv/bin/python -m pytest backend/tests/test_lan_topology.py -q` ->
  `44 passed in 0.08s`.
- `.venv/bin/python -m compileall -q backend/app/windows_ssh.py
  backend/tests/test_windows_ssh.py` -> passed.
- `git diff --check` -> passed.
- Local `ssh -G -F /dev/null` parse check with the complete forced policy,
  non-default port, IdentityFile, and CertificateFile options -> exit 0.

### Second Review Checklist Closure

1. Critical, pre-validation `Match exec`: closed. Python now reads the root
   config and recursively expands every Include without invoking OpenSSH,
   rejecting direct, negated, `key=value`, and nested `Match exec` forms before
   the runner is called. Include globs are snapshotted, cycles/depth and unsafe
   files are rejected, and relative includes use OpenSSH's `~/.ssh` base. Only
   the fully expanded in-memory snapshot is written to a private temporary file
   for `ssh -G`; actual SSH/SCP commands use `-F os.devnull`, so mutations to the
   original graph after the read cannot affect parsing or execution.
2. Critical, multiplexed transport bypass: closed. Non-default ControlPath,
   ControlMaster, and ControlPersist settings are rejected. Every SSH/SCP argv
   forces `ControlPath=none`, `ControlMaster=no`, and `ControlPersist=no`.
3. Important, alternative host-key sources: closed. KnownHostsCommand and
   VerifyHostKeyDNS are rejected unless disabled, then explicitly forced to
   `none`/`no` alongside the canonical user known-hosts file and disabled global
   file.
4. Important, alias authentication semantics: closed. The frozen target retains
   a validated port, all IdentityFile entries, and all CertificateFile entries.
   SSH and both SCP directions use the safe alias operand with `-F os.devnull`
   while explicitly pinning numeric HostName, HostKeyAlias, User, Port, policy,
   and authentication files. Host-key lookup uses the same hostname and emits
   `[hostname]:port` for non-default ports.

### Concerns

- Tests use fake process/DNS adapters; no live Windows host, authentication, or
  file transfer was exercised.
- Include token/environment expansion is rejected rather than reimplemented;
  absolute paths, `~`, relative `~/.ssh` paths, and globs are supported. This is
  intentionally fail-closed for config syntax the non-executing parser cannot
  reproduce exactly.
- Residual percent/environment tokens in IdentityFile and CertificateFile are
  rejected to avoid changing their meaning when converted to explicit options.
  Missing IdentityFile candidates remain allowed because OpenSSH emits missing
  default key paths; configured CertificateFile entries must exist.
- Authentication and known-host files are opened later by OpenSSH. A separate
  actor with permission to replace canonical files after validation remains
  outside this adapter's process boundary.
