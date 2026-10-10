from __future__ import annotations

import threading
import time
import hashlib
import json
import pytest

from app.native_gpu.coordinator import GPUCoordinator
from app.native_gpu.protocol import CoordinationTimeout, CoordinationUnavailable, LeaseDenied, NativeStatus, ProtocolError
from app.native_gpu.journal import GPUJournal, JournalError


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def status(*, instance="native-1", pid=123, residency="cpu", active=0, waiting=0, revision=0, idle=300, ready=True):
    return NativeStatus(instance, pid, active, waiting, revision, idle, residency, ready)


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.005)
    raise AssertionError("thread did not reach the expected state")


def async_call(function):
    results = []
    errors = []

    def run():
        try:
            results.append(function())
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, results, errors


def test_all_fixed_participants_must_be_fresh_ready_cpu_before_comfy_admission():
    coordinator = GPUCoordinator({"gpu": ["web", "api"]})
    coordinator.update_native("web", status())
    with pytest.raises(CoordinationTimeout):
        coordinator.acquire_comfy("tts", 0)
    coordinator.update_native("api", status(instance="native-2", ready=False))
    with pytest.raises(CoordinationTimeout):
        coordinator.acquire_comfy("tts", 0)
    coordinator.update_native("api", status(instance="native-2"))
    token = coordinator.acquire_comfy("tts", 0)
    assert coordinator.check_comfy(token)
    assert coordinator.release_comfy(token, True)


@pytest.mark.parametrize("updates", [dict(active=1), dict(waiting=1), dict(residency="gpu"), dict(residency="unknown"), dict(ready=False)])
def test_published_native_activity_or_unknown_state_revokes_comfy(updates):
    coordinator = GPUCoordinator({"gpu": ["web"]})
    coordinator.update_native("web", status())
    token = coordinator.acquire_comfy("tts", 0)
    coordinator.update_native("web", status(**updates))
    assert not coordinator.check_comfy(token)
    with pytest.raises(CoordinationTimeout):
        coordinator.acquire_native("web", 0)
    assert coordinator.release_comfy(token, True)
    native = coordinator.acquire_native("web", 0)
    assert coordinator.release_native(native)


def test_native_priority_waits_for_explicit_clean_acknowledgement():
    coordinator = GPUCoordinator({"gpu": ["web"]})
    coordinator.update_native("web", status())
    comfy = coordinator.acquire_comfy("workflow", 0)
    thread, tokens, errors = async_call(lambda: coordinator.acquire_native("web", 2))
    wait_for(lambda: coordinator.snapshot()["groups"]["gpu"]["native_waiting"] == 1)
    assert not coordinator.check_comfy(comfy)
    assert not tokens
    assert coordinator.release_comfy(comfy, False)
    assert not tokens
    assert coordinator.snapshot()["groups"]["gpu"]["comfy"]["cleanup_required"]
    assert coordinator.snapshot()["groups"]["gpu"]["comfy"]["cleanup_failed"]
    assert coordinator.snapshot()["groups"]["gpu"]["comfy"]["reason"] == "comfy_cleanup_unconfirmed"
    assert coordinator.release_comfy(comfy, True)
    thread.join(2)
    assert not thread.is_alive() and not errors and len(tokens) == 1
    coordinator.release_native(tokens[0])


def test_ttl_expiry_does_not_become_gpu_cleanup_and_cannot_be_revived():
    clock = Clock()
    coordinator = GPUCoordinator({"gpu": ["web"]}, monotonic=clock, freshness_seconds=60, comfy_ttl_seconds=10)
    coordinator.update_native("web", status())
    token = coordinator.acquire_comfy("workflow", 0)
    clock.now = 10
    assert not coordinator.check_comfy(token)
    coordinator.update_native("web", status())
    assert not coordinator.check_comfy(token)
    with pytest.raises(CoordinationTimeout):
        coordinator.acquire_native("web", 0)
    assert coordinator.release_comfy(token, True)
    native = coordinator.acquire_native("web", 0)
    coordinator.release_native(native)


def test_valid_check_renews_lease_but_stale_native_heartbeat_revokes_it():
    clock = Clock()
    coordinator = GPUCoordinator({"gpu": ["web"]}, monotonic=clock, freshness_seconds=5, comfy_ttl_seconds=4)
    coordinator.update_native("web", status())
    token = coordinator.acquire_comfy("workflow", 0)
    clock.now = 3
    coordinator.update_native("web", status())
    assert coordinator.check_comfy(token)
    clock.now = 6
    coordinator.update_native("web", status())
    assert coordinator.check_comfy(token)
    clock.now = 11
    assert not coordinator.check_comfy(token)
    assert "native_status_stale:web" in coordinator.snapshot()["groups"]["gpu"]["reasons"]


def test_atomic_offload_only_after_continuous_five_minute_native_evidence():
    called = []

    def offload(participant, instance, revision):
        called.append((participant, instance, revision))
        return status(instance=instance, revision=revision)

    coordinator = GPUCoordinator({"gpu": ["web"]}, offload_callbacks={"web": offload})
    coordinator.update_native("web", status(residency="gpu", idle=299.9))
    with pytest.raises(CoordinationTimeout):
        coordinator.acquire_comfy("workflow", 0)
    assert called == []
    coordinator.update_native("web", status(residency="gpu", idle=300, revision=7))
    token = coordinator.acquire_comfy("workflow", 2)
    assert called == [("web", "native-1", 7)]
    coordinator.release_comfy(token, True)


def test_native_request_cannot_restore_gpu_while_an_offload_is_in_flight():
    entered, finish = threading.Event(), threading.Event()

    def offload(participant, instance, revision):
        entered.set()
        assert finish.wait(2)
        return status(instance=instance, revision=revision)

    coordinator = GPUCoordinator({"gpu": ["web"]}, offload_callbacks={"web": offload})
    coordinator.update_native("web", status(residency="gpu"))
    comfy_thread, comfy, comfy_errors = async_call(lambda: coordinator.acquire_comfy("workflow", 0.2))
    assert entered.wait(1)
    native_thread, native, native_errors = async_call(lambda: coordinator.acquire_native("web", 2))
    wait_for(lambda: coordinator.snapshot()["groups"]["gpu"]["native_waiting"] == 1)
    assert native == []
    finish.set()
    native_thread.join(2)
    assert not native_errors and len(native) == 1
    comfy_thread.join(2)
    assert comfy == [] and isinstance(comfy_errors[0], CoordinationTimeout)
    coordinator.release_native(native[0])


@pytest.mark.parametrize("result", [status(instance="wrong"), status(revision=2), status(residency="gpu"), status(ready=False), status(active=1)])
def test_incomplete_or_stale_offload_reply_cannot_grant_comfy(result):
    coordinator = GPUCoordinator({"gpu": ["web"]}, offload_callbacks={"web": lambda *args: result})
    coordinator.update_native("web", status(residency="gpu"))
    with pytest.raises(CoordinationTimeout):
        coordinator.acquire_comfy("workflow", 0.08)
    assert "native_offload_failed:web" in coordinator.snapshot()["groups"]["gpu"]["reasons"]


def test_changed_native_revision_during_offload_rejects_old_cpu_result():
    entered, finish = threading.Event(), threading.Event()

    def offload(participant, instance, revision):
        entered.set()
        assert finish.wait(2)
        return status(instance=instance, revision=revision)

    coordinator = GPUCoordinator({"gpu": ["web"]}, offload_callbacks={"web": offload})
    coordinator.update_native("web", status(residency="gpu"))
    worker, result, errors = async_call(lambda: coordinator.acquire_comfy("workflow", 0.15))
    assert entered.wait(1)
    coordinator.update_native("web", status(residency="gpu", revision=1, waiting=1, idle=0))
    finish.set()
    worker.join(2)
    assert result == [] and isinstance(errors[0], CoordinationTimeout)
    assert coordinator.snapshot()["groups"]["gpu"]["natives"]["web"]["status"]["revision"] == 1


def test_offload_failure_is_not_blindly_retried_and_never_leaks_private_errors():
    called = []

    def offload(*args):
        called.append(args)
        raise RuntimeError("private path and credential")

    coordinator = GPUCoordinator({"gpu": ["web"]}, offload_callbacks={"web": offload})
    coordinator.update_native("web", status(residency="gpu"))
    with pytest.raises(CoordinationTimeout):
        coordinator.acquire_comfy("workflow", 0.1)
    assert len(called) == 1
    assert "private" not in str(coordinator.snapshot())


def test_concurrent_native_leases_keep_comfy_closed_until_all_return():
    coordinator = GPUCoordinator({"gpu": ["web", "api"]})
    coordinator.update_native("web", status())
    coordinator.update_native("api", status(instance="native-2"))
    first = coordinator.acquire_native("web", 0)
    second = coordinator.acquire_native("api", 0)
    coordinator.release_native(first)
    with pytest.raises(CoordinationTimeout):
        coordinator.acquire_comfy("workflow", 0)
    coordinator.release_native(second)
    comfy = coordinator.acquire_comfy("workflow", 0)
    coordinator.release_comfy(comfy, True)


def test_groups_are_independent_and_multi_group_comfy_requires_explicit_group():
    coordinator = GPUCoordinator({"local": ["local-web"], "other": ["other-web"]})
    coordinator.update_native("local-web", status())
    coordinator.update_native("other-web", status(instance="other"))
    native = coordinator.acquire_native("local-web", 0)
    with pytest.raises(LeaseDenied):
        coordinator.acquire_comfy("workflow", 0)
    comfy = coordinator.acquire_comfy("workflow", 0, resource_group="other")
    assert coordinator.check_comfy(comfy)
    coordinator.release_comfy(comfy, True)
    coordinator.release_native(native)


def test_status_identity_revision_and_schema_are_checked():
    coordinator = GPUCoordinator({"gpu": ["web"]})
    coordinator.update_native("web", status(revision=4))
    with pytest.raises(LeaseDenied):
        coordinator.update_native("web", status(revision=3))
    with pytest.raises(LeaseDenied):
        coordinator.update_native("web", status(pid=456, revision=4))
    with pytest.raises(LeaseDenied):
        coordinator.update_native("unknown", status())
    for patch in ({"active": True}, {"ready": 1}, {"idle_seconds": float("nan")}, {"residency": []}):
        with pytest.raises(ProtocolError):
            coordinator.update_native("web", {**status().to_dict(), **patch})


def test_native_instance_change_revokes_comfy_even_when_new_snapshot_is_cpu():
    coordinator = GPUCoordinator({"gpu": ["web"]})
    coordinator.update_native("web", status())
    token = coordinator.acquire_comfy("workflow", 0)
    coordinator.update_native("web", status(instance="replacement", pid=456))
    assert not coordinator.check_comfy(token)
    assert coordinator.snapshot()["groups"]["gpu"]["comfy"]["reason"] == "native_instance_changed"
    coordinator.release_comfy(token, True)


def test_tokens_are_not_published_in_snapshot_and_return_is_idempotent():
    coordinator = GPUCoordinator({"gpu": ["web"]})
    native = coordinator.acquire_native("web", 0)
    assert native not in str(coordinator.snapshot())
    assert coordinator.release_native(native)
    assert not coordinator.release_native(native)
    coordinator.update_native("web", status())
    comfy = coordinator.acquire_comfy("workflow", 0)
    assert comfy not in str(coordinator.snapshot())
    assert coordinator.release_comfy(comfy, True)
    assert not coordinator.release_comfy(comfy, True)
    assert not coordinator.check_comfy(comfy)


def test_lower_idle_threshold_and_ambiguous_fixed_registration_are_rejected():
    with pytest.raises(ProtocolError):
        GPUCoordinator({"gpu": ["web"]}, idle_seconds=299)
    with pytest.raises(ProtocolError):
        GPUCoordinator({"first": ["web"], "second": ["web"]})
    with pytest.raises(ProtocolError):
        GPUCoordinator({"gpu": []})


def test_journal_dirty_restart_blocks_both_sides_and_unknown_old_token_cannot_clean(tmp_path):
    path = tmp_path / "gpu-journal.json"
    first = GPUCoordinator({"gpu": ["web"]}, journal_path=path)
    first.update_native("web", status())
    old = first.acquire_comfy("workflow", 0)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["groups"]["gpu"] == {
        "dirty": True,
        "lease_hash": hashlib.sha256(old.encode("ascii")).hexdigest(),
        "lease_epoch": first.epoch,
    }
    assert old not in path.read_text(encoding="utf-8")
    first.close()  # Simulates controller exit; this never acknowledges GPU cleanup.
    second = GPUCoordinator({"gpu": ["web"]}, journal_path=path)
    try:
        second.update_native("web", status())
        observed = second.snapshot()["groups"]["gpu"]
        assert observed["recovery_required"] and observed["cleanup_failed"]
        assert observed["state"] == "recovery_required"
        assert observed["recovery_reason"] == "journal_dirty_restart"
        assert second.epoch != first.epoch
        with pytest.raises(LeaseDenied, match="maintenance"):
            second.acquire_native("web", 0)
        with pytest.raises(LeaseDenied, match="maintenance"):
            second.acquire_comfy("new-workflow", 0)
        assert not second.check_comfy(old)
        assert not second.release_comfy(old, True)
        assert second.snapshot()["groups"]["gpu"]["recovery_required"]
        assert json.loads(path.read_text(encoding="utf-8"))["groups"]["gpu"]["dirty"]
    finally:
        second.close()


def test_clean_acknowledgement_is_durable_before_native_admission_and_restart(tmp_path):
    path = tmp_path / "gpu-journal.json"
    first = GPUCoordinator({"gpu": ["web"]}, journal_path=path)
    first.update_native("web", status())
    token = first.acquire_comfy("workflow", 0)
    assert first.release_comfy(token, True)
    assert json.loads(path.read_text(encoding="utf-8"))["groups"]["gpu"] == {
        "dirty": False, "lease_hash": None, "lease_epoch": None,
    }
    first.close()
    second = GPUCoordinator({"gpu": ["web"]}, journal_path=path)
    try:
        assert not second.snapshot()["groups"]["gpu"]["recovery_required"]
        native = second.acquire_native("web", 0)
        second.release_native(native)
    finally:
        second.close()


def test_expiry_or_failed_cleanup_never_clears_persistent_fence(tmp_path):
    clock = Clock()
    path = tmp_path / "gpu-journal.json"
    first = GPUCoordinator({"gpu": ["web"]}, journal_path=path, monotonic=clock)
    first.update_native("web", status())
    token = first.acquire_comfy("workflow", 0)
    clock.now = 20
    assert not first.check_comfy(token)
    assert first.snapshot()["groups"]["gpu"]["comfy"]["reason"] == "comfy_heartbeat_expired"
    first.release_comfy(token, False)
    first.close()
    second = GPUCoordinator({"gpu": ["web"]}, journal_path=path)
    try:
        assert second.snapshot()["groups"]["gpu"]["recovery_required"]
    finally:
        second.close()


@pytest.mark.parametrize("content", ["{", "{}", '{"version":1,"version":1}', '{"groups":NaN}'])
def test_invalid_journal_blocks_every_registered_group_without_overwriting_it(tmp_path, content):
    path = tmp_path / "gpu-journal.json"
    path.write_text(content, encoding="utf-8")
    coordinator = GPUCoordinator({"one": ["web"], "two": ["api"]}, journal_path=path)
    try:
        for group, participant in (("one", "web"), ("two", "api")):
            assert coordinator.snapshot()["groups"][group]["recovery_required"]
            with pytest.raises(LeaseDenied):
                coordinator.acquire_native(participant, 0)
            with pytest.raises(LeaseDenied):
                coordinator.acquire_comfy("workflow", 0, resource_group=group)
        assert path.read_text(encoding="utf-8") == content
    finally:
        coordinator.close()


def test_journal_write_failure_cannot_grant_token_or_native_gpu(tmp_path, monkeypatch):
    path = tmp_path / "gpu-journal.json"
    coordinator = GPUCoordinator({"gpu": ["web"]}, journal_path=path)
    coordinator.update_native("web", status())

    def failure(*args, **kwargs):
        raise JournalError("private path and credential")

    monkeypatch.setattr(coordinator._journal, "commit", failure)
    try:
        with pytest.raises(CoordinationUnavailable, match="maintenance"):
            coordinator.acquire_comfy("workflow", 0)
        observed = coordinator.snapshot()["groups"]["gpu"]
        assert observed["comfy"] is None and observed["recovery_required"]
        assert observed["recovery_reason"] == "journal_write_failed"
        assert "private" not in str(observed)
        with pytest.raises(LeaseDenied):
            coordinator.acquire_native("web", 0)
    finally:
        coordinator.close()


def test_clean_commit_failure_retains_dirty_fence_across_restart(tmp_path, monkeypatch):
    path = tmp_path / "gpu-journal.json"
    coordinator = GPUCoordinator({"gpu": ["web"]}, journal_path=path)
    coordinator.update_native("web", status())
    token = coordinator.acquire_comfy("workflow", 0)

    def failure(*args, **kwargs):
        raise JournalError("fsync failed")

    monkeypatch.setattr(coordinator._journal, "commit", failure)
    with pytest.raises(CoordinationUnavailable):
        coordinator.release_comfy(token, True)
    with pytest.raises(LeaseDenied):
        coordinator.acquire_native("web", 0)
    coordinator.close()
    second = GPUCoordinator({"gpu": ["web"]}, journal_path=path)
    try:
        assert second.snapshot()["groups"]["gpu"]["recovery_reason"] == "journal_dirty_restart"
    finally:
        second.close()


def test_existing_sidecar_and_missing_journal_is_not_treated_as_first_deployment(tmp_path):
    path = tmp_path / "gpu-journal.json"
    first = GPUCoordinator({"gpu": ["web"]}, journal_path=path)
    first.close()
    path.unlink()
    second = GPUCoordinator({"gpu": ["web"]}, journal_path=path)
    try:
        assert second.snapshot()["groups"]["gpu"]["recovery_required"]
        assert not path.exists()
    finally:
        second.close()


def test_journal_lock_rejects_two_live_controllers_without_touching_owner(tmp_path):
    path = tmp_path / "gpu-journal.json"
    first = GPUCoordinator({"gpu": ["web"]}, journal_path=path)
    second = GPUCoordinator({"gpu": ["web"]}, journal_path=path)
    try:
        assert second.snapshot()["groups"]["gpu"]["recovery_required"]
        assert not first.snapshot()["groups"]["gpu"]["recovery_required"]
        native = first.acquire_native("web", 0)
        first.release_native(native)
    finally:
        second.close()
        first.close()


def test_fixed_participant_registration_changes_require_maintenance(tmp_path):
    path = tmp_path / "gpu-journal.json"
    first = GPUCoordinator({"gpu": ["web"]}, journal_path=path)
    first.close()
    second = GPUCoordinator({"gpu": ["replacement"]}, journal_path=path)
    try:
        assert second.snapshot()["groups"]["gpu"]["recovery_required"]
    finally:
        second.close()


def test_journal_commit_fsyncs_before_atomic_publication(tmp_path, monkeypatch):
    import app.native_gpu.journal as module

    journal = GPUJournal(tmp_path / "journal.json", {"gpu": ["web"]}, "00000000-0000-0000-0000-000000000001")
    events = []
    actual_fsync, actual_replace = module.os.fsync, module.os.replace

    def fsync(descriptor):
        events.append("fsync")
        return actual_fsync(descriptor)

    def replace(source, destination):
        events.append("replace")
        return actual_replace(source, destination)

    monkeypatch.setattr(module.os, "fsync", fsync)
    monkeypatch.setattr(module.os, "replace", replace)
    try:
        journal.commit("gpu", lease_hash="a" * 64)
        assert events[0:3] == ["fsync", "replace", "fsync"]
    finally:
        journal.close()


def test_recovery_acknowledgement_is_not_an_http_action(tmp_path):
    coordinator = GPUCoordinator({"gpu": ["web"]}, journal_path=tmp_path / "journal.json")
    try:
        for action in ("recover", "reset_journal", "ack_clean", "maintenance_recovery"):
            with pytest.raises(ProtocolError):
                coordinator.dispatch(action, {})
    finally:
        coordinator.close()
