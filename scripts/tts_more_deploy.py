from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_BUNDLE_RELATIVE_PATH = Path("deployment/tts-repos")
DEFAULT_REPO_PATHS_RELATIVE_PATH = Path("deployment/app/repo-paths.local.json")
MANAGED_REPO_RELATIVE_PATH = Path("repo")

SAFE_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
PINNED_COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}\Z")


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
    raw_repositories = payload.get("repositories") if isinstance(payload, dict) else None
    if not isinstance(raw_repositories, list) or not raw_repositories:
        raise ValueError(f"repo lock must contain a non-empty repositories list: {path}")
    repositories = [dict(repo) for repo in raw_repositories if isinstance(repo, dict)]
    if len(repositories) != len(raw_repositories):
        raise ValueError(f"every repo lock entry must be an object: {path}")
    _validate_repo_manifest(repositories)
    return repositories


def load_deployment_repositories(
    root: Path = PROJECT_ROOT,
    repo_paths: str | Path | None = None,
    *,
    service_ids: set[str] | None = None,
    require_complete: bool = False,
) -> list[dict[str, Any]]:
    repositories = [dict(repo) for repo in load_repo_lock(root)]
    overrides = load_repo_path_overrides(root, repo_paths)
    if overrides:
        apply_repo_path_overrides(repositories, overrides)
    selected = _select_repositories(repositories, service_ids)
    if require_complete:
        confirmed = set(overrides)
        missing = [str(repo["service_id"]) for repo in selected if str(repo["service_id"]) not in confirmed]
        if missing:
            source = _repo_paths_config_path(root, repo_paths)
            detail = str(source) if source else "no confirmation file"
            raise ValueError(
                "missing confirmed repository paths for service_id(s): "
                f"{', '.join(missing)} ({detail})"
            )
    return repositories


def save_repo_lock(repositories: list[dict[str, Any]], root: Path = PROJECT_ROOT) -> None:
    write_json(root / "repo.lock.json", {"repositories": repositories}, boundary=root)


def load_repo_path_overrides(root: Path = PROJECT_ROOT, repo_paths: str | Path | None = None) -> dict[str, str]:
    path = _repo_paths_config_path(root, repo_paths)
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
    raw = payload.get("repositories") if isinstance(payload, dict) else payload
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"repo paths must be a non-empty service-id keyed object: {path}")
    overrides: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"repository service_id keys must be non-empty strings: {path}")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"repository path for {key!r} must be a non-empty string: {path}")
        overrides[key.strip()] = value.strip()
    return overrides


def apply_repo_path_overrides(repositories: list[dict[str, Any]], overrides: Mapping[str, str]) -> None:
    by_service_id = {str(repo["service_id"]): repo for repo in repositories}
    unknown = sorted(set(overrides) - set(by_service_id))
    if unknown:
        raise ValueError(f"unknown repository service_id(s): {', '.join(unknown)}")
    for service_id, path in overrides.items():
        repo = by_service_id[service_id]
        repo["path"] = path
        repo["path_source"] = service_id
        repo["path_confirmed"] = True


def _repo_paths_config_path(root: Path, repo_paths: str | Path | None) -> Path | None:
    if repo_paths is None:
        default = root / DEFAULT_REPO_PATHS_RELATIVE_PATH
        return default if default.exists() else None
    path = Path(repo_paths)
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        raise FileNotFoundError(f"repo paths file not found: {path}")
    return path


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key in repository confirmation: {key}")
        result[key] = value
    return result


def _validate_repo_manifest(repositories: list[dict[str, Any]]) -> None:
    seen_service_ids: set[str] = set()
    for index, repo in enumerate(repositories):
        service_id = repo.get("service_id")
        if not isinstance(service_id, str) or not service_id.strip():
            raise ValueError(f"repository entry {index} requires a non-empty service_id")
        if service_id in seen_service_ids:
            raise ValueError(f"duplicate service_id in repo lock: {service_id}")
        seen_service_ids.add(service_id)
        if type(repo.get("default_selected")) is not bool:
            raise ValueError(f"repository {service_id} requires explicit boolean default_selected")
        for field in ("name", "provider_type", "path", "remote", "branch"):
            if not isinstance(repo.get(field), str) or not str(repo[field]).strip():
                raise ValueError(f"repository {service_id} requires non-empty {field}")
        _validate_branch(str(repo["branch"]), service_id=service_id)
        commit = repo.get("commit")
        if commit is not None and (not isinstance(commit, str) or not PINNED_COMMIT_RE.fullmatch(commit)):
            raise ValueError(f"repository {service_id} has invalid pinned commit")


def _validate_branch(branch: str, *, service_id: str = "repository") -> None:
    invalid = (
        not SAFE_BRANCH_RE.fullmatch(branch)
        or ".." in branch
        or "@{" in branch
        or "//" in branch
        or branch.endswith(("/", ".", ".lock"))
        or any(part.startswith(".") for part in branch.split("/"))
    )
    if invalid:
        raise ValueError(f"repository {service_id} has invalid branch: {branch!r}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _cache_paths(root: Path, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    if environ is None:
        environ = os.environ
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
    last_error = ""
    for method in ("HEAD", "GET"):
        headers = {"User-Agent": "tts-more-deploy/1"}
        if method == "GET":
            headers["Range"] = "bytes=0-0"
        request = Request(url, method=method, headers=headers)
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
            latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            if 200 <= status < 400:
                return {"url": url, "ok": True, "latency_ms": latency_ms, "error": ""}
            last_error = f"{method} returned HTTP {status}"
        except Exception as exc:
            message = str(exc.reason) if isinstance(exc, URLError) and getattr(exc, "reason", None) else str(exc)
            last_error = f"{method}: {message}"
    latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return {"url": url, "ok": False, "latency_ms": latency_ms, "error": last_error}


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
    request_context: Mapping[str, str],
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
        "request_context": dict(request_context),
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
    if environ is None:
        environ = os.environ
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
        environ=environ,
        request_context=request_context,
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
        write_json(_network_profile_path(root), profile, boundary=root)
    if output:
        write_json(root / output, profile, boundary=root)
    return profile


def render_services(
    root: Path = PROJECT_ROOT,
    *,
    profile: str = "local-all",
    platform_name: str | None = None,
    host: str = "127.0.0.1",
    service_ids: set[str] | None = None,
    template: bool = False,
    repositories: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    platform_name = platform_name or _platform_name()
    repositories = _select_repositories(
        [repo for repo in (repositories or load_repo_lock(root)) if _is_tts_repo(repo)],
        service_ids,
    )
    services: list[dict[str, Any]] = []
    for repo in repositories:
        service_id = str(repo.get("service_id") or _default_service_id(repo))
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


def _repo_selected(repo: dict[str, Any], service_ids: set[str] | None) -> bool:
    if service_ids is None:
        return repo.get("default_selected") is True
    if not service_ids:
        return False
    if "all" in service_ids:
        return True
    if "default" in service_ids and repo.get("default_selected") is True:
        return True
    return bool(_repo_selector_candidates(repo) & service_ids)


def _repo_selector_candidates(repo: Mapping[str, Any]) -> set[str]:
    return {
        str(repo.get("name") or ""),
        str(repo.get("provider_type") or ""),
        str(repo.get("service_id") or _default_service_id(repo)),
        str(repo.get("variant") or ""),
        str(repo.get("branch") or ""),
        str(repo.get("path") or ""),
    }


def _select_repositories(
    repositories: list[dict[str, Any]],
    service_ids: set[str] | None,
) -> list[dict[str, Any]]:
    if service_ids is not None and not service_ids:
        raise ValueError("empty target selector set")
    if service_ids is None:
        selected = [repo for repo in repositories if repo.get("default_selected") is True]
    else:
        matched: set[str] = set()
        selected = []
        for repo in repositories:
            candidates = _repo_selector_candidates(repo)
            repo_matches = candidates & service_ids
            if "all" in service_ids:
                repo_matches.add("all")
            if "default" in service_ids and repo.get("default_selected") is True:
                repo_matches.add("default")
            matched.update(repo_matches)
            if repo_matches:
                selected.append(repo)
        unknown = sorted(service_ids - matched)
        if unknown:
            raise ValueError(f"unknown target selector(s): {', '.join(unknown)}")
    if not selected:
        raise ValueError("target selectors resolved to no repositories")
    return selected


def _repo_status(path: Path) -> str:
    return _git_output(["git", "-C", str(path), "status", "--porcelain"])


def _ensure_clean_repo(path: Path, name: str) -> None:
    status = _repo_status(path)
    if status:
        raise RuntimeError(
            f"refusing to update dirty service repository {name} at {path}; "
            "commit, stash, or clean local changes first, or pass --force-reset-repos"
        )


def _canonical_remote_identity(remote: str) -> str:
    value = remote.strip()
    scp_match = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", value)
    if scp_match and "://" not in value:
        host = scp_match.group(1).lower()
        path = scp_match.group(2)
    else:
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.hostname:
            return value.rstrip("/").removesuffix(".git")
        host = parsed.hostname.lower()
        path = parsed.path
    normalized_path = path.strip("/").removesuffix(".git")
    return f"{host}/{normalized_path}"


def _ensure_repo_origin(path: Path, expected_remote: str) -> None:
    actual_remote = _git_output(["git", "-C", str(path), "remote", "get-url", "origin"])
    if not actual_remote or _canonical_remote_identity(actual_remote) != _canonical_remote_identity(expected_remote):
        raise RuntimeError(
            f"repository origin mismatch at {path}: expected {expected_remote!r}, "
            f"found {actual_remote or '<missing>'!r}"
        )


def _git_exclude_path(repo_path: Path) -> Path | None:
    dot_git = repo_path / ".git"
    if not dot_git.exists():
        return None
    _assert_safe_path(dot_git, repo_path)
    if not dot_git.is_dir():
        return None
    candidate = dot_git / "info" / "exclude"
    _assert_safe_path(candidate, repo_path)
    return candidate


def _exclude_local_update_scripts(repo_path: Path) -> None:
    _exclude_local_helper_paths(
        repo_path,
        [
            "tts-more-update.sh",
            "tts-more-update.ps1",
            "tts-more-update.py",
            "tts-more-update.json",
        ],
    )


def _exclude_local_helper_paths(repo_path: Path, names: list[str]) -> None:
    exclude_path = _git_exclude_path(repo_path)
    if exclude_path is None:
        return
    _safe_mkdir(exclude_path.parent, repo_path)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    additions = [name for name in names if name not in existing.splitlines()]
    if not additions:
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    _atomic_write_text(
        exclude_path,
        existing + prefix + "\n".join(additions) + "\n",
        boundary=repo_path,
    )


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


def sync_repos(
    root: Path = PROJECT_ROOT,
    *,
    clean: bool = False,
    dry_run: bool = False,
    latest: bool = False,
    write_lock: bool = False,
    service_ids: set[str] | None = None,
    force_reset: bool = False,
    repositories: list[dict[str, Any]] | None = None,
) -> list[list[str]]:
    save_lock_on_change = repositories is None
    if clean:
        _remove_repo_dir(root, dry_run=dry_run)
    actions: list[list[str]] = []
    repositories = _select_repositories(
        [dict(repo) for repo in (repositories or load_repo_lock(root))],
        service_ids,
    )
    lock_changed = False
    for repo in repositories:
        path = _resolve_repo_path(root, str(repo["path"]))
        remote = str(repo["remote"])
        branch = str(repo["branch"])
        commit = repo.get("commit")
        if path.exists() and (path / ".git").exists():
            if not force_reset:
                _ensure_clean_repo(path, str(repo.get("name") or repo.get("service_id") or path.name))
            _ensure_repo_origin(path, remote)
            if force_reset:
                commands = [
                    ["git", "-C", str(path), "fetch", "--prune", "origin", branch],
                    ["git", "-C", str(path), "checkout", branch],
                    ["git", "-C", str(path), "reset", "--hard", f"origin/{branch}"],
                ]
            else:
                commands = [
                    ["git", "-C", str(path), "fetch", "--prune", "origin", branch],
                    ["git", "-C", str(path), "checkout", branch],
                    ["git", "-C", str(path), "pull", "--ff-only", "origin", branch],
                ]
        else:
            if not dry_run:
                _safe_mkdir(path.parent, root)
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
        if latest:
            if not dry_run and write_lock:
                head = _git_output(["git", "-C", str(path), "rev-parse", "HEAD"])
                if head and repo.get("commit") != head:
                    repo["commit"] = head
                    lock_changed = True
            continue
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
    if lock_changed and save_lock_on_change and not dry_run:
        save_repo_lock(repositories, root)
    return actions


def _service_update_script_sh() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${TTS_MORE_UPDATE_PYTHON:-}" ]]; then
  PYTHON="$TTS_MORE_UPDATE_PYTHON"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="python3"
fi
exec "$PYTHON" "$ROOT/tts-more-update.py" "$@"
"""


def _service_update_script_ps1() -> str:
    return r"""$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($env:TTS_MORE_UPDATE_PYTHON) {
  $Python = $env:TTS_MORE_UPDATE_PYTHON
} elseif (Test-Path -LiteralPath (Join-Path $Root ".venv\Scripts\python.exe")) {
  $Python = Join-Path $Root ".venv\Scripts\python.exe"
} else {
  $Python = "python"
}
& $Python (Join-Path $Root "tts-more-update.py") @args
exit $LASTEXITCODE
"""


def _service_update_script_py() -> str:
    return r'''from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}\Z")


def validate_branch(value: str) -> str:
    invalid = (
        not BRANCH_RE.fullmatch(value)
        or ".." in value
        or "@{" in value
        or "//" in value
        or value.endswith(("/", ".", ".lock"))
        or any(part.startswith(".") for part in value.split("/"))
    )
    if invalid:
        raise ValueError(f"invalid update branch: {value!r}")
    return value


def canonical_remote(value: str) -> str:
    value = value.strip()
    scp_match = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", value)
    if scp_match and "://" not in value:
        host, path = scp_match.group(1).lower(), scp_match.group(2)
    else:
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.hostname:
            return value.rstrip("/").removesuffix(".git")
        host, path = parsed.hostname.lower(), parsed.path
    return f"{host}/{path.strip('/').removesuffix('.git')}"


def output(args: list[str], root: Path) -> str:
    result = subprocess.run(args, cwd=root, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"command failed: {args!r}")
    return result.stdout.strip()


def run(args: list[str], root: Path) -> None:
    subprocess.run(args, cwd=root, check=True)


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent
    config = json.loads((root / "tts-more-update.json").read_text(encoding="utf-8"))
    branch = validate_branch(os.environ.get("TTS_MORE_UPDATE_BRANCH") or str(config["branch"]))
    commit = os.environ.get("TTS_MORE_PINNED_COMMIT") or str(config.get("commit") or "")
    if commit and not COMMIT_RE.fullmatch(commit):
        raise ValueError(f"invalid pinned commit: {commit!r}")
    expected_remote = str(config["remote"])
    actual_remote = output(["git", "remote", "get-url", "origin"], root)
    if canonical_remote(actual_remote) != canonical_remote(expected_remote):
        raise RuntimeError(
            f"repository origin mismatch: expected {expected_remote!r}, found {actual_remote!r}"
        )
    dirty = output(["git", "status", "--porcelain"], root)
    if dirty:
        raise RuntimeError("refusing to update a dirty repository; commit, stash, or clean local changes first")
    print(f"[update] {config.get('name') or config.get('service_id') or branch}")
    print(f"[remote] {expected_remote}")
    run(["git", "fetch", "--prune", "origin", branch], root)
    run(["git", "checkout", branch], root)
    run(["git", "pull", "--ff-only", "origin", branch], root)
    if argv and argv[0] == "--pinned" and commit:
        run(["git", "fetch", "origin", commit], root)
        run(["git", "checkout", commit], root)
    run(["git", "status", "--short", "--branch"], root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


def install_update_scripts(
    root: Path = PROJECT_ROOT,
    *,
    service_ids: set[str] | None = None,
    dry_run: bool = False,
    repositories: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    reports = []
    selected = _select_repositories(repositories or load_repo_lock(root), service_ids)
    for repo in selected:
        service_id = str(repo["service_id"])
        branch = str(repo["branch"])
        commit = str(repo.get("commit") or "")
        _validate_branch(branch, service_id=service_id)
        if commit and not PINNED_COMMIT_RE.fullmatch(commit):
            raise ValueError(f"repository {service_id} has invalid pinned commit")
        repo_path = _resolve_repo_path(root, str(repo["path"]))
        destinations = {
            "tts-more-update.sh": repo_path / "tts-more-update.sh",
            "tts-more-update.ps1": repo_path / "tts-more-update.ps1",
            "tts-more-update.py": repo_path / "tts-more-update.py",
            "tts-more-update.json": repo_path / "tts-more-update.json",
        }
        exists = repo_path.exists()
        report = {
            "name": repo.get("name"),
            "path": str(repo.get("path")),
            "exists": exists,
            "scripts": [path.relative_to(root).as_posix() for path in destinations.values()],
            "actions": [
                {"action": "write", "path": path.relative_to(root).as_posix()}
                for path in destinations.values()
            ],
        }
        reports.append(report)
        if exists:
            for destination in destinations.values():
                _assert_safe_path(destination, repo_path)
        if dry_run or not exists:
            continue
        sidecar = {
            "schema_version": 1,
            "service_id": service_id,
            "name": repo.get("name"),
            "remote": repo["remote"],
            "branch": branch,
            "commit": commit,
        }
        executable_mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
        _atomic_write_text(
            destinations["tts-more-update.sh"],
            _service_update_script_sh(),
            boundary=repo_path,
            mode=executable_mode,
        )
        _atomic_write_text(
            destinations["tts-more-update.ps1"],
            _service_update_script_ps1(),
            boundary=repo_path,
        )
        _atomic_write_text(
            destinations["tts-more-update.py"],
            _service_update_script_py(),
            boundary=repo_path,
        )
        write_json(destinations["tts-more-update.json"], sidecar, boundary=repo_path)
        _exclude_local_update_scripts(repo_path)
    return reports


def install_repo_bundles(
    root: Path = PROJECT_ROOT,
    *,
    service_ids: set[str] | None = None,
    dry_run: bool = False,
    repositories: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    reports = []
    selected = _select_repositories(repositories or load_repo_lock(root), service_ids)
    for repo in selected:
        provider = str(repo.get("provider_type") or "")
        bundle_path = root / REPO_BUNDLE_RELATIVE_PATH / provider
        repo_path = _resolve_repo_path(root, str(repo["path"]))
        target_path = repo_path / "tts-more"
        manifest_path = target_path / "tts-more-repo.json"
        report = {
            "name": repo.get("name"),
            "provider_type": provider,
            "path": str(repo.get("path")),
            "exists": repo_path.exists(),
            "bundle": str(bundle_path.relative_to(root)) if bundle_path.exists() else "",
            "target": str(target_path.relative_to(root)),
            "installed": False,
            "actions": [],
        }
        reports.append(report)
        if not bundle_path.exists():
            report["error"] = f"missing bundle for provider: {provider}"
            continue
        _assert_safe_path(bundle_path, root)
        source_files = _bundle_inventory(bundle_path)
        current_owned = sorted(source_files)
        _assert_safe_path(target_path, repo_path)
        _assert_safe_path(manifest_path, repo_path)
        previous_owned = _read_previous_owned_files(manifest_path, target_path) if target_path.exists() else []
        stale_owned = sorted(set(previous_owned) - set(current_owned))
        for relative in stale_owned:
            destination = target_path / relative
            _assert_safe_path(destination, target_path)
            report["actions"].append({"action": "remove", "path": destination.relative_to(root).as_posix()})
        for relative in current_owned:
            destination = target_path / relative
            _assert_safe_path(destination, target_path)
            report["actions"].append({"action": "copy", "path": destination.relative_to(root).as_posix()})
        report["actions"].append(
            {"action": "write-manifest", "path": manifest_path.relative_to(root).as_posix()}
        )
        manifest = {
            "schema_version": 2,
            "service_id": repo.get("service_id"),
            "name": repo.get("name"),
            "provider_type": provider,
            "variant": repo.get("variant"),
            "branch": repo.get("branch"),
            "commit": repo.get("commit"),
            "source_bundle": (REPO_BUNDLE_RELATIVE_PATH / provider).as_posix(),
            "owned_files": current_owned,
        }
        if dry_run or not repo_path.exists():
            continue
        _git_exclude_path(repo_path)
        _safe_mkdir(target_path, repo_path)
        for relative in stale_owned:
            _remove_owned_bundle_file(target_path / relative, target_path)
        for relative in current_owned:
            source = source_files[relative]
            destination = target_path / relative
            source_mode = source.stat().st_mode & 0o777
            if destination.suffix == ".sh":
                source_mode |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            _atomic_write_bytes(destination, source.read_bytes(), boundary=target_path, mode=source_mode)
        write_json(manifest_path, manifest, boundary=repo_path)
        _exclude_local_helper_paths(repo_path, ["tts-more/"])
        report["installed"] = True
    return reports


def _bundle_inventory(source: Path) -> dict[str, Path]:
    inventory: dict[str, Path] = {}
    for directory, directory_names, file_names in os.walk(source, followlinks=False):
        directory_path = Path(directory)
        for name in [*directory_names, *file_names]:
            candidate = directory_path / name
            if _is_link_or_reparse(candidate):
                raise ValueError(f"bundle source contains a symlink or reparse point: {candidate}")
        for name in file_names:
            candidate = directory_path / name
            if not candidate.is_file():
                raise ValueError(f"bundle source contains a non-regular file: {candidate}")
            inventory[candidate.relative_to(source).as_posix()] = candidate
    return dict(sorted(inventory.items()))


def _read_previous_owned_files(manifest_path: Path, target_path: Path) -> list[str]:
    _assert_safe_path(manifest_path, target_path)
    if not manifest_path.exists():
        return []
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_owned = payload.get("owned_files") if isinstance(payload, dict) else None
    if not isinstance(raw_owned, list):
        return []
    owned: list[str] = []
    for raw in raw_owned:
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"invalid owned file entry in {manifest_path}")
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"owned file escapes bundle target: {raw}")
        owned.append(relative.as_posix())
    return sorted(set(owned))


def _remove_owned_bundle_file(path: Path, target_path: Path) -> None:
    _assert_safe_path(path, target_path)
    if path.exists():
        if not path.is_file():
            raise ValueError(f"owned bundle path is not a regular file: {path}")
        path.unlink()
    parent = path.parent
    while parent != target_path and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def validate_repo_paths(
    root: Path = PROJECT_ROOT,
    *,
    service_ids: set[str] | None = None,
    require_exists: bool = False,
    repositories: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    reports = []
    selected = _select_repositories(repositories or load_repo_lock(root), service_ids)
    for repo in selected:
        raw_path = str(repo.get("path") or "")
        try:
            resolved = _resolve_repo_path(root, raw_path)
            inside_project = True
            error = ""
        except ValueError as exc:
            resolved = Path(raw_path).resolve(strict=False)
            inside_project = False
            error = str(exc)
        exists = resolved.exists()
        git_repo = exists and (resolved / ".git").exists()
        origin_matches = False
        if git_repo:
            actual_remote = _git_output(["git", "-C", str(resolved), "remote", "get-url", "origin"])
            origin_matches = bool(actual_remote) and _canonical_remote_identity(actual_remote) == _canonical_remote_identity(
                str(repo["remote"])
            )
            if not origin_matches and not error:
                error = (
                    f"repository origin mismatch: expected {repo['remote']!r}, "
                    f"found {actual_remote or '<missing>'!r}"
                )
        elif exists and not error:
            error = "path exists but is not a Git repository"
        ok = inside_project and (exists or not require_exists) and (not exists or (git_repo and origin_matches))
        reports.append(
            {
                "name": repo.get("name"),
                "service_id": repo.get("service_id"),
                "provider_type": repo.get("provider_type"),
                "path": raw_path,
                "absolute_path": str(resolved),
                "exists": exists,
                "inside_project": inside_project,
                "git_repository": git_repo,
                "origin_matches": origin_matches,
                "ok": ok,
                "error": error or ("path does not exist" if require_exists and not exists else ""),
            }
        )
    return reports


def update_project(
    root: Path = PROJECT_ROOT,
    *,
    dry_run: bool = False,
    skip_app: bool = False,
    skip_repos: bool = False,
    clean: bool = False,
    latest_repos: bool = False,
    write_lock: bool = False,
    service_ids: set[str] | None = None,
    install_scripts: bool = True,
    render: bool = True,
    force_render: bool = False,
    force_reset_repos: bool = False,
    platform_name: str | None = None,
    repositories: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    app_actions: list[list[str]] = []
    if not skip_app:
        branch = _git_output(["git", "-C", str(root), "branch", "--show-current"])
        if branch:
            app_actions = [
                ["git", "-C", str(root), "fetch", "--prune", "origin", branch],
                ["git", "-C", str(root), "pull", "--ff-only", "origin", branch],
            ]
            if not dry_run:
                for command in app_actions:
                    _run_git_command(command, cwd=root)
    repo_actions: list[list[str]] = []
    if not skip_repos:
        repo_actions = sync_repos(
            root,
            clean=clean,
            dry_run=dry_run,
            latest=latest_repos,
            write_lock=write_lock,
            service_ids=service_ids,
            force_reset=force_reset_repos,
            repositories=repositories,
        )
    update_scripts = (
        install_update_scripts(root, service_ids=service_ids, dry_run=dry_run, repositories=repositories)
        if install_scripts
        else []
    )
    services_output = ""
    services_rendered = False
    if render:
        services_output = "data/local/services.json"
        output_path = root / services_output
        should_render = force_render or not output_path.exists()
        services_rendered = should_render and not dry_run
        if should_render and not dry_run:
            services = render_services(
                root,
                profile="local-all",
                platform_name=platform_name,
                service_ids=service_ids,
                repositories=repositories,
            )
            write_json(root / services_output, services, boundary=root)
    return {
        "app_actions": app_actions,
        "repo_actions": repo_actions,
        "update_scripts": update_scripts,
        "services_output": services_output,
        "services_rendered": services_rendered,
        "services_render_policy": "force" if force_render else "missing-only",
    }


def doctor(
    root: Path = PROJECT_ROOT,
    *,
    service_ids: set[str] | None = None,
    repositories: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    reports = []
    all_repos = repositories or load_repo_lock(root)
    expected_paths = {str(repo["path"]).replace("\\", "/").rstrip("/") for repo in all_repos}
    for repo in _select_repositories(all_repos, service_ids):
        path = _resolve_repo_path(root, str(repo["path"]))
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
    repositories: list[dict[str, Any]] | None = None,
) -> int:
    services = render_services(
        root,
        profile="local-all",
        platform_name=platform_name,
        service_ids=service_ids,
        repositories=repositories,
    )
    processes: list[subprocess.Popen] = []
    logs_dir = root / "data" / ".runtime" / "logs"
    _safe_mkdir(logs_dir, root)
    for service in services:
        command = _resolve_command(root, service["start_command"])
        env = {**os.environ, **_resolve_env(root, service.get("env") or {})}
        log_path = logs_dir / f"{service['service_id']}.log"
        _assert_safe_path(log_path, root)
        open_flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        open_flags |= getattr(os, "O_BINARY", 0)
        open_flags |= getattr(os, "O_NOFOLLOW", 0)
        log_file = os.fdopen(os.open(log_path, open_flags, 0o600), "ab")
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


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if getattr(metadata, "st_file_attributes", 0) & reparse_flag:
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))


def _assert_safe_path(path: Path, boundary: Path) -> None:
    boundary_absolute = Path(os.path.abspath(boundary))
    path_absolute = Path(os.path.abspath(path))
    try:
        relative = path_absolute.relative_to(boundary_absolute)
    except ValueError as exc:
        raise ValueError(f"write path is outside owned boundary {boundary_absolute}: {path}") from exc
    current = boundary_absolute
    if _is_link_or_reparse(current):
        raise ValueError(f"owned boundary is a symlink or reparse point: {current}")
    for part in relative.parts:
        current = current / part
        if _is_link_or_reparse(current):
            raise ValueError(f"refusing symlink or reparse-point destination: {current}")
    resolved_boundary = boundary_absolute.resolve(strict=False)
    resolved_path = path_absolute.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_boundary)
    except ValueError as exc:
        raise ValueError(f"resolved write path escapes owned boundary {resolved_boundary}: {path}") from exc


def _safe_mkdir(path: Path, boundary: Path) -> None:
    _assert_safe_path(path, boundary)
    boundary_absolute = Path(os.path.abspath(boundary))
    current = boundary_absolute
    if not current.exists():
        current.mkdir()
    for part in Path(os.path.abspath(path)).relative_to(boundary_absolute).parts:
        current = current / part
        if current.exists():
            if _is_link_or_reparse(current) or not current.is_dir():
                raise ValueError(f"refusing non-directory or redirected destination: {current}")
            continue
        current.mkdir()


def _atomic_write_bytes(path: Path, payload: bytes, *, boundary: Path, mode: int | None = None) -> None:
    _safe_mkdir(path.parent, boundary)
    _assert_safe_path(path, boundary)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".tts-more-write-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            temporary.chmod(mode)
        _assert_safe_path(path, boundary)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_text(path: Path, payload: str, *, boundary: Path, mode: int | None = None) -> None:
    _atomic_write_bytes(path, payload.encode("utf-8"), boundary=boundary, mode=mode)


def write_json(path: Path, payload: Any, *, boundary: Path | None = None) -> None:
    owned_boundary = boundary
    if owned_boundary is None:
        owned_boundary = path.parent
        while not owned_boundary.exists() and owned_boundary != owned_boundary.parent:
            owned_boundary = owned_boundary.parent
    _atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        boundary=owned_boundary,
    )


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
        model_dir = str(repo.get("model_dir") or "pretrained_models/CosyVoice-300M").lstrip("/\\")
        repo_root = path.rstrip("/\\")
        env["TTS_MORE_COSYVOICE_MODEL_DIR"] = f"{repo_root}/{model_dir}"
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
    _safe_mkdir(target, root)


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


def _resolve_repo_path(root: Path, raw: str) -> Path:
    resolved = _resolve_project_path(root, raw)
    lexical = Path(raw.replace("\\", "/"))
    if not lexical.is_absolute():
        lexical = root / lexical
    _assert_safe_path(lexical, root)
    managed_root = (root / MANAGED_REPO_RELATIVE_PATH).resolve(strict=False)
    try:
        relative = resolved.relative_to(managed_root)
    except ValueError as exc:
        raise ValueError(
            f"path is outside dedicated repository area {managed_root}: {raw}"
        ) from exc
    if not relative.parts:
        raise ValueError(f"repository path must be below dedicated repository area: {raw}")
    return resolved


def _git_output(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _parse_service_ids(raw: str | None) -> set[str] | None:
    if raw is None:
        return None
    if not raw.strip():
        raise ValueError("empty target selector")
    items = [item.strip() for item in raw.split(",")]
    if any(not item for item in items):
        raise ValueError("empty target selector")
    if len(items) != len(set(items)):
        raise ValueError("duplicate target selector")
    return set(items)


def _load_cli_repositories(
    root: Path,
    repo_paths: str | None,
    service_ids: set[str] | None,
    *,
    require_complete: bool = True,
    verify_existing: bool = True,
) -> list[dict[str, Any]]:
    repositories = load_deployment_repositories(
        root,
        repo_paths,
        service_ids=service_ids,
        require_complete=require_complete,
    )
    if verify_existing:
        for repo in _select_repositories(repositories, service_ids):
            path = _resolve_repo_path(root, str(repo["path"]))
            if not path.exists():
                continue
            if not (path / ".git").exists():
                raise RuntimeError(f"confirmed repository path is not a Git checkout: {path}")
            _ensure_repo_origin(path, str(repo["remote"]))
    return repositories


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
    render.add_argument("--repo-paths", default=None, help="Complete service-id keyed repo path confirmation JSON")

    sync = sub.add_parser("sync-repos", help="Clone/fetch repositories from repo.lock.json")
    sync.add_argument("--clean", action="store_true")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--latest", action="store_true", help="Track each configured branch instead of checking out the pinned commit")
    sync.add_argument("--write-lock", action="store_true", help="After --latest, write current HEADs back to repo.lock.json")
    sync.add_argument("--service-ids", default=None)
    sync.add_argument("--repo-paths", default=None, help="Complete service-id keyed repo path confirmation JSON")

    list_repos = sub.add_parser("list-repos", help="Print repositories after applying optional local path overrides")
    list_repos.add_argument("--service-ids", default=None)
    list_repos.add_argument("--repo-paths", default=None)
    list_repos.add_argument("--json-lines", action="store_true")

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
    doctor_parser.add_argument("--service-ids", default=None)
    doctor_parser.add_argument("--repo-paths", default=None, help="Complete service-id keyed repo path confirmation JSON")

    start = sub.add_parser("start-workers", help="Start local worker processes from repo.lock.json")
    start.add_argument("--platform", choices=("windows", "posix"), default=None)
    start.add_argument("--service-ids", default=None)
    start.add_argument("--detach", action="store_true")
    start.add_argument("--repo-paths", default=None, help="Complete service-id keyed repo path confirmation JSON")

    install_scripts = sub.add_parser(
        "install-update-scripts",
        help="Write small update scripts into checked-out TTS service repositories",
    )
    install_scripts.add_argument("--service-ids", default=None)
    install_scripts.add_argument("--dry-run", action="store_true")
    install_scripts.add_argument("--repo-paths", default=None, help="Complete service-id keyed repo path confirmation JSON")

    install_bundles = sub.add_parser(
        "install-repo-bundles",
        help="Copy provider-specific TTS More helper bundles into checked-out TTS repositories",
    )
    install_bundles.add_argument("--service-ids", default=None)
    install_bundles.add_argument("--dry-run", action="store_true")
    install_bundles.add_argument("--repo-paths", default=None, help="Complete service-id keyed repo path confirmation JSON")

    validate_paths = sub.add_parser(
        "validate-repo-paths",
        help="Validate local TTS repo paths before one-click deployment",
    )
    validate_paths.add_argument("--service-ids", default=None)
    validate_paths.add_argument("--require-exists", action="store_true")
    validate_paths.add_argument("--repo-paths", default=None, help="Complete service-id keyed repo path confirmation JSON")

    update = sub.add_parser("update", help="Fast-forward the app and service repositories")
    update.add_argument("--dry-run", action="store_true")
    update.add_argument("--skip-app", action="store_true")
    update.add_argument("--skip-repos", action="store_true")
    update.add_argument("--clean", action="store_true")
    update.add_argument("--latest-repos", action="store_true")
    update.add_argument("--write-lock", action="store_true")
    update.add_argument("--force-reset-repos", action="store_true", help="Allow service repositories to be reset hard to the configured branch")
    update.add_argument("--service-ids", default=None)
    update.add_argument("--no-install-scripts", action="store_true")
    update.add_argument("--no-render", action="store_true")
    update.add_argument("--force-render-services", action="store_true")
    update.add_argument("--platform", choices=("windows", "posix"), default=None)
    update.add_argument("--repo-paths", default=None, help="Complete service-id keyed repo path confirmation JSON")

    args = parser.parse_args(argv)
    root = Path(args.root).resolve(strict=False)
    if args.command == "render-services":
        service_ids = _parse_service_ids(args.service_ids)
        repositories = _load_cli_repositories(
            root,
            args.repo_paths,
            service_ids,
            require_complete=args.profile != "app-only",
            verify_existing=args.profile != "app-only",
        )
        services = render_services(
            root,
            profile=args.profile,
            platform_name=args.platform,
            host=args.host,
            service_ids=service_ids,
            template=args.template,
            repositories=repositories,
        )
        if args.output:
            write_json(root / args.output, services, boundary=root)
        else:
            print(json.dumps(services, ensure_ascii=False, indent=2))
        return 0
    if args.command == "sync-repos":
        service_ids = _parse_service_ids(args.service_ids)
        repositories = _load_cli_repositories(
            root,
            args.repo_paths,
            service_ids,
            verify_existing=False,
        )
        actions = sync_repos(
            root,
            clean=args.clean,
            dry_run=args.dry_run,
            latest=args.latest,
            write_lock=args.write_lock,
            service_ids=service_ids,
            repositories=repositories,
        )
        print(
            json.dumps(
                {"actions": [{"argv": command} for command in actions]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "list-repos":
        service_ids = _parse_service_ids(args.service_ids)
        repositories = _load_cli_repositories(root, args.repo_paths, service_ids)
        repositories = _select_repositories(repositories, service_ids)
        for repo in repositories:
            repo["absolute_path"] = str(_resolve_repo_path(root, str(repo["path"])))
        if args.json_lines:
            for repo in repositories:
                print(json.dumps(repo, ensure_ascii=False))
        else:
            print(json.dumps(repositories, ensure_ascii=False, indent=2))
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
        service_ids = _parse_service_ids(args.service_ids)
        repositories = _load_cli_repositories(root, args.repo_paths, service_ids)
        payload = doctor(root, service_ids=service_ids, repositories=repositories)
        if args.output:
            write_json(root / args.output, payload, boundary=root)
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "start-workers":
        service_ids = _parse_service_ids(args.service_ids)
        repositories = _load_cli_repositories(root, args.repo_paths, service_ids)
        return start_workers(
            root,
            platform_name=args.platform,
            service_ids=service_ids,
            detach=args.detach,
            repositories=repositories,
        )
    if args.command == "install-update-scripts":
        service_ids = _parse_service_ids(args.service_ids)
        repositories = _load_cli_repositories(root, args.repo_paths, service_ids)
        reports = install_update_scripts(
            root,
            service_ids=service_ids,
            dry_run=args.dry_run,
            repositories=repositories,
        )
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0
    if args.command == "install-repo-bundles":
        service_ids = _parse_service_ids(args.service_ids)
        repositories = _load_cli_repositories(root, args.repo_paths, service_ids)
        reports = install_repo_bundles(
            root,
            service_ids=service_ids,
            dry_run=args.dry_run,
            repositories=repositories,
        )
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0
    if args.command == "validate-repo-paths":
        service_ids = _parse_service_ids(args.service_ids)
        repositories = _load_cli_repositories(
            root,
            args.repo_paths,
            service_ids,
            verify_existing=False,
        )
        reports = validate_repo_paths(
            root,
            service_ids=service_ids,
            require_exists=args.require_exists,
            repositories=repositories,
        )
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0 if all(item["ok"] for item in reports) else 1
    if args.command == "update":
        service_ids = _parse_service_ids(args.service_ids)
        repositories = _load_cli_repositories(root, args.repo_paths, service_ids)
        payload = update_project(
            root,
            dry_run=args.dry_run,
            skip_app=args.skip_app,
            skip_repos=args.skip_repos,
            clean=args.clean,
            latest_repos=args.latest_repos,
            write_lock=args.write_lock,
            service_ids=service_ids,
            install_scripts=not args.no_install_scripts,
            render=not args.no_render,
            force_render=args.force_render_services,
            force_reset_repos=args.force_reset_repos,
            platform_name=args.platform,
            repositories=repositories,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
