"""Launch-hook and dry-review tests using source literals and CPU-only objects."""

from __future__ import annotations

import builtins
import hashlib
import inspect
import json
import runpy
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.native_gpu import bootstrap
from app.native_gpu.bootstrap import SOURCE_REQUIREMENTS, install_hooks, validate_config
from app.native_gpu.guard import NativeASGIAdmission, NativeGuard, NativeGuardError
from app.native_gpu.protocol import ProtocolError


class Coordinator:
    def __init__(self):
        self.tokens = set()
        self.counter = 0
        self.statuses = []
        self.lock = threading.Lock()

    def acquire_native(self, participant, timeout):
        with self.lock:
            self.counter += 1
            token = f"fixture-{self.counter}"
            self.tokens.add(token)
            return token

    def release_native(self, token):
        with self.lock:
            self.tokens.remove(token)
        return True

    def update_native(self, participant, status):
        self.statuses.append(dict(status))


class Adapter:
    def __init__(self, kind, root, shared_modules):
        self.kind, self.root, self.shared_modules = kind, root, shared_modules

    def snapshot(self):
        return {"supported": True, "verified": True, "residency": "gpu"}


@pytest.fixture
def launch_surface():
    calls = []

    class Blocks:
        def __init__(self, functions, *, mapping=True):
            events = [SimpleNamespace(fn=fn) for fn in functions]
            self.fns = dict(enumerate(events)) if mapping else events

        def launch(self, *args, **kwargs):
            calls.append((self, args, kwargs))
            return "fixture launched"

    return SimpleNamespace(Blocks=Blocks), calls


@pytest.fixture
def native_modules(monkeypatch):
    mel = ModuleType("module.mel_processing")
    mel.mel_basis, mel.hann_window = {}, {}
    tts = ModuleType("TTS_infer_pack.TTS")
    tts.resample_transform_dict = {}
    audio = ModuleType("indextts.s2mel.modules.audio")
    audio.mel_basis, audio.hann_window = {}, {}
    for name in (
        "module.mel_processing", "GPT_SoVITS.module.mel_processing",
        "TTS_infer_pack.TTS", "GPT_SoVITS.TTS_infer_pack.TTS",
        "indextts.s2mel.modules.audio", "api_v2",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "module.mel_processing", mel)
    monkeypatch.setitem(sys.modules, "TTS_infer_pack.TTS", tts)
    monkeypatch.setitem(sys.modules, "indextts.s2mel.modules.audio", audio)
    return mel, tts, audio


def namespace(kind="gpt_webui", *, asynchronous=False):
    # Literal fixture code gives callbacks the same globals identity as a
    # native source module without importing or executing native model code.
    ns = {"__name__": "native_fixture", "calls": [], "vq_model": object(), "tts": object()}
    exec(
        "def get_tts_wav(text: str, *, speed: float = 1.0):\n"
        "    calls.append(('tts', text, speed))\n"
        "    yield text\n"
        "def change_weights(path: str, selected=None):\n"
        "    calls.append(('weights', path))\n"
        "    return selected\n"
        "def metadata(character: str = 'fixture'):\n"
        "    calls.append(('metadata', character))\n"
        "    return character\n"
        "def gen_single(text: str, *, progress=None):\n"
        "    calls.append(('index', text))\n"
        "    return text\n",
        ns,
    )
    if kind == "gpt_webui":
        del ns["gen_single"]
    else:
        del ns["get_tts_wav"]
        del ns["vq_model"]
    if asynchronous:
        exec("async def async_callback(text: str):\n    return text\n", ns)
    return ns


def begin_guard(components):
    coordinator = Coordinator()
    guard = NativeGuard("fixture", coordinator, expected_components=set(components), timeout=1)
    guard.begin_startup()
    return guard, coordinator


def factory_calls():
    calls = []

    def factory(kind, root, *, shared_modules):
        adapter = Adapter(kind, root, shared_modules)
        calls.append(adapter)
        return adapter

    return factory, calls


@pytest.fixture
def config_file(tmp_path):
    native = tmp_path / "native"
    native.mkdir()
    entry = native / "fixture_entry.py"
    entry.write_text("raise RuntimeError('native model entry must not execute in dry review')\n", encoding="utf-8")
    config = {
        "kind": "gpt_webui", "working_directory": str(native),
        "entry": str(entry), "python_executable": sys.executable,
        "source_sha256": {"fixture_entry.py": hashlib.sha256(entry.read_bytes()).hexdigest()},
        "participant": "fixture-native", "argv": [], "environment": {"FIXTURE_PRIVATE": "hidden"},
        "coordinator": {"url": "http://127.0.0.1:32111", "token": "c" * 40},
        "control": {"url": "http://127.0.0.1:32112", "token": "n" * 40},
    }
    pin_kind_sources(config, native)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path, config, entry


def save_config(path, config):
    path.write_text(json.dumps(config), encoding="utf-8")


def pin_kind_sources(config, native):
    for relative in SOURCE_REQUIREMENTS[config["kind"]]:
        source = native / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("raise RuntimeError('reviewed CPU fixture source must not execute')\n", encoding="utf-8")
        config["source_sha256"][relative] = hashlib.sha256(source.read_bytes()).hexdigest()


def test_pinned_entry_validates_without_executing_it(config_file):
    path, _, entry = config_file
    config = validate_config(path)
    assert config["entry"] == str(entry.resolve())
    assert config["argv"] == []


def test_changed_reviewed_source_is_rejected(config_file):
    path, _, entry = config_file
    entry.write_text("raise RuntimeError('changed fixture')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="reviewed native source changed"):
        validate_config(path)


def test_hash_manifest_must_include_entry(config_file):
    path, config, _ = config_file
    del config["source_sha256"]["fixture_entry.py"]
    save_config(path, config)
    with pytest.raises(ValueError, match="must include its entry"):
        validate_config(path)


@pytest.mark.parametrize("kind", ["gpt_webui", "gpt_dual", "index_v2"])
def test_model_and_cache_source_hashes_are_mandatory(config_file, kind):
    path, config, entry = config_file
    config["kind"] = kind
    pin_kind_sources(config, entry.parent)
    missing = next(iter(SOURCE_REQUIREMENTS[kind]))
    del config["source_sha256"][missing]
    save_config(path, config)
    with pytest.raises(ValueError, match="model and cache sources"):
        validate_config(path)


def test_changed_pinned_cache_source_rejects_unchanged_entry(config_file):
    path, _, entry = config_file
    source = entry.parent / "GPT_SoVITS/module/mel_processing.py"
    source.write_text("raise RuntimeError('changed cache implementation')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="reviewed native source changed"):
        validate_config(path)


@pytest.mark.parametrize("endpoint", ["http://192.168.0.2:32112", "http://localhost:32112", "https://127.0.0.1:32112"])
def test_dry_review_rejects_nonliteral_or_nonloopback_control(config_file, endpoint):
    path, config, _ = config_file
    config["control"]["url"] = endpoint
    save_config(path, config)
    with pytest.raises(ProtocolError):
        validate_config(path)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 121])
def test_dry_review_rejects_invalid_admission_timeout(config_file, timeout):
    path, config, _ = config_file
    config["admission_timeout"] = timeout
    save_config(path, config)
    with pytest.raises((ValueError, ProtocolError)):
        validate_config(path)


def test_startup_patch_source_must_be_pinned(config_file):
    path, config, _ = config_file
    config["startup_patch"] = "gpt_windows_bootstrap"
    save_config(path, config)
    with pytest.raises(ValueError, match="startup patch source"):
        validate_config(path)


def test_external_launch_acquires_priority_then_patches_before_native_dependency_import(
    config_file, launch_surface, monkeypatch,
):
    path, config, entry = config_file
    config["startup_patch"] = "gpt_windows_bootstrap"
    source = entry.parent / "tools/startup_bootstrap.py"
    source.parent.mkdir(exist_ok=True)
    source.write_text("raise RuntimeError('CPU fixture; never execute native patch source')\n", encoding="utf-8")
    config["source_sha256"]["tools/startup_bootstrap.py"] = hashlib.sha256(source.read_bytes()).hexdigest()
    save_config(path, config)
    reviewed = validate_config(path)
    trace = []

    class Guard:
        def __init__(self, participant, client, *, expected_components, **kwargs):
            self.expected_components = expected_components

        def start_heartbeat(self):
            trace.append("heartbeat")

        def begin_startup(self):
            trace.append("priority")

        def close(self):
            trace.append("guard_closed")

    class Server:
        def __init__(self, dispatch, token, *, host, port, allowed_actions):
            assert callable(dispatch)
            assert host == "127.0.0.1"
            assert port == 32112
            assert allowed_actions == {"status", "offload_if_idle"}

        def start(self):
            trace.append("server_started")

        def close(self):
            trace.append("server_closed")

    from app.native_gpu import transport
    monkeypatch.setattr(bootstrap, "NativeGuard", Guard)
    monkeypatch.setattr(transport, "CoordinatorClient", lambda *args: object())
    monkeypatch.setattr(transport, "ControlServer", Server)
    monkeypatch.setattr(bootstrap.runpy, "run_path", lambda *args, **kwargs: trace.append("entry"))
    tools = ModuleType("tools")
    tools.__path__ = []
    patch_module = ModuleType("tools.startup_bootstrap")
    patch_module.apply_startup_patches = lambda: trace.append("patch")
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.startup_bootstrap", patch_module)
    gradio, _ = launch_surface
    monkeypatch.setitem(sys.modules, "gradio", gradio)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *args, **kwargs: None))
    original_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        if name == "torch":
            pytest.fail("launcher eagerly imported torch before native source execution")
        if name in {"gradio", "uvicorn"}:
            assert "priority" in trace and "patch" in trace
            trace.append(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    # run_native deliberately changes the new process's cwd/argv/path/env.
    # The fixture restores those local process properties after its CPU test.
    monkeypatch.chdir(Path.cwd())
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("FIXTURE_PRIVATE", "fixture_initial")
    bootstrap.run_native(reviewed)
    assert trace == ["server_started", "heartbeat", "priority", "patch", "gradio", "uvicorn", "entry", "guard_closed", "server_closed"]


def test_hash_manifest_cannot_read_outside_native_checkout(config_file):
    path, config, _ = config_file
    outside = path.parent / "outside.py"
    outside.write_text("fixture", encoding="utf-8")
    config["source_sha256"]["../outside.py"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    save_config(path, config)
    with pytest.raises(ValueError):
        validate_config(path)


def test_dry_cli_does_not_import_native_models_torch_gradio_or_uvicorn(config_file, monkeypatch, capsys):
    path, config, _ = config_file
    script = Path(__file__).resolve().parents[2] / "scripts" / "run-native-gpu-wrapper.py"
    original_import = builtins.__import__
    forbidden = {"torch", "gradio", "uvicorn", "api_v2", "GPT_SoVITS", "indextts"}
    attempted = []

    def checked_import(name, *args, **kwargs):
        if name.split(".")[0] in forbidden:
            attempted.append(name)
            pytest.fail(f"dry review imported native dependency: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    monkeypatch.setattr(sys, "argv", [str(script), "--config", str(path)])
    previous = {name for name in sys.modules if name.startswith("tts_more_native_gpu")}
    try:
        with pytest.raises(SystemExit) as exit_result:
            runpy.run_path(str(script), run_name="__main__")
        assert exit_result.value.code == 0
    finally:
        for name in list(sys.modules):
            if name.startswith("tts_more_native_gpu") and name not in previous:
                del sys.modules[name]
    captured = capsys.readouterr().out
    assert json.loads(captured) == {"valid": True, "kind": "gpt_webui", "launch_requested": False}
    assert attempted == []
    assert config["control"]["token"] not in captured
    assert str(path.parent) not in captured
    assert "FIXTURE_PRIVATE" not in captured


@pytest.mark.parametrize("mapping", [True, False])
def test_all_same_namespace_gradio_callbacks_wrap_and_preserve_signature(
    launch_surface, native_modules, mapping,
):
    gradio, launched = launch_surface
    ns = namespace()
    original = [ns["get_tts_wav"], ns["change_weights"], ns["metadata"]]
    helper = lambda: "external CPU helper"
    blocks = gradio.Blocks([*original, helper], mapping=mapping)
    factory, adapters = factory_calls()
    guard, coordinator = begin_guard({"gpt_webui"})
    install_hooks(guard, gradio=gradio, uvicorn=None, adapter_factory=factory)
    assert blocks.launch(server_name="127.0.0.1") == "fixture launched"
    events = list(blocks.fns.values()) if mapping else blocks.fns
    for before, event in zip(original, events):
        assert event.fn is not before
        assert event.fn.__wrapped__ is before
        assert inspect.signature(event.fn) == inspect.signature(before)
        assert event.fn.__annotations__ == before.__annotations__
    assert inspect.isgeneratorfunction(events[0].fn)
    assert list(events[0].fn("fixture text", speed=0.9)) == ["fixture text"]
    assert events[1].fn("fixture.ckpt", selected="retained") == "retained"
    assert events[2].fn("fixture character") == "fixture character"
    assert events[3].fn is helper
    assert len(adapters) == len(launched) == 1
    assert adapters[0].root is ns
    assert adapters[0].shared_modules == {"gpt_mel_processing": native_modules[0]}
    assert guard.snapshot()["ready"]
    assert not coordinator.tokens


def test_same_kind_distinct_model_namespaces_fail_closed(launch_surface, native_modules):
    gradio, launched = launch_surface
    first, second = namespace(), namespace()
    blocks = gradio.Blocks([first["get_tts_wav"], second["get_tts_wav"]])
    factory, adapters = factory_calls()
    guard, _ = begin_guard({"gpt_webui"})
    install_hooks(guard, gradio=gradio, uvicorn=None, adapter_factory=factory)
    with pytest.raises(NativeGuardError):
        blocks.launch()
    assert not adapters
    assert not launched
    assert not guard.snapshot()["ready"]


def test_async_gradio_callback_is_explicitly_unsupported(launch_surface, native_modules):
    gradio, launched = launch_surface
    ns = namespace(asynchronous=True)
    blocks = gradio.Blocks([ns["get_tts_wav"], ns["async_callback"]])
    factory, adapters = factory_calls()
    guard, _ = begin_guard({"gpt_webui"})
    install_hooks(guard, gradio=gradio, uvicorn=None, adapter_factory=factory)
    with pytest.raises(NativeGuardError, match="async Gradio"):
        blocks.launch()
    assert not adapters
    assert not launched
    assert not guard.snapshot()["ready"]


def test_index_namespace_supplies_its_own_pipeline_and_audio_cache_module(launch_surface, native_modules):
    gradio, launched = launch_surface
    ns = namespace("index_v2")
    blocks = gradio.Blocks([ns["gen_single"], ns["metadata"]])
    factory, adapters = factory_calls()
    guard, _ = begin_guard({"index_v2"})
    install_hooks(guard, gradio=gradio, uvicorn=None, adapter_factory=factory)
    blocks.launch()
    assert adapters[0].root is ns["tts"]
    assert adapters[0].shared_modules == {"index_audio": native_modules[2]}
    assert len(launched) == 1


def test_conflicting_loaded_shared_module_identity_never_launches(
    launch_surface, native_modules, monkeypatch,
):
    gradio, launched = launch_surface
    monkeypatch.setitem(sys.modules, "GPT_SoVITS.module.mel_processing", ModuleType("conflicting_fixture"))
    ns = namespace()
    blocks = gradio.Blocks([ns["get_tts_wav"]])
    factory, adapters = factory_calls()
    guard, _ = begin_guard({"gpt_webui"})
    install_hooks(guard, gradio=gradio, uvicorn=None, adapter_factory=factory)
    with pytest.raises(NativeGuardError, match="conflicting identities"):
        blocks.launch()
    assert not adapters
    assert not launched


def test_dual_ui_first_waits_for_independent_api_before_callback_can_run(
    launch_surface, native_modules, monkeypatch,
):
    gradio, launched = launch_surface
    api = ModuleType("api_v2")
    api.APP, api.tts_pipeline = object(), object()
    monkeypatch.setitem(sys.modules, "api_v2", api)
    uvicorn_calls = []
    uvicorn = SimpleNamespace(run=lambda app, **kwargs: uvicorn_calls.append((app, kwargs)))
    guard, coordinator = begin_guard({"gpt_webui", "gpt_api_v2"})
    waiting = threading.Event()

    class ObservableEvent(threading.Event):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)

    guard._ready_event = ObservableEvent()
    factory, adapters = factory_calls()
    install_hooks(guard, gradio=gradio, uvicorn=uvicorn, adapter_factory=factory)
    ns = namespace()
    blocks = gradio.Blocks([ns["get_tts_wav"], ns["metadata"]])
    blocks.launch()
    assert not guard.snapshot()["ready"]
    assert coordinator.tokens
    errors = []

    def callback():
        try:
            blocks.fns[1].fn("fixture")
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=callback)
    thread.start()
    try:
        assert waiting.wait(1)
        assert ns["calls"] == []
        uvicorn.run(app=api.APP, host="127.0.0.1", port=32115, workers=1)
    finally:
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert not errors
    assert ns["calls"] == [("metadata", "fixture")]
    assert {a.kind for a in adapters} == {"gpt_webui", "gpt_api_v2"}
    assert adapters[0].root is ns
    assert adapters[1].root is api.tts_pipeline
    assert adapters[1].shared_modules == {
        "gpt_mel_processing": native_modules[0], "gpt_tts_module": native_modules[1],
    }
    assert isinstance(uvicorn_calls[0][0], NativeASGIAdmission)
    assert uvicorn_calls[0][0].app is api.APP
    assert len(launched) == 1
    assert guard.snapshot()["ready"]
    assert not coordinator.tokens


@pytest.mark.parametrize("kwargs", [{"workers": 2}, {"reload": True}])
def test_dual_api_rejects_workers_and_reload(launch_surface, native_modules, monkeypatch, kwargs):
    gradio, _ = launch_surface
    api = ModuleType("api_v2")
    api.APP, api.tts_pipeline = object(), object()
    monkeypatch.setitem(sys.modules, "api_v2", api)
    calls = []
    uvicorn = SimpleNamespace(run=lambda app, **kw: calls.append(app))
    guard, _ = begin_guard({"gpt_webui", "gpt_api_v2"})
    factory, adapters = factory_calls()
    install_hooks(guard, gradio=gradio, uvicorn=uvicorn, adapter_factory=factory)
    with pytest.raises(NativeGuardError, match="single-process"):
        uvicorn.run(app=api.APP, **kwargs)
    assert not calls
    assert not adapters
