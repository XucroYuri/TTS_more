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

## Round 3 re-review

### Result

`DONE_WITH_CONCERNS`

C1, I1, I2, and documentation M2 are closed in implementation and regression coverage. M1 is materially improved on POSIX logs and explicitly retained as a residual cross-platform concurrent parent-swap threat rather than overstated as race-free. The existing non-skipping Windows gate is unchanged; native success still requires a pushed GitHub Windows run.

### Round 3 RED log

- Baseline focused suite: `126 passed, 4 skipped in 4.98s`.
- C1 app runner RED: `10 failed, 108 deselected in 1.70s`. `GIT_CONFIG_COUNT` fsmonitor and the default `post-checkout` hook created marker files; checkout-local fsmonitor/hooksPath/sshCommand/credential helper/URL rewrite/filter/submodule/include config was accepted.
- C1 updater RED: generated updater executed the configured fsmonitor and reached real GitHub fetch/pull before failing (`1 failed in 10.49s`); the temporary test checkout was discarded. Subsequent RED tests hard-failed before network mutation.
- C1 policy consistency RED: all five `status/config/fetch/checkout/pull` cases retained a PATH-resolved `git` token instead of an absolute trusted executable (`5 failed`). Supplemental REDs covered `GIT_EXEC_PATH`/template/external-diff/SSL/protocol injection, `http.sslVerify`, `core.askPass`, and `extensions.worktreeConfig`.
- I1 RED: same-identity schema-3 manifest deleted a matching user file; no app-owned anchor existed; lost-anchor and explicit adoption APIs were absent (`4 failed, 118 deselected in 0.80s`).
- I2 RED: duplicate, nested, and case-equivalent selected paths were accepted; real clean reached the Git mutation sentinel; complete confirmation accepted aliases (`5 failed, 122 deselected in 0.29s`).
- M2 RED: expanded maintained-doc test found bare secondary `sync-repos`, update, prepare, render, worker, and doctor commands outside the three previously checked guides (`1 failed`).
- M1 RED: POSIX `logs_dir` was opened with `O_DIRECTORY` but not `O_NOFOLLOW`, and docs omitted the concurrent parent-swap/Windows-handle residual threat (`2 failed`).

### Round 3 closure mapping

| ID | GREEN closure |
|---|---|
| C1 | Every app-side Git path and generated updater path now uses the same hardened runner. It resolves trusted Git/SSH executables, strips repository/config/SSH/askpass/exec/template/diff/SSL/protocol injection variables, ignores system/global config, disables prompts/hooks/fsmonitor/credential helpers, fixes SSH to null config with ProxyCommand/LocalCommand disabled, and allowlists HTTPS/SSH protocols. Before Git reads a checkout, local config rejects executable or rewriting hooks/fsmonitor/askpass/ssh/credential/url/filter/diff/include/submodule/HTTP/remote/worktree-config keys. Marker execution tests and identical policy tests cover status, config, fetch, checkout, and pull. |
| I1 | Bundle deletion authority is anchored outside the target checkout at ignored app-owned `data/local/deployment-ownership/<service_id>.json`. Pending anchors are written before target mutation and completed anchors bind exact manifest bytes. Same-identity forged schema-3 manifests, forged pending state, missing/mismatched anchors, and modified files fail closed. `--adopt-existing` validates hashes and only creates an anchor; it performs no upgrade/delete/overwrite until a separate rerun. |
| I2 | The complete selected set is resolved before any Git/app/file mutation. Platform `normcase` canonical duplicates and ancestor/descendant paths are rejected with both service IDs. The gate runs during complete confirmation loading, sync, render, updater/bundle installation, and before `update_project` app fetch/pull. Dry and real clean both fail before mutation for conflicting sets. |
| M2 | The consistency test now enumerates nine maintained deployment documents, including `docs/deployment.md`, `docs/current-state-and-simplification-plan.md`, app deployment docs, and provider READMEs. Every standalone managed-local update/sync/prepare/render/install/start/doctor/one-click command explicitly passes the complete confirmation file; `app-only` and network-only commands remain separate. |
| M1 | POSIX worker logs now open `logs_dir` itself with `O_DIRECTORY | O_NOFOLLOW` and retain the dirfd for final no-follow creation. Documentation explicitly states that general pathname-based bundle/output replacements and Windows parent handles are not race-free. Full cross-platform handle-based parent protection remains open as a residual hardening concern. |

### Round 3 verification

- C1 focused policy group: `17 passed, 117 deselected in 1.30s`; later askpass/worktree-config additions: `2 passed`.
- I1 anchor/adoption group: `4 passed, 118 deselected in 0.70s`; complete bundle group: `12 passed` after updating the stronger unanchored-pending expectation.
- I2 selected-set group: `5 passed, 122 deselected in 0.11s`; update-before-app-Git coverage is included in final focused.
- M2 maintained-doc consistency and M1 docs/log tests: passed.
- Final focused deployment/docs: `156 passed, 4 skipped in 8.76s`.
- Final full backend (Python 3.11): `438 passed, 6 skipped, 1 warning in 27.62s`.
- `python -m compileall -q scripts backend`: exit 0.
- `bash -n` for both app wrappers and all provider prepare scripts: exit 0.
- Deployment/app JSON parse: 2 files validated.
- `git diff --check` and staged diff-checks: exit 0.
- Existing warning: FastAPI/Starlette `httpx` deprecation, unrelated to deployment.
- Native Windows/PowerShell was not executed on this macOS host. `.github/workflows/ci.yml` and its mandatory `windows-latest` deployment step were not weakened or bypassed.

### Round 3 commits

- `50ae51e` - hardened Git runner, app-owned bundle trust anchor, selected-set path gate, POSIX log hardening, and deployment regressions.
- `b0bc5fa` - maintained-doc confirmation contract, adoption/race documentation, and consistency tests.
- Report-only finalization commit is recorded in the final task response because a commit cannot contain its own SHA.

### Round 3 concerns

- General bundle/output parent replacement remains pathname-based; concurrent parent-swap resistance is not complete, and no Windows handle-based equivalent is implemented.
- Hardened Git intentionally ignores system/global Git config and rejects executable/rewrite-sensitive local config. Environments that depend on custom Git credential helpers, SSH config, or Git-configured CA/proxy settings must provide an explicitly supported trusted deployment configuration rather than relaxing this fail-closed policy.
- Native Windows evidence remains pending a pushed GitHub Actions run; no macOS skip is reported as Windows success.

## Round 4 re-review

### Result

`DONE_WITH_CONCERNS`

C1 and documentation M2 are closed in implementation and regression coverage. M1 remains the explicitly documented residual concurrent parent-swap threat. The existing non-skipping Windows CI gate remains required; this macOS run does not claim native Windows success.

### Round 4 RED log

- Review source: the latest `.superpowers/sdd/manifest-deployment-review.md` (`Critical 1 / Important 0 / Minor 2`), with Round 4 scoped to open C1 and partial M2 while preserving the M1 caveat.
- Initial C1 RED: `15 failed, 137 deselected in 3.79s`. The local-config audit invoked Git, `core.alternateRefsCommand` and unknown transport/helper/maintenance keys were not fail-closed, checkout-controlled PATH entries selected fake Git, updater sidecars remained schema 1 without bound executables, and updater path tampering was not revalidated.
- C1 trust-boundary RED: `2 failed, 11 passed, 140 deselected in 4.45s`. An allowlisted-shaped refspec could map `main` to a different remote-tracking name, and a sidecar could select an executable outside the checkout but outside fixed trusted installation directories.
- C1 Windows lookup RED: `1 failed, 156 deselected in 0.11s`. Fixed Windows candidates were derived from the Python/workspace drive and did not use the trusted Windows system-directory API.
- M2 valid RED: the maintained-doc consistency test failed with exactly two violations: `docs/workers.md` bare `start-service-workers.sh` and the P0 bare update/sync/doctor acceptance commands in `docs/current-state-and-simplification-plan.md`.

### Round 4 closure mapping

| ID | GREEN closure |
|---|---|
| C1 | App and generated updater parse `.git/config` directly with Python's non-interpolating strict parser before any Git executable call. The allowlist is limited to validated inert checkout metadata: required `core` repository/filesystem booleans, `remote "origin"` supported-GitHub URL/same-name fetch metadata and exact partial-clone metadata, plus validated `branch` origin/merge metadata. Every unknown section/key, duplicate normalized key, malformed/oversized/NUL config, unsafe allowlisted value, include/url/filter/diff/merge/gc/maintenance/submodule/HTTP/CA/curl/alternate-command key fails closed. Marker tests cover app/updater alternate refs and updater unknown config without execution. Git/SSH resolution never calls `shutil.which` or searches PATH/cwd: POSIX uses fixed absolute directories; Windows obtains the system root with `GetWindowsDirectoryW` and checks fixed Git/OpenSSH locations; custom paths require exact absolute `TTS_MORE_TRUSTED_GIT`/`TTS_MORE_TRUSTED_SSH`. Candidates and every ancestor must be real non-symlink/non-reparse executable files outside managed roots. Installer sidecar schema 2 binds the validated absolute Git/SSH paths; updater repeats fixed-directory-or-explicit-env, boundary, link/reparse, existence, and executable validation before config/status/fetch/checkout/pull. Fake `git`/`git.exe`, cwd/empty/relative PATH, sidecar tampering, and all five Git verbs are covered. |
| M2 | The nine-document consistency test now recognizes bare managed commands in command lines and inline code without requiring a `scripts/` prefix, while retaining explicit network-probe and `app-only` exclusions. It reports all violations together. The distributed worker steps now create/check a complete confirmation file and pass it to `start-service-workers.sh`; all P0 update/sync/doctor acceptance commands pass the same explicit confirmation. |
| M1 | No cross-platform handle-based expansion was attempted in Round 4. The existing deployment documentation continues to state that general bundle/output ancestor replacement and Windows parent handles remain raceable; POSIX final `logs_dir`/log entry no-follow protection remains in place. |

### Round 4 verification

- C1 policy group after initial implementation: `37 passed, 115 deselected in 3.02s`.
- M2 consistency GREEN: `1 passed, 22 deselected`.
- Final focused deployment/docs: `176 passed, 4 skipped in 8.40s`.
- Final full backend (Python 3.11): `458 passed, 6 skipped, 1 warning in 27.30s`.
- `python -m compileall -q scripts backend`: exit 0.
- Generated `tts-more-update.py` source compilation: exit 0.
- `bash -n` for the five maintained POSIX deployment wrappers: exit 0.
- Deployment/app JSON parse: 2 files validated.
- `git diff --check` and both functional staged diff-checks: exit 0.
- Existing warning: FastAPI/Starlette `httpx` deprecation, unrelated to deployment.
- Native PowerShell parser/runtime was unavailable (`pwsh` is not installed on this macOS host). Windows-only tests remain skipped locally. `.github/workflows/ci.yml` retains the mandatory non-skipping `windows-latest` deployment validation; its result requires a pushed CI run and is not inferred from macOS.

### Round 4 commits

- `f08a151b3bdca3f01357be95d7ad901100592b0d` - strict local Git config allowlist, trusted executable resolution/binding, generated updater parity, and C1 regressions.
- `e974ee41484bee22f3f61f8914914e9cfee2f24e` - bare managed-command documentation gate and the two M2 documentation fixes.
- Report-only finalization commit is recorded in the final task response because a commit cannot contain its own SHA.

### Round 4 concerns

- General concurrent ancestor replacement remains M1 residual; no Windows handle-based parent-chain protection is claimed.
- Native Windows/PowerShell execution evidence is pending the existing GitHub Actions Windows job. The cross-platform test is non-skipping, but this macOS run is not native evidence.
- Strict local config intentionally rejects otherwise common checkout-local customizations such as `user.*`, custom CA/proxy settings, maintenance, filters, merge/diff tools, submodules, includes, and extra remotes. Operators must remove them from managed checkouts rather than weakening the allowlist.
- When Git/SSH are installed outside fixed platform locations, the same explicit absolute `TTS_MORE_TRUSTED_GIT`/`TTS_MORE_TRUSTED_SSH` values used to install the updater must also be present when the generated updater runs; updater-side revalidation fails closed otherwise.
