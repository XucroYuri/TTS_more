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

## Third Review Fixes (2026-07-13)

### RED

- Snapshot boundary/Match group:
  `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q -k
  'bare_carriage or synthesize_match or all_match_directives'` ->
  `5 failed, 1 passed, 91 deselected`. Bare CR reached snapshot generation,
  no-final-LF Include fragments could concatenate, and non-exec Match forms were
  retained. The one early pass was an accidental overbroad `exec` token check,
  confirming criterion parsing was not reliable.
- Include resource bounds group:
  `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q -k
  'fifo_include or config_single_file or config_total_byte or
  config_file_count'` -> `4 failed, 97 deselected`. FIFO open occurred before
  type validation and no size/count budgets existed.
- Authentication serialization/ControlPersist group:
  `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q -k
  'unsafe_config_characters or generated_auth_arguments or
  authentication_semantics or multiplexing'` ->
  `10 failed, 6 passed, 95 deselected`. Unsafe path characters and
  `ControlPersist=0` were accepted, and IdentityFile was not emitted with `-i`.

### GREEN

- `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q` ->
  `111 passed in 0.16s`. This includes a real local
  `ssh -G -F os.devnull` parse of the generated IdentityFile and
  CertificateFile arguments.
- `.venv/bin/python -m pytest backend/tests/test_lan_topology.py -q` ->
  `44 passed in 0.09s`.
- `.venv/bin/python -m compileall -q backend/app/windows_ssh.py
  backend/tests/test_windows_ssh.py` -> passed.
- `git diff --check` -> passed.

### Third Review Checklist Closure

1. Critical, scanner/snapshot line-boundary disagreement: closed. Config bytes
   preserve physical newlines, every control character except LF is rejected,
   every nonempty expanded fragment ends with LF, and the final assembled
   snapshot is validated again before it is written as unchanged UTF-8 bytes.
   Directives cannot be synthesized across Include EOF boundaries.
2. Important, authentication option serialization: closed fail-closed.
   IdentityFile and CertificateFile paths reject whitespace, quotes,
   backslashes, residual tokens, control characters, and symlinks. IdentityFile
   uses the dedicated repeated `-i <path>` argv form; safe certificate options
   remain explicit `-o` values. A real OpenSSH parse test validates generated
   safe arguments.
3. Important, operation-dependent Match semantics: closed fail-closed. All Match
   directives are rejected before runner invocation, including `all`, `command`,
   `sessiontype`, and `exec`; the brief does not require Match support.
4. Important, unsafe/unbounded Include nodes: closed. Each node is checked with
   `lstat` before open, must be regular and non-symlinked, is opened with available
   no-follow/nonblocking flags, and is rechecked with `fstat` on the same file
   descriptor. Per-file bytes, aggregate bytes, assembled snapshot bytes, file
   count, recursion depth, and glob match materialization are bounded.
5. Minor, `ControlPersist=0`: closed. Only `no` and `false` are accepted as
   disabled resolved values; execution still forces `ControlPersist=no`.

### Concerns

- Tests do not contact a live Windows host or perform authentication/transfers;
  only local OpenSSH option parsing is exercised with the real executable.
- Match is intentionally unsupported in all forms. Configs requiring Match must
  be flattened into unconditional alias-specific settings before use.
- Config graphs are limited to 64 files, 1 MiB per source file, and 4 MiB total.
  Include token/environment expansion remains unsupported and fails closed.
- Authentication paths with whitespace, quotes, backslashes, or unresolved
  tokens are intentionally rejected rather than escaped.

## Fourth Review Fixes (2026-07-13)

### RED

- Final canonical path group:
  `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q -k
  'percent_token_after or canonical_parent or safe_known_hosts_literal'` ->
  `16 failed, 1 passed, 111 deselected`. `%%h` became a residual `%h`, and
  canonical parents reintroduced `%`, `${...}`, whitespace, quotes, or
  backslashes for known_hosts, IdentityFile, and CertificateFile. The safe
  literal SSH/keygen consistency baseline already passed.
- Config whitespace/newline group:
  `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q -k
  'tab_indentation or crlf_is_normalized or non_whitespace_config_controls'` ->
  `2 failed, 4 passed, 128 deselected`. Tab and CRLF were rejected while the
  other control-character cases remained correctly blocked.

### GREEN

- `.venv/bin/python -m pytest backend/tests/test_windows_ssh.py -q` ->
  `134 passed in 0.23s`.
- `.venv/bin/python -m pytest backend/tests/test_lan_topology.py -q` ->
  `44 passed in 0.09s`.
- `.venv/bin/python -m compileall -q backend/app/windows_ssh.py
  backend/tests/test_windows_ssh.py` -> passed.
- `git diff --check` -> passed.

### Fourth Review Checklist Closure

1. Critical, second expansion of validated file paths: closed. A shared literal
   path validator now runs on both the resolved source value and the final
   expanded/absolute/canonical string for UserKnownHostsFile, IdentityFile, and
   CertificateFile. Residual `%`, `${...}`, all whitespace, quotes, backslashes,
   and control characters fail closed after canonicalization. Regression tests
   cover `%%h -> %h` and canonical parents introducing each unsafe class. For a
   safe path, SSH receives `UserKnownHostsFile=<canonical literal>` and
   `ssh-keygen -f` receives the exact same string.
2. Minor, Tab and CRLF compatibility: closed. UTF-8 config text normalizes CRLF
   to LF before scanning and assembly, permits Tab as OpenSSH whitespace, and
   writes an LF-only snapshot. Bare CR and all other Unicode control characters
   remain rejected before runner invocation.

### Concerns

- No live Windows host, authentication, or transfer was exercised; focused tests
  continue to use fake process/DNS adapters except for the real local OpenSSH
  option-parse regression.
- Canonical trust/authentication paths containing any rejected character class
  are intentionally unsupported rather than escaped.
