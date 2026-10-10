"""CPU adapters over our own ephemeral loopback servers; no native/GPU calls."""
from __future__ import annotations

import threading
import time

import pytest

from app.native_gpu.coordinator import GPUCoordinator
from app.native_gpu.guard import NativeGuard
from app.native_gpu.protocol import CoordinationTimeout
from app.native_gpu.transport import ControlClient, ControlServer, CoordinatorClient


class Clock:
    value = 0.0
    def __call__(self):
        return self.value


class CPUFixtureAdapter:
    def __init__(self):
        self.residency = "gpu"  # Evidence label only: this fixture allocates no GPU.
        self.moves = []
    def snapshot(self):
        return {"residency": self.residency, "verified": True, "supported": True}
    def offload(self):
        self.moves.append("cpu")
        self.residency = "cpu"
        return self.snapshot()
    def restore(self):
        self.moves.append("gpu")
        self.residency = "gpu"
        return self.snapshot()


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.005)
    raise AssertionError("fixture did not reach the expected lifecycle state")


def test_real_loopback_native_idle_offload_comfy_lease_and_priority_cleanup(tmp_path):
    clock = Clock()
    secret = "cpu-fixture-control-secret-123456789"
    coordinator = GPUCoordinator({"gpu": ["native"]}, monotonic=clock, journal_path=tmp_path / "journal.json")
    entered, finish = threading.Event(), threading.Event()
    errors = []
    worker = None
    guard = None
    try:
        with ControlServer(coordinator.dispatch, secret) as server:
            client = CoordinatorClient(server.base_url, secret, resource_group="gpu", timeout=2)
            guard = NativeGuard("native", client, expected_components={"ui"}, monotonic=clock,
                                memory_allocated=lambda: 0, timeout=2)
            adapter = CPUFixtureAdapter()
            guard.begin_startup()
            guard.register_component("ui", adapter)
            original = guard.snapshot()
            def native_dispatch(action, payload):
                if action == "status":
                    return guard.snapshot()
                return guard.offload_if_idle(payload["instance_id"], payload["revision"])
            with ControlServer(native_dispatch, secret, allowed_actions={"status", "offload_if_idle"}) as native_server:
                native_client = ControlClient(native_server.base_url, secret, timeout=2)
                coordinator.set_offload_callback("native", lambda _participant, instance, revision:
                    native_client.call("offload_if_idle", {"instance_id": instance, "revision": revision}))
                clock.value = 299.9
                client.update_native("native", guard.snapshot())
                with pytest.raises(CoordinationTimeout):
                    client.acquire_comfy("suite", 0)
                assert adapter.moves == []
                clock.value = 300
                client.update_native("native", guard.snapshot())
                comfy = client.acquire_comfy("suite", 2)
                assert client.check_comfy(comfy)
                assert adapter.moves == ["cpu"]
                assert native_client.call("status")["revision"] == original["revision"]
                def native_work():
                    try:
                        with guard.operation():
                            entered.set()
                            if not finish.wait(3):
                                raise AssertionError("fixture completion was not signalled")
                    except BaseException as exc:
                        errors.append(exc)
                worker = threading.Thread(target=native_work, daemon=True)
                worker.start()
                wait_for(lambda: client.snapshot()["groups"]["gpu"]["native_waiting"] == 1)
                assert not client.check_comfy(comfy)
                assert not entered.is_set()
                assert client.release_comfy(comfy, clean=False)
                assert client.snapshot()["groups"]["gpu"]["cleanup_failed"]
                assert adapter.moves == ["cpu"]
                assert client.release_comfy(comfy, clean=True)
                assert entered.wait(2)
                assert adapter.moves == ["cpu", "gpu"]
                current = guard.snapshot()
                assert current["active"] == 1
                assert (current["pid"], current["instance_id"]) == (original["pid"], original["instance_id"])
                finish.set()
                worker.join(2)
                assert not worker.is_alive() and not errors
                assert guard.snapshot()["idle_seconds"] == 0
                assert client.snapshot()["groups"]["gpu"]["native_active"] == 0
    finally:
        finish.set()
        if worker:
            worker.join(3)
        if guard:
            guard.close()
        coordinator.close()
    restarted = GPUCoordinator({"gpu": ["native"]}, journal_path=tmp_path / "journal.json")
    try:
        assert not restarted.snapshot()["groups"]["gpu"]["recovery_required"]
    finally:
        restarted.close()
