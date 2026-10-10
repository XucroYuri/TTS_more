from __future__ import annotations

import asyncio
import threading

import pytest

from app.native_gpu.guard import NativeASGIAdmission, NativeGuard, NativeGuardError


class Clock:
    value = 0.0
    def __call__(self):
        return self.value


class Coordinator:
    def __init__(self):
        self.statuses = []
        self.tokens = set()
        self.allowed = threading.Event()
        self.allowed.set()

    def update_native(self, participant, status):
        self.statuses.append(dict(status))

    def acquire_native(self, participant, timeout):
        if not self.allowed.wait(timeout):
            raise TimeoutError("cleanup is not confirmed")
        token = f"native-{len(self.statuses)}"
        self.tokens.add(token)
        return token

    def release_native(self, token):
        self.tokens.remove(token)
        return True


class Adapter:
    def __init__(self):
        self.residency = "gpu"
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


def ready_guard(*, components=("ui",), memory=lambda: 0):
    clock = Clock()
    coordinator = Coordinator()
    guard = NativeGuard("native", coordinator, expected_components=set(components),
                        monotonic=clock, memory_allocated=memory, timeout=1)
    guard.begin_startup()
    adapters = {name: Adapter() for name in components}
    for name, adapter in adapters.items():
        guard.register_component(name, adapter)
    return guard, clock, coordinator, adapters


def test_idle_threshold_and_operation_resets_timer_without_changing_process_identity():
    guard, clock, coordinator, adapters = ready_guard()
    original = guard.snapshot()
    clock.value = 299.9
    with pytest.raises(NativeGuardError):
        guard.offload_if_idle(original["instance_id"], original["revision"])
    assert adapters["ui"].moves == []
    clock.value = 300
    result = guard.offload_if_idle(original["instance_id"], original["revision"])
    assert result["residency"] == "cpu"
    assert result["pid"] == original["pid"]
    assert result["instance_id"] == original["instance_id"]
    with guard.operation():
        assert coordinator.tokens
        assert guard.snapshot()["active"] == 1
    assert adapters["ui"].moves == ["cpu", "gpu"]
    assert guard.snapshot()["idle_seconds"] == 0
    assert not coordinator.tokens
    with pytest.raises(NativeGuardError):
        guard.offload_if_idle(original["instance_id"], original["revision"])


def test_dual_ui_api_startup_holds_native_priority_until_both_are_registered():
    coordinator = Coordinator()
    guard = NativeGuard("native", coordinator, expected_components={"ui", "api"})
    guard.begin_startup()
    guard.register_component("ui", Adapter())
    assert not guard.snapshot()["ready"]
    assert guard.snapshot()["active"] == 1
    assert coordinator.tokens
    guard.register_component("api", Adapter())
    assert guard.snapshot()["ready"]
    assert not coordinator.tokens


def test_streaming_generator_keeps_priority_until_closed():
    guard, clock, coordinator, _ = ready_guard()
    @guard.wrap
    def stream():
        yield "first"
        yield "second"
    generator = stream()
    assert not coordinator.tokens
    assert next(generator) == "first"
    assert coordinator.tokens
    clock.value = 900
    status = guard.snapshot()
    with pytest.raises(NativeGuardError):
        guard.offload_if_idle(status["instance_id"], status["revision"])
    generator.close()
    assert guard.snapshot()["active"] == 0
    assert guard.snapshot()["idle_seconds"] == 0
    assert not coordinator.tokens


def test_waiting_priority_prevents_offload_and_gpu_restore_until_cleanup_confirmed():
    guard, clock, coordinator, adapters = ready_guard()
    clock.value = 300
    status = guard.snapshot()
    guard.offload_if_idle(status["instance_id"], status["revision"])
    coordinator.allowed.clear()
    started = threading.Event()
    done = threading.Event()
    errors = []
    def request():
        try:
            with guard.operation():
                started.set()
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()
    thread = threading.Thread(target=request)
    thread.start()
    # A publication is the synchronization point, not a timing-based GPU guess.
    for _ in range(1000):
        if guard.snapshot()["waiting"]:
            break
        done.wait(0.001)
    assert guard.snapshot()["waiting"] == 1
    assert adapters["ui"].moves == ["cpu"]
    assert not started.is_set()
    with pytest.raises(NativeGuardError):
        guard.offload_if_idle(status["instance_id"], status["revision"])
    coordinator.allowed.set()
    assert done.wait(1)
    thread.join()
    assert not errors
    assert started.is_set()
    assert adapters["ui"].moves == ["cpu", "gpu"]


def test_remaining_gpu_allocation_blocks_comfy_and_future_unverified_restoration():
    guard, clock, coordinator, _ = ready_guard(memory=lambda: 1024)
    clock.value = 300
    status = guard.snapshot()
    with pytest.raises(NativeGuardError, match="allocation remains"):
        guard.offload_if_idle(status["instance_id"], status["revision"])
    assert guard.snapshot()["residency"] == "unknown"
    assert not guard.snapshot()["ready"]
    with pytest.raises(NativeGuardError, match="operator recovery"):
        with guard.operation():
            pytest.fail("must not admit GPU work")
    assert not coordinator.tokens


def test_failed_callback_returns_priority_and_marks_latest_activity():
    guard, clock, coordinator, _ = ready_guard()
    @guard.wrap
    def fail():
        raise ValueError("synthesis failed")
    clock.value = 500
    with pytest.raises(ValueError):
        fail()
    assert not coordinator.tokens
    assert guard.snapshot()["active"] == guard.snapshot()["waiting"] == 0
    assert guard.snapshot()["idle_seconds"] == 0


@pytest.mark.asyncio
async def test_asgi_stream_has_priority_until_final_body_and_non_gpu_health_is_ungated():
    guard, _, coordinator, _ = ready_guard()
    seen = []
    async def app(scope, receive, send):
        seen.append(bool(coordinator.tokens))
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        assert coordinator.tokens
        await send({"type": "http.response.body", "body": b"b", "more_body": False})
    async def send(message):
        assert coordinator.tokens
    await NativeASGIAdmission(app, guard)({"type": "http", "path": "/tts"}, None, send)
    assert seen == [True]
    assert not coordinator.tokens
    async def health(scope, receive, send):
        assert not coordinator.tokens
    await NativeASGIAdmission(health, guard)({"type": "http", "path": "/openapi.json"}, None, send)


@pytest.mark.asyncio
async def test_disconnected_asgi_request_cleans_admission_that_finishes_in_worker_thread():
    guard, _, coordinator, _ = ready_guard()
    coordinator.allowed.clear()
    async def app(scope, receive, send):
        pytest.fail("disconnected request must not execute")
    task = asyncio.create_task(NativeASGIAdmission(app, guard)({"type": "http", "path": "/tts"}, None, None))
    while guard.snapshot()["waiting"] == 0:
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    coordinator.allowed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not coordinator.tokens
    assert guard.snapshot()["active"] == 0


@pytest.mark.asyncio
async def test_asgi_native_restart_cannot_bypass_wrapper():
    guard, _, _, _ = ready_guard()
    messages = []
    async def original(scope, receive, send):
        pytest.fail("native control must not re-exec the unwrapped entry")
    async def send(message):
        messages.append(message)
    await NativeASGIAdmission(original, guard)({"type": "http", "path": "/control"}, None, send)
    assert messages[0]["status"] == 409


def test_unknown_or_changed_instance_never_offloads():
    guard, clock, _, adapters = ready_guard()
    clock.value = 301
    status = guard.snapshot()
    with pytest.raises(NativeGuardError):
        guard.offload_if_idle("previous-process", status["revision"])
    assert not adapters["ui"].moves


def test_unsupported_component_never_admits_native_callbacks_or_comfy():
    coordinator = Coordinator()
    guard = NativeGuard("native", coordinator, expected_components={"ui"})
    guard.begin_startup()
    class Unsupported:
        def snapshot(self):
            return {"supported": False, "verified": False, "residency": "unknown"}
    with pytest.raises(NativeGuardError, match="verified supported"):
        guard.register_component("ui", Unsupported())
    assert not guard.snapshot()["ready"]
    assert guard.snapshot()["active"] == 1
    assert coordinator.tokens
