# SDD ledger — plan: docs/superpowers/plans/2026-08-03-windows-private-recovery-namespace.md

## Setup

- Worktree: `F:\Code\Github\TTS_more\.worktrees\windows-comfyui-run-evidence`
- Branch: `dev-xu/windows-comfyui-run-evidence`
- Plan commit: `b008dfd588742c6576f7553b7756f63d8415af07`
- Design amendment commit: `25a4bc0f9b58ea8ff0977821af648a36376d2541`
- Execution mode: Subagent-Driven Development

## Tasks

- Task 1: pending — no-follow private namespace boundary
- Task 2: pending — bounded static/dynamic snapshots
- Task 3: pending — public evidence verification integration
- Task 4: pending — supervised launcher/supervisor integration
- Task 5: pending — explicit recovery cleanup
- Task 6: pending — deterministic gates and final readiness review

## Review loop

- Task 1: review NOT READY — I1 Windows validation accepted an in-root junction; I2 portable creation was pathname-based without descriptor-relative `O_NOFOLLOW`; M1 unused failure argument. Fix round 1 dispatched to the original implementer.
- Task 1: fix round 1/5 — I1/I2/M1 addressed in `95d231e`; scoped re-review pending.
- Task 1: complete (commits `b008dfd..95d231e`, review READY; I1/I2/M1 addressed).
- Task 2: review NOT READY — I1 `max_stable_member_bytes` and `max_snapshot_bytes` accepted values above fixed ceilings; fix round 1 dispatched.
- Task 2: fix round 1/5 — fixed ceilings and runtime defense-in-depth added in `eac0c16`; scoped re-review pending.
- Task 2: complete (commits `95d231e..eac0c16`, review READY; fixed ceilings verified).
- Task 3: review NOT READY — public terminal verification did not pass the run-boundary namespace identity; fix round 1 dispatched.
- Task 3: fix round 1/5 — identity propagation, CLI forwarding, and mismatch regression added in `79c33ac`; scoped re-review pending.
- Task 3: complete (commits `eac0c16..79c33ac`, review READY; boundary identity binding verified).
- Task 4: review NOT READY — private parent was leased but run-key leaf could be moved/replaced and path-based `os.rmdir` was used; fix round 1 dispatched before commit.
- Task 4: fix round 1/5 — run-key leaf identity and continuous lease added; cleanup uses held-handle delete with absent proof; leaf-swap regression RED→GREEN; commit `6bcc169`; scoped re-review pending.
- Task 4: re-review NOT READY — cleanup-success released the private leaf before finalize/terminal/CAS/current verification, leaving a post-delete same-name recreation window; fix round 2 dispatched.
- Task 4: fix round 2/5 — delete-pending leaf handle retained through terminal/CAS/current verification; post-delete recreation regressions added; commit `4defc33`; scoped re-review pending.
- Task 4: final review NOT READY — private observation ignored rogue top-level members outside `.p/.o/.h/.c`, allowing contradictory cleanup-failed current; fix round 3 dispatched.
- Task 4: fix round 3/5 — rogue top-level members now fail closed before snapshot/terminal/CAS; old current and sentinels remain unchanged; commit `7ab72ae`; scoped re-review pending.
- Task 4: re-review NOT READY — rogue member injected after snapshot was not revalidated before terminal/CAS/current; fix round 4 dispatched.
- Task 4: fix round 4/5 — committed snapshot exact-member-set and double stable no-follow checks added before publication, terminal, and CAS; pause-after-snapshot rogue regression preserves old current; commit `814bf12`; scoped re-review pending.
- Task 4: final review NOT READY — `_kind` was discarded, so same-name file↔directory replacement passed member-name checks and could publish current; fix round 5 dispatched.
- Task 4: fix round 5/5 — exact `(name, kind)` tuples are now committed and revalidated at publication, terminal, and CAS; type-swap regressions preserve old current; commit `5a728f2`; final scoped re-review pending.
- Task 4: complete (commits `79c33ac..5a728f2`, final scoped re-review READY; 158 passed, 1 skipped; 8 focused Windows checks passed; no Critical/Important findings).
- Task 5: implementation complete pending independent review (`46eb173a4f77880ab7b2defead83d5081aa0c11f`; recovery+private 47 passed, 1 skipped; recovery+private+public 123 passed, 1 skipped; parser/compile/diff clean).
- Task 5: review NOT READY — transparent/stale plan token, missing 8000 owner/query-failure fail-closed behavior, and absent public supervisor/run-result/lifecycle binding; fix round 1 dispatched.
- Task 5: fix round 1/5 — one-shot capability and execute-time fresh observations added; 8000/8188 and provider-failure checks fail closed; `.h` cross-binds canonical public `.l` plus supervisor/run-result (optional `.s` material); commit `e9bb0e4`; scoped re-review pending.
- Task 5: re-review NOT READY — capability store under shared `%TEMP%` is writable by other SIDs; plaintext cap leaks paths and token filename is forgeable; fix round 2 dispatched.
- Task 5: fix round 2/5 — protected owner-only capability store with DPAPI/owner-bound POSIX encryption, ACL/reparse checks, and no plaintext paths; commit `1ac4f12`; scoped re-review pending.
- Task 5: final review NOT READY — capability decode checked ACL/reparse only at entry; ACL mutation before `.claim`/consume remained a TOCTOU; fix round 3 dispatched.
- Task 5: fix round 3/5 — two-phase store identity/DACL/reparse revalidation around claim and consume; Windows delete-on-close claim rollback on drift; race regressions pass; commit `a42f26c`; final scoped re-review pending.
- Task 5: release review NOT READY — last pre-consume ACL check to pathname `os.unlink(.cap)` remains TOCTOU; real grant→unlink→restore bypass reproduced; fix round 4 dispatched.
- Task 5: fix round 4/5 — store identity handle + security notification and handle-relative cap disposition with cancellation on transient unsafe signal; real grant→restore regressions pass; commit `fe55e5d`; release re-review pending.
- Task 5: release re-review NOT READY — store handle identity was not bound to named path identity; parent rename/swap to exact-ACL replacement let decode consume replacement cap while original survived; fix round 5 dispatched.
- Task 5: fix round 5/5 — store and direct-parent DELETE/no-share-delete anchors plus fixed-path no-follow identity revalidation prevent rename/replacement replay; regression passes; commit `f7278f6`; final release review pending.
- Task 5: complete (commits `46eb173..f7278f6`, final release review READY; 198 passed, 1 skipped; 15 focused Windows checks; no Critical/Important; one non-blocking Minor recorded).
- Task 6: initial docs commit `f64b3fd` reported NOT READY (private suites twice green; plugin twice red; 3 inner fixture regressions; full backend bounded no-result; other static gates green).
- Task 6: correction round 1/5 — full-backend orphan cleanup and exact no-result status, plugin full SHA, and PS5.1 command bound in docs; commit `6c5ba28`; scoped correction re-review pending.
- Task 6: remediation fixture commit `cacf8b2` independently READY; plugin remediation `151e566..978a778` independently READY with review-docs commit `e6da700`; deterministic reruns green for private suites, plugin, reliability/supervisor, and static gates.
- Task 6: portable retirement test fix `96e3576` independently READY; short-basetemp full backend authoritative `7 failed, 1666 passed, 40 skipped, 2 warnings`, exactly the permitted seven host-capability causes; docs bindings committed `f50d1fc`, `32de9e5`, `6f96053`.
- Task 6: complete — final readiness review READY for deterministic code gates; live preflight/CUDA/three-engine/WAV/47-case matrix remain explicit opt-in and were not run because private fixture/model/runtime inputs are unset. Non-blocking cleanup Minor: task-owned `F:\t96r` and `F:\t96d` remain because recursive removal was rejected by execution policy; no active processes.
