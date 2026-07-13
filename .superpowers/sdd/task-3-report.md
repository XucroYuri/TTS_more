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
