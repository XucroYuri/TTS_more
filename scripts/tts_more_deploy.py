from __future__ import annotations

import argparse
import configparser
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
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
SERVICE_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?\Z")
GITHUB_REPOSITORY_COMPONENT_RE = re.compile(r"[A-Za-z0-9_.-]+\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
BUNDLE_MANIFEST_SCHEMA_VERSION = 3
BUNDLE_PENDING_MANIFEST = "tts-more-install-pending.json"
BUNDLE_OWNERSHIP_RELATIVE_PATH = Path("data/local/deployment-ownership")
GIT_BLOCKED_ENV_EXACT = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_PROXY_COMMAND",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_EXEC_PATH",
    "GIT_TEMPLATE_DIR",
    "GIT_EXTERNAL_DIFF",
    "GIT_SSH_VARIANT",
    "GIT_SSL_NO_VERIFY",
    "GIT_SSL_CERT",
    "GIT_SSL_KEY",
    "GIT_SSL_CAINFO",
    "GIT_SSL_CAPATH",
    "GIT_ALLOW_PROTOCOL",
}
GIT_BLOCKED_ENV_PREFIXES = ("GIT_CONFIG_",)
TRUSTED_GIT_ENV = "TTS_MORE_TRUSTED_GIT"
TRUSTED_SSH_ENV = "TTS_MORE_TRUSTED_SSH"
MAX_LOCAL_GIT_CONFIG_BYTES = 1024 * 1024
UPDATER_EXECUTABLE_POLICY = "fixed-dirs-or-explicit-env-v1"
TOPOLOGY_SCHEMA_VERSION = 1


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
        "artifact-transfer",
    ],
    "indextts": [
        "tts",
        "reference_audio_voice",
        "emotion_text",
        "emotion_audio",
        "wav_output",
        "tts-more-worker",
        "artifact-transfer",
    ],
    "cosyvoice": [
        "tts",
        "reference_audio_voice",
        "zero_shot_voice",
        "cross_lingual_voice",
        "style_instruction",
        "wav_output",
        "tts-more-worker",
        "artifact-transfer",
    ],
}

PROVIDER_PRIORITY = {"gpt-sovits": 10, "indextts": 20, "cosyvoice": 30}

NETWORK_PROFILE_RELATIVE_PATH = Path("data/local/network-profile.json")
DEFAULT_CACHE_RELATIVE_PATH = Path("data/cache")
NETWORK_PROFILE_SCHEMA_VERSION = 1

HOST_LIMITS_GIB = {
    "single-clean": {"repo": 40.0, "temp": 10.0},
    "single-release": {"repo": 15.0, "temp": 5.0},
    "distributed": {"repo": 15.0, "temp": 5.0},
}

FORMAL_WORKER_MODULES = {
    "local-gpt-sovits-main": "app.workers.gpt_sovits_worker:app",
    "local-indextts": "app.workers.indextts_worker:app",
    "local-cosyvoice": "app.workers.cosyvoice_worker:app",
}
MIN_GPU_TOTAL_MIB = 16000
MAX_INITIAL_GPU_USED_MIB = 1024
# large-v3 may need to download before CUDA initialization; never let that child
# process outlive this bounded ten-minute certification probe.
ASR_SMOKE_TIMEOUT_SECONDS = 600.0
HOST_COMMAND_TIMEOUT_SECONDS = 30.0

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
    _validate_selected_repository_paths(root, selected)
    return repositories


def save_repo_lock(repositories: list[dict[str, Any]], root: Path = PROJECT_ROOT) -> None:
    write_json(root / "repo.lock.json", {"repositories": repositories}, boundary=root)


def _save_repo_lock_commit_updates(root: Path, commits_by_service_id: Mapping[str, str]) -> bool:
    repositories = load_repo_lock(root)
    by_service_id = {str(repo["service_id"]): repo for repo in repositories}
    unknown = sorted(set(commits_by_service_id) - set(by_service_id))
    if unknown:
        raise ValueError(f"cannot update unknown repository service_id(s): {', '.join(unknown)}")
    changed = False
    for service_id, commit in commits_by_service_id.items():
        if not PINNED_COMMIT_RE.fullmatch(commit):
            raise ValueError(f"repository {service_id} has invalid pinned commit")
        repo = by_service_id[service_id]
        if repo.get("commit") != commit:
            repo["commit"] = commit
            changed = True
    if changed:
        save_repo_lock(repositories, root)
    return changed


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
        _validate_service_id(service_id)
        if service_id in seen_service_ids:
            raise ValueError(f"duplicate service_id in repo lock: {service_id}")
        seen_service_ids.add(service_id)
        if type(repo.get("default_selected")) is not bool:
            raise ValueError(f"repository {service_id} requires explicit boolean default_selected")
        for field in ("name", "provider_type", "path", "remote", "branch"):
            if not isinstance(repo.get(field), str) or not str(repo[field]).strip():
                raise ValueError(f"repository {service_id} requires non-empty {field}")
        _validate_branch(str(repo["branch"]), service_id=service_id)
        _parse_github_remote(str(repo["remote"]))
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


def _validate_service_id(service_id: str) -> str:
    if not SERVICE_ID_RE.fullmatch(service_id):
        raise ValueError(
            "service_id must be 1-64 lowercase ASCII letters, digits, dots, underscores, or hyphens "
            f"and must start/end alphanumeric: {service_id!r}"
        )
    return service_id


def _parse_github_remote(remote: str) -> tuple[str, str, str]:
    if not isinstance(remote, str) or not remote or remote != remote.strip():
        raise ValueError(f"unsupported GitHub remote: {remote!r}")
    if remote.startswith("-") or any(ord(character) < 32 or ord(character) == 127 for character in remote):
        raise ValueError(f"unsupported GitHub remote: {remote!r}")
    if "%" in remote or "::" in remote:
        raise ValueError(f"unsupported GitHub remote: {remote!r}")

    path: str
    if "://" not in remote:
        match = re.fullmatch(r"git@github\.com:(.+)", remote, flags=re.IGNORECASE)
        if not match:
            raise ValueError(f"unsupported GitHub remote: {remote!r}")
        path = match.group(1)
    else:
        try:
            parsed = urlparse(remote)
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"unsupported GitHub remote: {remote!r}") from exc
        scheme = parsed.scheme.lower()
        if parsed.query or parsed.fragment or parsed.params:
            raise ValueError(f"unsupported GitHub remote: {remote!r}")
        if parsed.hostname != "github.com" or parsed.hostname is None:
            raise ValueError(f"unsupported GitHub remote: {remote!r}")
        if scheme == "https":
            if parsed.username is not None or parsed.password is not None or port not in (None, 443):
                raise ValueError(f"unsupported GitHub remote: {remote!r}")
        elif scheme == "ssh":
            if parsed.username != "git" or parsed.password is not None or port not in (None, 22):
                raise ValueError(f"unsupported GitHub remote: {remote!r}")
        else:
            raise ValueError(f"unsupported GitHub remote: {remote!r}")
        path = parsed.path.lstrip("/")

    normalized_path = path.rstrip("/")
    if normalized_path.endswith(".git"):
        normalized_path = normalized_path[:-4]
    parts = normalized_path.split("/")
    if (
        len(parts) != 2
        or any(part in {"", ".", ".."} for part in parts)
        or any(not GITHUB_REPOSITORY_COMPONENT_RE.fullmatch(part) for part in parts)
    ):
        raise ValueError(f"unsupported GitHub remote: {remote!r}")
    owner, repository = (part.lower() for part in parts)
    return ("github.com", owner, repository)


def _github_remote_requires_ssh(remote: str) -> bool:
    _parse_github_remote(remote)
    return "://" not in remote or urlparse(remote).scheme.lower() == "ssh"


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
    topology: str | Path | None = None,
    node: str | None = None,
) -> list[dict[str, Any]]:
    platform_name = platform_name or _platform_name()
    selected_repositories = _select_repositories(
        [repo for repo in (repositories or load_repo_lock(root)) if _is_tts_repo(repo)],
        service_ids,
    )
    _validate_selected_repository_paths(root, selected_repositories)
    topology_payload: dict[str, Any] | None = None
    topology_worker: tuple[str, dict[str, Any]] | None = None
    assignments: dict[str, tuple[str, dict[str, Any]]] = {}
    if topology is not None:
        selected_service_ids = {
            str(repo.get("service_id") or _default_service_id(repo)) for repo in selected_repositories
        }
        topology_payload = load_topology(root, topology, selected_service_ids=selected_service_ids)
        assignments = _topology_assignments(topology_payload)
        topology_worker = _resolve_topology_worker(topology_payload, profile=profile, node=node)
    services: list[dict[str, Any]] = []
    for repo in selected_repositories:
        service_id = str(repo.get("service_id") or _default_service_id(repo))
        _validate_service_id(service_id)
        assigned_node = assignments.get(service_id)
        if topology_worker is not None and assigned_node is not None and assigned_node[0] != topology_worker[0]:
            continue
        provider = str(repo["provider_type"])
        port = int(repo.get("port") or _default_port(provider))
        is_external = profile == "app-only"
        endpoint_host = host
        bind_host = "127.0.0.1"
        resource_group = str(repo.get("resource_group") or _resource_group(repo))
        capacity = int(repo.get("capacity") or 1)
        if assigned_node is not None:
            node_config = assigned_node[1]
            endpoint_host = str(node_config["host"])
            bind_host = str(node_config["bind_host"])
            resource_group = str(node_config["resource_group"])
            capacity = int(node_config["capacity"])
        if topology_payload is not None:
            is_lan = is_external or endpoint_host not in {"127.0.0.1", "localhost", "::1"}
        else:
            is_lan = is_external and endpoint_host not in {"127.0.0.1", "localhost", "::1"}
        worker_env = {} if is_external else _worker_env(repo, platform_name)
        if not is_external:
            worker_env["TTS_MORE_WORKER_ALLOW_PATH_DELIVERY"] = (
                "1" if bind_host in {"127.0.0.1", "localhost", "::1"} else "0"
            )
        service = {
            "service_id": service_id,
            "service_kind": "tts",
            "display_name": str(repo.get("display_name") or _display_name(repo)),
            "engine": PROVIDER_ENGINES[provider],
            "provider_type": provider,
            "source_profile": "lan_endpoint" if is_lan else "local_endpoint",
            "catalog_provider": provider,
            "setup_state": "not_configured" if template else ("endpoint_unreachable" if is_external else "repo_found"),
            "api_contract": "tts-more-v1",
            "base_url": f"http://{endpoint_host}:{port}",
            "mode": "external" if is_external else "local",
            "network_scope": "lan" if is_lan else "localhost",
            "managed": not is_external,
            "enabled": not template,
            "poll_interval_seconds": 5,
            "repo_path": None if is_external else repo["path"],
            "start_command": [] if is_external else _start_command(repo, platform_name, port, bind_host=bind_host),
            "start_cwd": None if is_external else ".",
            "env": worker_env,
            "health_url": f"http://{endpoint_host}:{port}/health",
            "resource_group": resource_group,
            "capacity": capacity,
            "priority": int(repo.get("priority") or PROVIDER_PRIORITY[provider]),
            "capabilities": list(repo.get("capabilities") or PROVIDER_CAPABILITIES[provider]),
        }
        if provider == "cosyvoice":
            service["default_params"] = {"mode": "zero_shot", "response_format": "wav"}
        services.append(service)
    return services


def load_topology(
    root: Path,
    topology: str | Path,
    *,
    selected_service_ids: set[str],
) -> dict[str, Any]:
    path = Path(topology)
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        raise FileNotFoundError(f"topology file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_topology(payload, selected_service_ids=selected_service_ids)
    return payload


def validate_topology(payload: Any, *, selected_service_ids: set[str]) -> None:
    if not isinstance(payload, dict):
        raise ValueError("topology must be a JSON object")
    if payload.get("schema_version") != TOPOLOGY_SCHEMA_VERSION or isinstance(payload.get("schema_version"), bool):
        raise ValueError(f"topology schema_version must be {TOPOLOGY_SCHEMA_VERSION}")
    if not isinstance(payload.get("name"), str) or not payload["name"].strip():
        raise ValueError("topology name must be a nonempty string")
    app_node = payload.get("app_node")
    if not isinstance(app_node, str) or not app_node.strip():
        raise ValueError("topology app_node must be a nonempty string")
    nodes = payload.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        raise ValueError("topology nodes must be a nonempty object")

    required_fields = {"role", "host", "bind_host", "services", "resource_group", "capacity"}
    service_owners: dict[str, str] = {}
    for node_name, node_config in nodes.items():
        if not isinstance(node_name, str) or not node_name.strip() or not isinstance(node_config, dict):
            raise ValueError("topology node names and values must be nonempty strings and objects")
        missing = required_fields.difference(node_config)
        if missing:
            raise ValueError(f"topology node {node_name} is missing fields: {', '.join(sorted(missing))}")
        if node_config["role"] not in {"app", "worker"}:
            raise ValueError(f"topology node {node_name} role must be app or worker")
        for field in ("host", "bind_host", "resource_group"):
            value = node_config[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"topology node {node_name} {field} must be a nonempty string")
        services = node_config["services"]
        if not isinstance(services, list) or any(not isinstance(item, str) or not item.strip() for item in services):
            raise ValueError(f"topology node {node_name} services must be a list of nonempty strings")
        if node_config["role"] == "app" and services:
            raise ValueError("topology app node services must be empty")
        capacity = node_config["capacity"]
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError(f"topology node {node_name} capacity must be an integer >= 1")
        if node_config["role"] == "worker":
            for service_id in services:
                previous_owner = service_owners.get(service_id)
                if previous_owner is not None:
                    raise ValueError(
                        f"service {service_id} must be assigned to exactly one worker; "
                        f"found {previous_owner} and {node_name}"
                    )
                service_owners[service_id] = node_name

    if app_node not in nodes:
        raise ValueError(f"topology app_node does not exist: {app_node}")
    if nodes[app_node]["role"] != "app":
        raise ValueError(f"topology app_node {app_node} must have role app")

    assignments = _topology_assignments(payload)
    for service_id in sorted(selected_service_ids):
        assigned_workers = [
            node_name
            for node_name, node_config in nodes.items()
            if node_config["role"] == "worker" and service_id in node_config["services"]
        ]
        if len(assigned_workers) != 1:
            raise ValueError(
                f"selected service {service_id} must be assigned to exactly one worker; "
                f"found {len(assigned_workers)}"
            )
        if service_id not in assignments:
            raise ValueError(f"selected service {service_id} must be assigned to exactly one worker")
    worker_nodes = [
        (node_name, node_config)
        for node_name, node_config in nodes.items()
        if node_config["role"] == "worker"
    ]
    if len(worker_nodes) > 1:
        declared_hosts: dict[str, str] = {}
        for node_name, node_config in nodes.items():
            host = str(node_config["host"]).strip()
            normalized_host = host.casefold().rstrip(".")
            candidate_ip = normalized_host.strip("[]")
            try:
                address = ipaddress.ip_address(candidate_ip)
            except ValueError:
                address = None
            if normalized_host in {"localhost", "ip6-localhost"} or (
                address is not None and (address.is_loopback or address.is_unspecified)
            ):
                raise ValueError(f"distributed topology node {node_name} host must be non-loopback")
            previous_node = declared_hosts.get(normalized_host)
            if previous_node is not None:
                raise ValueError(
                    f"distributed topology nodes {previous_node} and {node_name} must use a distinct host"
                )
            declared_hosts[normalized_host] = node_name
        for node_name, node_config in worker_nodes:
            if len(node_config["services"]) != 1:
                raise ValueError(f"distributed worker {node_name} must own exactly one service")


def _topology_assignments(payload: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    assignments: dict[str, tuple[str, dict[str, Any]]] = {}
    for node_name, node_config in payload["nodes"].items():
        if node_config["role"] != "worker":
            continue
        for service_id in node_config["services"]:
            assignments[service_id] = (node_name, node_config)
    return assignments


def _resolve_topology_worker(
    payload: dict[str, Any],
    *,
    profile: str,
    node: str | None,
) -> tuple[str, dict[str, Any]] | None:
    nodes = payload["nodes"]
    if profile == "app-only":
        selected_node = node or payload["app_node"]
        if selected_node not in nodes:
            raise ValueError(f"topology node does not exist: {selected_node}")
        if nodes[selected_node]["role"] != "app":
            raise ValueError(f"app-only node {selected_node} must have role app")
        return None

    if node is not None:
        if node not in nodes:
            raise ValueError(f"topology node does not exist: {node}")
        if nodes[node]["role"] != "worker":
            raise ValueError(f"{profile} node {node} must have role worker")
        return node, nodes[node]

    workers = [(node_name, node_config) for node_name, node_config in nodes.items() if node_config["role"] == "worker"]
    if profile == "worker-node":
        raise ValueError("worker-node profile requires --node")
    if len(workers) != 1:
        raise ValueError("local-all topology requires --node when multiple worker nodes are configured")
    return workers[0]


def _clone_command(remote: str, branch: str, path: Path) -> list[str]:
    _parse_github_remote(remote)
    command = ["git", "clone", "--depth", "1"]
    command.extend(["--branch", branch, "--single-branch", "--", remote, str(path)])
    return command


def _run_git_command(
    command: list[str],
    *,
    cwd: Path,
    validated_submodule_remotes: tuple[str, ...] | None = None,
) -> None:
    _run_git_process(
        command,
        cwd=cwd,
        check=True,
        validated_submodule_remotes=validated_submodule_remotes,
    )


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


def _validate_selected_repository_paths(
    root: Path,
    repositories: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], Path]]:
    resolved: list[tuple[dict[str, Any], Path, str]] = []
    for repo in repositories:
        path = _resolve_repo_path(root, str(repo["path"]))
        canonical = os.path.normcase(os.path.abspath(path))
        resolved.append((repo, path, canonical))
    for index, (left_repo, _left_path, left_key) in enumerate(resolved):
        for right_repo, _right_path, right_key in resolved[index + 1 :]:
            left_id = str(left_repo.get("service_id") or left_repo.get("name"))
            right_id = str(right_repo.get("service_id") or right_repo.get("name"))
            if left_key == right_key:
                raise ValueError(
                    f"selected services resolve to the same canonical repository path: {left_id}, {right_id}"
                )
            try:
                common = os.path.commonpath([left_key, right_key])
            except ValueError:
                continue
            if common in {left_key, right_key}:
                raise ValueError(
                    f"selected services resolve to nested repository paths: {left_id}, {right_id}"
                )
    return [(repo, path) for repo, path, _key in resolved]


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
    host, owner, repository = _parse_github_remote(remote)
    return f"{host}/{owner}/{repository}"


def _ensure_repo_origin(path: Path, expected_remote: str) -> str:
    actual_remote = _git_output(["git", "-C", str(path), "remote", "get-url", "origin"])
    try:
        expected_identity = _canonical_remote_identity(expected_remote)
        actual_identity = _canonical_remote_identity(actual_remote)
    except ValueError as exc:
        raise RuntimeError(f"repository origin is not a supported GitHub remote at {path}: {actual_remote!r}") from exc
    if actual_identity != expected_identity:
        raise RuntimeError(
            f"repository origin mismatch at {path}: expected {expected_remote!r}, "
            f"found {actual_remote or '<missing>'!r}"
        )
    return actual_remote


def _resolve_submodule_remote(origin: str, remote: str) -> str:
    _parse_github_remote(origin)
    if not remote.startswith(("./", "../")):
        _parse_github_remote(remote)
        return remote
    if not remote or "\\" in remote or any(ord(character) < 32 for character in remote):
        raise ValueError(f"unsupported GitHub remote: {remote!r}")
    if "://" in origin:
        parsed = urlparse(origin)
        prefix = None
        origin_path = parsed.path.lstrip("/")
    else:
        prefix, origin_path = origin.split(":", 1)
        parsed = None
    components = origin_path.split("/")
    for component in remote.split("/"):
        if component in {"", "."}:
            continue
        if component == "..":
            if not components:
                raise ValueError(f"unsupported GitHub remote: {remote!r}")
            components.pop()
        else:
            components.append(component)
    resolved_path = "/".join(components)
    if parsed is not None:
        resolved = parsed._replace(path=f"/{resolved_path}", params="", query="", fragment="").geturl()
    else:
        resolved = f"{prefix}:{resolved_path}"
    _parse_github_remote(resolved)
    return resolved


def _load_validated_submodules(repo_path: Path, origin: str) -> list[dict[str, str]]:
    _parse_github_remote(origin)
    gitmodules_path = repo_path / ".gitmodules"
    if _is_link_or_reparse(gitmodules_path):
        raise RuntimeError(f".gitmodules must not be a symlink or reparse point: {gitmodules_path}")
    if not gitmodules_path.exists():
        return []
    _assert_safe_path(gitmodules_path, repo_path)
    try:
        payload = gitmodules_path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"unable to read .gitmodules safely: {gitmodules_path}") from exc
    if len(payload) > MAX_LOCAL_GIT_CONFIG_BYTES or b"\0" in payload:
        raise RuntimeError(f".gitmodules is oversized or contains NUL bytes: {gitmodules_path}")
    try:
        text = payload.decode("utf-8", errors="strict")
        parser = configparser.RawConfigParser(
            interpolation=None,
            strict=True,
            delimiters=("=",),
            comment_prefixes=("#", ";"),
            inline_comment_prefixes=None,
            empty_lines_in_values=False,
        )
        parser.optionxform = str
        parser.read_string(text, source=str(gitmodules_path))
    except (UnicodeDecodeError, configparser.Error) as exc:
        raise RuntimeError(f"unable to parse .gitmodules safely: {gitmodules_path}") from exc
    if parser.defaults():
        raise RuntimeError("unknown .gitmodules key: DEFAULT")
    submodules: list[dict[str, str]] = []
    seen_names: set[str] = set()
    seen_paths: list[str] = []
    safe_component = re.compile(r"[A-Za-z0-9_.-]+\Z")
    for section in parser.sections():
        match = re.fullmatch(r'submodule "([^"\\]+)"', section, flags=re.IGNORECASE)
        if not match:
            raise RuntimeError(f"unknown .gitmodules section: {section}")
        name = match.group(1)
        name_key = name.casefold()
        if name_key in seen_names:
            raise RuntimeError(f"duplicate submodule name: {name}")
        seen_names.add(name_key)
        name_parts = name.split("/")
        if any(not safe_component.fullmatch(part) or part in {".", "..", ".git"} for part in name_parts):
            raise RuntimeError(f"unsafe submodule name: {name}")
        options: dict[str, str] = {}
        for option, value in parser.items(section, raw=True):
            normalized_option = option.casefold()
            if normalized_option in options:
                raise RuntimeError(f"duplicate .gitmodules key: {section}.{option}")
            if normalized_option not in {"path", "url"}:
                raise RuntimeError(f"unknown .gitmodules key: {section}.{option}")
            options[normalized_option] = value
        if set(options) != {"path", "url"}:
            raise RuntimeError(f"submodule must define exactly path and url: {name}")
        submodule_path = options["path"]
        path_parts = submodule_path.split("/")
        if (
            not submodule_path
            or "\\" in submodule_path
            or any(not safe_component.fullmatch(part) or part in {".", "..", ".git"} for part in path_parts)
        ):
            raise RuntimeError(f"unsafe submodule path: {submodule_path}")
        path_key = submodule_path.casefold()
        for existing in seen_paths:
            if path_key == existing:
                raise RuntimeError(f"duplicate submodule path: {submodule_path}")
            if path_key.startswith(f"{existing}/") or existing.startswith(f"{path_key}/"):
                raise RuntimeError(f"overlapping submodule path: {submodule_path}")
        seen_paths.append(path_key)
        _assert_safe_path(repo_path.joinpath(*path_parts), repo_path)
        resolved_remote = _resolve_submodule_remote(origin, options["url"])
        submodules.append({"name": name, "path": submodule_path, "url": resolved_remote})
    return submodules


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in GIT_BLOCKED_ENV_EXACT
        and not any(key.startswith(prefix) for prefix in GIT_BLOCKED_ENV_PREFIXES)
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_PAGER": "cat",
            "GIT_EDITOR": "true",
            "GIT_SEQUENCE_EDITOR": "true",
            "GIT_ALLOW_PROTOCOL": "https:ssh",
        }
    )
    return environment


def _windows_directory() -> Path:
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise RuntimeError("unable to resolve the trusted Windows system directory")
    directory = Path(buffer.value)
    if not directory.is_absolute():
        raise RuntimeError("Windows system directory is not absolute")
    return directory


def _trusted_executable_candidates(name: str, *, git_executable: Path | None = None) -> list[Path]:
    executable_name = f"{name}.exe" if os.name == "nt" else name
    if os.name != "nt":
        return [Path(directory) / executable_name for directory in ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/opt/local/bin")]
    candidates: list[Path] = []
    windows_directory = _windows_directory()
    drive_root = Path(windows_directory.anchor)
    if name == "git":
        for directory in ("Program Files", "Program Files (x86)"):
            root = drive_root / directory / "Git"
            candidates.extend((root / "cmd" / executable_name, root / "bin" / executable_name))
    else:
        candidates.append(windows_directory / "System32" / "OpenSSH" / executable_name)
        if git_executable is not None:
            git_root = git_executable.parent.parent
            candidates.extend((git_root / "usr" / "bin" / executable_name, git_root / "bin" / executable_name))
    return candidates


def _validate_trusted_executable(
    value: str | Path,
    *,
    name: str,
    managed_roots: tuple[Path, ...] = (),
    git_executable: Path | None = None,
) -> str:
    label = "Git" if name == "git" else "SSH"
    candidate = Path(value)
    expected_names = {name, f"{name}.exe"}
    if not candidate.is_absolute() or candidate.name.lower() not in expected_names:
        raise RuntimeError(f"trusted {label} executable must be an absolute {name} path: {value!s}")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"trusted {label} executable does not exist: {candidate}") from exc
    if os.path.normcase(str(candidate)) != os.path.normcase(str(resolved)):
        raise RuntimeError(f"trusted {label} executable must not use symlink or reparse paths: {candidate}")
    for component in (candidate, *candidate.parents):
        if _is_link_or_reparse(component):
            raise RuntimeError(f"trusted {label} executable must not use symlink or reparse paths: {candidate}")
    if not resolved.is_file() or (os.name != "nt" and not os.access(resolved, os.X_OK)):
        raise RuntimeError(f"trusted {label} executable is not executable: {resolved}")
    environment_variable = TRUSTED_GIT_ENV if name == "git" else TRUSTED_SSH_ENV
    configured_paths = _trusted_executable_candidates(name, git_executable=git_executable)
    explicit = os.environ.get(environment_variable)
    if explicit:
        configured_paths.append(Path(explicit))
    if not any(
        path.is_absolute()
        and os.path.normcase(str(path)) == os.path.normcase(str(resolved))
        for path in configured_paths
    ):
        raise RuntimeError(
            f"trusted {label} executable is not in a fixed installation directory and does not match "
            f"{environment_variable}: {resolved}"
        )
    for root in managed_roots:
        canonical_root = root.resolve(strict=False)
        if resolved == canonical_root or resolved.is_relative_to(canonical_root):
            raise RuntimeError(f"trusted {label} executable must be outside managed root: {resolved}")
    return str(resolved)


def _resolve_trusted_executable(
    name: str,
    *,
    environment_variable: str,
    managed_roots: tuple[Path, ...] = (),
    git_executable: Path | None = None,
) -> str:
    explicit = os.environ.get(environment_variable)
    if explicit:
        return _validate_trusted_executable(
            explicit,
            name=name,
            managed_roots=managed_roots,
            git_executable=git_executable,
        )
    for candidate in _trusted_executable_candidates(name, git_executable=git_executable):
        try:
            return _validate_trusted_executable(
                candidate,
                name=name,
                managed_roots=managed_roots,
                git_executable=git_executable,
            )
        except RuntimeError:
            continue
    label = "Git" if name == "git" else "SSH"
    raise RuntimeError(
        f"trusted {label} executable was not found in fixed installation directories; "
        f"set {environment_variable} to an absolute trusted path"
    )


def _trusted_git_executable(*, managed_roots: tuple[Path, ...] = ()) -> str:
    return _resolve_trusted_executable(
        "git",
        environment_variable=TRUSTED_GIT_ENV,
        managed_roots=managed_roots,
    )


def _trusted_ssh_executable(
    *,
    managed_roots: tuple[Path, ...] = (),
    git_executable: str | Path | None = None,
) -> str:
    git_path = Path(git_executable) if git_executable is not None else None
    return _resolve_trusted_executable(
        "ssh",
        environment_variable=TRUSTED_SSH_ENV,
        managed_roots=managed_roots,
        git_executable=git_path,
    )


def _trusted_ssh_command(executable: str) -> str:
    arguments = [
        executable,
        "-F",
        os.devnull,
        "-oBatchMode=yes",
        "-oPermitLocalCommand=no",
        "-oProxyCommand=none",
    ]
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


def _harden_git_command(
    command: list[str],
    *,
    trusted_file: Path | None = None,
    git_executable: str | None = None,
    ssh_executable: str | None = None,
    managed_roots: tuple[Path, ...] = (),
    requires_ssh: bool = False,
) -> list[str]:
    if not command or Path(command[0]).name.lower() not in {"git", "git.exe"}:
        raise ValueError(f"hardened Git runner requires a git command: {command!r}")
    trusted_git = (
        _validate_trusted_executable(git_executable, name="git", managed_roots=managed_roots)
        if git_executable is not None
        else _trusted_git_executable(managed_roots=managed_roots)
    )
    trusted_ssh = None
    if requires_ssh or ssh_executable is not None:
        trusted_ssh = (
            _validate_trusted_executable(
                ssh_executable,
                name="ssh",
                managed_roots=managed_roots,
                git_executable=Path(trusted_git),
            )
            if ssh_executable is not None
            else _trusted_ssh_executable(managed_roots=managed_roots, git_executable=trusted_git)
        )
    hook_sink = str((trusted_file or Path(__file__)).resolve(strict=False))
    overrides = [
        ("core.hooksPath", hook_sink),
        ("core.fsmonitor", "false"),
        ("credential.helper", ""),
        ("core.sshCommand", _trusted_ssh_command(trusted_ssh) if trusted_ssh else "tts-more-ssh-disabled"),
        ("protocol.allow", "never"),
        ("protocol.https.allow", "always"),
        ("protocol.ssh.allow", "always"),
        ("protocol.file.allow", "never"),
        ("protocol.ext.allow", "never"),
    ]
    prefix = [trusted_git]
    for key, value in overrides:
        prefix.extend(["-c", f"{key}={value}"])
    return [*prefix, *command[1:]]


def _git_command_repo_path(command: list[str], cwd: Path) -> Path | None:
    for index, argument in enumerate(command[:-1]):
        if argument == "-C":
            candidate = Path(command[index + 1])
            return candidate if candidate.is_absolute() else cwd / candidate
    return cwd if (cwd / ".git" / "config").is_file() else None


def _validate_local_git_config_value(
    section: str,
    option: str,
    value: str,
    *,
    config_path: Path | None = None,
    expected_worktree: Path | None = None,
) -> None:
    normalized_section = section.lower()
    normalized_option = option.lower()
    subsection = re.fullmatch(r'([^" ]+) "([^"\\]+)"', section)
    display_key = (
        f"{subsection.group(1)}.{subsection.group(2)}.{option}"
        if subsection
        else f"{section}.{option}"
    )
    boolean_values = {"true", "false", "yes", "no", "on", "off", "1", "0"}
    core_validators: dict[str, Callable[[str], bool]] = {
        "repositoryformatversion": lambda item: item == "0",
        "filemode": lambda item: item.lower() in boolean_values,
        "bare": lambda item: item.lower() == "false",
        "logallrefupdates": lambda item: item.lower() in boolean_values,
        "ignorecase": lambda item: item.lower() in boolean_values,
        "precomposeunicode": lambda item: item.lower() in boolean_values,
        "symlinks": lambda item: item.lower() in boolean_values,
    }
    if normalized_section == "core" and normalized_option in core_validators:
        valid = core_validators[normalized_option](value)
    elif normalized_section == "core" and normalized_option == "worktree":
        candidate = Path(value)
        valid = bool(
            config_path is not None
            and expected_worktree is not None
            and not candidate.is_absolute()
            and os.path.normcase(str((config_path.parent / candidate).resolve(strict=False)))
            == os.path.normcase(str(expected_worktree.resolve(strict=False)))
        )
    elif normalized_section == 'remote "origin"':
        if normalized_option == "url":
            try:
                _parse_github_remote(value)
                valid = True
            except ValueError:
                valid = False
        elif normalized_option == "fetch":
            refspec = re.fullmatch(
                r"\+?refs/heads/(\*|[A-Za-z0-9][A-Za-z0-9._/-]*):refs/remotes/origin/(\*|[A-Za-z0-9][A-Za-z0-9._/-]*)",
                value,
            )
            valid = bool(refspec and refspec.group(1) == refspec.group(2))
        else:
            valid = False
    else:
        branch_match = re.fullmatch(r'branch "([^"\\]+)"', section, flags=re.IGNORECASE)
        valid = False
        if branch_match:
            branch = branch_match.group(1)
            try:
                _validate_branch(branch)
            except ValueError:
                pass
            else:
                valid = (normalized_option == "remote" and value == "origin") or (
                    normalized_option == "merge" and value == f"refs/heads/{branch}"
                )
    if not valid:
        raise RuntimeError(f"local Git config key is not allowlisted or has an unsafe value: {display_key}")


def _audit_local_git_config(
    repo_path: Path,
    *,
    environment: Mapping[str, str] | None = None,
    config_path: Path | None = None,
    expected_worktree: Path | None = None,
) -> dict[str, str]:
    config_path = config_path or repo_path / ".git" / "config"
    if _is_link_or_reparse(config_path):
        raise RuntimeError(f"local Git config must not be a symlink or reparse point: {config_path}")
    if not config_path.exists():
        return {}
    try:
        payload = config_path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"unable to read local Git config: {config_path}") from exc
    if len(payload) > MAX_LOCAL_GIT_CONFIG_BYTES or b"\0" in payload:
        raise RuntimeError(f"local Git config is oversized or contains NUL bytes: {config_path}")
    try:
        text = payload.decode("utf-8", errors="strict")
        parser = configparser.RawConfigParser(
            interpolation=None,
            strict=True,
            delimiters=("=",),
            comment_prefixes=("#", ";"),
            inline_comment_prefixes=None,
            empty_lines_in_values=False,
        )
        parser.optionxform = str
        parser.read_string(text, source=str(config_path))
    except (UnicodeDecodeError, configparser.Error) as exc:
        raise RuntimeError(f"unable to parse local Git config safely: {config_path}") from exc
    if parser.defaults():
        raise RuntimeError("local Git config key is not allowlisted: DEFAULT")
    seen: set[str] = set()
    values: dict[str, str] = {}
    for section in parser.sections():
        for option, value in parser.items(section, raw=True):
            normalized_key = f"{section}.{option}".lower()
            if normalized_key in seen:
                raise RuntimeError(f"duplicate local Git config key: {section}.{option}")
            seen.add(normalized_key)
            _validate_local_git_config_value(
                section,
                option,
                value,
                config_path=config_path,
                expected_worktree=expected_worktree,
            )
            values[normalized_key] = value
    return values


def _audit_nested_submodule_config(
    repo_path: Path,
    superproject_path: Path,
    expected_remote: str,
) -> str:
    _parse_github_remote(expected_remote)
    dot_git = repo_path / ".git"
    _assert_safe_path(dot_git, superproject_path)
    if _is_link_or_reparse(dot_git) or not dot_git.is_file():
        raise RuntimeError(f"nested submodule .git must be a regular gitdir file: {dot_git}")
    try:
        payload = dot_git.read_bytes()
        text = payload.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"unable to read nested submodule gitdir safely: {dot_git}") from exc
    lines = text.splitlines()
    if len(payload) > 4096 or b"\0" in payload or len(lines) != 1 or not lines[0].startswith("gitdir: "):
        raise RuntimeError(f"invalid nested submodule gitdir file: {dot_git}")
    raw_git_dir = lines[0][len("gitdir: ") :]
    git_dir_reference = Path(raw_git_dir)
    if (
        not raw_git_dir
        or git_dir_reference.is_absolute()
        or any(ord(character) < 32 for character in raw_git_dir)
    ):
        raise RuntimeError(f"invalid nested submodule gitdir file: {dot_git}")
    try:
        git_dir = (repo_path / git_dir_reference).resolve(strict=True)
        modules_boundary = (superproject_path / ".git" / "modules").resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"invalid nested submodule gitdir target: {dot_git}") from exc
    if git_dir == modules_boundary or not git_dir.is_relative_to(modules_boundary):
        raise RuntimeError(f"nested submodule gitdir escapes superproject metadata: {git_dir}")
    _assert_safe_path(git_dir, superproject_path)
    config_path = git_dir / "config"
    values = _audit_local_git_config(
        repo_path,
        config_path=config_path,
        expected_worktree=repo_path,
    )
    actual_remote = values.get('remote "origin".url')
    if not actual_remote:
        raise RuntimeError(f"nested submodule origin is missing: {repo_path}")
    if _canonical_remote_identity(actual_remote) != _canonical_remote_identity(expected_remote):
        raise RuntimeError(
            f"nested submodule origin mismatch at {repo_path}: expected {expected_remote!r}, "
            f"found {actual_remote!r}"
        )
    return actual_remote


def _git_command_verb(command: list[str]) -> str:
    index = 1
    while index < len(command):
        if command[index] in {"-C", "-c"} and index + 1 < len(command):
            index += 2
            continue
        return command[index].lower()
    return ""


def _git_command_requires_ssh(
    command: list[str],
    *,
    local_config: Mapping[str, str],
    validated_submodule_remotes: tuple[str, ...] | None = None,
) -> bool:
    verb = _git_command_verb(command)
    if verb == "clone" and "--" in command:
        separator = command.index("--")
        return separator + 1 < len(command) and _github_remote_requires_ssh(command[separator + 1])
    if verb in {"fetch", "pull"}:
        remote = local_config.get('remote "origin".url')
        return bool(remote and _github_remote_requires_ssh(remote))
    if verb == "submodule":
        if validated_submodule_remotes is None:
            raise RuntimeError("submodule Git commands require prevalidated remotes")
        return any(_github_remote_requires_ssh(remote) for remote in validated_submodule_remotes)
    return False


def _run_git_process(
    command: list[str],
    *,
    cwd: Path,
    check: bool,
    capture_output: bool = False,
    text: bool = False,
    validated_submodule_remotes: tuple[str, ...] | None = None,
) -> subprocess.CompletedProcess[Any]:
    environment = _git_environment()
    repo_path = _git_command_repo_path(command, cwd)
    local_config: dict[str, str] = {}
    if repo_path is not None:
        local_config = _audit_local_git_config(repo_path.resolve(strict=False), environment=environment)
    managed_roots = tuple(path for path in (cwd, repo_path) if path is not None)
    requires_ssh = _git_command_requires_ssh(
        command,
        local_config=local_config,
        validated_submodule_remotes=validated_submodule_remotes,
    )
    return subprocess.run(
        _harden_git_command(
            command,
            managed_roots=managed_roots,
            requires_ssh=requires_ssh,
        ),
        cwd=cwd,
        env=environment,
        capture_output=capture_output,
        text=text,
        check=check,
    )


def _validate_git_checkout(repo_path: Path) -> Path:
    dot_git = repo_path / ".git"
    if _is_link_or_reparse(dot_git):
        raise ValueError(f"Git metadata must not be a symlink or reparse point: {dot_git}")
    if not dot_git.exists():
        raise RuntimeError(f"path is not a supported Git checkout: {repo_path}")
    if not dot_git.is_dir():
        raise RuntimeError(
            f"Git worktree/submodule gitdir files are not supported; .git must be a directory: {dot_git}"
        )
    _assert_safe_path(dot_git, repo_path)
    inside = _git_output(["git", "-C", str(repo_path), "rev-parse", "--is-inside-work-tree"])
    absolute_git_dir = _git_output(["git", "-C", str(repo_path), "rev-parse", "--absolute-git-dir"])
    if inside != "true" or not absolute_git_dir:
        raise RuntimeError(f"corrupt Git metadata directory: {dot_git}")
    if Path(absolute_git_dir).resolve(strict=False) != dot_git.resolve(strict=False):
        raise RuntimeError(
            f"Git metadata resolves outside the supported checkout-local .git directory: {absolute_git_dir}"
        )
    return dot_git


def _git_exclude_path(repo_path: Path) -> Path | None:
    dot_git = _validate_git_checkout(repo_path)
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
    actions: list[dict[str, Any]],
) -> None:
    command = _clone_command(remote, branch, path)
    actions.append({"action": "git", "argv": command})
    if dry_run:
        return
    _run_git_command(command, cwd=root)


def _run_clone(
    root: Path,
    remote: str,
    branch: str,
    path: Path,
    dry_run: bool,
    actions: list[dict[str, Any]],
) -> None:
    _run_clone_with_fallback(root, remote, branch, path, dry_run, actions)


def _validated_submodule_update_command(
    repo_path: Path,
    submodules: list[dict[str, str]],
) -> list[str]:
    command = ["git"]
    for submodule in submodules:
        name = submodule["name"]
        command.extend(
            [
                "-c",
                f"submodule.{name}.url={submodule['url']}",
                "-c",
                f"submodule.{name}.active=true",
            ]
        )
    command.extend(
        [
            "-C",
            str(repo_path),
            "submodule",
            "update",
            "--",
            *(submodule["path"] for submodule in submodules),
        ]
    )
    return command


def _sync_validated_submodules(
    root: Path,
    repo_path: Path,
    origin: str,
    actions: list[dict[str, Any]],
    *,
    depth: int = 0,
    metadata_root: Path | None = None,
) -> None:
    if depth > 32:
        raise RuntimeError(f"submodule nesting exceeds supported depth at {repo_path}")
    submodules = _load_validated_submodules(repo_path, origin)
    if not submodules:
        return
    metadata_root = metadata_root or repo_path
    command = _validated_submodule_update_command(repo_path, submodules)
    remotes = tuple(submodule["url"] for submodule in submodules)
    actions.append(
        {
            "action": "git",
            "argv": command,
            "validated_submodule_remotes": list(remotes),
        }
    )
    _run_git_command(
        command,
        cwd=root,
        validated_submodule_remotes=remotes,
    )
    for submodule in submodules:
        child_path = repo_path.joinpath(*submodule["path"].split("/"))
        if _is_link_or_reparse(child_path):
            raise RuntimeError(f"submodule path must not be a symlink or reparse point: {child_path}")
        if not child_path.is_dir():
            raise RuntimeError(f"submodule update did not create a directory: {child_path}")
        _assert_safe_path(child_path, repo_path)
        actual_child_origin = _audit_nested_submodule_config(
            child_path,
            metadata_root,
            submodule["url"],
        )
        _sync_validated_submodules(
            root,
            child_path,
            actual_child_origin,
            actions,
            depth=depth + 1,
            metadata_root=metadata_root,
        )


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
) -> list[dict[str, Any]]:
    repositories = _select_repositories(
        [dict(repo) for repo in (repositories or load_repo_lock(root))],
        service_ids,
    )
    actions: list[dict[str, Any]] = []
    resolved_repositories = _validate_selected_repository_paths(root, repositories)
    for repo, _path in resolved_repositories:
        _validate_service_id(str(repo["service_id"]))
        _parse_github_remote(str(repo["remote"]))

    if clean:
        for repo, path in resolved_repositories:
            if not path.exists():
                continue
            _validate_git_checkout(path)
            _ensure_clean_repo(path, str(repo.get("name") or repo["service_id"]))
            _ensure_repo_origin(path, str(repo["remote"]))
            actions.append({"action": "remove-repository", "path": str(path)})
        if not dry_run:
            for action in actions:
                _remove_path(Path(str(action["path"])))

    lock_updates: dict[str, str] = {}
    for repo, path in resolved_repositories:
        remote = str(repo["remote"])
        actual_origin = remote
        branch = str(repo["branch"])
        commit = repo.get("commit")
        will_clone = clean or not path.exists()
        if not will_clone:
            _validate_git_checkout(path)
            if not force_reset:
                _ensure_clean_repo(path, str(repo.get("name") or repo.get("service_id") or path.name))
            actual_origin = _ensure_repo_origin(path, remote)
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
            _run_clone(
                root,
                remote=remote,
                branch=branch,
                path=path,
                dry_run=dry_run,
                actions=actions,
            )
            commands = []
        for command in commands:
            actions.append({"action": "git", "argv": command})
            if not dry_run:
                _run_git_command(command, cwd=root)
        if latest:
            if not dry_run and write_lock:
                head = _git_output(["git", "-C", str(path), "rev-parse", "HEAD"])
                if not PINNED_COMMIT_RE.fullmatch(head):
                    raise RuntimeError(f"unable to resolve final HEAD for repository {repo['service_id']}")
                repo["commit"] = head
                lock_updates[str(repo["service_id"])] = head
            if repo.get("submodules"):
                if dry_run:
                    actions.append(
                        {
                            "action": "validate-and-update-submodules",
                            "path": str(path),
                            "origin": actual_origin,
                            "after": "final-superproject-selection",
                        }
                    )
                else:
                    _sync_validated_submodules(root, path, actual_origin, actions)
            continue
        if commit:
            checkout_command = ["git", "-C", str(path), "checkout", str(commit)]
            fetch_command = ["git", "-C", str(path), "fetch", "origin", str(commit)]
            if will_clone:
                actions.append({"action": "git", "argv": fetch_command})
                actions.append({"action": "git", "argv": checkout_command})
                if not dry_run:
                    _run_git_command(fetch_command, cwd=root)
                    _run_git_command(checkout_command, cwd=root)
            elif dry_run:
                head = _git_output(["git", "-C", str(path), "rev-parse", "HEAD"])
                if head != str(commit):
                    actions.append({"action": "git", "argv": fetch_command})
                    actions.append({"action": "git", "argv": checkout_command})
            else:
                head = _git_output(["git", "-C", str(path), "rev-parse", "HEAD"])
                if head != str(commit):
                    actions.append({"action": "git", "argv": fetch_command})
                    _run_git_command(fetch_command, cwd=root)
                    actions.append({"action": "git", "argv": checkout_command})
                    _run_git_command(checkout_command, cwd=root)
        if repo.get("submodules"):
            if dry_run:
                actions.append(
                    {
                        "action": "validate-and-update-submodules",
                        "path": str(path),
                        "origin": actual_origin,
                        "after": "final-superproject-selection",
                    }
                )
            else:
                _sync_validated_submodules(root, path, actual_origin, actions)
    if lock_updates and not dry_run:
        _save_repo_lock_commit_updates(root, lock_updates)
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

import configparser
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}\Z")
GITHUB_COMPONENT_RE = re.compile(r"[A-Za-z0-9_.-]+\Z")
BLOCKED_GIT_ENV = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_PROXY_COMMAND",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_EXEC_PATH",
    "GIT_TEMPLATE_DIR",
    "GIT_EXTERNAL_DIFF",
    "GIT_SSH_VARIANT",
    "GIT_SSL_NO_VERIFY",
    "GIT_SSL_CERT",
    "GIT_SSL_KEY",
    "GIT_SSL_CAINFO",
    "GIT_SSL_CAPATH",
    "GIT_ALLOW_PROTOCOL",
}
MAX_LOCAL_GIT_CONFIG_BYTES = 1024 * 1024
TRUSTED_GIT_ENV = "TTS_MORE_TRUSTED_GIT"
TRUSTED_SSH_ENV = "TTS_MORE_TRUSTED_SSH"
UPDATER_EXECUTABLE_POLICY = "fixed-dirs-or-explicit-env-v1"


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


def parse_github_remote(value: str) -> tuple[str, str, str]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"unsupported GitHub remote: {value!r}")
    if value.startswith("-") or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"unsupported GitHub remote: {value!r}")
    if "%" in value or "::" in value:
        raise ValueError(f"unsupported GitHub remote: {value!r}")
    if "://" not in value:
        match = re.fullmatch(r"git@github\.com:(.+)", value, flags=re.IGNORECASE)
        if not match:
            raise ValueError(f"unsupported GitHub remote: {value!r}")
        path = match.group(1)
    else:
        try:
            parsed = urlparse(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"unsupported GitHub remote: {value!r}") from exc
        scheme = parsed.scheme.lower()
        if parsed.query or parsed.fragment or parsed.params or parsed.hostname != "github.com":
            raise ValueError(f"unsupported GitHub remote: {value!r}")
        if scheme == "https":
            if parsed.username is not None or parsed.password is not None or port not in (None, 443):
                raise ValueError(f"unsupported GitHub remote: {value!r}")
        elif scheme == "ssh":
            if parsed.username != "git" or parsed.password is not None or port not in (None, 22):
                raise ValueError(f"unsupported GitHub remote: {value!r}")
        else:
            raise ValueError(f"unsupported GitHub remote: {value!r}")
        path = parsed.path.lstrip("/")
    normalized = path.rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    parts = normalized.split("/")
    if (
        len(parts) != 2
        or any(part in {"", ".", ".."} for part in parts)
        or any(not GITHUB_COMPONENT_RE.fullmatch(part) for part in parts)
    ):
        raise ValueError(f"unsupported GitHub remote: {value!r}")
    return ("github.com", parts[0].lower(), parts[1].lower())


def remote_requires_ssh(value: str) -> bool:
    parse_github_remote(value)
    return "://" not in value or urlparse(value).scheme.lower() == "ssh"


def git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in BLOCKED_GIT_ENV and not key.startswith("GIT_CONFIG_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_PAGER": "cat",
            "GIT_EDITOR": "true",
            "GIT_SEQUENCE_EDITOR": "true",
            "GIT_ALLOW_PROTOCOL": "https:ssh",
        }
    )
    return environment


def windows_directory() -> Path:
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise RuntimeError("unable to resolve the trusted Windows system directory")
    directory = Path(buffer.value)
    if not directory.is_absolute():
        raise RuntimeError("Windows system directory is not absolute")
    return directory


def trusted_executable_candidates(name: str, *, git_executable: Path | None = None) -> list[Path]:
    executable_name = f"{name}.exe" if os.name == "nt" else name
    if os.name != "nt":
        return [Path(directory) / executable_name for directory in ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/opt/local/bin")]
    system_directory = windows_directory()
    drive_root = Path(system_directory.anchor)
    candidates = []
    if name == "git":
        for directory in ("Program Files", "Program Files (x86)"):
            root = drive_root / directory / "Git"
            candidates.extend((root / "cmd" / executable_name, root / "bin" / executable_name))
    else:
        candidates.append(system_directory / "System32" / "OpenSSH" / executable_name)
        if git_executable is not None:
            git_root = git_executable.parent.parent
            candidates.extend((git_root / "usr" / "bin" / executable_name, git_root / "bin" / executable_name))
    return candidates


def validate_trusted_executable(
    value: object,
    *,
    name: str,
    root: Path,
    git_executable: Path | None = None,
) -> str:
    label = "Git" if name == "git" else "SSH"
    if not isinstance(value, str):
        raise RuntimeError(f"trusted {label} executable is missing from updater sidecar")
    candidate = Path(value)
    if not candidate.is_absolute() or candidate.name.lower() not in {name, f"{name}.exe"}:
        raise RuntimeError(f"trusted {label} executable must be an absolute {name} path: {value!s}")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"trusted {label} executable does not exist: {candidate}") from exc
    if os.path.normcase(str(candidate)) != os.path.normcase(str(resolved)):
        raise RuntimeError(f"trusted {label} executable must not use symlink or reparse paths: {candidate}")
    for component in (candidate, *candidate.parents):
        if is_link_or_reparse(component):
            raise RuntimeError(f"trusted {label} executable must not use symlink or reparse paths: {candidate}")
    if not resolved.is_file() or (os.name != "nt" and not os.access(resolved, os.X_OK)):
        raise RuntimeError(f"trusted {label} executable is not executable: {resolved}")
    environment_variable = TRUSTED_GIT_ENV if name == "git" else TRUSTED_SSH_ENV
    configured_paths = trusted_executable_candidates(name, git_executable=git_executable)
    explicit = os.environ.get(environment_variable)
    if explicit:
        configured_paths.append(Path(explicit))
    if not any(
        path.is_absolute()
        and os.path.normcase(str(path)) == os.path.normcase(str(resolved))
        for path in configured_paths
    ):
        raise RuntimeError(
            f"trusted {label} executable is not in a fixed installation directory and does not match "
            f"{environment_variable}: {resolved}"
        )
    canonical_root = root.resolve(strict=False)
    if resolved == canonical_root or resolved.is_relative_to(canonical_root):
        raise RuntimeError(f"trusted {label} executable must be outside managed root: {resolved}")
    return str(resolved)


def resolve_trusted_executable(
    name: str,
    *,
    root: Path,
    git_executable: Path | None = None,
) -> str:
    environment_variable = TRUSTED_GIT_ENV if name == "git" else TRUSTED_SSH_ENV
    explicit = os.environ.get(environment_variable)
    if explicit:
        return validate_trusted_executable(
            explicit,
            name=name,
            root=root,
            git_executable=git_executable,
        )
    for candidate in trusted_executable_candidates(name, git_executable=git_executable):
        try:
            return validate_trusted_executable(
                str(candidate),
                name=name,
                root=root,
                git_executable=git_executable,
            )
        except RuntimeError:
            continue
    label = "Git" if name == "git" else "SSH"
    raise RuntimeError(
        f"trusted {label} executable was not found in fixed installation directories; "
        f"set {environment_variable} to an absolute trusted path"
    )


def trusted_ssh_command(executable: str) -> str:
    arguments = [
        executable,
        "-F",
        os.devnull,
        "-oBatchMode=yes",
        "-oPermitLocalCommand=no",
        "-oProxyCommand=none",
    ]
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


def harden_git_command(
    args: list[str],
    git_executable: str,
    ssh_executable: str | None,
) -> list[str]:
    overrides = [
        ("core.hooksPath", str(Path(__file__).resolve(strict=False))),
        ("core.fsmonitor", "false"),
        ("credential.helper", ""),
        ("core.sshCommand", trusted_ssh_command(ssh_executable) if ssh_executable else "tts-more-ssh-disabled"),
        ("protocol.allow", "never"),
        ("protocol.https.allow", "always"),
        ("protocol.ssh.allow", "always"),
        ("protocol.file.allow", "never"),
        ("protocol.ext.allow", "never"),
    ]
    command = [git_executable]
    for key, value in overrides:
        command.extend(["-c", f"{key}={value}"])
    return [*command, *args[1:]]


def validate_local_git_config_value(section: str, option: str, value: str) -> None:
    normalized_section = section.lower()
    normalized_option = option.lower()
    subsection = re.fullmatch(r'([^" ]+) "([^"\\]+)"', section)
    display_key = (
        f"{subsection.group(1)}.{subsection.group(2)}.{option}"
        if subsection
        else f"{section}.{option}"
    )
    boolean_values = {"true", "false", "yes", "no", "on", "off", "1", "0"}
    core_validators = {
        "repositoryformatversion": lambda item: item == "0",
        "filemode": lambda item: item.lower() in boolean_values,
        "bare": lambda item: item.lower() == "false",
        "logallrefupdates": lambda item: item.lower() in boolean_values,
        "ignorecase": lambda item: item.lower() in boolean_values,
        "precomposeunicode": lambda item: item.lower() in boolean_values,
        "symlinks": lambda item: item.lower() in boolean_values,
    }
    if normalized_section == "core" and normalized_option in core_validators:
        valid = core_validators[normalized_option](value)
    elif normalized_section == 'remote "origin"':
        if normalized_option == "url":
            try:
                parse_github_remote(value)
                valid = True
            except ValueError:
                valid = False
        elif normalized_option == "fetch":
            refspec = re.fullmatch(
                r"\+?refs/heads/(\*|[A-Za-z0-9][A-Za-z0-9._/-]*):refs/remotes/origin/(\*|[A-Za-z0-9][A-Za-z0-9._/-]*)",
                value,
            )
            valid = bool(refspec and refspec.group(1) == refspec.group(2))
        else:
            valid = False
    else:
        branch_match = re.fullmatch(r'branch "([^"\\]+)"', section, flags=re.IGNORECASE)
        valid = False
        if branch_match:
            branch = branch_match.group(1)
            try:
                validate_branch(branch)
            except ValueError:
                pass
            else:
                valid = (normalized_option == "remote" and value == "origin") or (
                    normalized_option == "merge" and value == f"refs/heads/{branch}"
                )
    if not valid:
        raise RuntimeError(f"local Git config key is not allowlisted or has an unsafe value: {display_key}")


def audit_local_git_config(root: Path) -> None:
    config_path = root / ".git" / "config"
    if is_link_or_reparse(config_path):
        raise RuntimeError(f"local Git config must not be a symlink or reparse point: {config_path}")
    if not config_path.exists():
        return
    try:
        payload = config_path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"unable to read local Git config: {config_path}") from exc
    if len(payload) > MAX_LOCAL_GIT_CONFIG_BYTES or b"\0" in payload:
        raise RuntimeError(f"local Git config is oversized or contains NUL bytes: {config_path}")
    try:
        text = payload.decode("utf-8", errors="strict")
        parser = configparser.RawConfigParser(
            interpolation=None,
            strict=True,
            delimiters=("=",),
            comment_prefixes=("#", ";"),
            inline_comment_prefixes=None,
            empty_lines_in_values=False,
        )
        parser.optionxform = str
        parser.read_string(text, source=str(config_path))
    except (UnicodeDecodeError, configparser.Error) as exc:
        raise RuntimeError(f"unable to parse local Git config safely: {config_path}") from exc
    if parser.defaults():
        raise RuntimeError("local Git config key is not allowlisted: DEFAULT")
    seen = set()
    for section in parser.sections():
        for option, value in parser.items(section, raw=True):
            normalized_key = f"{section}.{option}".lower()
            if normalized_key in seen:
                raise RuntimeError(f"duplicate local Git config key: {section}.{option}")
            seen.add(normalized_key)
            validate_local_git_config_value(section, option, value)


def is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))


def output(args: list[str], root: Path, git_executable: str, ssh_executable: str | None) -> str:
    audit_local_git_config(root)
    result = subprocess.run(
        harden_git_command(args, git_executable, ssh_executable),
        cwd=root,
        env=git_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"command failed: {args!r}")
    return result.stdout.strip()


def run(args: list[str], root: Path, git_executable: str, ssh_executable: str | None) -> None:
    audit_local_git_config(root)
    subprocess.run(
        harden_git_command(args, git_executable, ssh_executable),
        cwd=root,
        env=git_environment(),
        check=True,
    )


def validate_git_checkout(root: Path, git_executable: str, ssh_executable: str | None) -> None:
    dot_git = root / ".git"
    if is_link_or_reparse(dot_git):
        raise ValueError(f"Git metadata must not be a symlink or reparse point: {dot_git}")
    if not dot_git.exists():
        raise RuntimeError(f"path is not a supported Git checkout: {root}")
    if not dot_git.is_dir():
        raise RuntimeError(f"Git worktree/submodule gitdir files are not supported: {dot_git}")
    audit_local_git_config(root)
    inside = output(["git", "rev-parse", "--is-inside-work-tree"], root, git_executable, ssh_executable)
    absolute_git_dir = output(["git", "rev-parse", "--absolute-git-dir"], root, git_executable, ssh_executable)
    if inside != "true" or Path(absolute_git_dir).resolve(strict=False) != dot_git.resolve(strict=False):
        raise RuntimeError(f"corrupt or redirected Git metadata: {dot_git}")


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent
    config = json.loads((root / "tts-more-update.json").read_text(encoding="utf-8"))
    expected_keys = {
        "schema_version",
        "executable_policy",
        "requires_ssh",
        "service_id",
        "name",
        "remote",
        "branch",
        "commit",
    }
    if not isinstance(config, dict) or set(config) != expected_keys or config.get("schema_version") != 3:
        raise ValueError("unsupported updater sidecar schema")
    if config.get("executable_policy") != UPDATER_EXECUTABLE_POLICY:
        raise ValueError("unsupported updater executable policy")
    remote = config.get("remote")
    if not isinstance(remote, str):
        raise ValueError("updater remote must be a string")
    expected_identity = parse_github_remote(remote)
    expected_requires_ssh = remote_requires_ssh(remote)
    if not isinstance(config.get("requires_ssh"), bool) or config["requires_ssh"] != expected_requires_ssh:
        raise ValueError("updater requires_ssh does not match remote")
    configured_branch = config.get("branch")
    configured_commit = config.get("commit")
    if not isinstance(configured_branch, str) or not isinstance(configured_commit, str):
        raise ValueError("updater branch and commit must be strings")
    branch = validate_branch(os.environ.get("TTS_MORE_UPDATE_BRANCH") or configured_branch)
    commit = os.environ.get("TTS_MORE_PINNED_COMMIT") or configured_commit
    if commit and not COMMIT_RE.fullmatch(commit):
        raise ValueError(f"invalid pinned commit: {commit!r}")
    git_executable = resolve_trusted_executable("git", root=root)
    validate_git_checkout(root, git_executable, None)
    actual_remote = output(["git", "remote", "get-url", "origin"], root, git_executable, None)
    if parse_github_remote(actual_remote) != expected_identity:
        raise RuntimeError(
            f"repository origin mismatch: expected {remote!r}, found {actual_remote!r}"
        )
    actual_requires_ssh = remote_requires_ssh(actual_remote)
    ssh_executable = (
        resolve_trusted_executable("ssh", root=root, git_executable=Path(git_executable))
        if actual_requires_ssh
        else None
    )
    dirty = output(["git", "status", "--porcelain"], root, git_executable, ssh_executable)
    if dirty:
        raise RuntimeError("refusing to update a dirty repository; commit, stash, or clean local changes first")
    print(f"[update] {config.get('name') or config.get('service_id') or branch}")
    print(f"[remote] {remote}")
    run(["git", "fetch", "--prune", "origin", branch], root, git_executable, ssh_executable)
    run(["git", "checkout", branch], root, git_executable, ssh_executable)
    run(["git", "pull", "--ff-only", "origin", branch], root, git_executable, ssh_executable)
    if argv and argv[0] == "--pinned" and commit:
        run(["git", "fetch", "origin", commit], root, git_executable, ssh_executable)
        run(["git", "checkout", commit], root, git_executable, ssh_executable)
    run(["git", "status", "--short", "--branch"], root, git_executable, ssh_executable)
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
    _validate_selected_repository_paths(root, selected)
    for repo in selected:
        service_id = str(repo["service_id"])
        _validate_service_id(service_id)
        if repo.get("submodules"):
            repo_path = _resolve_repo_path(root, str(repo["path"]))
            reports.append(
                {
                    "name": repo.get("name"),
                    "path": str(repo.get("path")),
                    "exists": repo_path.exists(),
                    "standalone_updater": False,
                    "managed_sync_required": True,
                    "message": (
                        "submodule repositories must be updated from TTS More managed sync-repos; "
                        "the standalone updater is not installed"
                    ),
                    "scripts": [],
                    "actions": [],
                }
            )
            continue
        branch = str(repo["branch"])
        commit = str(repo.get("commit") or "")
        _parse_github_remote(str(repo["remote"]))
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
        if exists:
            _validate_git_checkout(repo_path)
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
        remote = str(repo["remote"])
        sidecar = {
            "schema_version": 3,
            "executable_policy": UPDATER_EXECUTABLE_POLICY,
            "requires_ssh": _github_remote_requires_ssh(remote),
            "service_id": service_id,
            "name": repo.get("name"),
            "remote": remote,
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
    adopt_existing: bool = False,
    repositories: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    reports = []
    selected = _select_repositories(repositories or load_repo_lock(root), service_ids)
    _validate_selected_repository_paths(root, selected)
    for repo in selected:
        service_id = _validate_service_id(str(repo["service_id"]))
        _parse_github_remote(str(repo["remote"]))
        provider = str(repo.get("provider_type") or "")
        bundle_path = root / REPO_BUNDLE_RELATIVE_PATH / provider
        source_bundle = (REPO_BUNDLE_RELATIVE_PATH / provider).as_posix()
        repo_path = _resolve_repo_path(root, str(repo["path"]))
        target_path = repo_path / "tts-more"
        manifest_path = target_path / "tts-more-repo.json"
        pending_path = target_path / BUNDLE_PENDING_MANIFEST
        anchor_path = root / BUNDLE_OWNERSHIP_RELATIVE_PATH / f"{service_id}.json"
        repo_relative_path = repo_path.relative_to(root).as_posix()
        anchor_identity = {
            "service_id": service_id,
            "provider_type": provider,
            "source_bundle": source_bundle,
            "repo_path": repo_relative_path,
        }
        report = {
            "name": repo.get("name"),
            "provider_type": provider,
            "path": str(repo.get("path")),
            "exists": repo_path.exists(),
            "bundle": str(bundle_path.relative_to(root)) if bundle_path.exists() else "",
            "target": str(target_path.relative_to(root)),
            "launchers": [],
            "installed": False,
            "adopted": False,
            "actions": [],
        }
        reports.append(report)
        if repo_path.exists():
            _validate_git_checkout(repo_path)
        if not bundle_path.exists():
            report["error"] = f"missing bundle for provider: {provider}"
            continue
        _assert_safe_path(bundle_path, root)
        source_files = _bundle_inventory(bundle_path)
        current_owned = {
            relative: hashlib.sha256(source.read_bytes()).hexdigest()
            for relative, source in source_files.items()
        }
        source_hash = _bundle_source_hash(current_owned)
        manifest = {
            "schema_version": BUNDLE_MANIFEST_SCHEMA_VERSION,
            "service_id": service_id,
            "provider_type": provider,
            "source_bundle": source_bundle,
            "source_hash": source_hash,
            "owned_files": current_owned,
        }
        _assert_safe_path(target_path, repo_path)
        _assert_safe_path(manifest_path, repo_path)
        _assert_safe_path(pending_path, repo_path)
        _assert_safe_path(anchor_path, root)
        trusted_previous = _read_previous_owned_files(
            manifest_path,
            target_path,
            service_id=service_id,
            provider=provider,
            source_bundle=source_bundle,
        ) if manifest_path.exists() else {}
        desired_manifest_hash = _bundle_manifest_hash(manifest)
        current_manifest_hash = _sha256_file(manifest_path) if manifest_path.exists() else None
        anchor = _read_bundle_anchor(anchor_path, expected_identity=anchor_identity) if anchor_path.exists() else None

        if adopt_existing:
            if anchor is not None:
                raise RuntimeError(f"bundle ownership is already anchored: {anchor_path}")
            if pending_path.exists():
                raise RuntimeError(f"cannot adopt an interrupted or unanchored pending install: {pending_path}")
            if not manifest_path.exists():
                raise RuntimeError(f"no existing bundle manifest to adopt: {manifest_path}")
            _require_owned_files_match(target_path, trusted_previous)
            report["actions"].append(
                {"action": "adopt-ownership", "path": anchor_path.relative_to(root).as_posix()}
            )
            if not dry_run:
                write_json(
                    anchor_path,
                    _installed_bundle_anchor(anchor_identity, str(current_manifest_hash)),
                    boundary=root,
                )
                report["adopted"] = True
            continue

        if anchor is None and (manifest_path.exists() or pending_path.exists()):
            raise RuntimeError(
                "unanchored bundle ownership is not trusted; inspect the target and run "
                f"install-repo-bundles --adopt-existing only to adopt it: {manifest_path}"
            )

        resuming = pending_path.exists() or bool(anchor and anchor["state"] == "pending")
        if anchor is None:
            previous_owned = {}
        elif anchor["state"] == "installed":
            if current_manifest_hash is None or anchor["manifest_hash"] != current_manifest_hash:
                raise RuntimeError(f"bundle ownership anchor does not match target manifest: {anchor_path}")
            previous_owned = trusted_previous
        else:
            if anchor["desired_manifest_hash"] != desired_manifest_hash:
                raise RuntimeError(
                    "pending bundle ownership anchor does not match current inputs; rerun the original command: "
                    f"{anchor_path}"
                )
            previous_hash = anchor["previous_manifest_hash"]
            if current_manifest_hash == desired_manifest_hash:
                previous_owned = current_owned
            elif current_manifest_hash == previous_hash:
                previous_owned = trusted_previous
            elif current_manifest_hash is None and previous_hash is None:
                previous_owned = {}
            else:
                raise RuntimeError(f"pending bundle ownership anchor does not match target manifest: {anchor_path}")
        if pending_path.exists():
            pending_previous = _read_pending_bundle_install(
                pending_path,
                target_path,
                desired_manifest=manifest,
            )
            if current_manifest_hash != desired_manifest_hash and pending_previous != previous_owned:
                raise ValueError(
                    "pending bundle ownership does not match the app-owned anchor: "
                    f"{pending_path}"
                )
        _validate_owned_bundle_files(
            target_path,
            previous_owned,
            desired_owned=current_owned,
            resuming=resuming,
        )
        stale_owned = sorted(set(previous_owned) - set(current_owned))
        for relative in stale_owned:
            destination = target_path / relative
            _assert_safe_path(destination, target_path)
            report["actions"].append({"action": "remove", "path": destination.relative_to(root).as_posix()})
        for relative in sorted(current_owned):
            destination = target_path / relative
            _assert_safe_path(destination, target_path)
            report["actions"].append({"action": "copy", "path": destination.relative_to(root).as_posix()})
        report["actions"] = [
            {"action": "write-pending-anchor", "path": anchor_path.relative_to(root).as_posix()},
            {"action": "write-pending", "path": pending_path.relative_to(root).as_posix()},
            *report["actions"],
            {"action": "write-manifest", "path": manifest_path.relative_to(root).as_posix()},
            {"action": "write-anchor", "path": anchor_path.relative_to(root).as_posix()},
            {"action": "remove-pending", "path": pending_path.relative_to(root).as_posix()},
        ]
        if dry_run or not repo_path.exists():
            continue
        _git_exclude_path(repo_path)
        _safe_mkdir(target_path, repo_path)
        pending_payload = {
            "schema_version": 1,
            "desired_manifest": manifest,
            "previous_owned_files": previous_owned,
        }
        pending_anchor = {
            "schema_version": 1,
            "state": "pending",
            **anchor_identity,
            "previous_manifest_hash": current_manifest_hash,
            "desired_manifest_hash": desired_manifest_hash,
        }
        write_json(anchor_path, pending_anchor, boundary=root)
        if not resuming:
            write_json(pending_path, pending_payload, boundary=repo_path)
        elif not pending_path.exists():
            write_json(pending_path, pending_payload, boundary=repo_path)
        for relative in sorted(current_owned):
            source = source_files[relative]
            destination = target_path / relative
            source_mode = source.stat().st_mode & 0o777
            if destination.suffix == ".sh":
                source_mode |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            _atomic_write_bytes(destination, source.read_bytes(), boundary=target_path, mode=source_mode)
        for relative in stale_owned:
            _remove_owned_bundle_file(target_path / relative, target_path)
        write_json(manifest_path, manifest, boundary=repo_path)
        write_json(
            anchor_path,
            _installed_bundle_anchor(anchor_identity, desired_manifest_hash),
            boundary=root,
        )
        _assert_safe_path(pending_path, target_path)
        pending_path.unlink()
        installed_launchers: list[str] = []
        launcher_templates = bundle_path / "launchers"
        for launcher_name in ("Start.cmd", "Stop.cmd"):
            source = launcher_templates / launcher_name
            if not source.is_file():
                continue
            destination = repo_path / launcher_name
            _assert_safe_path(source, bundle_path)
            _assert_safe_path(destination, repo_path)
            payload = source.read_bytes()
            if destination.exists() and (not destination.is_file() or destination.read_bytes() != payload):
                raise RuntimeError(f"unowned portable launcher will not be overwritten: {destination}")
            _atomic_write_bytes(destination, payload, boundary=repo_path, mode=source.stat().st_mode & 0o777)
            installed_launchers.append(destination.relative_to(root).as_posix())
        report["launchers"] = installed_launchers
        _exclude_local_helper_paths(
            repo_path,
            ["tts-more/", *[Path(path).name for path in installed_launchers]],
        )
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


def _bundle_source_hash(owned_files: Mapping[str, str]) -> str:
    encoded = json.dumps(dict(owned_files), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _bundle_manifest_hash(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json_bytes(dict(manifest))).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _installed_bundle_anchor(identity: Mapping[str, str], manifest_hash: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "state": "installed",
        **dict(identity),
        "manifest_hash": manifest_hash,
    }


def _read_bundle_anchor(path: Path, *, expected_identity: Mapping[str, str]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid app-owned bundle anchor: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"invalid app-owned bundle anchor schema: {path}")
    state = payload.get("state")
    expected_keys = {"schema_version", "state", *expected_identity.keys()}
    if state == "installed":
        expected_keys.add("manifest_hash")
    elif state == "pending":
        expected_keys.update({"previous_manifest_hash", "desired_manifest_hash"})
    if state not in {"installed", "pending"} or set(payload) != expected_keys:
        raise ValueError(f"invalid app-owned bundle anchor state: {path}")
    if any(payload.get(key) != value for key, value in expected_identity.items()):
        raise ValueError(f"app-owned bundle anchor identity mismatch: {path}")
    hash_fields = ["manifest_hash"] if state == "installed" else ["desired_manifest_hash"]
    for field in hash_fields:
        if not isinstance(payload.get(field), str) or not SHA256_RE.fullmatch(str(payload[field])):
            raise ValueError(f"invalid app-owned bundle anchor hash: {path}")
    previous_hash = payload.get("previous_manifest_hash")
    if state == "pending" and previous_hash is not None and (
        not isinstance(previous_hash, str) or not SHA256_RE.fullmatch(previous_hash)
    ):
        raise ValueError(f"invalid app-owned bundle anchor previous hash: {path}")
    return payload


def _require_owned_files_match(target_path: Path, owned_files: Mapping[str, str]) -> None:
    for relative, expected_hash in owned_files.items():
        path = target_path / relative
        _assert_safe_path(path, target_path)
        if not path.is_file() or _sha256_file(path) != expected_hash:
            raise RuntimeError(f"cannot adopt missing or modified owned file: {path}")


def _validate_owned_files_mapping(raw_owned: Any, *, context: Path) -> dict[str, str]:
    if not isinstance(raw_owned, dict):
        raise ValueError(f"invalid bundle ownership manifest owned_files: {context}")
    owned: dict[str, str] = {}
    for raw_path, raw_hash in raw_owned.items():
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError(f"invalid bundle ownership manifest path: {context}")
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != raw_path:
            raise ValueError(f"bundle ownership manifest path escapes target: {raw_path}")
        if not isinstance(raw_hash, str) or not SHA256_RE.fullmatch(raw_hash):
            raise ValueError(f"invalid bundle ownership manifest hash for {raw_path}: {context}")
        owned[raw_path] = raw_hash
    return dict(sorted(owned.items()))


def _read_previous_owned_files(
    manifest_path: Path,
    target_path: Path,
    *,
    service_id: str,
    provider: str,
    source_bundle: str,
) -> dict[str, str]:
    _assert_safe_path(manifest_path, target_path)
    if not manifest_path.exists():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid bundle ownership manifest JSON: {manifest_path}") from exc
    required_keys = {
        "schema_version",
        "service_id",
        "provider_type",
        "source_bundle",
        "source_hash",
        "owned_files",
    }
    if not isinstance(payload, dict) or set(payload) != required_keys:
        raise ValueError(f"invalid bundle ownership manifest schema: {manifest_path}")
    if payload.get("schema_version") != BUNDLE_MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"invalid bundle ownership manifest schema version: {manifest_path}")
    if (
        payload.get("service_id") != service_id
        or payload.get("provider_type") != provider
        or payload.get("source_bundle") != source_bundle
    ):
        raise ValueError(f"bundle ownership manifest identity mismatch: {manifest_path}")
    owned = _validate_owned_files_mapping(payload.get("owned_files"), context=manifest_path)
    if payload.get("source_hash") != _bundle_source_hash(owned):
        raise ValueError(f"bundle ownership manifest source hash mismatch: {manifest_path}")
    return owned


def _read_pending_bundle_install(
    pending_path: Path,
    target_path: Path,
    *,
    desired_manifest: Mapping[str, Any],
) -> dict[str, str]:
    _assert_safe_path(pending_path, target_path)
    try:
        payload = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid pending bundle ownership manifest: {pending_path}") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "desired_manifest", "previous_owned_files"}
        or payload.get("schema_version") != 1
        or payload.get("desired_manifest") != dict(desired_manifest)
    ):
        raise ValueError(
            "pending bundle install does not match current inputs; rerun the original command or audit/remove "
            f"{pending_path}"
        )
    return _validate_owned_files_mapping(payload.get("previous_owned_files"), context=pending_path)


def _validate_owned_bundle_files(
    target_path: Path,
    previous_owned: Mapping[str, str],
    *,
    desired_owned: Mapping[str, str],
    resuming: bool,
) -> None:
    paths_to_validate = set(previous_owned)
    if resuming:
        paths_to_validate.update(desired_owned)
    for relative in sorted(paths_to_validate):
        path = target_path / relative
        _assert_safe_path(path, target_path)
        if not path.exists():
            continue
        if not path.is_file():
            raise RuntimeError(f"locally modified owned file is not regular: {path}")
        current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        allowed_hashes: set[str] = set()
        previous_hash = previous_owned.get(relative)
        if previous_hash is not None:
            allowed_hashes.add(previous_hash)
        if resuming and relative in desired_owned:
            allowed_hashes.add(desired_owned[relative])
        if current_hash not in allowed_hashes:
            raise RuntimeError(f"locally modified owned file will not be overwritten or deleted: {path}")
    if not resuming:
        for relative in sorted(set(desired_owned) - set(previous_owned)):
            path = target_path / relative
            _assert_safe_path(path, target_path)
            if path.exists():
                raise RuntimeError(f"unowned bundle file will not be overwritten: {path}")


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
        git_repo = False
        origin_matches = False
        if exists and not error:
            try:
                _validate_git_checkout(resolved)
                git_repo = True
                _ensure_repo_origin(resolved, str(repo["remote"]))
                origin_matches = True
            except (ValueError, RuntimeError) as exc:
                error = str(exc)
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
    repositories = [dict(repo) for repo in (repositories or load_repo_lock(root))]
    selected_repositories = _select_repositories(repositories, service_ids)
    _validate_selected_repository_paths(root, selected_repositories)
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
    repo_actions: list[dict[str, Any]] = []
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
        valid_git = False
        if path.exists():
            _validate_git_checkout(path)
            valid_git = True
        branch = _git_output(["git", "-C", str(path), "branch", "--show-current"]) if valid_git else ""
        head = _git_output(["git", "-C", str(path), "rev-parse", "HEAD"]) if valid_git else ""
        report = {
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
        if str(repo.get("provider_type")) == "gpt-sovits":
            report["worker_prerequisites"] = _gpt_worker_prerequisites(root, repo)
        reports.append(report)
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
    topology: str | Path | None = None,
    node: str | None = None,
    pid_manifest: str | Path | None = None,
) -> int:
    services = render_services(
        root,
        profile="worker-node" if topology is not None and node is not None else "local-all",
        platform_name=platform_name,
        service_ids=service_ids,
        repositories=repositories,
        topology=topology,
        node=node,
    )
    processes: list[subprocess.Popen] = []
    app_commit = _git_output(["git", "-C", str(root), "rev-parse", "HEAD"])
    logs_dir = root / "data" / ".runtime" / "logs"
    _safe_mkdir(logs_dir, root)
    manifest_path = _resolve_project_path(root, str(pid_manifest)) if pid_manifest else None
    if manifest_path is not None:
        _assert_safe_path(manifest_path, root)
    manifest_payload: dict[str, Any] = {"schema_version": 1, "processes": []}
    if manifest_path is not None and manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("validation process manifest is invalid") from exc
        if (
            not isinstance(existing, dict)
            or existing.get("schema_version") != 1
            or not isinstance(existing.get("processes"), list)
        ):
            raise RuntimeError("validation process manifest is invalid")
        manifest_payload = existing
    for service in services:
        command = _resolve_command(root, service["start_command"])
        env = {**os.environ, **_resolve_env(root, service.get("env") or {})}
        env["TTS_MORE_APP_COMMIT"] = app_commit
        service_id = _validate_service_id(str(service["service_id"]))
        log_path = logs_dir / f"{service_id}.log"
        log_file = _open_worker_log(logs_dir, service_id)
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
        if manifest_path is not None:
            try:
                executable = _resolve_project_path(root, str(command[0]))
                expected_module = FORMAL_WORKER_MODULES.get(str(service["service_id"]))
                if expected_module is None or expected_module not in {str(item) for item in command}:
                    raise RuntimeError("worker command is not eligible for validation cleanup")
                creation_date = _windows_process_creation_date(process.pid)
                manifest_payload["processes"].append(
                    {
                        "pid": process.pid,
                        "creation_date": creation_date,
                        "executable_path": str(executable),
                        "project_root": str(root.resolve(strict=False)),
                        "worker_module": expected_module,
                        "service_id": str(service["service_id"]),
                    }
                )
                _write_process_manifest(manifest_path, manifest_payload)
            except Exception:
                process.terminate()
                raise RuntimeError("validation worker could not be recorded for owned cleanup") from None
        print(f"{service['service_id']} PID {process.pid} {service['health_url']} log={log_path}")
    if detach:
        return 0
    try:
        return max((process.wait() for process in processes), default=0)
    except KeyboardInterrupt:
        for process in processes:
            process.terminate()
        return 130


def _open_worker_log(logs_dir: Path, service_id: str):
    _validate_service_id(service_id)
    _assert_safe_path(logs_dir, logs_dir)
    if not logs_dir.is_dir():
        raise ValueError(f"worker logs directory does not exist: {logs_dir}")
    filename = f"{service_id}.log"
    log_path = logs_dir / filename
    _assert_safe_path(log_path, logs_dir)
    open_flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    open_flags |= getattr(os, "O_BINARY", 0)
    open_flags |= getattr(os, "O_NOFOLLOW", 0)
    if os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY"):
        directory_fd = os.open(
            logs_dir,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            descriptor = os.open(filename, open_flags, 0o600, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
    else:
        descriptor = os.open(log_path, open_flags, 0o600)
    return os.fdopen(descriptor, "ab")


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


def _windows_process_creation_date(process_id: int) -> str:
    if os.name != "nt":
        raise RuntimeError("owned process manifests require Windows")
    script = (
        f"$p = $null; for ($i = 0; $i -lt 20 -and $null -eq $p; $i++) {{ "
        f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {int(process_id)}' "
        "-ErrorAction SilentlyContinue; if ($null -eq $p) { Start-Sleep -Milliseconds 50 } }; "
        "if ($null -eq $p) { exit 1 }; [Console]::Write([string]$p.CreationDate)"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    creation_date = completed.stdout.strip()
    if completed.returncode != 0 or not creation_date:
        raise RuntimeError("validation process creation identity is unavailable")
    return creation_date


def _write_process_manifest(path: Path, payload: dict[str, Any]) -> None:
    write_json(path, payload)


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


def _start_command(
    repo: dict[str, Any],
    platform_name: str,
    port: int,
    *,
    bind_host: str = "127.0.0.1",
) -> list[str]:
    return [
        _python_path(repo, platform_name),
        "-m",
        "uvicorn",
        PROVIDER_MODULES[str(repo["provider_type"])],
        "--app-dir",
        "backend",
        "--host",
        bind_host,
        "--port",
        str(port),
    ]


def _repo_scoped_model_dir(repo_path: str, model_dir: str, platform_name: str) -> str:
    path_type = PureWindowsPath if platform_name == "windows" else PurePosixPath
    target_model_dir = path_type(model_dir)
    if target_model_dir.is_absolute():
        return model_dir
    return (path_type(repo_path) / target_model_dir).as_posix()


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
        model_dir = str(repo.get("model_dir") or "pretrained_models/CosyVoice-300M")
        env["TTS_MORE_COSYVOICE_MODEL_DIR"] = _repo_scoped_model_dir(path, model_dir, platform_name)
    return env


def _python_path(repo: dict[str, Any], platform_name: str) -> str:
    path = str(repo["path"])
    if platform_name == "windows":
        return f"{path}/.venv/Scripts/python.exe"
    return f"{path}/.venv/bin/python"


def _platform_name() -> str:
    return "windows" if os.name == "nt" else "posix"


def _remove_selected_repo_paths(
    root: Path,
    repositories: list[dict[str, Any]],
    service_ids: set[str] | None,
    *,
    dry_run: bool,
) -> list[str]:
    selected_paths: list[tuple[Path, str]] = []
    root_resolved = root.resolve(strict=False)
    repo_root = (root / "repo").resolve(strict=False)
    for repo in _select_repositories(repositories, service_ids):
        target = _resolve_repo_path(root, str(repo["path"]))
        if target in {root_resolved, repo_root}:
            raise RuntimeError(f"refusing to clean repository root: {target}")
        label = target.relative_to(root_resolved).as_posix()
        selected_paths.append((target, label))

    for target, label in selected_paths:
        print(f"clean repository: {label}")
        if target.exists() and not dry_run:
            _remove_path(target)
    return [label for _, label in selected_paths]


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


def _host_volume_key(path: str | os.PathLike[str]) -> str:
    raw = os.fspath(path).replace("\\", "/")
    drive_match = re.match(r"^([A-Za-z]:)(?:/|$)", raw)
    if drive_match:
        return drive_match.group(1).casefold()
    if raw.startswith("//"):
        unc_parts = [part for part in raw[2:].split("/") if part]
        if len(unc_parts) >= 2:
            return f"//{unc_parts[0]}/{unc_parts[1]}".casefold()
    absolute = os.path.abspath(raw)
    drive, _tail = os.path.splitdrive(absolute)
    return (drive or Path(absolute).anchor or absolute).casefold()


def _sanitize_host_message(message: object, *, limit: int = 200) -> str:
    text = " ".join(str(message or "").split())
    text = re.sub(
        r"(?i)\b[a-z]:[\\/](?:[^\s,;]+)",
        "<path>",
        text,
    )
    text = re.sub(r"\\\\[^\\\s]+\\[^\s,;]+", "<path>", text)
    text = re.sub(r"(?i)\b[\w.-]+\.exe\b", "<process>", text)
    text = re.sub(r"(?i)\bGPU-[0-9a-f-]{32,}\b", "<uuid>", text)
    text = re.sub(
        r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        "<uuid>",
        text,
    )
    return (text or "no diagnostic message")[:limit]


def _append_host_check(
    checks: list[dict[str, Any]],
    check_id: str,
    passed: bool,
    message: str,
    **details: Any,
) -> None:
    checks.append({"id": check_id, "passed": bool(passed), "message": message, **details})


def _run_host_command(
    command_runner: Callable[..., Any],
    command: list[str],
    *,
    timeout: float,
    cwd: Path | None = None,
) -> Any:
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "check": False,
        "timeout": timeout,
    }
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    return command_runner(command, **kwargs)


def _python_version_string(version: Any) -> str:
    parts = [int(version[index]) for index in range(min(3, len(version)))]
    while len(parts) < 3:
        parts.append(0)
    return ".".join(str(item) for item in parts)


def _tool_version(
    executable: str,
    tool: str,
    command_runner: Callable[..., Any],
) -> str:
    arguments = [executable, "--version"]
    try:
        result = _run_host_command(
            command_runner,
            arguments,
            timeout=HOST_COMMAND_TIMEOUT_SECONDS,
        )
    except Exception:
        return "unknown"
    if int(getattr(result, "returncode", 1)) != 0:
        return "unknown"
    first_line = str(getattr(result, "stdout", "") or "").strip().splitlines()
    return _sanitize_host_message(first_line[0] if first_line else f"{tool} version unknown")


def _inspect_host_disks(
    mode: str,
    *,
    repo_path: Path,
    temp_path: Path,
    disk_usage: Callable[[str | os.PathLike[str]], Any],
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    limits = HOST_LIMITS_GIB[mode]
    shared = _host_volume_key(repo_path) == _host_volume_key(temp_path)
    specifications = (
        [("repository-and-temp", repo_path, max(limits.values()))]
        if shared
        else [
            ("repository", repo_path, limits["repo"]),
            ("temp", temp_path, limits["temp"]),
        ]
    )
    volumes: list[dict[str, Any]] = []
    for label, path, required_gib in specifications:
        check_id = f"disk_{label.replace('-', '_')}"
        try:
            usage = disk_usage(path)
            free_gib = round(int(usage.free) / 1024**3, 2)
            passed = free_gib >= required_gib
            message = (
                f"{label} volume has {free_gib:.2f} GiB free"
                if passed
                else f"{mode} requires at least {required_gib:g} GiB free on the {label} volume"
            )
            _append_host_check(
                checks,
                check_id,
                passed,
                message,
                free_gib=free_gib,
                required_gib=required_gib,
            )
            volumes.append(
                {"label": label, "free_gib": free_gib, "required_gib": required_gib}
            )
        except Exception:
            _append_host_check(
                checks,
                check_id,
                False,
                f"Unable to inspect free space on the {label} volume",
                required_gib=required_gib,
            )
            volumes.append({"label": label, "free_gib": None, "required_gib": required_gib})
    return {"volumes": volumes}


def _inspect_host_gpu(
    executable: str | None,
    *,
    command_runner: Callable[..., Any],
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    empty = {
        "count": 0,
        "aggregate_total_mib": 0,
        "aggregate_used_mib": 0,
        "max_total_mib": 0,
        "driver_versions": [],
    }
    if executable is None:
        return empty
    command = [
        executable,
        "--query-gpu=memory.total,memory.used,driver_version",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = _run_host_command(
            command_runner,
            command,
            timeout=HOST_COMMAND_TIMEOUT_SECONDS,
        )
        if int(getattr(result, "returncode", 1)) != 0:
            raise RuntimeError("GPU query failed")
        rows: list[tuple[int, int, str]] = []
        for raw_line in str(getattr(result, "stdout", "") or "").splitlines():
            if not raw_line.strip():
                continue
            fields = [field.strip() for field in raw_line.split(",")]
            if len(fields) != 3:
                raise ValueError("unexpected GPU query columns")
            rows.append((int(fields[0]), int(fields[1]), fields[2]))
        if not rows:
            raise ValueError("no NVIDIA GPU returned")
    except Exception:
        nvidia_check = next(item for item in checks if item["id"] == "nvidia-smi")
        nvidia_check.update(
            passed=False,
            message="nvidia-smi is required and must return GPU memory data",
        )
        return empty

    total_mib = sum(row[0] for row in rows)
    used_mib = sum(row[1] for row in rows)
    max_total_mib = max(row[0] for row in rows)
    driver_versions = sorted({_sanitize_host_message(row[2], limit=40) for row in rows})
    _append_host_check(
        checks,
        "gpu_total",
        max_total_mib >= MIN_GPU_TOTAL_MIB,
        (
            f"At least one GPU has {max_total_mib} MiB total memory"
            if max_total_mib >= MIN_GPU_TOTAL_MIB
            else f"GPU memory must be at least {MIN_GPU_TOTAL_MIB} MiB"
        ),
        observed_mib=max_total_mib,
        required_mib=MIN_GPU_TOTAL_MIB,
    )
    _append_host_check(
        checks,
        "gpu_idle",
        used_mib <= MAX_INITIAL_GPU_USED_MIB,
        (
            f"Aggregate initial GPU use is {used_mib} MiB"
            if used_mib <= MAX_INITIAL_GPU_USED_MIB
            else f"GPU must use no more than {MAX_INITIAL_GPU_USED_MIB} MiB before certification"
        ),
        observed_mib=used_mib,
        maximum_mib=MAX_INITIAL_GPU_USED_MIB,
    )
    return {
        "count": len(rows),
        "aggregate_total_mib": total_mib,
        "aggregate_used_mib": used_mib,
        "max_total_mib": max_total_mib,
        "driver_versions": driver_versions,
    }


def _gpt_worker_prerequisites(root: Path, repo: dict[str, Any]) -> dict[str, Any]:
    """Report portable, static prerequisites for the non-invasive GPT worker."""
    repo_path = _resolve_project_path(root, str(repo["path"]))
    package_dir = repo_path / "GPT_SoVITS"
    ffmpeg_bin = repo_path / "ffmpeg-shared" / "bin"
    if _platform_name() == "windows":
        media_runtime_ready = any(ffmpeg_bin.glob("avcodec-*.dll"))
        media_runtime_message = "full-shared FFmpeg DLLs are available"
    else:
        media_runtime_ready = (ffmpeg_bin / "ffmpeg").is_file()
        media_runtime_message = "bundled FFmpeg executable is available"
    checks = [
        {
            "id": "gpt_package_dir",
            "passed": package_dir.is_dir(),
            "message": "GPT_SoVITS package directory is available",
        },
        {
            "id": "ffmpeg_shared_dll",
            "passed": media_runtime_ready,
            "message": media_runtime_message,
        },
    ]
    if _platform_name() == "windows":
        onnxruntime_version = _venv_package_version(repo_path, "onnxruntime-gpu")
        cuda12_compatible = _is_cuda12_compatible_onnxruntime(onnxruntime_version)
        checks.append(
            {
                "id": "conda_executable",
                "passed": shutil.which("conda") is not None,
                "message": "Conda is available for the GPT-SoVITS official installer",
            }
        )
        checks.append(
            {
                "id": "onnxruntime_cuda12_compatible",
                "passed": cuda12_compatible,
                "message": (
                    f"onnxruntime-gpu {onnxruntime_version} is compatible with CUDA 12"
                    if cuda12_compatible
                    else f"onnxruntime-gpu {onnxruntime_version or 'is not installed'} must be pinned to 1.26.0 for CUDA 12"
                ),
            }
        )
    ready = all(bool(check["passed"]) for check in checks)
    failed_checks = {str(check["id"]) for check in checks if not check["passed"]}
    if "conda_executable" in failed_checks:
        next_action = "Install Conda for the GPT-SoVITS official installer, then run scripts/prepare-tts-repos.ps1."
    elif failed_checks:
        next_action = "Run scripts/prepare-tts-repos.ps1 for the selected GPT-SoVITS checkout."
    else:
        next_action = "GPT worker static prerequisites are present."
    return {
        "ready": ready,
        "checks": checks,
        "next_action": next_action,
    }


def _venv_package_version(repo_path: Path, package: str) -> str | None:
    """Read a package version from the configured repo venv without importing it."""
    if _platform_name() == "windows":
        site_packages = repo_path / ".venv" / "Lib" / "site-packages"
    else:
        candidates = sorted((repo_path / ".venv" / "lib").glob("python*/site-packages"))
        site_packages = candidates[-1] if candidates else repo_path / ".venv" / "lib" / "site-packages"
    normalized = package.replace("-", "_")
    for metadata in sorted(site_packages.glob(f"{normalized}-*.dist-info")):
        try:
            for line in (metadata / "METADATA").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Version: "):
                    return line.removeprefix("Version: ").strip() or None
        except OSError:
            continue
    return None


def _is_cuda12_compatible_onnxruntime(version: str | None) -> bool:
    if not version:
        return False
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", version)
    if match is None:
        return False
    major, minor, patch = (int(value or 0) for value in match.groups())
    return (major, minor, patch) >= (1, 19, 0) and (major, minor, patch) < (1, 27, 0)


def _inspect_ctranslate2(
    command_runner: Callable[..., Any],
    *,
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    probe = (
        "import json, ctranslate2; "
        "print(json.dumps(sorted(ctranslate2.get_supported_compute_types('cuda'))))"
    )
    compute_types: list[str] = []
    try:
        result = _run_host_command(
            command_runner,
            [sys.executable, "-c", probe],
            timeout=HOST_COMMAND_TIMEOUT_SECONDS,
        )
        if int(getattr(result, "returncode", 1)) == 0:
            payload = json.loads(str(getattr(result, "stdout", "") or "[]"))
            if isinstance(payload, list):
                compute_types = sorted(str(item) for item in payload)
    except Exception:
        compute_types = []
    passed = "float16" in compute_types
    _append_host_check(
        checks,
        "ctranslate2_cuda_float16",
        passed,
        (
            "CTranslate2 reports CUDA float16 support"
            if passed
            else "CTranslate2 CUDA float16 support is required"
        ),
    )
    return {"cuda_compute_types": compute_types}


def _inspect_playwright_chromium(
    node_executable: str | None,
    *,
    repo_path: Path,
    command_runner: Callable[..., Any],
    path_exists: Callable[[str | os.PathLike[str]], bool],
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    chromium_path = ""
    if node_executable is not None:
        probe = (
            "const { chromium } = require('@playwright/test'); "
            "process.stdout.write(chromium.executablePath());"
        )
        try:
            result = _run_host_command(
                command_runner,
                [node_executable, "-e", probe],
                timeout=HOST_COMMAND_TIMEOUT_SECONDS,
                cwd=repo_path / "frontend",
            )
            if int(getattr(result, "returncode", 1)) == 0:
                chromium_path = str(getattr(result, "stdout", "") or "").strip()
        except Exception:
            chromium_path = ""
    passed = bool(chromium_path and path_exists(chromium_path))
    _append_host_check(
        checks,
        "playwright_chromium",
        passed,
        (
            "Playwright Chromium is installed"
            if passed
            else "Playwright Chromium is required; run pnpm --dir frontend cuda:e2e:install"
        ),
    )
    return {"chromium_present": passed}


def _run_large_v3_cuda_smoke(
    command_runner: Callable[..., Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    probe = """
import gc
import json
try:
    from faster_whisper import WhisperModel
    model = WhisperModel('large-v3', device='cuda', compute_type='float16')
    del model
    gc.collect()
    print(json.dumps({'ok': True}))
except BaseException as exc:
    print(json.dumps({'ok': False, 'error_type': type(exc).__name__, 'message': str(exc)}))
    raise SystemExit(1)
""".strip()
    try:
        result = _run_host_command(
            command_runner,
            [sys.executable, "-c", probe],
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {
            "attempted": True,
            "passed": False,
            "status": "failed",
            "error_type": "TimeoutExpired",
            "message": _sanitize_host_message(
                f"large-v3 CUDA float16 smoke exceeded {timeout_seconds:g} seconds"
            ),
        }
    except Exception as exc:
        return {
            "attempted": True,
            "passed": False,
            "status": "failed",
            "error_type": re.sub(r"[^A-Za-z0-9_.]", "", type(exc).__name__)[:64],
            "message": _sanitize_host_message(exc),
        }
    stdout = str(getattr(result, "stdout", "") or "").strip()
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except (IndexError, json.JSONDecodeError, TypeError):
        payload = {}
    if int(getattr(result, "returncode", 1)) == 0 and payload.get("ok") is True:
        return {"attempted": True, "passed": True, "status": "passed"}
    error_type = re.sub(r"[^A-Za-z0-9_.]", "", str(payload.get("error_type") or "ChildProcessError"))[:64]
    raw_message = payload.get("message") or getattr(result, "stderr", "") or "large-v3 CUDA float16 smoke failed"
    return {
        "attempted": True,
        "passed": False,
        "status": "failed",
        "error_type": error_type,
        "message": _sanitize_host_message(raw_message),
    }


def inspect_cuda_host(
    mode: str,
    *,
    command_runner: Callable[..., Any] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    disk_usage: Callable[[str | os.PathLike[str]], Any] = shutil.disk_usage,
    python_version: Any = None,
    repo_path: str | os.PathLike[str] = PROJECT_ROOT,
    temp_path: str | os.PathLike[str] | None = None,
    path_exists: Callable[[str | os.PathLike[str]], bool] = os.path.isfile,
    smoke_timeout_seconds: float = ASR_SMOKE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if mode not in HOST_LIMITS_GIB:
        raise ValueError(f"unsupported CUDA host preflight mode: {mode}")
    if smoke_timeout_seconds <= 0:
        raise ValueError("smoke_timeout_seconds must be positive")
    repo = Path(repo_path)
    temp = Path(temp_path or tempfile.gettempdir())
    version = python_version if python_version is not None else sys.version_info
    checks: list[dict[str, Any]] = []
    versions: dict[str, str | None] = {"python": _python_version_string(version)}

    python_passed = int(version[0]) == 3 and int(version[1]) == 11
    _append_host_check(
        checks,
        "python",
        python_passed,
        (
            f"Python {versions['python']} is supported"
            if python_passed
            else f"Python 3.11 is required; detected {versions['python']}"
        ),
    )

    executables: dict[str, str | None] = {}
    required_tools = {
        "conda": "conda is required for GPT-SoVITS on Windows",
        "git": "git is required",
        "node": "node is required",
        "pnpm": "pnpm is required",
        "nvidia-smi": "nvidia-smi is required",
    }
    for tool, failure_message in required_tools.items():
        executable = which(tool)
        executables[tool] = executable
        passed = executable is not None
        _append_host_check(
            checks,
            tool,
            passed,
            f"{tool} is available" if passed else failure_message,
        )
        versions[tool] = (
            _tool_version(executable, tool, command_runner)
            if executable is not None and tool != "nvidia-smi"
            else None
        )

    disk = _inspect_host_disks(
        mode,
        repo_path=repo,
        temp_path=temp,
        disk_usage=disk_usage,
        checks=checks,
    )
    gpu = _inspect_host_gpu(
        executables["nvidia-smi"],
        command_runner=command_runner,
        checks=checks,
    )
    versions["nvidia_driver"] = ", ".join(gpu["driver_versions"]) or None
    ctranslate2 = _inspect_ctranslate2(command_runner, checks=checks)
    playwright = _inspect_playwright_chromium(
        executables["node"],
        repo_path=repo,
        command_runner=command_runner,
        path_exists=path_exists,
        checks=checks,
    )

    cheap_passed = all(item["passed"] for item in checks)
    if cheap_passed:
        asr_smoke = _run_large_v3_cuda_smoke(
            command_runner,
            timeout_seconds=smoke_timeout_seconds,
        )
        _append_host_check(
            checks,
            "large_v3_cuda_smoke",
            asr_smoke["passed"],
            (
                "large-v3 CUDA float16 smoke passed"
                if asr_smoke["passed"]
                else (
                    "large-v3 CUDA float16 smoke failed "
                    f"({asr_smoke['error_type']}): {asr_smoke['message']}"
                )
            ),
        )
    else:
        asr_smoke = {"attempted": False, "passed": False, "status": "skipped"}

    passed = cheap_passed and asr_smoke["passed"]
    failed_ids = {item["id"] for item in checks if not item["passed"]}
    if passed:
        next_action = "Continue to input preflight and deployment."
    else:
        actions: list[str] = []
        if "python" in failed_ids:
            actions.append("Run host preflight with Python 3.11.")
        failed_tools = [tool for tool in required_tools if tool in failed_ids]
        if failed_tools:
            actions.append(f"Install or repair required tools: {', '.join(failed_tools)}.")
        if any(item.startswith("disk_") for item in failed_ids):
            actions.append("Free the required repository or temp volume space.")
        if "gpu_total" in failed_ids:
            actions.append(f"Use an NVIDIA GPU with at least {MIN_GPU_TOTAL_MIB} MiB total memory.")
        if "gpu_idle" in failed_ids:
            actions.append(
                "Wait for unrelated GPU work to finish, then rerun; "
                "this preflight never stops GPU processes."
            )
        if "ctranslate2_cuda_float16" in failed_ids:
            actions.append("Install a CTranslate2 build with CUDA float16 support.")
        if "playwright_chromium" in failed_ids:
            actions.append("Install Playwright Chromium from the frontend workspace.")
        if "large_v3_cuda_smoke" in failed_ids:
            actions.append("Repair the large-v3 cache, network, or CUDA runtime.")
        actions.append("Then rerun host preflight.")
        next_action = " ".join(actions)
    return {
        "schema_version": 1,
        "stage": "host-preflight",
        "mode": mode,
        "passed": passed,
        "checks": checks,
        "versions": versions,
        "disk": disk,
        "gpu": gpu,
        "ctranslate2": ctranslate2,
        "playwright": playwright,
        "asr_smoke": asr_smoke,
        "next_action": next_action,
    }


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
        result = _run_git_process(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
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
            _validate_git_checkout(path)
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
    render.add_argument("--topology", default=None, help="Optional deployment topology JSON")
    render.add_argument("--node", default=None, help="Node name from --topology")

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
    start.add_argument("--topology", default=None, help="Optional deployment topology JSON")
    start.add_argument("--node", default=None, help="Worker node name from --topology")
    start.add_argument("--pid-manifest", default=None, help="Run-local owned process manifest inside the project root")

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
    install_bundles.add_argument(
        "--adopt-existing",
        action="store_true",
        help="Anchor a reviewed existing schema-3 bundle manifest without upgrading files",
    )
    install_bundles.add_argument("--repo-paths", default=None, help="Complete service-id keyed repo path confirmation JSON")

    validate_paths = sub.add_parser(
        "validate-repo-paths",
        help="Validate local TTS repo paths before one-click deployment",
    )
    validate_paths.add_argument("--service-ids", default=None)
    validate_paths.add_argument("--require-exists", action="store_true")
    validate_paths.add_argument("--repo-paths", default=None, help="Complete service-id keyed repo path confirmation JSON")

    host_preflight = sub.add_parser(
        "preflight-cuda-host",
        help="Validate the Windows CUDA host before deployment or repository cleanup",
    )
    host_preflight.add_argument(
        "--mode",
        choices=tuple(HOST_LIMITS_GIB),
        required=True,
    )
    host_preflight.add_argument(
        "--output",
        required=True,
        help="environment-preflight.json output path",
    )

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
            topology=args.topology,
            node=args.node,
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
                {"actions": actions},
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
            topology=args.topology,
            node=args.node,
            pid_manifest=args.pid_manifest,
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
            adopt_existing=args.adopt_existing,
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
    if args.command == "preflight-cuda-host":
        report = inspect_cuda_host(args.mode, repo_path=root)
        output = Path(args.output)
        if not output.is_absolute():
            output = root / output
        output = _resolve_project_path(root, str(output))
        write_json(output, report, boundary=root)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1
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
