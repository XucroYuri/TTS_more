from __future__ import annotations

import importlib.util
import json
import sys
import threading
import types
from pathlib import Path

import pytest

from app.native_gpu import launcher
from app.native_gpu.coordinator import GPUCoordinator
from app.native_gpu.protocol import ProtocolError
from app.native_gpu.transport import ControlClient


SECRET = "private-token-only-for-fixtures-123456789"
REPO = Path(__file__).resolve().parents[2]


def configuration():
    return {
        "control": {"url": "http://127.0.0.1:43101", "token": SECRET},
        "groups": {"gpu": ["native"]},
        "participants": {"native": {"url": "http://127.0.0.1:43102", "token": SECRET}},
        "journal_path": "private-state/journal.json",
    }


def write_config(tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_dry_validation_never_calls_network_or_writes_journal(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry validation must not contact a control endpoint")
    monkeypatch.setattr(ControlClient, "call", forbidden)
    monkeypatch.setattr(launcher, "ControlServer", forbidden)
    config_path = write_config(tmp_path, configuration())
    existing_paths = set(tmp_path.rglob("*"))
    config = launcher.validate_coordinator_config(config_path)
    assert Path(config["journal_path"]) == tmp_path / "private-state" / "journal.json"
    assert not (tmp_path / "private-state").exists()
    assert set(tmp_path.rglob("*")) == existing_paths


@pytest.mark.parametrize("url", [
    "http://localhost:43101", "http://192.168.1.10:43101", "https://127.0.0.1:43101",
    "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:private-secret",
    "http://127.0.0.1:43101/v1/action", "http://user:pass@127.0.0.1:43101",
])
def test_config_rejects_nonliteral_or_invalid_endpoint(tmp_path, url):
    config = configuration()
    config["control"]["url"] = url
    with pytest.raises(ProtocolError):
        launcher.validate_coordinator_config(write_config(tmp_path, config))
    assert not (tmp_path / "private-state").exists()


@pytest.mark.parametrize("groups,participants", [
    ({}, {"native": {}}), ({"gpu": []}, {}),
    ({"gpu": ["native", "native"]}, {"native": {}}),
    ({"a": ["native"], "b": ["native"]}, {"native": {}}),
    ({"gpu": ["native"]}, {}), ({"gpu": ["native"]}, {"extra": {}}),
    ({"gpu": "native"}, {"native": {}}),
])
def test_config_requires_complete_fixed_participant_registration(tmp_path, groups, participants):
    config = configuration()
    config.update(groups=groups, participants=participants)
    with pytest.raises((ValueError, ProtocolError)):
        launcher.validate_coordinator_config(write_config(tmp_path, config))


def test_missing_journal_is_rejected_and_validation_coordinator_always_closed(tmp_path, monkeypatch):
    instances = []
    class TrackedCoordinator(GPUCoordinator):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)
    monkeypatch.setattr(launcher, "GPUCoordinator", TrackedCoordinator)
    config = configuration()
    config.pop("journal_path")
    with pytest.raises(ValueError):
        launcher.validate_coordinator_config(write_config(tmp_path, config))
    assert len(instances) == 1 and instances[0]._closed


@pytest.mark.parametrize("failure_phase", ["constructor", "start", "close"])
def test_serve_failure_always_releases_persistent_journal_lock(tmp_path, monkeypatch, failure_phase):
    class FakeServer:
        def __init__(self, *_args, **_kwargs):
            if failure_phase == "constructor":
                raise RuntimeError("private constructor diagnostics")
        def start(self):
            if failure_phase == "start":
                raise RuntimeError("private startup diagnostics")
        def close(self):
            if failure_phase == "close":
                raise RuntimeError("private close diagnostics")
    monkeypatch.setattr(launcher, "ControlServer", FakeServer)
    config = launcher.validate_coordinator_config(write_config(tmp_path, configuration()))
    stop = threading.Event()
    stop.set()
    with pytest.raises(RuntimeError):
        launcher.serve_coordinator(config, stop=stop)
    reopened = GPUCoordinator(config["groups"], journal_path=config["journal_path"])
    try:
        assert not reopened.snapshot()["groups"]["gpu"]["recovery_required"]
    finally:
        reopened.close()


def load_script(name):
    spec = importlib.util.spec_from_file_location("fixture_" + name.replace("-", "_"), REPO / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def script_fixture(monkeypatch, *, native=False, failure=None):
    suffix = "bootstrap" if native else "launcher"
    stub = types.ModuleType("tts_more_native_gpu." + suffix)
    calls = []
    config = {"kind": "gpt_webui"} if native else configuration()
    def validate(_path):
        calls.append("validate")
        return config
    def run(_config):
        calls.append("run")
        if failure is not None:
            raise failure
    if native:
        stub.validate_config, stub.run_native = validate, run
    else:
        stub.validate_coordinator_config, stub.serve_coordinator = validate, run
    monkeypatch.setitem(sys.modules, stub.__name__, stub)
    # Dynamic private package loading is restored after every fixture.
    monkeypatch.setitem(sys.modules, "tts_more_native_gpu", types.ModuleType("tts_more_native_gpu"))
    return calls


@pytest.mark.parametrize("native", [False, True])
def test_cli_requires_explicit_launch_and_outputs_only_nonsecret_summary(monkeypatch, capsys, native):
    calls = script_fixture(monkeypatch, native=native)
    name = "run-native-gpu-wrapper.py" if native else "run-gpu-coordinator.py"
    script = load_script(name)
    monkeypatch.setattr(sys, "argv", [name, "--config", "private-config.json"])
    assert script.main() == 0
    assert calls == ["validate"]
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err and "private-config" not in output.out + output.err
    assert json.loads(output.out)["valid"] is True
    flag = "--launch" if native else "--serve"
    monkeypatch.setattr(sys, "argv", [name, "--config", "private-config.json", flag])
    assert script.main() == 0
    assert calls == ["validate", "validate", "run"]


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("failure,expected", [(RuntimeError(SECRET), 2), (KeyboardInterrupt(), 130)])
def test_cli_future_entry_failure_is_sanitized_and_interrupt_has_exit130(monkeypatch, capsys, native, failure, expected):
    calls = script_fixture(monkeypatch, native=native, failure=failure)
    name = "run-native-gpu-wrapper.py" if native else "run-gpu-coordinator.py"
    script = load_script(name)
    monkeypatch.setattr(sys, "argv", [name, "--config", "private-config.json", "--launch" if native else "--serve"])
    assert script.main() == expected
    assert calls == ["validate", "run"]
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err and "Traceback" not in output.out + output.err
    if expected == 2:
        assert json.loads(output.err) == {"valid": False, "error": "native_wrapper_failed" if native else "gpu_coordinator_failed"}
    else:
        assert not output.err
