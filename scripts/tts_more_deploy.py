from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
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
REPO_BUNDLE_RELATIVE_PATH = Path("deployment/tts-repos")
DEFAULT_REPO_PATHS_RELATIVE_PATH = Path("deployment/app/repo-paths.local.json")
TOPOLOGY_SCHEMA_VERSION = 1
_HOSTNAME_PATTERN = re.compile(
    r"(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)


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


def load_deployment_repositories(
    root: Path = PROJECT_ROOT,
    repo_paths: str | Path | None = None,
) -> list[dict[str, Any]]:
    repositories = [dict(repo) for repo in load_repo_lock(root)]
    overrides = load_repo_path_overrides(root, repo_paths)
    if overrides:
        apply_repo_path_overrides(repositories, overrides)
    return repositories


def save_repo_lock(repositories: list[dict[str, Any]], root: Path = PROJECT_ROOT) -> None:
    write_json(root / "repo.lock.json", {"repositories": repositories})


def load_repo_path_overrides(root: Path = PROJECT_ROOT, repo_paths: str | Path | None = None) -> dict[str, str]:
    path = _repo_paths_config_path(root, repo_paths)
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("repositories") if isinstance(payload, dict) else payload
    overrides: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, str) and value.strip():
                overrides[key] = value.strip()
        return overrides
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            path_value = item.get("path")
            if not isinstance(path_value, str) or not path_value.strip():
                continue
            for key_name in ("service_id", "name", "provider_type", "variant"):
                key = item.get(key_name)
                if isinstance(key, str) and key.strip():
                    overrides[key.strip()] = path_value.strip()
        return overrides
    raise ValueError(f"unsupported repo paths format: {path}")


def apply_repo_path_overrides(repositories: list[dict[str, Any]], overrides: Mapping[str, str]) -> None:
    for repo in repositories:
        for key in _repo_override_keys(repo):
            if key in overrides:
                repo["path"] = overrides[key]
                repo["path_source"] = key
                break


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


def _repo_override_keys(repo: Mapping[str, Any]) -> list[str]:
    keys = [
        str(repo.get("service_id") or ""),
        str(repo.get("name") or ""),
        str(repo.get("provider_type") or ""),
        str(repo.get("variant") or ""),
        str(repo.get("path") or ""),
    ]
    return [key for key in keys if key]


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
    repositories: list[dict[str, Any]] | None = None,
    topology: str | Path | None = None,
    node: str | None = None,
) -> list[dict[str, Any]]:
    platform_name = platform_name or _platform_name()
    repositories = [repo for repo in (repositories or load_repo_lock(root)) if _is_tts_repo(repo)]
    selected_repositories = [repo for repo in repositories if _repo_selected(repo, service_ids)]
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
            "setup_state": "not_configured" if template else (None if is_external else "repo_found"),
            "api_contract": "tts-more-v1",
            "base_url": _service_base_url(endpoint_host, port),
            "mode": "external" if is_external else "local",
            "network_scope": "lan" if is_lan else "localhost",
            "managed": not is_external,
            "enabled": not template,
            "poll_interval_seconds": 5,
            "repo_path": None if is_external else repo["path"],
            "start_command": [] if is_external else _start_command(repo, platform_name, port, bind_host=bind_host),
            "start_cwd": None if is_external else ".",
            "env": worker_env,
            "health_url": f"{_service_base_url(endpoint_host, port)}/health",
            "resource_group": resource_group,
            "capacity": capacity,
            "priority": int(repo.get("priority") or PROVIDER_PRIORITY[provider]),
            "capabilities": list(repo.get("capabilities") or PROVIDER_CAPABILITIES[provider]),
        }
        if provider == "cosyvoice":
            service["default_params"] = {"mode": "zero_shot", "response_format": "wav"}
        services.append(service)
    return services


def _service_base_url(host: str, port: int) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        url_host = host
    else:
        url_host = f"[{host}]" if address.version == 6 else host
    return f"http://{url_host}:{port}"


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
        for field in ("host", "bind_host"):
            _validate_topology_host(node_name, field, node_config[field])
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


def _validate_topology_host(node_name: str, field: str, value: str) -> None:
    if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"topology node {node_name} {field} must be a valid host")
    if any(char in value for char in ":/@\\?#[]%"):
        try:
            ipaddress.ip_address(value)
        except ValueError:
            raise ValueError(f"topology node {node_name} {field} must be a valid host") from None
        return
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not _HOSTNAME_PATTERN.fullmatch(value):
            raise ValueError(f"topology node {node_name} {field} must be a valid host") from None


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


def _clone_command(remote: str, branch: str, path: Path, *, partial: bool = True) -> list[str]:
    command = ["git", "clone", "--depth", "1"]
    if partial:
        command.append("--filter=blob:none")
    command.extend(["--branch", branch, "--single-branch", remote, str(path)])
    return command


def _run_git_command(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _repo_selected(repo: dict[str, Any], service_ids: set[str] | None) -> bool:
    if not service_ids:
        return bool(repo.get("default_selected", True))
    if "all" in service_ids:
        return True
    if "default" in service_ids and bool(repo.get("default_selected", True)):
        return True
    candidates = {
        str(repo.get("name") or ""),
        str(repo.get("provider_type") or ""),
        str(repo.get("service_id") or _default_service_id(repo)),
        str(repo.get("variant") or ""),
        str(repo.get("branch") or ""),
        str(repo.get("path") or ""),
    }
    return any(item in candidates for item in service_ids)


def _repo_status(path: Path) -> str:
    return _git_output(["git", "-C", str(path), "status", "--porcelain"])


def _ensure_clean_repo(path: Path, name: str) -> None:
    status = _repo_status(path)
    if status:
        raise RuntimeError(
            f"refusing to update dirty service repository {name} at {path}; "
            "commit, stash, or clean local changes first, or pass --force-reset-repos"
        )


def _git_exclude_path(repo_path: Path) -> Path | None:
    dot_git = repo_path / ".git"
    if dot_git.is_dir():
        return dot_git / "info" / "exclude"
    git_dir = _git_output(["git", "-C", str(repo_path), "rev-parse", "--git-dir"])
    if not git_dir:
        return None
    candidate = Path(git_dir)
    if not candidate.is_absolute():
        candidate = repo_path / candidate
    return candidate / "info" / "exclude"


def _exclude_local_update_scripts(repo_path: Path) -> None:
    _exclude_local_helper_paths(repo_path, ["tts-more-update.sh", "tts-more-update.ps1"])


def _exclude_local_helper_paths(repo_path: Path, names: list[str]) -> None:
    exclude_path = _git_exclude_path(repo_path)
    if exclude_path is None:
        return
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    additions = [name for name in names if name not in existing.splitlines()]
    if not additions:
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    exclude_path.write_text(existing + prefix + "\n".join(additions) + "\n", encoding="utf-8")


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
    repositories = [dict(repo) for repo in (repositories or load_repo_lock(root))]
    lock_changed = False
    for repo in repositories:
        if not _repo_selected(repo, service_ids):
            continue
        path = _resolve_project_path(root, str(repo["path"]))
        remote = str(repo["remote"])
        branch = str(repo["branch"])
        commit = repo.get("commit")
        if path.exists() and (path / ".git").exists():
            if force_reset:
                commands = [
                    ["git", "-C", str(path), "fetch", "--prune", "origin", branch],
                    ["git", "-C", str(path), "checkout", branch],
                    ["git", "-C", str(path), "reset", "--hard", f"origin/{branch}"],
                ]
            else:
                _ensure_clean_repo(path, str(repo.get("name") or repo.get("service_id") or path.name))
                commands = [
                    ["git", "-C", str(path), "fetch", "--prune", "origin", branch],
                    ["git", "-C", str(path), "checkout", branch],
                    ["git", "-C", str(path), "pull", "--ff-only", "origin", branch],
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


def _service_update_script_sh(repo: dict[str, Any]) -> str:
    branch = str(repo.get("branch") or "main")
    commit = str(repo.get("commit") or "")
    remote = str(repo.get("remote") or "")
    return f"""#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
BRANCH="${{TTS_MORE_UPDATE_BRANCH:-{branch}}}"
PINNED_COMMIT="${{TTS_MORE_PINNED_COMMIT:-{commit}}}"

cd "$ROOT"
echo "[update] {repo.get('name') or repo.get('service_id') or branch}"
echo "[remote] {remote}"
git fetch --prune origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

if [[ "${{1:-}}" == "--pinned" && -n "$PINNED_COMMIT" ]]; then
  git fetch origin "$PINNED_COMMIT"
  git checkout "$PINNED_COMMIT"
fi

git status --short --branch
"""


def _service_update_script_ps1(repo: dict[str, Any]) -> str:
    branch = str(repo.get("branch") or "main")
    commit = str(repo.get("commit") or "")
    remote = str(repo.get("remote") or "")
    return f"""$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Branch = if ($env:TTS_MORE_UPDATE_BRANCH) {{ $env:TTS_MORE_UPDATE_BRANCH }} else {{ "{branch}" }}
$PinnedCommit = if ($env:TTS_MORE_PINNED_COMMIT) {{ $env:TTS_MORE_PINNED_COMMIT }} else {{ "{commit}" }}

Set-Location $Root
Write-Host "[update] {repo.get('name') or repo.get('service_id') or branch}" -ForegroundColor Cyan
Write-Host "[remote] {remote}" -ForegroundColor DarkCyan
git fetch --prune origin $Branch
git checkout $Branch
git pull --ff-only origin $Branch

if ($args.Count -gt 0 -and $args[0] -eq "--pinned" -and $PinnedCommit) {{
  git fetch origin $PinnedCommit
  git checkout $PinnedCommit
}}

git status --short --branch
exit $LASTEXITCODE
"""


def install_update_scripts(
    root: Path = PROJECT_ROOT,
    *,
    service_ids: set[str] | None = None,
    dry_run: bool = False,
    repositories: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    reports = []
    for repo in repositories or load_repo_lock(root):
        if not _repo_selected(repo, service_ids):
            continue
        repo_path = _resolve_project_path(root, str(repo["path"]))
        sh_path = repo_path / "tts-more-update.sh"
        ps1_path = repo_path / "tts-more-update.ps1"
        exists = repo_path.exists()
        report = {
            "name": repo.get("name"),
            "path": str(repo.get("path")),
            "exists": exists,
            "scripts": [str(sh_path.relative_to(root)), str(ps1_path.relative_to(root))],
        }
        reports.append(report)
        if dry_run or not exists:
            continue
        sh_path.write_text(_service_update_script_sh(repo), encoding="utf-8")
        ps1_path.write_text(_service_update_script_ps1(repo), encoding="utf-8")
        current_mode = sh_path.stat().st_mode
        sh_path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
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
    for repo in repositories or load_repo_lock(root):
        if not _repo_selected(repo, service_ids):
            continue
        provider = str(repo.get("provider_type") or "")
        bundle_path = root / REPO_BUNDLE_RELATIVE_PATH / provider
        repo_path = _resolve_project_path(root, str(repo["path"]))
        target_path = repo_path / "tts-more"
        report = {
            "name": repo.get("name"),
            "provider_type": provider,
            "path": str(repo.get("path")),
            "exists": repo_path.exists(),
            "bundle": str(bundle_path.relative_to(root)) if bundle_path.exists() else "",
            "target": str(target_path.relative_to(root)),
            "installed": False,
        }
        reports.append(report)
        if not bundle_path.exists():
            report["error"] = f"missing bundle for provider: {provider}"
            continue
        if dry_run or not repo_path.exists():
            continue
        _copy_tree_contents(bundle_path, target_path)
        manifest = {
            "schema_version": 1,
            "installed_at": _isoformat(_utc_now()),
            "service_id": repo.get("service_id"),
            "name": repo.get("name"),
            "provider_type": provider,
            "variant": repo.get("variant"),
            "branch": repo.get("branch"),
            "commit": repo.get("commit"),
            "source_bundle": str(REPO_BUNDLE_RELATIVE_PATH / provider),
        }
        write_json(target_path / "tts-more-repo.json", manifest)
        _chmod_shell_scripts(target_path)
        _exclude_local_helper_paths(repo_path, ["tts-more/"])
        report["installed"] = True
    return reports


def _copy_tree_contents(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        destination = target / child.name
        if child.is_dir():
            shutil.copytree(child, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(child, destination)


def _chmod_shell_scripts(root: Path) -> None:
    for path in root.rglob("*.sh"):
        current_mode = path.stat().st_mode
        path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def validate_repo_paths(
    root: Path = PROJECT_ROOT,
    *,
    service_ids: set[str] | None = None,
    require_exists: bool = False,
    repositories: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    reports = []
    for repo in repositories or load_repo_lock(root):
        if not _repo_selected(repo, service_ids):
            continue
        raw_path = str(repo.get("path") or "")
        try:
            resolved = _resolve_project_path(root, raw_path)
            inside_project = True
            error = ""
        except ValueError as exc:
            resolved = Path(raw_path).resolve(strict=False)
            inside_project = False
            error = str(exc)
        exists = resolved.exists()
        reports.append(
            {
                "name": repo.get("name"),
                "service_id": repo.get("service_id"),
                "provider_type": repo.get("provider_type"),
                "path": raw_path,
                "absolute_path": str(resolved),
                "exists": exists,
                "inside_project": inside_project,
                "ok": inside_project and (exists or not require_exists),
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
            write_json(root / services_output, services)
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
    for repo in all_repos:
        if not _repo_selected(repo, service_ids):
            continue
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
    repositories: list[dict[str, Any]] | None = None,
    topology: str | Path | None = None,
    node: str | None = None,
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
    logs_dir.mkdir(parents=True, exist_ok=True)
    for service in services:
        command = _resolve_command(root, service["start_command"])
        env = {**os.environ, **_resolve_env(root, service.get("env") or {})}
        env["TTS_MORE_APP_COMMIT"] = app_commit
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


def _load_cli_repositories(root: Path, repo_paths: str | None) -> list[dict[str, Any]] | None:
    if not repo_paths:
        return None
    return load_deployment_repositories(root, repo_paths)


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
    render.add_argument("--repo-paths", default=None, help="Optional local repo path confirmation JSON")
    render.add_argument("--topology", default=None, help="Optional deployment topology JSON")
    render.add_argument("--node", default=None, help="Node name from --topology")

    sync = sub.add_parser("sync-repos", help="Clone/fetch repositories from repo.lock.json")
    sync.add_argument("--clean", action="store_true")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--latest", action="store_true", help="Track each configured branch instead of checking out the pinned commit")
    sync.add_argument("--write-lock", action="store_true", help="After --latest, write current HEADs back to repo.lock.json")
    sync.add_argument("--force-reset", action="store_true", help="Allow service repositories to be reset hard to the configured branch")
    sync.add_argument("--service-ids", default=None)
    sync.add_argument("--repo-paths", default=None, help="Optional local repo path confirmation JSON")

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
    doctor_parser.add_argument("--repo-paths", default=None, help="Optional local repo path confirmation JSON")

    start = sub.add_parser("start-workers", help="Start local worker processes from repo.lock.json")
    start.add_argument("--platform", choices=("windows", "posix"), default=None)
    start.add_argument("--service-ids", default=None)
    start.add_argument("--detach", action="store_true")
    start.add_argument("--repo-paths", default=None, help="Optional local repo path confirmation JSON")
    start.add_argument("--topology", default=None, help="Optional deployment topology JSON")
    start.add_argument("--node", default=None, help="Worker node name from --topology")

    install_scripts = sub.add_parser(
        "install-update-scripts",
        help="Write small update scripts into checked-out TTS service repositories",
    )
    install_scripts.add_argument("--service-ids", default=None)
    install_scripts.add_argument("--dry-run", action="store_true")
    install_scripts.add_argument("--repo-paths", default=None, help="Optional local repo path confirmation JSON")

    install_bundles = sub.add_parser(
        "install-repo-bundles",
        help="Copy provider-specific TTS More helper bundles into checked-out TTS repositories",
    )
    install_bundles.add_argument("--service-ids", default=None)
    install_bundles.add_argument("--dry-run", action="store_true")
    install_bundles.add_argument("--repo-paths", default=None, help="Optional local repo path confirmation JSON")

    validate_paths = sub.add_parser(
        "validate-repo-paths",
        help="Validate local TTS repo paths before one-click deployment",
    )
    validate_paths.add_argument("--service-ids", default=None)
    validate_paths.add_argument("--require-exists", action="store_true")
    validate_paths.add_argument("--repo-paths", default=None, help="Optional local repo path confirmation JSON")

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
    update.add_argument("--repo-paths", default=None, help="Optional local repo path confirmation JSON")

    args = parser.parse_args(argv)
    root = Path(args.root).resolve(strict=False)
    if args.command == "render-services":
        repositories = _load_cli_repositories(root, args.repo_paths)
        services = render_services(
            root,
            profile=args.profile,
            platform_name=args.platform,
            host=args.host,
            service_ids=_parse_service_ids(args.service_ids),
            template=args.template,
            repositories=repositories,
            topology=args.topology,
            node=args.node,
        )
        if args.output:
            write_json(root / args.output, services)
        else:
            print(json.dumps(services, ensure_ascii=False, indent=2))
        return 0
    if args.command == "sync-repos":
        repositories = _load_cli_repositories(root, args.repo_paths)
        actions = sync_repos(
            root,
            clean=args.clean,
            dry_run=args.dry_run,
            latest=args.latest,
            write_lock=args.write_lock,
            service_ids=_parse_service_ids(args.service_ids),
            force_reset=args.force_reset,
            repositories=repositories,
        )
        for command in actions:
            print(" ".join(command))
        return 0
    if args.command == "list-repos":
        repositories = load_deployment_repositories(root, args.repo_paths)
        service_ids = _parse_service_ids(args.service_ids)
        repositories = [repo for repo in repositories if _repo_selected(repo, service_ids)]
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
        repositories = _load_cli_repositories(root, args.repo_paths)
        payload = doctor(root, service_ids=_parse_service_ids(args.service_ids), repositories=repositories)
        if args.output:
            write_json(root / args.output, payload)
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "start-workers":
        repositories = _load_cli_repositories(root, args.repo_paths)
        return start_workers(
            root,
            platform_name=args.platform,
            service_ids=_parse_service_ids(args.service_ids),
            detach=args.detach,
            repositories=repositories,
            topology=args.topology,
            node=args.node,
        )
    if args.command == "install-update-scripts":
        repositories = _load_cli_repositories(root, args.repo_paths)
        reports = install_update_scripts(
            root,
            service_ids=_parse_service_ids(args.service_ids),
            dry_run=args.dry_run,
            repositories=repositories,
        )
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0
    if args.command == "install-repo-bundles":
        repositories = _load_cli_repositories(root, args.repo_paths)
        reports = install_repo_bundles(
            root,
            service_ids=_parse_service_ids(args.service_ids),
            dry_run=args.dry_run,
            repositories=repositories,
        )
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0
    if args.command == "validate-repo-paths":
        repositories = _load_cli_repositories(root, args.repo_paths)
        reports = validate_repo_paths(
            root,
            service_ids=_parse_service_ids(args.service_ids),
            require_exists=args.require_exists,
            repositories=repositories,
        )
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0 if all(item["ok"] for item in reports) else 1
    if args.command == "update":
        repositories = _load_cli_repositories(root, args.repo_paths)
        payload = update_project(
            root,
            dry_run=args.dry_run,
            skip_app=args.skip_app,
            skip_repos=args.skip_repos,
            clean=args.clean,
            latest_repos=args.latest_repos,
            write_lock=args.write_lock,
            service_ids=_parse_service_ids(args.service_ids),
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
