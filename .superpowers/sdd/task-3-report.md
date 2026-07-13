# Task 3 Report

## RED

- Baseline: `105 passed, 3 skipped` for the existing CUDA validation, evidence sanitizer, and real-TTS suites.
- `test_lan_evidence.py`: collection failed because `app.lan_evidence` did not exist.
- `test_cuda_validation.py`: `13 failed, 90 passed, 2 skipped`; failures covered the missing five-mode vocabulary, LAN remote fixture policy, schema-v2 orchestration API, UUID policies, recursive HMAC, and CLI alias.
- `test_cuda_evidence_sanitizer.py`: `6 failed, 16 passed`; both LAN modes were rejected before cardinality policy could run.

## GREEN

- Added strict schema-v2 LAN node/orchestration models and an atomic preflight writer.
- Added explicit local, external-LAN, strict-LAN, and distinct-GPU policies without aliasing legacy `distributed`.
- Added strict LAN topology, fixture, mode, controller commit, injectable controller identity, token, worker commit, timestamp, and exact node-set verification with no schema-v1 downgrade.
- Added LAN remote fixture handling, topology-owner endpoint binding, shared/distributed UUID policies, and single-only GPT artifact comparison.
- Added token-derived recursive HMAC evidence sanitization after successful v2 verification; raw tokens and identities are not persisted.
- Extended the protected sanitizer with one-source `lan-shared` and three-source `lan-distributed` fail-closed GPU evidence policies.
- Targeted verification: `144 passed, 3 skipped`.
- Full backend verification: `864 passed, 6 skipped`.
- `.venv/bin/python -m compileall -q backend/app backend/tests scripts/sanitize-cuda-evidence.py`: passed.
- `git diff --check`: passed.

## Compatibility

- Preserved all five exact mode values, including legacy `distributed` as a separate schema-v1 mode.
- Preserved `DistributedOrchestrationPreflight`, `_verify_distributed_orchestration()`, `distributed_orchestration_verified`, `distributed_preflight`, `--distributed-preflight`, and `TTS_MORE_DISTRIBUTED_ORCHESTRATION_TOKEN`.
- Preserved certifiable/diagnostic/core-failure/post-core behavior, closed certification statuses, atomic runner evidence, controller GPU evidence, cleanup gating, and bundle allowlisting.
- PowerShell and workflow files were intentionally not modified.

## Commit

- Parent: `ef9451e`.
- Atomic commit message: `feat: add LAN policies to CUDA validation`.
- The final commit SHA is reported with task completion because this report is part of that commit.

## Concerns

- None within Task 3 scope. The PowerShell/workflow schema-v2 producer and LAN launch path remain intentionally deferred to the later orchestration task.

## Review Remediation

### RED

- Baseline before review fixes: `140 passed, 2 skipped` for the CUDA runner and protected sanitizer suites.
- Review regression suite: `15 failed, 140 passed, 2 skipped`.
- Failures reproduced all requested findings: malformed HMAC prefix bypass, missing runner worker manifest, validation-error sentinel leakage, non-boolean/missing orchestration verification, arbitrary worker directory substitution, and copied remote GPU evidence.
- A separate mode-binding test failed because a `lan-shared` source summary could be presented to the `lan-distributed` sanitizer path.
- The strengthened anonymity assertion then exposed a label collision where generated `worker-N` labels could equal real topology node names.

### GREEN

- LAN raw summaries now receive `orchestration_workers` only after successful schema-v2 and strict topology-policy verification.
- The protected sanitizer requires source `orchestration_verified is True`, exact source/CLI mode agreement, a valid expected worker manifest, an exact one-to-one directory set, required GPU CSV files, policy cardinality, and non-duplicated distributed source content before anonymization.
- Sanitized output retains only boolean verification state and `remote-N` labels; raw worker node names and GPU UUIDs are excluded.
- Legacy `distributed` retains its schema-v1 fields and its existing remote-source collection/count path without a new v2 requirement.
- LAN topology, controller-identity provider, and schema-v2 validation failures now emit fixed bounded errors without exception details or Pydantic `input_value` content.
- HMAC idempotence now requires the full canonical form `hmac-sha256:[0-9a-f]{64}`; all malformed variants are re-HMACed recursively.
- Targeted verification: `160 passed, 3 skipped`.
- Full backend verification: `880 passed, 6 skipped`.
- `.venv/bin/python -m compileall -q backend/app backend/tests scripts/sanitize-cuda-evidence.py`: passed.
- `git diff --check`: passed.

### Review Commit

- Parent: `2f4d1f9e7be0da66ccfdc17cc975cde980dd3646`.
- Atomic commit message: `fix: bind LAN evidence to verified topology`.
- Final SHA is reported with completion because this report is included in the commit.

### Review Concerns

- None within Task 3 review scope. PowerShell/workflow integration remains intentionally deferred and unchanged.

## Medium 1 Re-review

### RED

- The legacy distributed regression failed because protected CSV sources were emitted as `remote-1`, `remote-2`, and `remote-3` instead of the deployed `worker-N` labels.

### GREEN

- Source labels now branch only on strict LAN membership: `lan-shared` and `lan-distributed` use collision-resistant `remote-N`; legacy `distributed` retains `worker-N`.
- Legacy distributed received no schema-v2, `orchestration_verified`, or worker-manifest requirement.
- Protected sanitizer targeted verification: `31 passed`.
- `.venv/bin/python -m compileall -q backend/tests/test_cuda_evidence_sanitizer.py scripts/sanitize-cuda-evidence.py`: passed.
- `git diff --check`: passed.

### Commit

- Parent: `962e4074198053289c9d5d8dc0826e51fb3f18de`.
- Atomic commit message: `fix: preserve legacy distributed evidence labels`.
- Final SHA is reported with completion because this report is included in the commit.

### Concerns

- None.
