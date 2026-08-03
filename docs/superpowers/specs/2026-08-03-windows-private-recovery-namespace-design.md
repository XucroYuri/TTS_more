# Windows reliability private-recovery namespace: design amendment

## Status and precedence

This document is the approved design amendment for private runtime recovery
material produced by the formal Windows ComfyUI reliability supervisor. It
supplements the immutable-run evidence design and resolves its open F2 cleanup
residual. If this amendment conflicts with either
`2026-08-03-windows-comfyui-run-evidence-design.md` or
`2026-08-03-windows-comfyui-run-evidence.md`, this amendment controls only the
location, observation, retention, publication, and later deletion of private
recovery material.

The amendment does not change the current-pointer schema, the terminal schema,
legacy evidence preservation, CAS ordering, official ComfyUI or TTS source
ownership, or the requirement that public run evidence is immutable.

## Problem and decision

The validator's atomic-write recovery can leave `.p`, `.o`, `.h`, or `.c`
material when final cleanup fails. Keeping those private bytes inside the
public run directory makes strict terminal membership impossible. Deleting them
to force publication would destroy recovery evidence. Leaving the run orphaned
would also violate the approved contract that a cleanup failure becomes the
new current failure.

The selected solution places all such material in a run-key-derived private
namespace beside, but never inside, the public run store. A cleanup failure may
become current only after the supervisor safely identifies the private
namespace and first-writes a bounded, canonical, redacted public commitment.
If that observation cannot be completed safely enough to satisfy this contract,
the run remains an orphan and the previous current pointer does not advance.

## Layout and ownership

```text
<output-root>/
  current-terminal.json
  .current-terminal.lock
  .private-recovery/
    <64-lowercase-hex-run-key>/
      .p/
      .o
      .h
      .c
  runs/
    <64-lowercase-hex-run-key>/
      ...
      logs/
        private-recovery.log
      terminal.json
```

The supervisor derives both run directories from the same validated run key;
neither path is accepted from the child or a caller. Before the child starts,
the supervisor creates the public and private run directories with create-new,
handle-relative operations and rejects any existing object, reparse point, or
identity mismatch. It retains non-delete-shared identity leases over the
relevant output-root ancestry and both run directories through child exit,
cleanup, public terminal creation, pointer CAS, and current verification.

The inner launcher and validator write formal evidence only under
`runs/<run-key>/`. Their atomic-write runtime recovery material is directed only
to `.private-recovery/<run-key>/`. This narrowly amends the earlier statement
that the inner launcher writes only its public run directory; it does not allow
any other shared-root or caller-selected writes.

`.private-recovery/` and everything beneath it are excluded from public run
exact-membership rules, terminal commitments, pointer verification, and the
legacy root reader. Public readers never traverse it. Public evidence commits
only `logs/private-recovery.log` when private material must be retained.

## Namespace identity and filesystem safety

The private directory identity is the SHA-256 of a canonical binary record of
the Windows volume serial number and directory file ID obtained from the held
directory handle. The raw volume and file identifiers are not public. The
supervisor records the digest in the redacted snapshot and keeps the raw
identity only in process-private state for the duration of the run.

Every operation beneath the private run directory is handle-relative and
no-follow. The implementation rejects reparse points at the output root,
`.private-recovery`, private run directory, and every observed descendant.
It never follows a junction or symlink to inspect or remove a target. An unsafe
ancestor, replacement, non-regular static member, reparse descendant, or
identity drift fails closed and preserves any outside sentinel.

The only permitted top-level private members are `.p`, `.o`, `.h`, and `.c`.
`.p`, when present, must be a directory. `.o`, `.h`, and `.c`, when present,
must be regular non-reparse files. Extra top-level members make the private
namespace unsafe and prohibit pointer advancement and deletion.

## Public redacted snapshot contract

`runs/<run-key>/logs/private-recovery.log` is canonical UTF-8 JSON with no BOM,
one trailing newline, strict unknown-field rejection, and a maximum encoded
size of 4 MiB. It is first-written and then committed like every other public
run artifact. It has exactly these top-level fields:

- `schema_version=1`
- `kind="reliability-private-recovery-snapshot"`
- `run_key=<the public run key>`
- `namespace_identity_sha256=<64 lowercase hex>`
- `retained=true`
- `observation_complete=<boolean>`
- `overflow=<boolean>`
- `limits=<the applied fixed limits>`
- `static_members=<exactly .o, .h, and .c in that order>`
- `mutable_tree=<the .p observation>`

`limits` has exactly:

- `max_entries=4096`
- `max_total_observed_bytes=68719476736` (64 GiB)
- `max_stable_member_bytes=67108864` (64 MiB)
- `max_snapshot_bytes=4194304` (4 MiB)

Each `static_members` entry has exactly `role`, `present`, `size_bytes`, and
`sha256`. `role` is one of `.o`, `.h`, or `.c`. An absent role has
`present=false`, `size_bytes=null`, and `sha256=null`. A present role is opened
once through the leased directory, its handle identity is checked before and
after the read, and its full bytes are bounded by `max_stable_member_bytes`.
It has `present=true`, its exact nonnegative size, and its content SHA-256. A
static file that changes, exceeds the limit, cannot be read through the bound
handle, or changes identity makes the snapshot unsafe; it is not downgraded to
an incomplete observation.

`mutable_tree` has exactly `present`, `mutable=true`, `entry_count`,
`observed_total_bytes`, and `entries`. If `.p` is absent, `present=false`, both
counts are zero, and `entries=[]`. Each entry has exactly:

- `relative_name_sha256`, hashing the normalized UTF-8 relative name without
  exposing that name;
- `kind="file"|"directory"`;
- `observed_size_bytes`, zero for a directory;
- `stable=<boolean>`;
- `sha256=<content hash|null>`, present only for a stable file.

Traversal is bounded, handle-relative, non-recursive at the language-runtime
level, and deterministic. Entries are sorted by
`(relative_name_sha256, kind, observed_size_bytes)`. Duplicate hashed names,
case-fold collisions, impossible ordering, negative sizes, and integer values
above signed 64-bit range fail closed. For a file no larger than
`max_stable_member_bytes`, the observer may read and hash it; matching handle
identity, size, and last-write metadata before and after the read yields
`stable=true`. A concurrent mutation or a larger file yields `stable=false`
and `sha256=null`; such expected `.p` drift does not by itself make the snapshot
unsafe.

When an additional entry would exceed either the entry or total-observed-byte
bound, that entry is not added and enumeration stops. The snapshot records
`overflow=true` and `observation_complete=false`, and its recorded counts never
exceed the declared limits. A bounded overflow snapshot may still be published
because it truthfully proves only the observed prefix and the existence of
retained private recovery state. With no overflow and no mutable drift,
`observation_complete=true`; any unstable `.p` file makes it false. Unsafe
reparse, identity, schema, static-member, or snapshot-size conditions do not
produce a weaker public snapshot and do not advance current.

The snapshot never contains raw names, paths, file contents, command lines,
environment values, registry values, model identifiers, usernames, machine
identifiers, tokens, or excerpts from private files. Terminal commits the exact
public snapshot bytes; later authorized deletion of the private namespace does
not change or invalidate the historical terminal.

## Supervised run state machine

1. The supervisor snapshots the current-pointer token, validates the output
   root, and create-new prepares both run-key directories with retained identity
   leases before starting the child.
2. The child starts exactly once. Formal run evidence goes to the public run;
   `.p`, `.o`, `.h`, and `.c` recovery state goes only to the private run.
3. After child exit and stream closure, a successful cleanup removes the private
   run directory through its retained handle. The supervisor proves its absence
   before writing a normal passed or failed terminal. No private-recovery log is
   present for this path.
4. If cleanup fails and private recovery remains, the supervisor preserves the
   bytes, writes the redacted snapshot, and publishes a strict terminal with
   `cleanup_status=failed`, `outcome=failed`, and the already-defined primary
   failure-source rules. A preceding validator failure remains primary; a
   cleanup failure after validator success has `failure_source=cleanup`.
5. If snapshot creation, namespace identity, no-follow validation, or terminal
   construction fails, the supervisor preserves the run as an unreferenced
   orphan, does not advance the pointer, exits nonzero, and reports only a
   bounded sanitized error outside formal evidence.
6. A CAS loser preserves its complete public run and any retained private run,
   exits nonzero, and never modifies the winning current pointer.

A crash before successful private removal or failed-run publication can leave
an orphan private namespace. Orphan discovery is read-only and run-key based;
it never deletes, adopts, or makes an orphan current. Existing legacy root
evidence and pre-amendment runs are never moved, rewritten, or deleted.

## Retention and explicit recovery cleanup

Cleanup success removes the current run's private namespace before terminal
freeze. Cleanup failure, supervisor crash, or CAS loss retains it indefinitely
until an explicit identity-revalidated recovery action succeeds. No age-based,
startup, recursive, best-effort, or automatic garbage collection is allowed.

The only product entry for later removal is:

```powershell
scripts/recover-windows-comfyui-reliability-run.ps1 `
  -OutputRoot <output-root> `
  -RunKey <64-lowercase-hex-run-key>
```

The command accepts no private path. It fixed-derives the namespace, obtains
the same no-follow ancestor and directory leases, and validates the public
snapshot when one exists. The snapshot's static `.h` commitment is checked
first; only an exact, stable, identity-matched `.h` byte sequence may then be
decoded as the private process-identity record. The snapshot itself never
asserts ownership, and `.h` is not read at all when its public commitment or
namespace identity fails. The decoded record must agree with the run's strict,
first-written public supervisor, run-result, and lifecycle bindings. An orphan
without sufficient public or committed-private ownership facts cannot be
recovered by this command and remains a zero-delete manual investigation case.
Before deleting any byte the command completes one read-only prevalidation
transaction that proves:

- output-root, namespace, run-key, and snapshot identities agree;
- the namespace has no extra top-level members or reparse descendants;
- each recorded owner process is absent, or the exact recorded process was
  safely stopped by this invocation;
- PID, creation time, executable identity, command-line digest, parent identity,
  descendant relationship, owned ports, and namespace identity all agree;
- no unowned or ambiguous process holds an owned port or private artifact;
- every planned deletion remains beneath the leased private run directory.

PID reuse, missing creation time, inaccessible executable/command line, parent
or descendant ambiguity, port-owner drift, identity drift, or any partial proof
is a zero-delete nonzero result. Process termination, when needed, targets only
an exactly proven owned identity and waits for both process exit and owned-port
release before filesystem deletion.

After the complete proof succeeds, deletion order is `.p`, `.c`, `.h`, `.o`,
then the empty private run directory. `.p` is removed bottom-up using
handle-relative no-follow operations. Failure during the deletion phase stops
immediately and returns nonzero; remaining bytes stay in place and the public
history is unchanged. The root `.private-recovery` directory itself is retained.
The command never deletes a public run, terminal, pointer, legacy file, model,
configuration, official checkout, or unrelated runtime.

The zero-delete guarantee covers every identity, ownership, traversal, access,
and deletion-plan proof before the explicit destructive commit. Windows does
not provide a supported transactional recursive-delete primitive for this
contract, so an operating-system failure after the first successful deletion
cannot restore already deleted private bytes. Such a failure is nonzero,
strictly bounded by the retained handle leases, and never proceeds to the next
planned member. Tests inject every controllable failure before destructive
commit and require zero deletion there; they separately verify bounded stop on
an injected post-commit failure.

## Required behavior tests

Implementation is accepted only with behavior-first tests covering:

- cleanup success removes the private run and produces no private-recovery log;
- residual `.p/.o/.h/.c` produces a strict cleanup-failed current plus the raw
  private bytes and exact public snapshot commitment;
- validator failure followed by cleanup failure preserves validator as primary;
- static-member mutation, oversize, replacement, and read failure prohibit
  publication;
- dynamic `.p` mutation yields `stable=false`; deterministic ordering, entry
  overflow, byte overflow, and snapshot-size bounds are enforced;
- raw private names, paths, contents, commands, registry values, and secrets do
  not occur in pointer, terminal, public snapshot, or other public evidence;
- supervisor crash boundaries, orphan discovery, and CAS loser retention leave
  the prior current unchanged where required;
- same run-key collision never overwrites either public or private bytes;
- a real Windows junction/reparse escape is refused while an outside sentinel
  remains byte-identical;
- explicit recovery succeeds only after exact process, port, namespace, and
  snapshot validation and leaves historical public evidence byte-identical;
- recovery rejects identity drift, PID reuse, owner/parent/descendant ambiguity,
  outside targets, and extra members with zero deletions;
- every injected partial prevalidation or access-probe failure is zero-delete;
  an injected post-commit deletion failure is nonzero, remains bounded to the
  private namespace, and leaves all not-yet-deleted material intact;
- pointer verification and pointer-absent legacy audit ignore
  `.private-recovery`, while an invalid present pointer still forbids fallback.

All filesystem-race and process-identity tests that depend on Windows semantics
run on Windows. Hosted deterministic tests use synthetic identities and no
private fixtures. Real CUDA validation remains separately opt-in and cannot
begin until this amendment's deterministic implementation and independent
review are green.

## Compatibility and non-goals

The amendment applies only to newly supervised attempts after its implementation.
There is no migration of existing public runs, legacy evidence, or old residual
files. The pointer and terminal remain path-free and retain their existing schema
versions. `.private-recovery` is not a general cache, user backup, quarantine,
or plugin data directory.

This work does not modify official ComfyUI, GPT-SoVITS, IndexTTS, or CosyVoice;
does not expose private recovery contents through an API or UI; does not add
automatic retention expiry; and does not authorize deletion of Fix11/Fix12 or
other worktrees. Any future relaxation of the bounds, privacy rules, identity
proof, or zero-delete prevalidation requires a new approved specification.
