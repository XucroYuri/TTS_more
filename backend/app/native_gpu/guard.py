"""In-process native admission, activity accounting and atomic idle migration."""
from __future__ import annotations

import functools
import inspect
import os
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable


class NativeGuardError(RuntimeError):
    pass


class NativeGuard:
    def __init__(
        self, participant: str, coordinator: Any, *,
        expected_components: set[str], idle_seconds: float = 300.0,
        timeout: float = 60.0, monotonic: Callable[[], float] = time.monotonic,
        memory_allocated: Callable[[], int] | None = None,
    ) -> None:
        if not participant or not expected_components or idle_seconds < 300:
            raise ValueError("participant, components and a minimum 300-second idle threshold are required")
        self.participant = participant
        self.coordinator = coordinator
        self.expected_components = frozenset(expected_components)
        self.idle_seconds = idle_seconds
        self.timeout = timeout
        self.clock = monotonic
        self.memory_allocated = memory_allocated
        self.instance_id = uuid.uuid4().hex
        self._state_lock = threading.RLock()
        # Lock is deliberately not owner-bound: ASGI enter/exit may use different workers.
        self._operation_lock = threading.Lock()
        self._ready_event = threading.Event()
        self._adapters: dict[str, Any] = {}
        self._active = 0
        self._waiting = 0
        self._revision = 0
        self._last_activity = self.clock()
        self._residency = "unknown"
        self._failure: str | None = None
        self._startup_token: str | None = None
        self._stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "instance_id": self.instance_id, "pid": os.getpid(),
                "active": self._active, "waiting": self._waiting,
                "revision": self._revision,
                "idle_seconds": max(0.0, self.clock() - self._last_activity),
                "residency": self._residency,
                "ready": self._ready_event.is_set() and self._failure is None,
            }

    def _publish(self) -> None:
        self.coordinator.update_native(self.participant, self.snapshot())

    def start_heartbeat(self, interval: float = 1.0) -> None:
        if not 0 < interval <= 2:
            raise ValueError("heartbeat interval must be at most two seconds")
        if self._heartbeat_thread is not None:
            raise NativeGuardError("heartbeat already started")

        def heartbeat() -> None:
            while not self._stop.wait(interval):
                try:
                    self._publish()
                except Exception:
                    # A missing heartbeat closes the coordinator's Comfy admission.
                    # It cannot authorize a local GPU restoration.
                    continue

        self._heartbeat_thread = threading.Thread(target=heartbeat, name="native-gpu-status", daemon=True)
        self._heartbeat_thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=3)

    def begin_startup(self) -> None:
        """Acquire native priority before importing any model-loading entry point."""
        with self._state_lock:
            if self._startup_token or self._active:
                raise NativeGuardError("startup already active")
            self._waiting += 1
            self._revision += 1
            self._last_activity = self.clock()
        self._publish()
        try:
            token = self.coordinator.acquire_native(self.participant, self.timeout)
        except BaseException:
            with self._state_lock:
                self._waiting -= 1
                self._revision += 1
                self._last_activity = self.clock()
            self._publish()
            raise
        with self._state_lock:
            self._waiting -= 1
            self._active += 1
            self._startup_token = token
            self._residency = "gpu"
            self._revision += 1
        self._publish()

    def register_component(self, name: str, adapter: Any) -> None:
        with self._state_lock:
            if not self._startup_token or name not in self.expected_components or name in self._adapters:
                raise NativeGuardError("unexpected, duplicate or late native component")
            # This validates the supported shape before admitting the WebUI/API.
            evidence = adapter.snapshot()
            if (evidence.get("supported") is not True or evidence.get("verified") is not True
                    or evidence.get("residency") != "gpu"):
                raise NativeGuardError("native component does not have a verified supported GPU layout")
            self._adapters[name] = adapter
            if set(self._adapters) != self.expected_components:
                return
            token = self._startup_token
            self._startup_token = None
            self._active -= 1
            self._revision += 1
            self._last_activity = self.clock()
            self._ready_event.set()
        self._publish()
        self.coordinator.release_native(token)

    def offload_if_idle(self, instance_id: str, revision: int) -> dict[str, Any]:
        """Only the exact inactive incarnation/revision may be migrated."""
        if not self._operation_lock.acquire(blocking=False):
            raise NativeGuardError("native GPU operation is active")
        try:
            with self._state_lock:
                if (
                    instance_id != self.instance_id or revision != self._revision
                    or self._active or self._waiting
                    or not self.snapshot()["ready"]
                    or self.clock() - self._last_activity < self.idle_seconds
                ):
                    raise NativeGuardError("native idle evidence changed or is not mature")
                if self._residency == "cpu":
                    return self.snapshot()
                try:
                    for adapter in self._adapters.values():
                        outcome = adapter.offload()
                        if outcome.get("residency") != "cpu" or outcome.get("verified") is not True:
                            raise NativeGuardError("adapter did not verify CPU residency")
                    for adapter in self._adapters.values():
                        evidence = adapter.snapshot()
                        if evidence.get("residency") != "cpu" or evidence.get("verified") is not True:
                            raise NativeGuardError("combined native CPU residency changed")
                    # Moving all whitelisted fields does not excuse an unaccounted CUDA allocation.
                    if self.memory_allocated is None or self.memory_allocated() != 0:
                        raise NativeGuardError("native CUDA allocation remains or cannot be verified")
                    self._residency = "cpu"
                except BaseException as exc:
                    self._failure = type(exc).__name__
                    self._residency = "unknown"
                    raise
                return self.snapshot()
        finally:
            self._operation_lock.release()

    @contextmanager
    def operation(self):
        if not self._ready_event.wait(self.timeout):
            raise NativeGuardError("native component registration is incomplete")
        with self._state_lock:
            if self._failure:
                raise NativeGuardError("native device migration needs operator recovery")
            self._waiting += 1
            self._revision += 1
            self._last_activity = self.clock()
        token: str | None = None
        locked = False
        started = False
        try:
            self._publish()
            token = self.coordinator.acquire_native(self.participant, self.timeout)
            locked = self._operation_lock.acquire(timeout=self.timeout)
            if not locked:
                raise NativeGuardError("native operation serialization timed out")
            with self._state_lock:
                if self._failure:
                    raise NativeGuardError("native device migration needs operator recovery")
                if self._residency == "cpu":
                    try:
                        for adapter in self._adapters.values():
                            outcome = adapter.restore()
                            if outcome.get("residency") != "gpu" or outcome.get("verified") is not True:
                                raise NativeGuardError("adapter did not verify GPU restoration")
                        self._residency = "gpu"
                    except BaseException as exc:
                        self._failure = type(exc).__name__
                        self._residency = "unknown"
                        raise
                if self._residency != "gpu":
                    raise NativeGuardError("native residency is unknown")
                self._waiting -= 1
                self._active += 1
                self._revision += 1
                started = True
            self._publish()
            yield
        finally:
            with self._state_lock:
                if started:
                    self._active -= 1
                else:
                    self._waiting -= 1
                self._revision += 1
                self._last_activity = self.clock()
            if locked:
                self._operation_lock.release()
            try:
                self._publish()
            finally:
                if token is not None:
                    self.coordinator.release_native(token)

    def wrap(self, function: Callable[..., Any]) -> Callable[..., Any]:
        """Keep a generator's admission until consumption, close or failure."""
        if inspect.isasyncgenfunction(function):
            raise NativeGuardError("async-generator Gradio callbacks need an explicit adapter")
        if inspect.isgeneratorfunction(function):
            @functools.wraps(function)
            def generator(*args, **kwargs):
                with self.operation():
                    yield from function(*args, **kwargs)
            return generator
        if inspect.iscoroutinefunction(function):
            raise NativeGuardError("async Gradio callbacks need the ASGI admission adapter")
        @functools.wraps(function)
        def regular(*args, **kwargs):
            with self.operation():
                return function(*args, **kwargs)
        return regular


class NativeASGIAdmission:
    """Hold native priority through the final streaming HTTP response body."""
    GPU_PATHS = frozenset({"/tts", "/set_refer_audio", "/set_gpt_weights", "/set_sovits_weights"})

    def __init__(self, app: Any, guard: NativeGuard) -> None:
        self.app = app
        self.guard = guard

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/control":
            # Native restart uses sys.argv and can re-exec the unwrapped source.
            # Lifecycle changes must retain the wrapper and use a maintenance launcher.
            body = b'{"message":"GPU coordination is active; use a reviewed maintenance launcher for restart or exit."}'
            await send({"type": "http.response.start", "status": 409,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode("ascii"))]})
            await send({"type": "http.response.body", "body": body})
            return
        if scope.get("type") != "http" or scope.get("path") not in self.GPU_PATHS:
            await self.app(scope, receive, send)
            return
        import asyncio
        async def finish(worker):
            cancelled = False
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    cancelled = True
            return worker.result(), cancelled

        admission = self.guard.operation()
        enter = asyncio.create_task(asyncio.to_thread(admission.__enter__))
        _, cancelled = await finish(enter)
        if cancelled:
            # Repeated disconnect/cancel signals cannot abandon a running worker.
            await finish(asyncio.create_task(asyncio.to_thread(admission.__exit__, None, None, None)))
            raise asyncio.CancelledError
        try:
            await self.app(scope, receive, send)
        finally:
            exit_task = asyncio.create_task(asyncio.to_thread(admission.__exit__, None, None, None))
            _, cancelled = await finish(exit_task)
            if cancelled:
                raise asyncio.CancelledError
