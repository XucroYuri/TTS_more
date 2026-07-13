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
