# Audit E: Upload, Reference Audio, and Logs Path Contracts

## Scope

- Branch: `dev-xu/macos-lan-cuda-validation`
- Base SHA: `794a2cbd46db1690ba3c673311412a364aa63340`
- Parent SHA at commit time: `cd9def689be5157af56368caa5cce6a2c864348a`
- Atomic commit SHA: reported with the completed task because a commit cannot contain its own final hash.
- Changes are limited to the seven allowed backend files plus this report.
- Concurrent changes in `net_guard.py`, `service_config.py`, `services.py`, and their tests were left untouched and excluded from staging.

## RED

Upload boundary tests initially produced `4 failed`: all three application upload endpoints called `UploadFile.read(-1)`, and a missing-project reference upload returned 200 after creating its directory.

Reference-audio contract tests initially produced `8 failed, 4 passed`: alias-only GPT and IndexTTS cases were not converted to the worker canonical field, while the pre-existing canonical cases already passed.

Logs path tests initially produced `22 failed, 3 passed`: empty/path-like names and symlink escapes were accepted by role-library and GPT worker sample discovery, while valid Unicode and unrelated cases passed.

## GREEN

- Application uploads read exactly `max_upload_bytes + 1`, preserve empty-file and suffix handling, and return 413 before writing oversized content.
- Project reference uploads validate project existence before resolving or creating the project reference-audio directory.
- GPT normalizes `reference_audio` or `voice` to `ref_audio_path`; IndexTTS normalizes `ref_audio_path` or `reference_audio` to `voice`. Existing canonical values win and all other parameters remain unchanged.
- Role-library and GPT worker model-sample lookup require a non-empty single directory name, reject POSIX/Windows path forms, and require resolved paths to remain under the configured logs root.
- Unicode role name `小品-斯月学杨师版` remains valid.

Verification:

- Upload regression selection: `6 passed`.
- Parameter contract selection: `12 passed`.
- Path contract selection: `25 passed`.
- `backend/tests/test_workers.py`, `backend/tests/test_role_library.py`, and `backend/tests/test_role_library_matching.py`: `64 passed`.
- `backend/tests/test_api.py`: `80 passed`.
- `.venv/bin/python -m compileall -q backend/app`: passed.
- `git diff --check`: passed.

## Focus Points

- No real GPU inference method signature was changed.
- Public logs-name APIs validate before invoking a service client.
- GPT-SoVITS fallback logs roots are both confined after `resolve()`; symlink escapes return 400.
- The only warning in test runs is the existing Starlette TestClient deprecation warning.
