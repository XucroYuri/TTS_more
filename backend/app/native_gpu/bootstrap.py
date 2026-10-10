"""Opt-in external launcher hooks; never attach to an already running process."""
from __future__ import annotations

import hashlib
import json
import os
import runpy
import sys
from pathlib import Path
from typing import Any

from .guard import NativeASGIAdmission, NativeGuard, NativeGuardError


KINDS = {
    "gpt_webui": frozenset({"gpt_webui"}),
    "gpt_dual": frozenset({"gpt_webui", "gpt_api_v2"}),
    "index_v2": frozenset({"index_v2"}),
}
SOURCE_REQUIREMENTS = {
    "gpt_webui": frozenset({"GPT_SoVITS/inference_webui.py", "GPT_SoVITS/module/mel_processing.py",
                            "GPT_SoVITS/sv.py", "tools/audio_sr.py", "GPT_SoVITS/AR/models/t2s_model.py",
                            "GPT_SoVITS/AR/modules/embedding.py"}),
    "gpt_dual": frozenset({"GPT_SoVITS/inference_webui.py", "GPT_SoVITS/module/mel_processing.py",
                          "GPT_SoVITS/sv.py", "tools/audio_sr.py", "api_v2.py", "GPT_SoVITS/TTS_infer_pack/TTS.py",
                          "GPT_SoVITS/AR/models/t2s_model.py", "GPT_SoVITS/AR/modules/embedding.py"}),
    "index_v2": frozenset({"indextts/infer_v2.py", "indextts/gpt/model_v2.py",
                          "indextts/s2mel/modules/audio.py", "indextts/s2mel/modules/gpt_fast/model.py",
                          "indextts/s2mel/modules/commons.py"}),
}


def validate_config(config_path: Path) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict) or config.get("kind") not in KINDS:
        raise ValueError("unsupported native launcher kind")
    base = config_path.resolve().parent
    for key in ("working_directory", "entry", "python_executable"):
        value = config.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} is required")
        path = Path(value)
        config[key] = str((base / path).resolve() if not path.is_absolute() else path.resolve())
    root = Path(config["working_directory"])
    if not root.is_dir() or not Path(config["python_executable"]).is_file():
        raise ValueError("native directory and interpreter must already exist")
    entry = Path(config["entry"])
    entry.relative_to(root)
    if not entry.is_file():
        raise ValueError("native source entry must already exist inside its checkout")
    source_hashes = config.get("source_sha256")
    if not isinstance(source_hashes, dict) or not source_hashes:
        raise ValueError("reviewed native source hashes are required before launch")
    if not SOURCE_REQUIREMENTS[config["kind"]] <= set(source_hashes):
        raise ValueError("native adapter model and cache sources must be reviewed and pinned")
    if entry.relative_to(root).as_posix() not in source_hashes:
        raise ValueError("native source hash manifest must include its entry")
    for relative, digest in source_hashes.items():
        source = (root / relative).resolve()
        source.relative_to(root)
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("native source SHA-256 is invalid")
        if hashlib.sha256(source.read_bytes()).hexdigest() != digest.lower():
            raise ValueError("reviewed native source changed; revalidate adapters before launch")
    argv = config.get("argv", [])
    environment = config.get("environment", {})
    if not isinstance(argv, list) or any(not isinstance(x, str) for x in argv):
        raise ValueError("native arguments must be a string array")
    if not isinstance(environment, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or not k
        or "=" in k or "\x00" in k + v for k, v in environment.items()
    ):
        raise ValueError("native environment must contain valid string entries")
    for section in ("coordinator", "control"):
        value = config.get(section)
        if not isinstance(value, dict) or not isinstance(value.get("token"), str) or len(value["token"]) < 32:
            raise ValueError("private coordinator/control authentication is required")
        from .transport import ControlClient
        ControlClient(value["url"], value["token"])
    if not isinstance(config.get("participant"), str) or not config["participant"]:
        raise ValueError("registered participant identity is required")
    config.setdefault("argv", [])
    config.setdefault("environment", {})
    from .protocol import finite_seconds
    timeout = finite_seconds(config.get("admission_timeout", 60), "admission_timeout", maximum=120)
    if timeout == 0:
        raise ValueError("native admission timeout must be positive")
    config["admission_timeout"] = timeout
    if config.get("startup_patch") not in (None, "gpt_windows_bootstrap"):
        raise ValueError("unsupported native startup patch")
    if config.get("startup_patch") == "gpt_windows_bootstrap" and "tools/startup_bootstrap.py" not in source_hashes:
        raise ValueError("native startup patch source must be reviewed and pinned")
    return config


def _loaded_module(names: tuple[str, ...]) -> Any:
    modules = {id(sys.modules[name]): sys.modules[name] for name in names if name in sys.modules}
    if len(modules) != 1:
        raise NativeGuardError("required native cache module is missing or loaded under conflicting identities")
    return next(iter(modules.values()))


def _shared_modules(kind: str) -> dict[str, Any]:
    if kind == "index_v2":
        return {"index_audio": _loaded_module(("indextts.s2mel.modules.audio",))}
    modules = {"gpt_mel_processing": _loaded_module(("module.mel_processing", "GPT_SoVITS.module.mel_processing"))}
    if kind == "gpt_api_v2":
        modules["gpt_tts_module"] = _loaded_module(("TTS_infer_pack.TTS", "GPT_SoVITS.TTS_infer_pack.TTS"))
    return modules


def install_hooks(guard: NativeGuard, *, gradio: Any, uvicorn: Any | None, adapter_factory: Any) -> None:
    original_launch = gradio.Blocks.launch
    originals: dict[int, Any] = {}

    def launch(blocks, *args, **kwargs):
        candidates: dict[tuple[str, int], dict[str, Any]] = {}
        events = blocks.fns.values() if isinstance(blocks.fns, dict) else blocks.fns
        events = list(events)
        for event in events:
            function = getattr(event, "fn", None)
            namespace = getattr(function, "__globals__", {})
            if callable(namespace.get("get_tts_wav")) and "vq_model" in namespace:
                candidates[("gpt_webui", id(namespace))] = namespace
            if callable(namespace.get("gen_single")) and "tts" in namespace:
                candidates[("index_v2", id(namespace))] = namespace
        if len(candidates) != 1:
            raise NativeGuardError("exactly one supported native Gradio model namespace is required")
        (kind, _namespace_id), namespace = next(iter(candidates.items()))
        if kind not in guard.expected_components:
            raise NativeGuardError("Gradio engine does not match the reviewed launcher")
        # All callbacks in the supported native namespace share one process gate.
        # CPU callbacks may conservatively reset the idle timer; GPU callbacks cannot bypass it.
        count = 0
        for event in events:
            function = getattr(event, "fn", None)
            if getattr(function, "__globals__", None) is namespace:
                originals[id(event)] = function
                event.fn = guard.wrap(function)
                count += 1
        if count == 0:
            raise NativeGuardError("native GPU callbacks were not registered")
        target = namespace if kind == "gpt_webui" else namespace["tts"]
        adapter = adapter_factory(kind, target, shared_modules=_shared_modules(kind))
        guard.register_component(kind, adapter)
        return original_launch(blocks, *args, **kwargs)

    gradio.Blocks.launch = launch
    if "gpt_api_v2" in guard.expected_components:
        if uvicorn is None:
            raise NativeGuardError("dual native launcher requires uvicorn")
        original_run = uvicorn.run

        def run(app=None, *args, **kwargs):
            module = _loaded_module(("api_v2",))
            if app is not getattr(module, "APP", None) or kwargs.get("workers", 1) != 1 or kwargs.get("reload", False):
                raise NativeGuardError("only the reviewed single-process native API can be wrapped")
            adapter = adapter_factory("gpt_api_v2", module.tts_pipeline, shared_modules=_shared_modules("gpt_api_v2"))
            guard.register_component("gpt_api_v2", adapter)
            return original_run(NativeASGIAdmission(app, guard), *args, **kwargs)

        uvicorn.run = run


def run_native(config: dict[str, Any]) -> None:
    """Launch only when called explicitly during an agreed maintenance window."""
    from .adapters import NativeAdapter
    from .transport import ControlServer, CoordinatorClient
    if Path(sys.executable).resolve() != Path(config["python_executable"]).resolve():
        raise NativeGuardError("use the configured original native interpreter to launch this wrapper")
    os.chdir(config["working_directory"])
    os.environ.update(config["environment"])
    sys.argv = [config["entry"], *config["argv"]]
    sys.path.insert(0, config["working_directory"])
    sys.path.insert(0, str(Path(config["entry"]).parent))
    client = CoordinatorClient(config["coordinator"]["url"], config["coordinator"]["token"])
    def memory_allocated():
        import torch
        return sum(torch.cuda.memory_allocated(i) for i in range(torch.cuda.device_count()))
    guard = NativeGuard(
        config["participant"], client, expected_components=set(KINDS[config["kind"]]),
        timeout=float(config.get("admission_timeout", 60)),
        memory_allocated=memory_allocated,
    )

    def dispatch(action, payload):
        if action == "status":
            return guard.snapshot()
        if action == "offload_if_idle":
            return guard.offload_if_idle(payload["instance_id"], payload["revision"])
        raise ValueError("unsupported native control action")

    from urllib.parse import urlsplit
    from .transport import _endpoint
    _endpoint(config["control"]["url"])
    endpoint = urlsplit(config["control"]["url"])
    control = ControlServer(
        dispatch, config["control"]["token"], host=endpoint.hostname, port=endpoint.port or 80,
        allowed_actions={"status", "offload_if_idle"},
    )
    control.start()
    guard.start_heartbeat()
    try:
        guard.begin_startup()
        if config.get("startup_patch") == "gpt_windows_bootstrap":
            from tools.startup_bootstrap import apply_startup_patches
            apply_startup_patches()
        import gradio
        import uvicorn
        install_hooks(guard, gradio=gradio, uvicorn=uvicorn, adapter_factory=NativeAdapter)
        runpy.run_path(config["entry"], run_name="__main__")
    finally:
        guard.close()
        control.close()
