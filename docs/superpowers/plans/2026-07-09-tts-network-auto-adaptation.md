# TTS Network Auto Adaptation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build automatic local-network-aware TTS dependency and model source selection, with faster first-time repository setup and shared caches.

**Architecture:** Keep all network selection in `scripts/tts_more_deploy.py` and make the platform wrappers consume its JSON output. The helper owns source probing, cache environment generation, clone command generation, doctor diagnostics, and the `probe-network` CLI. The PowerShell and Bash wrappers remain thin orchestration layers that pass the resolved full-quality model source into upstream installers without modifying upstream repositories.

**Tech Stack:** Python 3.11 standard library (`argparse`, `json`, `urllib.request`, `datetime`, `subprocess`, `pathlib`), pytest, PowerShell, Bash, Git.

## Global Constraints

- `scripts/prepare-tts-repos.ps1` and `scripts/prepare-tts-repos.sh` must default to `Auto`.
- China-accessible sources are preferred when they are healthy.
- Global sources are automatic fallbacks when domestic sources fail.
- Upstream TTS repositories remain unmodified.
- Recommended setup must not download, select, recommend, or configure quantized, distilled, simplified, small, low-memory, or quality-reduced models.
- If full-quality resources cannot be downloaded, the installer stops and reports the missing resource.
- Shared cache root defaults to `data/cache/` and can be overridden with `TTS_MORE_CACHE_ROOT`.
- Existing `local-all`, `app-only`, and `worker-node` service rendering must remain compatible.
- Follow TDD: every production behavior below starts with a failing test.

---

## File Structure

- Modify `scripts/tts_more_deploy.py`
  - Add network probe candidate constants.
  - Add cache environment helpers.
  - Add `resolve_network_profile()`, `network_env_from_profile()`, and `probe_network()` command support.
  - Add `doctor()` network and cache diagnostics.
  - Add shallow/partial clone command generation and fallback.
- Modify `scripts/prepare-tts-repos.ps1`
  - Default `Source` to `Auto`.
  - Call `probe-network --write`.
  - Export profile env vars.
  - Pass resolved `ModelScope`, `HF-Mirror`, or `HF` into TTS-specific install and download commands.
- Modify `scripts/prepare-tts-repos.sh`
  - Mirror the PowerShell behavior for POSIX environments.
- Modify `backend/tests/test_deploy_tool.py`
  - Add tests for network profile selection, cache env, doctor diagnostics, clone command generation, and fallback.
- Add `backend/tests/test_prepare_scripts.py`
  - Static/script-contract tests for Auto defaults, profile probing, env propagation, and no reduced-model defaults.
- Modify `README.md`, `docs/deployment.md`, `docs/open-source-tts-services.md`, `.env.example`
  - Document `Auto`, source override variables, cache paths, fallback behavior, and full-quality model baseline policy.

---

### Task 1: Network Profile Core

**Files:**
- Modify: `backend/tests/test_deploy_tool.py`
- Modify: `scripts/tts_more_deploy.py`

**Interfaces:**
- Consumes: `write_json(path: Path, payload: Any) -> None`
- Produces:
  - `resolve_network_profile(root: Path = PROJECT_ROOT, *, mode: str = "auto", source: str = "Auto", timeout_seconds: float = 2.0, ttl_hours: float = 24.0, force: bool = False, probe_func: Callable[[str, float], dict[str, Any]] | None = None, environ: Mapping[str, str] | None = None) -> dict[str, Any]`
  - `network_env_from_profile(profile: dict[str, Any]) -> dict[str, str]`
  - `_cache_paths(root: Path, environ: Mapping[str, str] | None = None) -> dict[str, str]`

- [ ] **Step 1: Write failing tests for domestic source selection and cache env**

Add these imports near the top of `backend/tests/test_deploy_tool.py`:

```python
import os
```

Append this test:

```python
def test_resolve_network_profile_prefers_healthy_domestic_source(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)

    timings = {
        "https://www.modelscope.cn": {"ok": True, "latency_ms": 40},
        "https://hf-mirror.com": {"ok": True, "latency_ms": 80},
        "https://huggingface.co": {"ok": True, "latency_ms": 240},
        "https://mirrors.aliyun.com/pypi/simple": {"ok": True, "latency_ms": 35},
        "https://pypi.org/simple": {"ok": True, "latency_ms": 260},
    }

    def fake_probe(url: str, timeout_seconds: float) -> dict[str, object]:
        result = timings[url]
        return {"url": url, "ok": result["ok"], "latency_ms": result["latency_ms"], "error": ""}

    profile = deploy.resolve_network_profile(
        tmp_path,
        mode="auto",
        source="Auto",
        force=True,
        probe_func=fake_probe,
        environ={},
    )

    assert profile["mode"] == "auto"
    assert profile["model_source"] == "ModelScope"
    assert profile["hf_endpoint"] == ""
    assert profile["pip_index_url"] == "https://mirrors.aliyun.com/pypi/simple"
    assert profile["cache_root"] == "data/cache"
    env = deploy.network_env_from_profile(profile)
    assert env["PIP_INDEX_URL"] == "https://mirrors.aliyun.com/pypi/simple"
    assert env["PIP_CACHE_DIR"].endswith(os.path.join("data", "cache", "pip"))
    assert env["HF_HOME"].endswith(os.path.join("data", "cache", "huggingface"))
    assert env["MODELSCOPE_CACHE"].endswith(os.path.join("data", "cache", "modelscope"))
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_deploy_tool.py::test_resolve_network_profile_prefers_healthy_domestic_source -q
```

Expected: `AttributeError: module 'tts_more_deploy' has no attribute 'resolve_network_profile'`.

- [ ] **Step 3: Implement cache helpers and profile selection**

In `scripts/tts_more_deploy.py`, update imports:

```python
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen
```

Add these constants after `PROVIDER_PRIORITY`:

```python
NETWORK_PROFILE_RELATIVE_PATH = Path("data/local/network-profile.json")
DEFAULT_CACHE_RELATIVE_PATH = Path("data/cache")
NETWORK_PROFILE_SCHEMA_VERSION = 1

MODEL_SOURCE_CANDIDATES = [
    {"name": "ModelScope", "url": "https://www.modelscope.cn", "scope": "china", "hf_endpoint": ""},
    {"name": "HF-Mirror", "url": "https://hf-mirror.com", "scope": "china", "hf_endpoint": "https://hf-mirror.com"},
    {"name": "HF", "url": "https://huggingface.co", "scope": "global", "hf_endpoint": ""},
]

PIP_INDEX_CANDIDATES = [
    {"name": "aliyun", "url": "https://mirrors.aliyun.com/pypi/simple", "scope": "china"},
    {"name": "pypi", "url": "https://pypi.org/simple", "scope": "global"},
]
```

Add these functions before `load_repo_lock()`:

```python
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _cache_paths(root: Path, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    environ = environ or os.environ
    raw_root = environ.get("TTS_MORE_CACHE_ROOT", "")
    cache_root = Path(raw_root) if raw_root else root / DEFAULT_CACHE_RELATIVE_PATH
    if not cache_root.is_absolute():
        cache_root = root / cache_root
    cache_root = cache_root.resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    try:
        rel_cache_root = cache_root.relative_to(root_resolved).as_posix()
    except ValueError:
        rel_cache_root = str(cache_root)
    paths = {
        "cache_root": rel_cache_root,
        "pip_cache_dir": str(cache_root / "pip"),
        "uv_cache_dir": str(cache_root / "uv"),
        "hf_home": str(cache_root / "huggingface"),
        "huggingface_hub_cache": str(cache_root / "huggingface" / "hub"),
        "transformers_cache": str(cache_root / "huggingface" / "transformers"),
        "modelscope_cache": str(cache_root / "modelscope"),
        "torch_cache_dir": str(cache_root / "torch"),
        "downloads_dir": str(cache_root / "downloads"),
    }
    return paths


def _probe_url(url: str, timeout_seconds: float) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    request = Request(url, method="HEAD", headers={"User-Agent": "tts-more-deploy/1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", 200))
        latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        return {"url": url, "ok": 200 <= status < 500, "latency_ms": latency_ms, "error": ""}
    except Exception as exc:
        latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        message = str(exc.reason) if isinstance(exc, URLError) and getattr(exc, "reason", None) else str(exc)
        return {"url": url, "ok": False, "latency_ms": latency_ms, "error": message}


def _candidate_allowed(candidate: dict[str, str], mode: str) -> bool:
    if mode == "china":
        return candidate["scope"] == "china"
    if mode == "global":
        return candidate["scope"] == "global"
    return True


def _choose_candidate(
    candidates: list[dict[str, str]],
    probes: dict[str, dict[str, Any]],
    mode: str,
) -> dict[str, str] | None:
    healthy = [
        candidate
        for candidate in candidates
        if _candidate_allowed(candidate, mode) and probes[candidate["url"]].get("ok")
    ]
    if mode == "auto":
        domestic = [item for item in healthy if item["scope"] == "china"]
        if domestic:
            return min(domestic, key=lambda item: int(probes[item["url"]]["latency_ms"]))
    if healthy:
        return min(healthy, key=lambda item: int(probes[item["url"]]["latency_ms"]))
    if mode == "china":
        global_healthy = [
            candidate for candidate in candidates if candidate["scope"] == "global" and probes[candidate["url"]].get("ok")
        ]
        if global_healthy:
            return min(global_healthy, key=lambda item: int(probes[item["url"]]["latency_ms"]))
    return None


def _probe_all_candidates(
    timeout_seconds: float,
    probe_func: Callable[[str, float], dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    probe = probe_func or _probe_url
    probes: dict[str, dict[str, Any]] = {}
    for candidate in [*MODEL_SOURCE_CANDIDATES, *PIP_INDEX_CANDIDATES]:
        url = candidate["url"]
        if url not in probes:
            probes[url] = probe(url, timeout_seconds)
    return probes


def _profile_from_choices(
    root: Path,
    *,
    mode: str,
    model_candidate: dict[str, str],
    pip_candidate: dict[str, str],
    probes: dict[str, dict[str, Any]],
    environ: Mapping[str, str],
) -> dict[str, Any]:
    now = _utc_now()
    cache_paths = _cache_paths(root, environ)
    model_source_override = environ.get("TTS_MORE_MODEL_SOURCE", "")
    pip_index_override = environ.get("TTS_MORE_PIP_INDEX_URL", "")
    hf_endpoint_override = environ.get("TTS_MORE_HF_ENDPOINT", "")
    model_source = model_source_override if model_source_override and model_source_override != "Auto" else model_candidate["name"]
    hf_endpoint = hf_endpoint_override if hf_endpoint_override else (model_candidate.get("hf_endpoint") or "")
    pip_index_url = pip_index_override if pip_index_override else pip_candidate["url"]
    profile = {
        "schema_version": NETWORK_PROFILE_SCHEMA_VERSION,
        "mode": mode,
        "model_source": model_source,
        "hf_endpoint": hf_endpoint,
        "pip_index_url": pip_index_url,
        "extra_pip_index_url": environ.get("TTS_MORE_EXTRA_PIP_INDEX_URL", ""),
        "pytorch_index_strategy": "official",
        "cache_root": cache_paths["cache_root"],
        "cache_paths": cache_paths,
        "created_at": _isoformat(now),
        "expires_at": _isoformat(now + timedelta(hours=24)),
        "probes": list(probes.values()),
    }
    profile["env"] = network_env_from_profile(profile)
    return profile


def network_env_from_profile(profile: dict[str, Any]) -> dict[str, str]:
    cache_paths = profile.get("cache_paths") or {}
    env = {
        "PIP_CACHE_DIR": str(cache_paths.get("pip_cache_dir", "")),
        "UV_CACHE_DIR": str(cache_paths.get("uv_cache_dir", "")),
        "HF_HOME": str(cache_paths.get("hf_home", "")),
        "HUGGINGFACE_HUB_CACHE": str(cache_paths.get("huggingface_hub_cache", "")),
        "TRANSFORMERS_CACHE": str(cache_paths.get("transformers_cache", "")),
        "MODELSCOPE_CACHE": str(cache_paths.get("modelscope_cache", "")),
        "TORCH_HOME": str(cache_paths.get("torch_cache_dir", "")),
    }
    if profile.get("pip_index_url"):
        env["PIP_INDEX_URL"] = str(profile["pip_index_url"])
        env["UV_INDEX_URL"] = str(profile["pip_index_url"])
    if profile.get("extra_pip_index_url"):
        env["PIP_EXTRA_INDEX_URL"] = str(profile["extra_pip_index_url"])
    if profile.get("hf_endpoint"):
        env["HF_ENDPOINT"] = str(profile["hf_endpoint"])
    return {key: value for key, value in env.items() if value}


def resolve_network_profile(
    root: Path = PROJECT_ROOT,
    *,
    mode: str = "auto",
    source: str = "Auto",
    timeout_seconds: float = 2.0,
    ttl_hours: float = 24.0,
    force: bool = False,
    probe_func: Callable[[str, float], dict[str, Any]] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environ = environ or os.environ
    mode = environ.get("TTS_MORE_NETWORK_PROFILE", mode).lower()
    source = environ.get("TTS_MORE_MODEL_SOURCE", source)
    if mode not in {"auto", "china", "global"}:
        raise ValueError(f"unsupported network profile mode: {mode}")
    if source not in {"Auto", "ModelScope", "HF-Mirror", "HF"}:
        raise ValueError(f"unsupported model source: {source}")
    probes = _probe_all_candidates(timeout_seconds, probe_func)
    model_candidate = next((item for item in MODEL_SOURCE_CANDIDATES if item["name"] == source), None)
    if model_candidate is None:
        model_candidate = _choose_candidate(MODEL_SOURCE_CANDIDATES, probes, mode)
    pip_candidate = _choose_candidate(PIP_INDEX_CANDIDATES, probes, mode)
    if model_candidate is None or pip_candidate is None:
        failed = [f"{url}: {result.get('error') or 'unreachable'}" for url, result in probes.items() if not result.get("ok")]
        raise RuntimeError("no usable network source found; " + "; ".join(failed))
    return _profile_from_choices(
        root,
        mode=mode,
        model_candidate=model_candidate,
        pip_candidate=pip_candidate,
        probes=probes,
        environ={**environ, "TTS_MORE_MODEL_SOURCE": source},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_deploy_tool.py::test_resolve_network_profile_prefers_healthy_domestic_source -q
```

Expected: `1 passed`.

- [ ] **Step 5: Commit**

```powershell
git add backend\tests\test_deploy_tool.py scripts\tts_more_deploy.py
git commit -m "feat(deploy): resolve network source profile"
```

---

### Task 2: Fallback, Manual Source, CLI, and Doctor Diagnostics

**Files:**
- Modify: `backend/tests/test_deploy_tool.py`
- Modify: `scripts/tts_more_deploy.py`

**Interfaces:**
- Consumes:
  - `resolve_network_profile(root: Path = PROJECT_ROOT, *, mode: str = "auto", source: str = "Auto", timeout_seconds: float = 2.0, ttl_hours: float = 24.0, force: bool = False, probe_func: Callable[[str, float], dict[str, Any]] | None = None, environ: Mapping[str, str] | None = None) -> dict[str, Any]`
  - `network_env_from_profile(profile: dict[str, Any]) -> dict[str, str]`
- Produces:
  - `probe_network(root: Path = PROJECT_ROOT, *, mode: str = "auto", source: str = "Auto", write: bool = False, force: bool = False, timeout_seconds: float = 2.0, ttl_hours: float = 24.0, output: str | None = None) -> dict[str, Any]`
  - `doctor(root: Path = PROJECT_ROOT) -> dict[str, Any]` includes `network_profile` and `cache_paths`
  - CLI subcommand `probe-network`

- [ ] **Step 1: Write failing tests for fallback and manual override**

Append these tests to `backend/tests/test_deploy_tool.py`:

```python
def test_resolve_network_profile_falls_back_to_global_when_domestic_fails(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)

    def fake_probe(url: str, timeout_seconds: float) -> dict[str, object]:
        if url in {"https://www.modelscope.cn", "https://hf-mirror.com", "https://mirrors.aliyun.com/pypi/simple"}:
            return {"url": url, "ok": False, "latency_ms": 2000, "error": "timeout"}
        return {"url": url, "ok": True, "latency_ms": 90, "error": ""}

    profile = deploy.resolve_network_profile(
        tmp_path,
        mode="auto",
        source="Auto",
        force=True,
        probe_func=fake_probe,
        environ={},
    )

    assert profile["model_source"] == "HF"
    assert profile["hf_endpoint"] == ""
    assert profile["pip_index_url"] == "https://pypi.org/simple"


def test_manual_source_keeps_cache_env_and_skips_auto_source_choice(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)

    def fake_probe(url: str, timeout_seconds: float) -> dict[str, object]:
        return {"url": url, "ok": True, "latency_ms": 10, "error": ""}

    profile = deploy.resolve_network_profile(
        tmp_path,
        mode="auto",
        source="HF-Mirror",
        force=True,
        probe_func=fake_probe,
        environ={},
    )

    assert profile["model_source"] == "HF-Mirror"
    assert profile["hf_endpoint"] == "https://hf-mirror.com"
    env = deploy.network_env_from_profile(profile)
    assert env["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert "PIP_CACHE_DIR" in env
```

- [ ] **Step 2: Run tests to verify they fail before implementation completion**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_deploy_tool.py::test_resolve_network_profile_falls_back_to_global_when_domestic_fails backend\tests\test_deploy_tool.py::test_manual_source_keeps_cache_env_and_skips_auto_source_choice -q
```

Expected before Task 2 implementation: at least one assertion fails because fallback/manual behavior is incomplete.

- [ ] **Step 3: Add failing tests for `probe_network()` write and doctor diagnostics**

Append:

```python
def test_probe_network_writes_profile_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)

    def fake_resolve(root: Path, **kwargs: object) -> dict[str, object]:
        return {
            "schema_version": 1,
            "mode": "auto",
            "model_source": "ModelScope",
            "hf_endpoint": "",
            "pip_index_url": "https://mirrors.aliyun.com/pypi/simple",
            "cache_root": "data/cache",
            "cache_paths": {"pip_cache_dir": str(root / "data/cache/pip")},
            "env": {"PIP_CACHE_DIR": str(root / "data/cache/pip")},
            "probes": [],
        }

    monkeypatch.setattr(deploy, "resolve_network_profile", fake_resolve)

    profile = deploy.probe_network(tmp_path, write=True)

    profile_path = tmp_path / "data" / "local" / "network-profile.json"
    assert profile["model_source"] == "ModelScope"
    assert json.loads(profile_path.read_text(encoding="utf-8"))["env"]["PIP_CACHE_DIR"].endswith("pip")


def test_doctor_reports_network_profile_and_cache_paths(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    profile_path = tmp_path / "data" / "local" / "network-profile.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "auto",
                "model_source": "HF-Mirror",
                "cache_root": "data/cache",
                "cache_paths": {"pip_cache_dir": str(tmp_path / "data/cache/pip")},
                "env": {"HF_ENDPOINT": "https://hf-mirror.com"},
            }
        ),
        encoding="utf-8",
    )

    report = deploy.doctor(tmp_path)

    assert report["network_profile"]["model_source"] == "HF-Mirror"
    assert report["cache_paths"]["cache_root"] == "data/cache"
```

- [ ] **Step 4: Run tests to verify missing function/diagnostics**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_deploy_tool.py::test_probe_network_writes_profile_json backend\tests\test_deploy_tool.py::test_doctor_reports_network_profile_and_cache_paths -q
```

Expected: `AttributeError` for `probe_network` or `KeyError` for missing doctor fields.

- [ ] **Step 5: Implement profile writing, cached read, doctor fields, and CLI**

Add these helpers to `scripts/tts_more_deploy.py` after `resolve_network_profile()`:

```python
def _network_profile_path(root: Path) -> Path:
    return root / NETWORK_PROFILE_RELATIVE_PATH


def _read_network_profile(root: Path) -> dict[str, Any] | None:
    path = _network_profile_path(root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def probe_network(
    root: Path = PROJECT_ROOT,
    *,
    mode: str = "auto",
    source: str = "Auto",
    write: bool = False,
    force: bool = False,
    timeout_seconds: float = 2.0,
    ttl_hours: float = 24.0,
    output: str | None = None,
) -> dict[str, Any]:
    profile = resolve_network_profile(
        root,
        mode=mode,
        source=source,
        timeout_seconds=timeout_seconds,
        ttl_hours=ttl_hours,
        force=force,
    )
    if write:
        write_json(_network_profile_path(root), profile)
    if output:
        write_json(root / output, profile)
    return profile
```

Modify `doctor()` so the final return becomes:

```python
    network_profile = _read_network_profile(root) or {}
    cache_paths = _cache_paths(root)
    return {
        "repositories": reports,
        "extra_repo_dirs": extra_dirs,
        "network_profile": network_profile,
        "cache_paths": cache_paths,
    }
```

Add this parser block in `main()` after the `sync` parser:

```python
    probe = sub.add_parser("probe-network", help="Probe local network and choose install/download sources")
    probe.add_argument("--mode", choices=("auto", "china", "global"), default="auto")
    probe.add_argument("--source", choices=("Auto", "ModelScope", "HF", "HF-Mirror"), default="Auto")
    probe.add_argument("--write", action="store_true")
    probe.add_argument("--force", action="store_true")
    probe.add_argument("--timeout-seconds", type=float, default=2.0)
    probe.add_argument("--ttl-hours", type=float, default=24.0)
    probe.add_argument("--output", default=None)
```

Add this command branch before `doctor`:

```python
    if args.command == "probe-network":
        profile = probe_network(
            root,
            mode=args.mode,
            source=args.source,
            write=args.write,
            force=args.force,
            timeout_seconds=args.timeout_seconds,
            ttl_hours=args.ttl_hours,
            output=args.output,
        )
        print(json.dumps(profile, ensure_ascii=False, indent=2))
        return 0
```

- [ ] **Step 6: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_deploy_tool.py::test_resolve_network_profile_falls_back_to_global_when_domestic_fails backend\tests\test_deploy_tool.py::test_manual_source_keeps_cache_env_and_skips_auto_source_choice backend\tests\test_deploy_tool.py::test_probe_network_writes_profile_json backend\tests\test_deploy_tool.py::test_doctor_reports_network_profile_and_cache_paths -q
```

Expected: `4 passed`.

- [ ] **Step 7: Commit**

```powershell
git add backend\tests\test_deploy_tool.py scripts\tts_more_deploy.py
git commit -m "feat(deploy): add network probe diagnostics"
```

---

### Task 3: Faster Repository Sync With Partial Clone Fallback

**Files:**
- Modify: `backend/tests/test_deploy_tool.py`
- Modify: `scripts/tts_more_deploy.py`

**Interfaces:**
- Consumes: `sync_repos(root: Path, clean: bool = False, dry_run: bool = False) -> list[list[str]]`
- Produces:
  - `_clone_command(remote: str, branch: str, path: Path, *, partial: bool = True) -> list[str]`
  - `_run_git_command(command: list[str], *, cwd: Path) -> None`
  - `_run_clone_with_fallback(root: Path, remote: str, branch: str, path: Path, dry_run: bool, actions: list[list[str]]) -> None`

- [ ] **Step 1: Write failing test for shallow partial clone command**

Append:

```python
def test_sync_repos_dry_run_uses_shallow_partial_clone(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)

    actions = deploy.sync_repos(tmp_path, dry_run=True)

    clone = actions[0]
    assert clone[:3] == ["git", "clone", "--depth"]
    assert "1" in clone
    assert "--filter=blob:none" in clone
    assert "--single-branch" in clone
    assert "--branch" in clone
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_deploy_tool.py::test_sync_repos_dry_run_uses_shallow_partial_clone -q
```

Expected: assertion failure because current clone command lacks `--depth` and `--filter=blob:none`.

- [ ] **Step 3: Write failing test for partial clone fallback**

Append:

```python
def test_sync_repos_retries_clone_without_partial_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    deploy = _load_deploy_module(repo_root)
    _write_repo_lock(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command: list[str], cwd: Path) -> None:
        calls.append(command)
        if command[:2] == ["git", "clone"] and "--filter=blob:none" in command:
            raise deploy.subprocess.CalledProcessError(128, command)
        clone_path = Path(command[-1]) if command[:2] == ["git", "clone"] else None
        if clone_path:
            (clone_path / ".git").mkdir(parents=True)

    monkeypatch.setattr(deploy, "_run_git_command", fake_run)
    monkeypatch.setattr(deploy, "_git_output", lambda command: "bf81cdb14a38b674b6e9996dabc97340bc9978d2")
    (tmp_path / "repo.lock.json").write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "name": "GPT-SoVITS-main",
                        "provider_type": "gpt-sovits",
                        "variant": "main",
                        "path": "repo/GPT-SoVITS-main",
                        "remote": "https://github.com/XucroYuri/GPT-SoVITS.git",
                        "branch": "main",
                        "commit": "bf81cdb14a38b674b6e9996dabc97340bc9978d2",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    deploy.sync_repos(tmp_path, dry_run=False)

    assert any("--filter=blob:none" in command for command in calls)
    assert any(command[:2] == ["git", "clone"] and "--filter=blob:none" not in command for command in calls)
```

- [ ] **Step 4: Run test to verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_deploy_tool.py::test_sync_repos_retries_clone_without_partial_filter -q
```

Expected: `AttributeError` for `_run_git_command` or no fallback clone call.

- [ ] **Step 5: Implement clone command helpers and fallback**

Add these helpers before `sync_repos()`:

```python
def _clone_command(remote: str, branch: str, path: Path, *, partial: bool = True) -> list[str]:
    command = ["git", "clone", "--depth", "1"]
    if partial:
        command.append("--filter=blob:none")
    command.extend(["--branch", branch, "--single-branch", remote, str(path)])
    return command


def _run_git_command(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _run_clone_with_fallback(
    root: Path,
    *,
    remote: str,
    branch: str,
    path: Path,
    dry_run: bool,
    actions: list[list[str]],
) -> None:
    primary = _clone_command(remote, branch, path, partial=True)
    actions.append(primary)
    if dry_run:
        return
    try:
        _run_git_command(primary, cwd=root)
        return
    except subprocess.CalledProcessError:
        if path.exists():
            _remove_path(path)
    fallback = _clone_command(remote, branch, path, partial=False)
    actions.append(fallback)
    _run_git_command(fallback, cwd=root)
```

Update the `else` block inside `sync_repos()`:

```python
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            _run_clone_with_fallback(
                root,
                remote=remote,
                branch=branch,
                path=path,
                dry_run=dry_run,
                actions=actions,
            )
            commands = []
```

Update the command loop so it uses `_run_git_command()`:

```python
        for command in commands:
            actions.append(command)
            if not dry_run:
                _run_git_command(command, cwd=root)
```

Update the checkout run:

```python
                    _run_git_command(checkout_command, cwd=root)
```

- [ ] **Step 6: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_deploy_tool.py::test_sync_repos_dry_run_uses_shallow_partial_clone backend\tests\test_deploy_tool.py::test_sync_repos_retries_clone_without_partial_filter backend\tests\test_deploy_tool.py::test_sync_repos_rejects_paths_outside_project -q
```

Expected: `3 passed`.

- [ ] **Step 7: Commit**

```powershell
git add backend\tests\test_deploy_tool.py scripts\tts_more_deploy.py
git commit -m "feat(deploy): speed up repository sync"
```

---

### Task 4: PowerShell Prepare Script Auto Source Integration

**Files:**
- Create: `backend/tests/test_prepare_scripts.py`
- Modify: `scripts/prepare-tts-repos.ps1`

**Interfaces:**
- Consumes:
  - CLI: `scripts\tts_more_deploy.py probe-network --write --source <Auto|ModelScope|HF|HF-Mirror>`
  - Profile JSON fields: `model_source`, `env`
- Produces:
  - PowerShell default `[string]$Source = "Auto"`
  - `Resolve-NetworkProfile` function
  - `Set-NetworkProfileEnvironment` function
  - `$ResolvedSource` passed into GPT, IndexTTS, and CosyVoice source decisions

- [ ] **Step 1: Write failing script contract tests**

Create `backend/tests/test_prepare_scripts.py`:

```python
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_powershell_prepare_defaults_to_auto_and_calls_probe_network() -> None:
    script = (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")

    assert '[ValidateSet("Auto", "ModelScope", "HF", "HF-Mirror")]' in script
    assert '[string]$Source = "Auto"' in script
    assert '"probe-network"' in script
    assert '"--write"' in script
    assert "$ResolvedSource" in script


def test_prepare_scripts_do_not_default_to_reduced_models() -> None:
    combined = "\n".join(
        [
            (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8"),
            (REPO_ROOT / "scripts" / "prepare-tts-repos.sh").read_text(encoding="utf-8"),
        ]
    ).lower()

    forbidden = ["quantized", "distilled", "small", "low-memory", "int8", "fp8", "q4", "q8"]
    assert not any(token in combined for token in forbidden)
```

- [ ] **Step 2: Run first PowerShell contract test to verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_prepare_scripts.py::test_powershell_prepare_defaults_to_auto_and_calls_probe_network -q
```

Expected: assertion failure because the script still defaults to `ModelScope`.

- [ ] **Step 3: Update PowerShell source parameter and profile helpers**

In `scripts/prepare-tts-repos.ps1`, replace the source parameter line with:

```powershell
    [ValidateSet("Auto", "ModelScope", "HF", "HF-Mirror")][string]$Source = "Auto",
```

Add these functions after `Invoke-Logged`:

```powershell
function Invoke-Captured {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )
    $line = "$FilePath $($Arguments -join ' ')"
    Write-Host "[run] $line" -ForegroundColor Cyan
    if ($DryRun) { return "{}" }
    Push-Location $WorkingDirectory
    try {
        $output = & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) { throw "Command failed: $line" }
        return ($output -join "`n")
    } finally {
        Pop-Location
    }
}

function Resolve-NetworkProfile {
    $args = @("scripts\tts_more_deploy.py", "probe-network", "--write", "--source", $Source)
    if ($DryRun) {
        Write-Host "[run] $Python $($args -join ' ')" -ForegroundColor Cyan
        return [pscustomobject]@{
            model_source = if ($Source -eq "Auto") { "ModelScope" } else { $Source }
            env = [pscustomobject]@{}
        }
    }
    $json = Invoke-Captured $Python $args $Root
    return $json | ConvertFrom-Json
}

function Set-NetworkProfileEnvironment {
    param($Profile)
    if ($null -eq $Profile.env) { return }
    foreach ($property in $Profile.env.PSObject.Properties) {
        [Environment]::SetEnvironmentVariable($property.Name, [string]$property.Value, "Process")
    }
}
```

Add this block after function declarations and before the line that reads `repo.lock.json`:

```powershell
$NetworkProfile = Resolve-NetworkProfile
Set-NetworkProfileEnvironment $NetworkProfile
$ResolvedSource = [string]$NetworkProfile.model_source
if (-not $ResolvedSource) { $ResolvedSource = if ($Source -eq "Auto") { "ModelScope" } else { $Source } }
Write-Host "[network] source=$ResolvedSource cache=$($NetworkProfile.cache_root)" -ForegroundColor Cyan
```

- [ ] **Step 4: Pass `$ResolvedSource` through service preparation**

In `Prepare-GPTSoVITS`, replace both upstream installer source arguments:

```powershell
Invoke-Logged "powershell" @("-ExecutionPolicy", "Bypass", "-File", $installPs1, "-Device", $Device, "-Source", $ResolvedSource) $repoPath
```

```powershell
Invoke-Logged "bash" @($installSh, "--device", $Device, "--source", $ResolvedSource) $repoPath
```

In `Prepare-IndexTTS`, replace source selection:

```powershell
$sourceArg = if ($ResolvedSource -eq "ModelScope") { "modelscope" } else { "huggingface" }
if ($ResolvedSource -eq "HF-Mirror") { $env:HF_ENDPOINT = "https://hf-mirror.com" }
```

In `Prepare-CosyVoice`, replace source condition:

```powershell
if ($ResolvedSource -eq "ModelScope") {
```

and:

```powershell
if ($ResolvedSource -eq "HF-Mirror") { $env:HF_ENDPOINT = "https://hf-mirror.com" }
```

- [ ] **Step 5: Run PowerShell parser and contract tests**

Run:

```powershell
$null = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path scripts\prepare-tts-repos.ps1), [ref]$null, [ref]$null)
.\.venv\Scripts\python.exe -m pytest backend\tests\test_prepare_scripts.py::test_powershell_prepare_defaults_to_auto_and_calls_probe_network backend\tests\test_prepare_scripts.py::test_prepare_scripts_do_not_default_to_reduced_models -q
```

Expected: parser exits successfully and `2 passed`.

- [ ] **Step 6: Commit**

```powershell
git add backend\tests\test_prepare_scripts.py scripts\prepare-tts-repos.ps1
git commit -m "feat(deploy): auto-select source in powershell prepare"
```

---

### Task 5: Bash Prepare Script Auto Source Integration

**Files:**
- Modify: `backend/tests/test_prepare_scripts.py`
- Modify: `scripts/prepare-tts-repos.sh`

**Interfaces:**
- Consumes:
  - CLI: `scripts/tts_more_deploy.py probe-network --write --source <Auto|ModelScope|HF|HF-Mirror>`
  - Profile JSON fields: `model_source`, `env`
- Produces:
  - Bash default `SOURCE="Auto"`
  - `resolve_network_profile()`
  - `export_network_env()`
  - Resolved source passed to GPT, IndexTTS, and CosyVoice preparation

- [ ] **Step 1: Write failing Bash contract test**

Append to `backend/tests/test_prepare_scripts.py`:

```python
def test_bash_prepare_defaults_to_auto_and_calls_probe_network() -> None:
    script = (REPO_ROOT / "scripts" / "prepare-tts-repos.sh").read_text(encoding="utf-8")

    assert 'SOURCE="Auto"' in script
    assert "probe-network" in script
    assert "--write" in script
    assert "RESOLVED_SOURCE" in script
    assert "export_network_env" in script
```

- [ ] **Step 2: Run Bash contract test to verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_prepare_scripts.py::test_bash_prepare_defaults_to_auto_and_calls_probe_network -q
```

Expected: assertion failure because the script still defaults to `ModelScope`.

- [ ] **Step 3: Update Bash default and helpers**

In `scripts/prepare-tts-repos.sh`, replace:

```bash
SOURCE="ModelScope"
```

with:

```bash
SOURCE="Auto"
RESOLVED_SOURCE=""
NETWORK_PROFILE_JSON=""
```

Add these functions after `run()`:

```bash
run_capture() {
  echo "[run] $*" >&2
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '{"model_source":"%s","env":{}}\n' "$([[ "$SOURCE" == "Auto" ]] && echo ModelScope || echo "$SOURCE")"
    return 0
  fi
  "$@"
}

json_field_from_profile() {
  "$APP_PY" -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2], ""))' "$NETWORK_PROFILE_JSON" "$1"
}

export_network_env() {
  "$APP_PY" - "$NETWORK_PROFILE_JSON" <<'PY'
import json
import sys
profile = json.loads(sys.argv[1])
for key, value in (profile.get("env") or {}).items():
    print(f"{key}={value}")
PY
}

resolve_network_profile() {
  NETWORK_PROFILE_JSON="$(run_capture "$APP_PY" "$ROOT/scripts/tts_more_deploy.py" probe-network --write --source "$SOURCE")"
  while IFS='=' read -r key value; do
    [[ -n "$key" ]] && export "$key=$value"
  done < <(export_network_env)
  RESOLVED_SOURCE="$(json_field_from_profile model_source)"
  [[ -z "$RESOLVED_SOURCE" ]] && RESOLVED_SOURCE="$([[ "$SOURCE" == "Auto" ]] && echo ModelScope || echo "$SOURCE")"
  echo "[network] source=$RESOLVED_SOURCE"
}
```

Call the helper before optional repo sync:

```bash
resolve_network_profile
```

- [ ] **Step 4: Pass `$RESOLVED_SOURCE` through service preparation**

In `prepare_gpt()`, replace the installer call:

```bash
run bash "$repo_path/install.sh" --device "$DEVICE" --source "$RESOLVED_SOURCE"
```

In `prepare_index()`, replace source selection:

```bash
local source_arg="huggingface"
[[ "$RESOLVED_SOURCE" == "ModelScope" ]] && source_arg="modelscope"
[[ "$RESOLVED_SOURCE" == "HF-Mirror" ]] && export HF_ENDPOINT="https://hf-mirror.com"
```

In `prepare_cosy()`, replace source condition and HF Mirror export:

```bash
if [[ "$RESOLVED_SOURCE" == "ModelScope" ]]; then
```

```bash
[[ "$RESOLVED_SOURCE" == "HF-Mirror" ]] && export HF_ENDPOINT="https://hf-mirror.com"
```

- [ ] **Step 5: Run Bash syntax and contract tests**

Run:

```powershell
bash -n scripts/prepare-tts-repos.sh
.\.venv\Scripts\python.exe -m pytest backend\tests\test_prepare_scripts.py::test_bash_prepare_defaults_to_auto_and_calls_probe_network backend\tests\test_prepare_scripts.py::test_prepare_scripts_do_not_default_to_reduced_models -q
```

Expected: syntax check exits `0` and `2 passed`.

- [ ] **Step 6: Commit**

```powershell
git add backend\tests\test_prepare_scripts.py scripts\prepare-tts-repos.sh
git commit -m "feat(deploy): auto-select source in bash prepare"
```

---

### Task 6: Documentation and Environment Examples

**Files:**
- Modify: `backend/tests/test_prepare_scripts.py`
- Modify: `README.md`
- Modify: `docs/deployment.md`
- Modify: `docs/open-source-tts-services.md`
- Modify: `.env.example`

**Interfaces:**
- Consumes:
  - `probe-network`
  - `TTS_MORE_NETWORK_PROFILE`
  - `TTS_MORE_MODEL_SOURCE`
  - `TTS_MORE_CACHE_ROOT`
- Produces: User-facing documentation for Auto mode, source overrides, cache overrides, and full-quality baseline policy.

- [ ] **Step 1: Write failing documentation contract test**

Append:

```python
def test_docs_describe_auto_source_cache_and_full_quality_policy() -> None:
    docs = "\n".join(
        [
            (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
            (REPO_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8"),
            (REPO_ROOT / "docs" / "open-source-tts-services.md").read_text(encoding="utf-8"),
            (REPO_ROOT / ".env.example").read_text(encoding="utf-8"),
        ]
    )

    assert "probe-network" in docs
    assert "TTS_MORE_NETWORK_PROFILE" in docs
    assert "TTS_MORE_MODEL_SOURCE" in docs
    assert "TTS_MORE_CACHE_ROOT" in docs
    assert "full-quality" in docs
    assert "manual" in docs
```

- [ ] **Step 2: Run docs test to verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_prepare_scripts.py::test_docs_describe_auto_source_cache_and_full_quality_policy -q
```

Expected: assertion failure for missing `probe-network` or env keys.

- [ ] **Step 3: Update `.env.example`**

Add this block near existing deployment variables:

```dotenv
# TTS deployment network adaptation.
# auto probes domestic-friendly sources first, then falls back to global sources.
TTS_MORE_NETWORK_PROFILE=auto
TTS_MORE_MODEL_SOURCE=Auto
TTS_MORE_CACHE_ROOT=data/cache
# TTS_MORE_PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
# TTS_MORE_HF_ENDPOINT=https://hf-mirror.com
```

- [ ] **Step 4: Update README quickstart commands**

Replace examples that hard-code `-Source ModelScope` or `--source ModelScope` with:

```powershell
.\scripts\prepare-tts-repos.ps1 -SyncRepos -CleanRepos -Device CU128
```

```bash
bash scripts/prepare-tts-repos.sh --sync-repos --clean-repos --device CU128
```

Add this paragraph near the commands:

```markdown
The prepare scripts default to `Auto`: they run `probe-network`, prefer healthy China-accessible sources such as ModelScope or HF Mirror, and fall back to global Hugging Face/PyPI sources when needed. The default install prepares full-quality baseline models only; quantized, distilled, simplified, small, or low-memory variants are manual advanced options.
```

- [ ] **Step 5: Update deployment docs**

In `docs/deployment.md`, add a section:

```markdown
## Network Auto Mode

`Source` defaults to `Auto`. The wrapper calls:

```text
scripts/tts_more_deploy.py probe-network --write --source Auto
```

The generated profile is stored at `data/local/network-profile.json` and is ignored by git. Domestic-friendly sources are tried first for China mainland networks. If they fail, setup falls back to global Hugging Face and PyPI routes.

Override behavior:

- `TTS_MORE_NETWORK_PROFILE=auto|china|global`
- `TTS_MORE_MODEL_SOURCE=Auto|ModelScope|HF-Mirror|HF`
- `TTS_MORE_CACHE_ROOT=data/cache`
- `TTS_MORE_PIP_INDEX_URL=<custom pip index>`
- `TTS_MORE_HF_ENDPOINT=<custom Hugging Face endpoint>`

The recommended installer prepares full-quality baseline models. It does not auto-select quantized, distilled, simplified, small, or low-memory models.
```

- [ ] **Step 6: Update open source TTS services docs**

Add this paragraph near local worker setup:

```markdown
The local worker deployment profile is independent from source selection. App-only, worker-node, and local-all modes can use the same generated `data/local/network-profile.json`, or each machine can run `probe-network` locally so package indexes and model endpoints match its own network.
```

- [ ] **Step 7: Run docs test**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_prepare_scripts.py::test_docs_describe_auto_source_cache_and_full_quality_policy -q
```

Expected: `1 passed`.

- [ ] **Step 8: Commit**

```powershell
git add backend\tests\test_prepare_scripts.py README.md docs\deployment.md docs\open-source-tts-services.md .env.example
git commit -m "docs(deploy): document auto network setup"
```

---

### Task 7: Final Verification and Local Dry Runs

**Files:**
- Modify only if verification finds a defect in files touched by Tasks 1-6.

**Interfaces:**
- Consumes all task outputs.
- Produces verified working tree with tests, syntax checks, and dry-run command evidence.

- [ ] **Step 1: Run deploy tool tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_deploy_tool.py backend\tests\test_prepare_scripts.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run full backend tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q
```

Expected: all backend tests pass.

- [ ] **Step 3: Run Python compile check**

Run:

```powershell
.\.venv\Scripts\python.exe -m py_compile scripts\tts_more_deploy.py
```

Expected: exit code `0`.

- [ ] **Step 4: Run shell syntax checks**

Run:

```powershell
bash -n scripts/prepare-tts-repos.sh
bash -n scripts/start-service-workers.sh
$null = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path scripts\prepare-tts-repos.ps1), [ref]$null, [ref]$null)
$null = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path scripts\start-service-workers.ps1), [ref]$null, [ref]$null)
```

Expected: all commands exit successfully.

- [ ] **Step 5: Run safe deployment dry runs**

Run:

```powershell
.\.venv\Scripts\python.exe scripts\tts_more_deploy.py sync-repos --dry-run
.\.venv\Scripts\python.exe scripts\tts_more_deploy.py render-services --profile local-all --platform windows --output data\local\services.json
.\scripts\prepare-tts-repos.ps1 -Targets none -SkipInstall -SkipDownloads -DryRun
bash scripts/prepare-tts-repos.sh --targets none --skip-install --skip-downloads --dry-run
```

Expected:

- `sync-repos --dry-run` prints clone commands containing `--depth 1 --filter=blob:none`.
- `render-services` exits `0`.
- Both prepare dry runs print a `probe-network --write --source Auto` command and do not install dependencies or download models.

- [ ] **Step 6: Run doctor**

Run:

```powershell
.\.venv\Scripts\python.exe scripts\tts_more_deploy.py doctor
```

Expected: JSON includes `repositories`, `extra_repo_dirs`, `network_profile`, and `cache_paths`.

- [ ] **Step 7: Review changed defaults for model baseline policy**

Run:

```powershell
rg -n "quantized|distilled|simplified|small|low-memory|int8|fp8|q4|q8" scripts README.md docs\deployment.md docs\open-source-tts-services.md .env.example
```

Expected: no script default uses those terms. Documentation may mention them only in the manual advanced option policy.

- [ ] **Step 8: Commit verification fixes if any**

If Step 1-7 revealed defects and fixes were made:

```powershell
git add backend\tests scripts README.md docs .env.example
git commit -m "fix(deploy): verify auto network setup"
```

If no fixes were made, do not create an empty commit.

---

## Self-Review

Spec coverage:

- `Auto` default is covered by Tasks 4 and 5.
- China-first and global fallback are covered by Tasks 1 and 2.
- Shared caches are covered by Tasks 1, 2, 4, and 5.
- Faster clone is covered by Task 3.
- Doctor diagnostics are covered by Task 2.
- Documentation and env variables are covered by Task 6.
- Full-quality baseline and no reduced model defaults are covered by Tasks 4, 5, 6, and 7.
- Distributed deployment compatibility is preserved because service rendering is not changed, and Task 7 runs render-services.

Placeholder scan:

- No unresolved planning markers or unspecified edge-handling steps are present.

Type consistency:

- Later tasks consistently use `resolve_network_profile()`, `network_env_from_profile()`, `probe_network()`, and `$ResolvedSource` / `RESOLVED_SOURCE` as defined in earlier tasks.
