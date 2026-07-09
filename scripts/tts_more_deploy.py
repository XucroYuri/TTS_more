from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


PROVIDER_MODULES = {
    "gpt-sovits": "app.workers.gpt_sovits_worker:app",
    "indextts": "app.workers.indextts_worker:app",
    "cosyvoice": "app.workers.cosyvoice_worker:app",
}

PROVIDER_ENGINES = {
    "gpt-sovits": "gpt-sovits",
    "indextts": "indextts",
    "cosyvoice": "cosyvoice",
}

PROVIDER_CAPABILITIES = {
    "gpt-sovits": [
        "tts",
        "trained_weights_voice",
        "reference_audio_voice",
        "gpt-weights",
        "sovits-weights",
        "wav_output",
        "tts-more-worker",
    ],
    "indextts": [
        "tts",
        "reference_audio_voice",
        "emotion_text",
        "emotion_audio",
        "wav_output",
        "tts-more-worker",
    ],
    "cosyvoice": [
        "tts",
        "reference_audio_voice",
        "zero_shot_voice",
        "cross_lingual_voice",
        "style_instruction",
        "wav_output",
        "tts-more-worker",
    ],
}

PROVIDER_PRIORITY = {"gpt-sovits": 10, "indextts": 20, "cosyvoice": 30}

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


def load_repo_lock(root: Path = PROJECT_ROOT) -> list[dict[str, Any]]:
    path = root / "repo.lock.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("repositories") or [])


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
    return {
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


def _probe_url(url: str, timeout_seconds: float) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    request = Request(url, method="HEAD", headers={"User-Agent": "tts-more-deploy/1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", 200))
        latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        return {"url": url, "ok": 200 <= status < 400, "latency_ms": latency_ms, "error": ""}
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
            candidate
            for candidate in candidates
            if candidate["scope"] == "global" and probes[candidate["url"]].get("ok")
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


def _parse_expiry(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_cached_network_profile(root: Path) -> dict[str, Any] | None:
    path = root / NETWORK_PROFILE_RELATIVE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != NETWORK_PROFILE_SCHEMA_VERSION:
        return None
    expires_at = _parse_expiry(payload.get("expires_at"))
    if expires_at is None or expires_at <= _utc_now():
        return None
    return payload


def _network_profile_request_context(root: Path, mode: str, source: str, environ: Mapping[str, str]) -> dict[str, str]:
    cache_paths = _cache_paths(root, environ)
    return {
        "mode": mode,
        "source": source,
        "cache_root": cache_paths["cache_root"],
        "model_source": environ.get("TTS_MORE_MODEL_SOURCE", ""),
        "pip_index_url": environ.get("TTS_MORE_PIP_INDEX_URL", ""),
        "hf_endpoint": environ.get("TTS_MORE_HF_ENDPOINT", ""),
        "extra_pip_index_url": environ.get("TTS_MORE_EXTRA_PIP_INDEX_URL", ""),
    }


def _cached_network_profile_matches_request(
    profile: dict[str, Any],
    request_context: dict[str, str],
) -> bool:
    cached_context = profile.get("request_context")
    if not isinstance(cached_context, dict):
        return False
    for key, expected in request_context.items():
        if str(cached_context.get(key, "")) != expected:
            return False
    return True


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


def _profile_from_choices(
    root: Path,
    *,
    mode: str,
    model_candidate: dict[str, str],
    pip_candidate: dict[str, str],
    probes: dict[str, dict[str, Any]],
    ttl_hours: float,
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
        "request_context": _network_profile_request_context(root, mode, environ.get("TTS_MORE_MODEL_SOURCE", ""), environ),
        "created_at": _isoformat(now),
        "expires_at": _isoformat(now + timedelta(hours=ttl_hours)),
        "probes": list(probes.values()),
    }
    profile["env"] = network_env_from_profile(profile)
    return profile


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
    request_context = _network_profile_request_context(root, mode, source, environ)
    if not force:
        cached_profile = _load_cached_network_profile(root)
        if cached_profile is not None and _cached_network_profile_matches_request(cached_profile, request_context):
            return cached_profile
    probes = _probe_all_candidates(timeout_seconds, probe_func)
    model_candidate = next((item for item in MODEL_SOURCE_CANDIDATES if item["name"] == source), None)
    if model_candidate is None:
        model_candidate = _choose_candidate(MODEL_SOURCE_CANDIDATES, probes, mode)
    pip_candidate = _choose_candidate(PIP_INDEX_CANDIDATES, probes, mode)
    if model_candidate is None or pip_candidate is None:
        failed = [f"{url}: {result.get('error') or 'unreachable'}" for url, result in probes.items() if not result.get("ok")]
        raise RuntimeError("no usable network source found; " + "; ".join(failed))
    profile = _profile_from_choices(
        root,
        mode=mode,
        model_candidate=model_candidate,
        pip_candidate=pip_candidate,
        probes=probes,
        ttl_hours=ttl_hours,
        environ={**environ, "TTS_MORE_MODEL_SOURCE": source},
    )
    return profile


def _network_profile_path(root: Path) -> Path:
    return root / NETWORK_PROFILE_RELATIVE_PATH


def _read_network_profile(root: Path) -> dict[str, Any] | None:
    path = _network_profile_path(root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


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


def render_services(
    root: Path = PROJECT_ROOT,
    *,
    profile: str = "local-all",
    platform_name: str | None = None,
    host: str = "127.0.0.1",
    service_ids: set[str] | None = None,
    template: bool = False,
) -> list[dict[str, Any]]:
    platform_name = platform_name or _platform_name()
    repositories = [repo for repo in load_repo_lock(root) if _is_tts_repo(repo)]
    services: list[dict[str, Any]] = []
    for repo in repositories:
        service_id = str(repo.get("service_id") or _default_service_id(repo))
        if service_ids and service_id not in service_ids:
            continue
        provider = str(repo["provider_type"])
        port = int(repo.get("port") or _default_port(provider))
        is_external = profile == "app-only"
        service = {
            "service_id": service_id,
            "service_kind": "tts",
            "display_name": str(repo.get("display_name") or _display_name(repo)),
            "engine": PROVIDER_ENGINES[provider],
            "provider_type": provider,
            "source_profile": "lan_endpoint" if is_external and host not in {"127.0.0.1", "localhost", "::1"} else "local_endpoint",
            "catalog_provider": provider,
            "setup_state": "not_configured" if template else ("endpoint_unreachable" if is_external else "repo_found"),
            "api_contract": "tts-more-v1",
            "base_url": f"http://{host}:{port}",
            "mode": "external" if is_external else "local",
            "network_scope": "lan" if is_external and host not in {"127.0.0.1", "localhost", "::1"} else "localhost",
            "managed": not is_external,
            "enabled": not template,
            "poll_interval_seconds": 5,
            "repo_path": None if is_external else repo["path"],
            "start_command": [] if is_external else _start_command(repo, platform_name, port),
            "start_cwd": None if is_external else ".",
            "env": {} if is_external else _worker_env(repo, platform_name),
            "health_url": f"http://{host}:{port}/health",
            "resource_group": str(repo.get("resource_group") or _resource_group(repo)),
            "capacity": int(repo.get("capacity") or 1),
            "priority": int(repo.get("priority") or PROVIDER_PRIORITY[provider]),
            "capabilities": list(repo.get("capabilities") or PROVIDER_CAPABILITIES[provider]),
        }
        if provider == "cosyvoice":
            service["default_params"] = {"mode": "zero_shot", "response_format": "wav"}
        services.append(service)
    return services


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
    remote: str,
    branch: str,
    path: Path,
    dry_run: bool,
    actions: list[list[str]],
) -> None:
    path_existed_before = path.exists()
    primary = _clone_command(remote, branch, path, partial=True)
    actions.append(primary)
    if dry_run:
        return
    try:
        _run_git_command(primary, cwd=root)
        return
    except subprocess.CalledProcessError:
        if not path_existed_before and path.exists():
            _remove_path(path)
    fallback = _clone_command(remote, branch, path, partial=False)
    actions.append(fallback)
    _run_git_command(fallback, cwd=root)


def sync_repos(root: Path = PROJECT_ROOT, *, clean: bool = False, dry_run: bool = False) -> list[list[str]]:
    if clean:
        _remove_repo_dir(root, dry_run=dry_run)
    actions: list[list[str]] = []
    for repo in load_repo_lock(root):
        path = _resolve_project_path(root, str(repo["path"]))
        remote = str(repo["remote"])
        branch = str(repo["branch"])
        commit = repo.get("commit")
        if path.exists() and (path / ".git").exists():
            commands = [
                ["git", "-C", str(path), "fetch", "--prune", "origin", branch],
                ["git", "-C", str(path), "checkout", branch],
                ["git", "-C", str(path), "reset", "--hard", f"origin/{branch}"],
            ]
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
        if repo.get("submodules"):
            commands.append(["git", "-C", str(path), "submodule", "update", "--init", "--recursive"])
        for command in commands:
            actions.append(command)
            if not dry_run:
                _run_git_command(command, cwd=root)
        if commit:
            checkout_command = ["git", "-C", str(path), "checkout", str(commit)]
            fetch_command = ["git", "-C", str(path), "fetch", "origin", str(commit)]
            if dry_run:
                if not (path.exists() and (path / ".git").exists()):
                    actions.append(fetch_command)
                    actions.append(checkout_command)
                else:
                    head = _git_output(["git", "-C", str(path), "rev-parse", "HEAD"])
                    if head != str(commit):
                        actions.append(fetch_command)
                        actions.append(checkout_command)
            else:
                head = _git_output(["git", "-C", str(path), "rev-parse", "HEAD"])
                if head != str(commit):
                    actions.append(fetch_command)
                    _run_git_command(fetch_command, cwd=root)
                    actions.append(checkout_command)
                    _run_git_command(checkout_command, cwd=root)
    return actions


def doctor(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    reports = []
    repos = load_repo_lock(root)
    expected_paths = {str(repo["path"]).replace("\\", "/").rstrip("/") for repo in repos}
    for repo in repos:
        path = root / str(repo["path"])
        branch = _git_output(["git", "-C", str(path), "branch", "--show-current"]) if (path / ".git").exists() else ""
        head = _git_output(["git", "-C", str(path), "rev-parse", "HEAD"]) if (path / ".git").exists() else ""
        reports.append(
            {
                "name": repo.get("name"),
                "path": repo.get("path"),
                "exists": path.exists(),
                "branch": branch,
                "expected_branch": repo.get("branch"),
                "head": head,
                "expected_commit": repo.get("commit"),
                "venv_python": _python_path(repo, _platform_name()),
                "venv_python_exists": (root / _python_path(repo, _platform_name())).exists(),
            }
        )
    repo_root = root / "repo"
    extra_dirs = []
    if repo_root.exists():
        for child in repo_root.iterdir():
            rel = child.relative_to(root).as_posix()
            if rel not in expected_paths:
                extra_dirs.append({"path": rel, "empty": child.is_dir() and not any(child.iterdir())})
    return {
        "repositories": reports,
        "extra_repo_dirs": extra_dirs,
        "network_profile": _read_network_profile(root) or {},
        "cache_paths": _cache_paths(root),
    }


def start_workers(
    root: Path = PROJECT_ROOT,
    *,
    platform_name: str | None = None,
    service_ids: set[str] | None = None,
    detach: bool = False,
) -> int:
    services = render_services(root, profile="local-all", platform_name=platform_name, service_ids=service_ids)
    processes: list[subprocess.Popen] = []
    logs_dir = root / "data" / ".runtime" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    for service in services:
        command = _resolve_command(root, service["start_command"])
        env = {**os.environ, **_resolve_env(root, service.get("env") or {})}
        log_path = logs_dir / f"{service['service_id']}.log"
        log_file = log_path.open("ab")
        kwargs: dict[str, Any] = {
            "cwd": root,
            "env": env,
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            flags = 0
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                flags |= subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            if detach and hasattr(subprocess, "CREATE_NO_WINDOW"):
                flags |= subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            if flags:
                kwargs["creationflags"] = flags
        process = subprocess.Popen(command, **kwargs)
        log_file.close()
        processes.append(process)
        print(f"{service['service_id']} PID {process.pid} {service['health_url']} log={log_path}")
    if detach:
        return 0
    try:
        return max((process.wait() for process in processes), default=0)
    except KeyboardInterrupt:
        for process in processes:
            process.terminate()
        return 130


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _is_tts_repo(repo: dict[str, Any]) -> bool:
    return str(repo.get("provider_type") or "") in PROVIDER_MODULES


def _display_name(repo: dict[str, Any]) -> str:
    provider = str(repo.get("provider_type"))
    if provider == "gpt-sovits":
        return f"GPT-SoVITS Worker ({repo.get('variant') or repo.get('branch')})"
    if provider == "indextts":
        return "IndexTTS Worker"
    if provider == "cosyvoice":
        return "CosyVoice Worker"
    return str(repo.get("name") or provider)


def _default_service_id(repo: dict[str, Any]) -> str:
    provider = str(repo["provider_type"])
    if provider == "gpt-sovits":
        return f"local-gpt-sovits-{repo.get('variant') or repo.get('branch')}"
    if provider == "indextts":
        return "local-indextts"
    if provider == "cosyvoice":
        return "local-cosyvoice"
    return f"local-{provider}"


def _default_port(provider: str) -> int:
    return {"gpt-sovits": 9880, "indextts": 9881, "cosyvoice": 9882}[provider]


def _resource_group(repo: dict[str, Any]) -> str:
    provider = str(repo["provider_type"])
    if provider == "gpt-sovits":
        return f"local-gpt-{repo.get('variant') or repo.get('branch')}"
    return "local-gpu-0"


def _start_command(repo: dict[str, Any], platform_name: str, port: int) -> list[str]:
    return [
        _python_path(repo, platform_name),
        "-m",
        "uvicorn",
        PROVIDER_MODULES[str(repo["provider_type"])],
        "--app-dir",
        "backend",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]


def _worker_env(repo: dict[str, Any], platform_name: str) -> dict[str, str]:
    provider = str(repo["provider_type"])
    path = str(repo["path"])
    env: dict[str, str] = {}
    if provider == "gpt-sovits":
        env["TTS_MORE_GPTSOVITS_REPO"] = path
        env["TTS_MORE_GPTSOVITS_VARIANT"] = str(repo.get("variant") or repo.get("branch"))
        separator = ";" if platform_name == "windows" else ":"
        env["PATH"] = f"{path}/ffmpeg-shared/bin{separator}{{PATH}}"
    elif provider == "indextts":
        env["TTS_MORE_INDEXTTS_REPO"] = path
        env["TTS_MORE_INDEXTTS_PYTHON"] = _python_path(repo, platform_name)
        env["TTS_MORE_INDEXTTS_MODEL_DIR"] = f"{path}/checkpoints"
        env["INDEXTTS2_MODEL_DIR"] = f"{path}/checkpoints"
    elif provider == "cosyvoice":
        env["TTS_MORE_COSYVOICE_REPO"] = path
        env["TTS_MORE_COSYVOICE_PYTHON"] = _python_path(repo, platform_name)
        env["TTS_MORE_COSYVOICE_MODEL_DIR"] = str(repo.get("model_dir") or "pretrained_models/CosyVoice-300M")
    return env


def _python_path(repo: dict[str, Any], platform_name: str) -> str:
    path = str(repo["path"])
    if platform_name == "windows":
        return f"{path}/.venv/Scripts/python.exe"
    return f"{path}/.venv/bin/python"


def _platform_name() -> str:
    return "windows" if os.name == "nt" else "posix"


def _remove_repo_dir(root: Path, *, dry_run: bool) -> None:
    target = (root / "repo").resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    if target == root_resolved or root_resolved not in target.parents:
        raise RuntimeError(f"refusing to remove repo directory outside project root: {target}")
    if target.name != "repo":
        raise RuntimeError(f"refusing to remove unexpected directory: {target}")
    if dry_run:
        return
    if target.exists():
        for child in list(target.iterdir()):
            _remove_path(child)
    target.mkdir(parents=True, exist_ok=True)


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, onerror=_remove_readonly)
        else:
            path.unlink()
    except PermissionError:
        if path.is_dir() and not any(path.iterdir()):
            print(f"warning: leaving locked empty directory in place: {path}", file=sys.stderr)
            return
        raise


def _remove_readonly(function: Any, path: str, _exc_info: Any) -> None:
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    function(path)


def _resolve_command(root: Path, command: list[str]) -> list[str]:
    if not command:
        return command
    executable = command[0]
    if "/" in executable or "\\" in executable:
        candidate = _resolve_project_path(root, executable)
        return [str(candidate), *command[1:]]
    return command


def _resolve_env(root: Path, env: dict[str, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    path_separator = ";" if os.name == "nt" else ":"
    for key, value in env.items():
        if key.upper() == "PATH":
            parts = []
            for part in value.replace("%PATH%", "{PATH}").split(path_separator):
                if part == "{PATH}":
                    parts.append(os.environ.get("PATH", ""))
                    continue
                if part and ("/" in part or "\\" in part):
                    parts.append(str(_resolve_project_path(root, part)))
                elif part:
                    parts.append(part)
            resolved[key] = path_separator.join(parts)
            continue
        if key.endswith(("_PATH", "_DIR", "_PYTHON")) and value and ("/" in value or "\\" in value):
            resolved[key] = str(_resolve_project_path(root, value))
        else:
            resolved[key] = value
    return resolved


def _resolve_project_path(root: Path, raw: str) -> Path:
    candidate = Path(raw.replace("\\", "/"))
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(f"path is outside project root: {raw}") from exc
    return resolved


def _git_output(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _parse_service_ids(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    return {item.strip() for item in raw.split(",") if item.strip()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TTS More deployment helper")
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="Project root")
    sub = parser.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render-services", help="Render services.json from repo.lock.json")
    render.add_argument("--profile", choices=("local-all", "app-only", "worker-node"), default="local-all")
    render.add_argument("--platform", choices=("windows", "posix"), default=None)
    render.add_argument("--host", default="127.0.0.1")
    render.add_argument("--service-ids", default=None)
    render.add_argument("--template", action="store_true", help="Render disabled committable defaults")
    render.add_argument("--output", default=None)

    sync = sub.add_parser("sync-repos", help="Clone/fetch repositories from repo.lock.json")
    sync.add_argument("--clean", action="store_true")
    sync.add_argument("--dry-run", action="store_true")

    probe = sub.add_parser("probe-network", help="Probe local network and choose install/download sources")
    probe.add_argument("--mode", choices=("auto", "china", "global"), default="auto")
    probe.add_argument("--source", choices=("Auto", "ModelScope", "HF", "HF-Mirror"), default="Auto")
    probe.add_argument("--write", action="store_true")
    probe.add_argument("--force", action="store_true")
    probe.add_argument("--timeout-seconds", type=float, default=2.0)
    probe.add_argument("--ttl-hours", type=float, default=24.0)
    probe.add_argument("--output", default=None)

    doctor_parser = sub.add_parser("doctor", help="Inspect repository checkout state")
    doctor_parser.add_argument("--output", default=None)

    start = sub.add_parser("start-workers", help="Start local worker processes from repo.lock.json")
    start.add_argument("--platform", choices=("windows", "posix"), default=None)
    start.add_argument("--service-ids", default=None)
    start.add_argument("--detach", action="store_true")

    args = parser.parse_args(argv)
    root = Path(args.root).resolve(strict=False)
    if args.command == "render-services":
        services = render_services(
            root,
            profile=args.profile,
            platform_name=args.platform,
            host=args.host,
            service_ids=_parse_service_ids(args.service_ids),
            template=args.template,
        )
        if args.output:
            write_json(root / args.output, services)
        else:
            print(json.dumps(services, ensure_ascii=False, indent=2))
        return 0
    if args.command == "sync-repos":
        actions = sync_repos(root, clean=args.clean, dry_run=args.dry_run)
        for command in actions:
            print(" ".join(command))
        return 0
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
    if args.command == "doctor":
        payload = doctor(root)
        if args.output:
            write_json(root / args.output, payload)
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "start-workers":
        return start_workers(
            root,
            platform_name=args.platform,
            service_ids=_parse_service_ids(args.service_ids),
            detach=args.detach,
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
