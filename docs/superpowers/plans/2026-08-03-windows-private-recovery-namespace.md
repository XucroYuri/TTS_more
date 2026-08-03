# Windows private recovery namespace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement this plan task-by-task. Each task is behavior-first, has its own test gate, and ends with a focused commit plus independent review.

**Goal:** Move supervised Windows runtime recovery residue into a run-key-derived private namespace, publish a strict redacted commitment on cleanup failure, and provide identity-revalidated explicit recovery cleanup without weakening immutable public evidence.

**Architecture:** A new Python module owns the private namespace boundary, bounded static/dynamic observations, canonical redacted snapshot, and no-follow deletion plan. The existing evidence store remains the only public run/pointer authority; supervision prepares and leases both public and private directories, routes the inner launcher’s `.p/.o/.h/.c` state to the private namespace, and commits the public snapshot before a cleanup-failed terminal. A dedicated PowerShell recovery entry derives the namespace from `-OutputRoot` and `-RunKey`, validates process/port/file identities, and deletes only after a complete read-only proof.

**Tech Stack:** Python 3.12, Pydantic strict models, existing `app.comfyui.reliability_evidence` Windows handle helpers, PowerShell 5.1, Win32 `CreateFileW`/`NtCreateFile`/file-information APIs, pytest, and existing `soundfile`/validator fixtures.

## Global Constraints

- Run keys are exactly 64 lowercase hexadecimal characters and are the only private namespace selector.
- New layout is `<output-root>/.private-recovery/<run-key>/` beside `runs/<run-key>/`; no caller-supplied private path is accepted.
- Private top-level members are only `.p/`, `.o`, `.h`, and `.c`; public run exact-membership never traverses the sibling private tree.
- Static snapshot limits are `max_entries=4096`, `max_total_observed_bytes=68719476736`, `max_stable_member_bytes=67108864`, and `max_snapshot_bytes=4194304`.
- Static `.o/.h/.c` reads are stable handle-relative reads; mutation, replacement, oversize, reparse, or identity drift fails closed.
- Dynamic `.p` observations expose only relative-name hashes, kind, observed size, stable flag, and stable-file content hashes; overflow is truthful and bounded.
- Public snapshot JSON is canonical UTF-8, strict, first-written, path-free, and committed as `logs/private-recovery.log`.
- Cleanup success removes the private namespace before terminal freeze; cleanup failure retains it and may become current only after a valid public snapshot is first-written.
- Snapshot, identity, reparse, terminal, or CAS failure leaves an orphan and does not advance the previous pointer.
- Recovery prevalidation is zero-delete; after the explicit destructive commit, any OS failure stops immediately, remains private-namespace-bounded, and never claims rollback of already deleted bytes.
- The public pointer and terminal schemas remain version 1; legacy root evidence is never moved, deleted, archived, quarantined, or used when a present pointer is invalid.
- Official ComfyUI, GPT-SoVITS, IndexTTS, and CosyVoice source remains unmodified; real CUDA validation remains opt-in after deterministic gates.
- Every implementation task starts with a behavior RED, runs the narrow test, implements the smallest GREEN change, runs the task suite, and commits only its scoped files.

## File Map

| File | Responsibility | Planned change |
|---|---|---|
| `backend/app/comfyui/reliability_private_recovery.py` | Private namespace identity, strict snapshot models, bounded observation, deletion planning | Create |
| `backend/tests/test_comfyui_private_recovery.py` | Unit/behavior tests for private boundary, snapshot, redaction, and deletion planning | Create |
| `backend/app/comfyui/reliability_evidence.py` | Public run membership, artifact validation, pointer/current readers | Modify only to validate the new public snapshot log and ignore the sibling private root |
| `backend/tests/test_comfyui_reliability_evidence.py` | Public-reader regression tests | Extend with private-root isolation cases |
| `backend/app/comfyui/reliability_supervision.py` | Prepare/validate both namespaces and finalize cleanup-failed terminals | Modify |
| `backend/app/comfyui/reliability_supervision_cli.py` | JSON CLI for new private boundary fields and snapshot/finalize operations | Modify |
| `backend/tests/test_windows_reliability_supervisor.py` | Real Windows supervisor/inner launcher behavior | Update residual expectations and add supervised cleanup/snapshot cases |
| `scripts/run-windows-comfyui-reliability-supervised.ps1` | Formal outer supervisor and directory leases | Modify to prepare/lease/validate private namespace and pass derived identity |
| `scripts/run-windows-comfyui-reliability.ps1` | Inner launcher runtime and cleanup | Modify recovery paths to private namespace only |
| `scripts/recover-windows-comfyui-reliability-run.ps1` | Explicit operator recovery entry | Create |
| `backend/app/comfyui/reliability_recovery.py` | Strict owner/process/port validation and deletion transaction orchestration | Create |
| `backend/app/comfyui/reliability_recovery_cli.py` | Sanitized internal JSON bridge from PowerShell observations to recovery planner | Create |
| `backend/tests/test_comfyui_reliability_recovery.py` | Recovery identity drift, PID reuse, zero-delete, and bounded deletion behavior | Create |
| `docs/superpowers/reviews/2026-08-03-windows-comfyui-run-evidence/task-{N}-private-recovery.md` | Independent task review evidence | Create one report per task review |

---

### Task 1: Create the no-follow private namespace boundary

**Files:**
- Create: `backend/app/comfyui/reliability_private_recovery.py`
- Create: `backend/tests/test_comfyui_private_recovery.py`

**Interfaces:**
- Consumes: `evidence.RunKey`, `evidence.SHA256`, `evidence._validated_root`, `evidence._windows_nt_create`, `evidence._windows_handle_information`, `evidence._windows_directory_identity`, and `evidence._windows_close_handle`.
- Produces:
  ```python
  PRIVATE_RECOVERY_DIRECTORY = ".private-recovery"
  PRIVATE_ROLES: tuple[str, ...] = (".o", ".h", ".c")

  class PrivateRecoveryError(evidence.EvidenceStoreError):
      pass

  class PrivateRecoveryBoundary(_StrictModel):
      status: Literal["prepared", "validated"]
      run_key: evidence.RunKey
      output_root: str
      root_identity: evidence.SHA256
      private_root: str
      private_root_identity: evidence.SHA256

  def prepare_private_recovery(
      output_root: Path,
      run_key: str,
      *,
      expected_root_identity: str,
  ) -> PrivateRecoveryBoundary:
      raise NotImplementedError

  def validate_private_recovery(
      output_root: Path,
      run_key: str,
      *,
      expected_root_identity: str,
      expected_private_root_identity: str,
  ) -> PrivateRecoveryBoundary:
      raise NotImplementedError

  def private_recovery_root(output_root: Path, run_key: str) -> Path:
      raise NotImplementedError
  ```

- [ ] **Step 1: Write the failing boundary tests.** Add tests for create-new private directory creation, exact run-key derivation, existing private run collision, output-root identity drift, and a junction from `.private-recovery/<run-key>` to an outside sentinel. Assert that all failures raise `PrivateRecoveryError` and the outside sentinel bytes/mtime remain unchanged.
- [ ] **Step 2: Run the focused RED suite.** Run `backend\.venv\Scripts\python.exe -m pytest -q backend/tests/test_comfyui_private_recovery.py -k "boundary or junction"`; it must fail because the module and functions do not exist.
- [ ] **Step 3: Implement the boundary.** Derive `output_root / ".private-recovery" / validated_run_key`, create the sibling root if absent, create the run-key leaf with `create_new=True`, and on Windows use the existing handle-relative evidence helpers with `FILE_FLAG_OPEN_REPARSE_POINT` semantics. Return the volume-serial/file-index SHA-256 identity. On non-Windows use `O_NOFOLLOW`/`lstat` checks and reject any resolved path that leaves the output root.
- [ ] **Step 4: Re-run the focused suite.** Run the same command and require all boundary tests to pass on Windows; on non-Windows, the junction-specific test may be skipped only by its existing platform guard.
- [ ] **Step 5: Commit the isolated module.** Run `git diff --check`, stage only the new module/test, and commit `feat: add private recovery namespace boundary`.

**Review gate:** Dispatch a fresh independent reviewer to inspect create-new semantics, ancestor/reparse handling, run-key derivation, and outside-sentinel preservation. The reviewer must return READY or identify a concrete failing test before Task 2 begins.

### Task 2: Implement bounded static/dynamic observation and redacted snapshots

**Files:**
- Modify: `backend/app/comfyui/reliability_private_recovery.py`
- Modify: `backend/tests/test_comfyui_private_recovery.py`

**Interfaces:**
- Consumes: `PrivateRecoveryBoundary` from Task 1.
- Produces:
  ```python
  class PrivateRecoveryLimits(_StrictModel):
      max_entries: StrictInt = 4096
      max_total_observed_bytes: StrictInt = 68_719_476_736
      max_stable_member_bytes: StrictInt = 67_108_864
      max_snapshot_bytes: StrictInt = 4_194_304

  class PrivateStaticMember(_StrictModel):
      role: Literal[".o", ".h", ".c"]
      present: StrictBool
      size_bytes: StrictInt | None
      sha256: evidence.SHA256 | None

  class PrivateMutableEntry(_StrictModel):
      relative_name_sha256: evidence.SHA256
      kind: Literal["file", "directory"]
      observed_size_bytes: StrictInt = Field(ge=0, le=2**63 - 1)
      stable: StrictBool
      sha256: evidence.SHA256 | None

  class PrivateMutableTree(_StrictModel):
      present: StrictBool
      mutable: Literal[True]
      entry_count: StrictInt = Field(ge=0, le=4096)
      observed_total_bytes: StrictInt = Field(ge=0, le=68_719_476_736)
      entries: tuple[PrivateMutableEntry, ...]

  class PrivateRecoverySnapshot(_StrictModel):
      schema_version: Literal[1]
      kind: Literal["reliability-private-recovery-snapshot"]
      run_key: evidence.RunKey
      namespace_identity_sha256: evidence.SHA256
      retained: Literal[True]
      observation_complete: StrictBool
      overflow: StrictBool
      limits: PrivateRecoveryLimits
      static_members: tuple[PrivateStaticMember, PrivateStaticMember, PrivateStaticMember]
      mutable_tree: PrivateMutableTree

  def observe_private_recovery(
      boundary: PrivateRecoveryBoundary,
      *,
      limits: PrivateRecoveryLimits = PrivateRecoveryLimits(),
  ) -> PrivateRecoverySnapshot:
      raise NotImplementedError

  def write_private_recovery_snapshot(
      output_root: Path,
      run_key: str,
      snapshot: PrivateRecoverySnapshot,
  ) -> evidence.ArtifactCommitment:
      raise NotImplementedError
  ```

- [ ] **Step 1: Write RED tests for schema and observation.** Cover stable `.o/.h/.c` hashes, missing static roles, changed/replaced/oversize static files failing closed, `.p` stable-file hashes, `.p` concurrent mutation yielding `stable=false`, deterministic hashed-name ordering, entry/byte overflow with truthful flags, and raw-name/content/path/secret absence from canonical snapshot bytes.
- [ ] **Step 2: Run the RED tests.** Run `backend\.venv\Scripts\python.exe -m pytest -q backend/tests/test_comfyui_private_recovery.py -k "snapshot or static or mutable or overflow or privacy"`; the tests must fail before implementation.
  - [ ] **Step 3: Implement strict observation.** Use handle-relative no-follow enumeration; include an entry only when adding it stays within both bounds, then set `overflow=true` and stop at the first excluded entry. Hash normalized UTF-8 relative names, sort by `(relative_name_sha256, kind, observed_size_bytes)`, reject duplicate hashes/case-fold collisions, and read stable files only through bounded descriptors. Serialize with `json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"`, enforce the 4 MiB encoded limit, and write the public `logs/private-recovery.log` with `evidence.write_artifact(output_root, run_key, "log", canonical_bytes, name="private-recovery")`.
- [ ] **Step 4: Run the GREEN suite.** Re-run the focused command and require static drift to fail closed, mutable drift to be truthful, and overflow counts never to exceed declared limits.
- [ ] **Step 5: Commit the snapshot implementation.** Run `git diff --check`, stage only the module/test changes, and commit `feat: add bounded private recovery snapshots`.

**Review gate:** Dispatch a fresh reviewer for schema exactness, canonical JSON, privacy, limits, and stable-versus-mutable semantics. The review must verify that `.h` can later be decoded only after its public hash commitment is checked.

### Task 3: Integrate public evidence verification and terminal membership

**Files:**
- Modify: `backend/app/comfyui/reliability_evidence.py`
- Modify: `backend/app/comfyui/reliability_validation.py`
- Modify: `backend/tests/test_comfyui_reliability_evidence.py`
- Modify: `backend/tests/test_comfyui_private_recovery.py`

**Interfaces:**
- Consumes: `PrivateRecoverySnapshot` and `write_private_recovery_snapshot` from Task 2.
- Produces:
  ```python
  def verify_private_recovery_log(
      payload: bytes,
      *,
      expected_run_key: str,
      expected_namespace_identity: str | None = None,
  ) -> PrivateRecoverySnapshot:
      raise NotImplementedError

  def verify_run(output_root: Path, run_key: str) -> RunVerification:
      raise NotImplementedError
  ```
  `verify_run` must validate an optional `logs/private-recovery.log` as a strict snapshot artifact while never scanning `.private-recovery`.

- [ ] **Step 1: Write RED reader tests.** Add cases where the sibling private namespace contains extra files or contradictory bytes but a valid public terminal remains verifiable; where a private snapshot log has an extra field, wrong run key, wrong namespace hash, path, raw name, or changed static hash; and where pointer-absent legacy audit ignores the sibling namespace.
- [ ] **Step 2: Run the RED reader suite.** Run `backend\.venv\Scripts\python.exe -m pytest -q backend/tests/test_comfyui_reliability_evidence.py backend/tests/test_comfyui_private_recovery.py -k "private or legacy"`; require failures for the new cases.
  - [ ] **Step 3: Implement strict public-log validation.** Parse the bounded log with `PrivateRecoverySnapshot.model_validate_json(payload, strict=True)`, compare run key and the namespace identity supplied by the run boundary, verify canonical bytes and SHA-256, and add no private-root traversal to `_scan_run_membership` or `verify_current`.
- [ ] **Step 4: Run the complete evidence suites.** Run `backend\.venv\Scripts\python.exe -m pytest -q backend/tests/test_comfyui_reliability_evidence.py backend/tests/test_comfyui_private_recovery.py` and require all existing pointer/CAS/tamper/legacy tests plus the new isolation tests to pass.
- [ ] **Step 5: Commit evidence integration.** Stage only evidence/validation tests and implementations and commit `fix: validate private recovery commitments as public logs`.

**Review gate:** Dispatch a fresh reviewer to check exact public membership, pointer behavior, legacy fallback, path-free reports, and that no public reader can follow `.private-recovery`.

### Task 4: Route the formal supervisor and inner launcher through both namespaces

**Files:**
- Modify: `backend/app/comfyui/reliability_supervision.py`
- Modify: `backend/app/comfyui/reliability_supervision_cli.py`
- Modify: `scripts/run-windows-comfyui-reliability-supervised.ps1`
- Modify: `scripts/run-windows-comfyui-reliability.ps1`
- Modify: `backend/tests/test_windows_reliability_supervisor.py`

**Interfaces:**
- Consumes: `PrivateRecoveryBoundary`, `observe_private_recovery`, `write_private_recovery_snapshot`, and Task 3 public-log verification.
- Produces:
  ```python
  class PreparedRun(_StrictModel):
      status: Literal["prepared"]
      run_key: evidence.RunKey
      output_root: str
      root_identity: evidence.SHA256
      run_root: str
      run_root_identity: evidence.SHA256
      private_root: str
      private_root_identity: evidence.SHA256

  def prepare_run(
      output_root: Path,
      run_key: str,
      *,
      expected_root_identity: str,
  ) -> PreparedRun:
      raise NotImplementedError

  def validate_run_boundary(
      output_root: Path,
      run_key: str,
      *,
      expected_root_identity: str,
      expected_run_root_identity: str,
      expected_private_root_identity: str,
  ) -> ValidatedRunBoundary:
      raise NotImplementedError

  def finalize_supervision(
      output_root: Path,
      run_key: str,
      *,
      mode: Literal["preflight", "matrix"],
      expected_token: str,
      expected_root_identity: str,
      expected_run_root_identity: str,
      expected_private_root_identity: str,
      launcher_exit_code: int,
      child_start_count: int,
  ) -> FinalizedSupervision:
      raise NotImplementedError
  ```

- [ ] **Step 1: Write RED supervisor tests.** Change the existing residual test to assert cleanup failure publishes `current-terminal.json` with `failure_source=launcher` or `cleanup` according to the established primary-source table, that raw `.p/.o/.h/.c` remain under `.private-recovery/<run-key>`, that `logs/private-recovery.log` is committed, and that `runs/<run-key>` has no private top-level residue. Add cleanup-success assertions for private-directory absence and a crash-before-snapshot assertion that leaves the old current unchanged.
- [ ] **Step 2: Run the focused Windows RED tests.** Run `backend\.venv\Scripts\python.exe -m pytest -q backend/tests/test_windows_reliability_supervisor.py -k "cleanup_residual or cleanup_failure or private_recovery"`; expected failures show the old run-root location and orphan-current behavior.
- [ ] **Step 3: Extend Python supervision.** Make `prepare_run` create both run-key leaves and return both identities; make `validate_run_boundary` validate both. In `finalize_supervision`, if `cleanup_status=completed`, remove the private leaf through the held boundary and assert absence before terminal freeze; if residue exists, observe it, first-write `logs/private-recovery.log`, and include that log in terminal commitments before CAS. If observation or identity verification fails, raise `SupervisionError` before terminal/pointer publication.
- [ ] **Step 4: Extend the CLI and PowerShell boundary.** Add `--expected-private-root-identity` to `validate-run-root`, emit private path/identity from `prepare-run`, and add `--expected-root-identity`, `--expected-run-root-identity`, and `--expected-private-root-identity` to `finalize`. Pass all derived identities from the outer script. Extend `ReliabilityDirectoryLease` to hold the private directory and ancestor handles. Add `-PrivateRecoveryRoot` and `-PrivateRecoveryRootIdentity` only as supervisor-generated internal parameters to the inner script; revalidate them against the fixed path `Join-Path (Join-Path $OutputRoot '.private-recovery') $runIdSha256` before any recovery write, then replace every `.p/.o/.h/.c` recovery path rooted at `$runEvidenceRoot` with `$privateRecoveryRoot`. Keep `run-result.json`, public lifecycle logs, cases, audio, and failure markers in `runs/<run-key>`.
- [ ] **Step 5: Run the full supervisor suite.** Run `backend\.venv\Scripts\python.exe -m pytest -q backend/tests/test_windows_reliability_supervisor.py backend/tests/test_comfyui_reliability_evidence.py backend/tests/test_comfyui_private_recovery.py`; then run the real Windows PowerShell parser and compile checks used by the existing governance gate.
- [ ] **Step 6: Commit supervision integration.** Stage only the supervisor/CLI/scripts/tests and commit `fix: publish supervised private recovery failures`.

**Review gate:** Dispatch a fresh reviewer to verify one child launch, exact signed exits, lease lifetime through terminal/CAS/current verification, private/public separation, and all four primary failure-source rows.

### Task 5: Add explicit identity-revalidated recovery cleanup

**Files:**
- Create: `backend/app/comfyui/reliability_recovery.py`
- Create: `backend/app/comfyui/reliability_recovery_cli.py`
- Create: `backend/tests/test_comfyui_reliability_recovery.py`
- Create: `scripts/recover-windows-comfyui-reliability-run.ps1`
- Modify: `backend/app/comfyui/reliability_private_recovery.py`

**Interfaces:**
- Consumes: fixed `output_root`/`run_key`, public terminal and `private-recovery.log`, stable `.h` hash commitment, and existing process/port identity models.
- Produces:
  ```python
  class RecoveryResult(_StrictModel):
      status: Literal["removed", "rejected"]
      run_key: evidence.RunKey
      deleted_roles: tuple[str, ...]
      reason_code: str | None

  class RecoveryPlan(_StrictModel):
      run_key: evidence.RunKey
      private_root: str
      namespace_identity_sha256: evidence.SHA256
      delete_order: tuple[Literal[".p", ".c", ".h", ".o"], ...]
      prevalidated: Literal[True]

  def validate_recovery_owner(
      output_root: Path,
      run_key: str,
      *,
      observed_processes: tuple[dict[str, object], ...],
      observed_ports: dict[str, int | None],
  ) -> RecoveryPlan:
      raise NotImplementedError

  def execute_recovery_delete(plan: RecoveryPlan) -> RecoveryResult:
      raise NotImplementedError
  ```

- [ ] **Step 1: Write RED recovery tests.** Cover successful cleanup of `.p`, `.c`, `.h`, `.o` and the private leaf while leaving `terminal.json`, `current-terminal.json`, and the public snapshot byte-identical; wrong namespace identity; `.h` hash mismatch; PID reuse; executable/command-line/parent/descendant drift; owned-port ambiguity; extra/reparse member; and injected prevalidation failure with zero deleted bytes.
- [ ] **Step 2: Run the recovery RED suite.** Run `backend\.venv\Scripts\python.exe -m pytest -q backend/tests/test_comfyui_reliability_recovery.py`; it must fail because the recovery module and script do not exist.
- [ ] **Step 3: Implement read-only recovery planning.** Fixed-derive the private path from validated output root/run key; obtain no-follow ancestor and private-directory leases; validate public snapshot canonical bytes and namespace identity; read `.h` only after its committed size/hash/identity matches; decode owner records and compare PID, creation time, executable hash, command-line hash, parent/descendant graph, ports, and run key. Any missing, ambiguous, reused, or drifted fact returns `RecoveryResult(status="rejected", deleted_roles=(), reason_code="recovery-proof-failed")` without opening a delete handle.
- [ ] **Step 4: Implement bounded deletion and PowerShell entry.** After the proof transaction, delete only `.p` bottom-up, then `.c`, `.h`, `.o`, then the empty private run directory using handle-relative no-follow calls. Stop on the first post-commit OS failure and return nonzero without touching public evidence. The PowerShell script accepts only `-OutputRoot` and `-RunKey`, invokes the Python validator for canonical/public checks, obtains exact process/port observations through `Get-CimInstance`/`Get-NetTCPConnection`, and passes sanitized facts to the Python deletion planner. It never accepts or constructs a caller-supplied private path.
- [ ] **Step 4: Implement the JSON bridge and bounded deletion entry.** Add `reliability_recovery_cli.py` with `plan` and `execute` subcommands. `plan` accepts only `--output-root`, `--run-key`, and bounded JSON observations from stdin, fixed-derives the private namespace, and emits either a sanitized rejection or an opaque plan token; `execute` accepts the same fixed root/key and token, revalidates the token-bound identities, then deletes only `.p` bottom-up, `.c`, `.h`, `.o`, and the empty private run directory using handle-relative no-follow calls. Stop on the first post-commit OS failure and return nonzero without touching public evidence. The PowerShell script accepts only `-OutputRoot` and `-RunKey`, invokes the bridge for canonical/public checks, obtains exact process/port observations through `Get-CimInstance`/`Get-NetTCPConnection`, and passes sanitized facts to the bridge. It never accepts or constructs a caller-supplied private path.
- [ ] **Step 5: Run recovery tests and parser checks.** Run `backend\.venv\Scripts\python.exe -m pytest -q backend/tests/test_comfyui_reliability_recovery.py backend/tests/test_comfyui_private_recovery.py`; run `powershell.exe -NoProfile -NonInteractive -Command "[System.Management.Automation.Language.Parser]::ParseFile('scripts/recover-windows-comfyui-reliability-run.ps1',[ref]$null,[ref]$null) | Out-Null"` and require exit 0.
- [ ] **Step 6: Commit recovery cleanup.** Stage only the recovery module/script/tests and commit `feat: add identity-revalidated recovery cleanup`.

**Review gate:** Dispatch a fresh reviewer to inspect zero-delete prevalidation, `.h` commitment ordering, PID reuse defense, junction/reparse refusal, deletion order, public-history immutability, and the post-commit Windows limitation.

### Task 6: Complete deterministic gates, documentation, and independent readiness review

**Files:**
- Modify: `docs/superpowers/plans/2026-08-03-windows-comfyui-run-evidence.md` with a link to this amendment plan.
- Create: `.superpowers/sdd/2026-08-03-windows-comfyui-run-evidence/task-private-recovery-review.md`

**Interfaces:**
- Consumes: all Task 1-5 commits and their focused tests.
- Produces: a deterministic-gate report binding commands, counts, commit SHAs, and any unchanged historical host-capability limitations.

- [ ] **Step 1: Run the private-recovery suites twice.** Run `backend\.venv\Scripts\python.exe -m pytest -q backend/tests/test_comfyui_private_recovery.py backend/tests/test_comfyui_reliability_recovery.py backend/tests/test_comfyui_reliability_evidence.py` twice consecutively; both runs must pass with identical collection/counts.
- [ ] **Step 2: Run the existing formal gates.** Run the repository’s established plugin unit tests twice, Python 3.12 `pip check`, TTS More focused reliability suites, full backend, frontend tests/build, governance, queue/process suites, PowerShell 5.1 parse, supervisor tests, Python compile, and `git diff --check`. Record exact commands and exit codes in the review report.
- [ ] **Step 3: Dispatch the final independent reviewer.** The reviewer must inspect TTS More, forked TTS-Audio-Suite, official ComfyUI boundary, GPT-SoVITS/IndexTTS/CosyVoice model boundaries, private input redaction, public/private runtime separation, recovery ownership, and all task review reports. The reviewer writes READY only when every deterministic gate is green and no new failure node appears.
- [ ] **Step 4: Update the progress ledger.** Append each task commit, reviewer verdict, test command/count, and remaining live-validation prerequisites to `.superpowers/sdd/2026-08-03-windows-comfyui-run-evidence/progress.md`; do not claim CUDA/matrix completion.
- [ ] **Step 5: Commit documentation only.** Stage the review report, ledger, and any required discoverability link, then commit `docs: record private recovery deterministic readiness`.

**Review gate:** No live preflight or 47-case matrix runs until this task’s independent reviewer returns READY. Any new Windows host-capability failure or changed historical cause stops the plan and is recorded as a blocker.

## Execution Handoff

The prior user choice is Subagent-Driven. Execute Tasks 1 through 6 in order with a fresh implementation subagent for each task, an independent review subagent after each task, behavior RED before GREEN, and one focused commit per task. Do not merge Fix11/Fix12 deletion/quarantine experiments, modify official engine sources, or start the real Windows preflight until Task 6 is READY.
