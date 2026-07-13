# Manifest deployment fix report

## Result

`DONE_WITH_CONCERNS`

All Critical, Important, and requested Minor findings are closed in implementation and regression coverage. The remaining concern is verification-environment-only: this macOS host has neither `pwsh` nor Windows PowerShell, so the committed PowerShell injection execution test is skipped and native PowerShell parser/runtime validation could not run. PowerShell-facing static contract tests pass.

## Baseline and TDD log

- Baseline: `python3 -m pytest backend/tests/test_deploy_tool.py backend/tests/test_prepare_scripts.py -q` -> `50 passed in 0.15s`.
- Initial review RED: the same command after adding review tests -> `23 failed, 50 passed, 1 skipped`. Each review item had a failure attributable to the missing behavior before production changes.
- Additional I2 RED 1: `test_sync_repos_dry_run_does_not_create_nested_repo_parents` failed because dry-run created `repo/`.
- Additional I2 RED 2: `test_one_click_dry_run_includes_dependency_and_model_plan_without_writes` failed because `(cd "$repo_path" ...)` accessed a checkout that dry-run had correctly not cloned.
- Focused GREEN: `/Users/huachi/Code/08-TTS/TTS_more/.venv/bin/python -m pytest backend/tests/test_deploy_tool.py backend/tests/test_prepare_scripts.py -q` -> `75 passed, 1 skipped in 1.75s`.

The one skipped test is the committed `pwsh` adversarial-sidecar execution test; its POSIX counterpart executes and passes.

## Closure mapping

| ID | RED evidence | GREEN closure |
|---|---|---|
| C1 | One-click and prepare dry-run wrappers returned success for a real dirty checkout; precursor direct RED/GREEN is in `0c7cfd5`. | Both wrappers execute the preserving sync planner, never pass reset opt-in, and abort without changing dirty content. `sync_repos(force_reset=False)` remains the default; explicit reset remains confined to `update --force-reset-repos`. |
| C2 | Partial/ambiguous confirmation was accepted; duplicate service IDs and origin mismatch were not rejected. | Confirmation is a complete selected-service map keyed only by unique formal `service_id`; duplicate JSON keys, unknown aliases, missing entries, paths outside `<root>/repo/`, non-Git targets, and canonical origin mismatches fail closed. SSH/HTTPS forms normalize to the same identity. CLI render/list/install/start/update surfaces verify existing identity before use. |
| C3 | Adversarial display data appeared directly in generated Bash/PowerShell source; unsafe branch/commit values loaded. | Generated launchers are fixed source. Manifest values live in `tts-more-update.json`; fixed Python executes argv arrays, validates branch/commit again at runtime, verifies origin and dirty state, and treats name/remote as data. Bash adversarial execution proves no marker creation; equivalent PowerShell test is committed. |
| C4 | Symlinked bundle/update destinations and nested source symlinks were followed; no reparse helper existed. | All deployment writes use boundary checks, symlink/junction/reparse rejection, safe parent creation, and atomic temporary-file replacement. Bundle source links, target links, nested links, Git exclude redirects, JSON parent redirects, worker log redirects, and simulated Windows reparse points are covered. |
| I1 | Resolved Cosy model path was `<root>/pretrained_models/...`. | Rendered env is `<confirmed Cosy repo>/pretrained_models/CosyVoice-300M`; integration test passes it through `_resolve_env` and checks the final absolute path. |
| I2 | Wrapper dry-run skipped deployment children and showed no Git/bundle/render plan; later REDs exposed directory creation and nonexistent-cwd access. | Dry-run executes only dry-run-aware/read-only children, emits structured Git/bundle argv/actions and quoted package/model commands, renders services to stdout, checks dirty/origin state, and performs no writes. Full one-click plan covers app skip, Git, bundle, update helpers, GPT, IndexTTS, CosyVoice, model actions, and render. |
| I3 | Prepare scripts rebuilt accepted absolute paths using root joins. | `list-repos` emits canonical `absolute_path`; both prepare scripts consume it directly. Tests cover absolute POSIX paths with spaces and assert PowerShell no longer joins `$Root` to `$Repo.path`; native Windows `Path` handling preserves drive/UNC forms when they are inside the managed root. |
| I4 | Empty/unknown/mixed selectors returned an empty selection and could overwrite services with `[]`; duplicates were collapsed. | Central selector resolution rejects empty, unknown, mixed-invalid, duplicate, and zero-result selections before writes. Service JSON uses atomic replacement only after successful selection/render. |
| I5 | Merge copy retained stale owned files and `installed_at` changed output. | Schema 2 bundle manifests record sorted `owned_files`; upgrades remove only stale owned files, preserve user files, copy the exact current source, and omit timestamps. A two-version upgrade plus identical-input rerun proves stale removal and byte-stable manifest output. |
| M1 | IndexTTS/CosyVoice omitted `default_selected`, and missing flags selected by default. | Every lock entry has an explicit boolean; schema validation rejects missing/non-boolean flags and selection defaults fail closed. |
| M2 | Provider READMEs lacked concrete copy commands/layout/overwrite semantics. | All three READMEs include executable POSIX and PowerShell commands, `<repo>/tts-more` layout, both resulting entry points, same-name overwrite behavior, and automated stale-owned-file semantics. |

## Verification

- Deployment tests: `75 passed, 1 skipped`.
- Full backend with required Python 3.11: `357 passed, 3 skipped, 1 warning in 20.30s`.
- The first full-backend attempt with system Python 3.9 stopped at collection with 15 annotation errors; `backend/pyproject.toml` requires Python `>=3.10`. It was rerun successfully with the existing project Python 3.11 venv.
- `python -m compileall -q backend scripts`: exit 0.
- `bash -n` on both app wrappers and all three provider prepare scripts: exit 0.
- `python -m json.tool repo.lock.json`: exit 0.
- `git diff --check` and staged diff-check: exit 0.
- Machine-path/temporary-path scan of the tracked diff: no matches.
- Native PowerShell parser/runtime: unavailable (`pwsh` and `powershell` not installed); static PowerShell wrapper tests pass and the native adversarial execution test remains committed with a capability skip.

The full backend warning is an existing FastAPI/Starlette `httpx` deprecation warning and is unrelated to this change.

## Commits

- `0c7cfd5` - precursor C1 direct default-preservation fix (pre-existing at task start).
- `a97bce4` - complete manifest deployment review closure implementation, regression tests, and docs.
- Report-only finalization commit: recorded in the final task response because a commit cannot contain its own SHA.

## Concerns

- Native PowerShell execution and parser verification remains outstanding on a Windows/PowerShell-capable runner. No implementation finding remains open.

## Round 2 re-review

### RED log

- Re-review source: `.superpowers/sdd/manifest-deployment-review.md` at `CHANGES_REQUESTED` (`Critical 1 / Important 6 / Minor 2`).
- Remote/clean RED run: focused deployment suite reached `25 failed, 56 passed, 1 skipped` before manual interruption. The real `clean` dirty-check test demonstrated the defect by deleting the dirty checkout and beginning a network clone; the run was stopped to avoid further external work. Tests now replace Git mutation with a hard-failing stub until the preflight is fixed.
- Remaining trust-boundary/docs/CI RED run: `21 failed, 106 deselected in 0.94s`.
- C1/I1 RED: no GitHub remote parser existed; helper/local/file/credential/non-default-port/control cases reached manifest/updater Git surfaces; clone lacked an option terminator.
- I2 RED: clean dry-run produced fetch/pull while real clean deleted the whole managed area and produced clone; dirty and unrecognized selected targets were deleted before validation.
- I3 RED: schema-2/list-only ownership accepted hostile/cross-provider claims, deleted modified owned files, emitted no hashes, and had no interruption journal.
- I4 RED: `.git` symlink and gitdir-file cases invoked Git before metadata validation; corrupt metadata failed later as an origin mismatch.
- I5 RED: unsafe service IDs loaded, `_open_worker_log` did not exist, and logs were not bounded by a dedicated directory API.
- I6 RED: root README, open-source service docs, and worker docs omitted mandatory complete confirmation setup/arguments.
- M1 RED: docs had no explicit whole-bundle non-atomic/retry contract and interrupted upgrades left no recoverable pending state.
- M2 RED: existing Windows matrix had no explicit native deployment step or named PowerShell 5.1/pwsh/drive/junction/gitdir test nodes.
- Supplemental trust-chain RED: forged pending ownership, a locally modified file copied before interruption, and case-equivalent GitHub identities produced `3 failed in 0.52s`.
- Supplemental updater `.git` RED: the generated updater invoked fake Git for a `gitdir:` file instead of rejecting metadata first (`1 failed in 1.77s`).

### GREEN log and closure mapping

| ID | GREEN closure |
|---|---|
| C1 | Lock loading, clone planning, origin checks, and the fixed updater allow only GitHub HTTPS (default/443), SSH URL (`git`, default/22), and scp-style `git@github.com:` remotes. Helper/local/file/credential/control/encoded/query/fragment/unexpected-host-or-port values fail before Git; clone uses `--`. |
| I1 | Structured identity validates effective ports and normalizes documented default endpoints plus GitHub owner/repository case. Existing origins and updater sidecars use the same allowlist and identity rules. |
| I2 | Clean resolves only selected repositories, preflights real `.git`, origin, and clean state before any deletion, rejects dirty/unknown targets, preserves unselected/extra directories, emits explicit removal actions, and returns identical dry/real plans. |
| I3 | Bundle schema 3 requires exact schema/service/provider/source/source-hash/file-hash fields. Stale files are removed only when their current hash matches trusted ownership; modified files abort. Pending recovery is chained to the prior validated manifest, so forged journals cannot claim user files. |
| I4 | Main deployment and generated updater both reject `.git` symlink/reparse, gitdir-file/worktree/submodule, missing, corrupt, and outside-boundary metadata. Git environment redirect variables are removed before every Git subprocess. The supported policy is checkout-local `.git` directories only and is documented. |
| I5 | `service_id` uses a conservative lowercase ASCII grammar. Worker logs are opened strictly below `logs_dir`, with service-id validation, symlink/reparse checks, directory-relative no-follow open where supported, and Windows-safe fallback checks. |
| I6 | Root README, open-source service docs, and worker docs create and pass a complete confirmation file for every managed-local one-click/sync/prepare/render command. A consistency test parses all published command lines. |
| M1 | Documentation explicitly says the bundle is not atomic as a whole. A deterministic pending journal records old ownership and desired manifest before per-file writes; identical rerun recovery and refusal after intervening local edits are tested. |
| M2 | Existing Windows matrix now has a mandatory named native step. It asserts Windows PowerShell 5.1 and pwsh are present and runs both launchers plus real drive, local UNC share, junction/reparse, and gitdir-file cases. Native success remains pending the GitHub Windows runner; macOS results are not used as evidence. |

Supplemental GREEN: the three trust-chain/identity tests passed (`3 passed in 0.40s`); updater-focused tests passed (`4 passed, 1 skipped, 102 deselected in 0.76s`).

### Round 2 verification

- Focused deployment/docs: `126 passed, 4 skipped in 5.44s`. The skips are Windows-only tests on macOS.
- Full backend (Python 3.11): `408 passed, 6 skipped, 1 warning in 23.69s`.
- `python -m compileall -q scripts backend`: exit 0.
- `bash -n` for both app wrappers and all provider prepare scripts: exit 0.
- Deployment/app JSON parse: 2 files validated.
- `git diff --check` and both staged diff-checks: exit 0.
- Warning: existing FastAPI/Starlette `httpx` deprecation warning, unrelated to deployment.
- Native Windows/PowerShell: not executed on this macOS host. The non-skippable Windows CI step is committed but requires a pushed PR/branch run before M2 can be declared verified.

### Round 2 commits

- `07c7585` - deployment trust boundaries, clean planner, bundle recovery, and regression tests.
- `ad555b2` - confirmation/recovery documentation, consistency tests, and native Windows CI gate.
- Report-only finalization commit is recorded in the final task response because a commit cannot contain its own SHA.

### Round 2 concerns

- M2 native execution evidence is pending an actual GitHub `windows-latest` run. No macOS-based PowerShell or Windows path success is claimed.
