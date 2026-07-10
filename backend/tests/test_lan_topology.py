from pathlib import Path

import pytest
from pydantic import ValidationError

from app.lan_topology import LanMode, LanTopology, load_lan_policy


FORMAL = ["local-gpt-sovits-main", "local-indextts", "local-cosyvoice"]


def topology_payload(workers: dict) -> dict:
    return {
        "schema_version": 1,
        "name": "test-lan",
        "app_node": "app-controller",
        "nodes": {
            "app-controller": {
                "role": "app",
                "host": "mac-controller.lan",
                "bind_host": "127.0.0.1",
                "services": [],
                "resource_group": "app",
                "capacity": 1,
            },
            **workers,
        },
    }


def write_payload(tmp_path: Path, payload: dict) -> Path:
    import json

    path = tmp_path / "topology.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_topology(tmp_path: Path, workers: dict) -> Path:
    return write_payload(tmp_path, topology_payload(workers))


def shared_workers() -> dict:
    return {
        "shared-worker": {
            "role": "worker",
            "host": "tts-shared.lan",
            "bind_host": "0.0.0.0",
            "services": FORMAL,
            "resource_group": "shared-worker:cuda-0",
            "capacity": 1,
        }
    }


def distributed_workers() -> dict:
    return {
        f"worker-{index}": {
            "role": "worker",
            "host": f"tts-{index}.lan",
            "bind_host": "0.0.0.0",
            "services": [service_id],
            "resource_group": f"worker-{index}:cuda-0",
            "capacity": 1,
        }
        for index, service_id in enumerate(FORMAL)
    }


@pytest.mark.parametrize("value", [True, "1", 1.0])
@pytest.mark.parametrize("field", ["schema_version", "capacity"])
def test_topology_rejects_non_strict_integer_fields(field: str, value: object) -> None:
    payload = topology_payload(shared_workers())
    if field == "schema_version":
        payload[field] = value
    else:
        payload["nodes"]["shared-worker"][field] = value

    with pytest.raises(ValidationError):
        LanTopology.model_validate(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(name=" \t "),
        lambda payload: payload.update(app_node=" \t "),
        lambda payload: payload["nodes"].update(
            {" \t ": payload["nodes"].pop("shared-worker")}
        ),
        lambda payload: payload["nodes"]["shared-worker"].update(host=" \t "),
        lambda payload: payload["nodes"]["shared-worker"].update(bind_host=" \t "),
        lambda payload: payload["nodes"]["shared-worker"].update(resource_group=" \t "),
        lambda payload: payload["nodes"]["shared-worker"].update(services=[" \t "]),
    ],
)
def test_topology_rejects_whitespace_only_required_strings(mutate) -> None:
    payload = topology_payload(shared_workers())
    mutate(payload)

    with pytest.raises(ValidationError):
        LanTopology.model_validate(payload)


def test_shared_policy_requires_one_capacity_one_worker(tmp_path: Path) -> None:
    path = write_topology(tmp_path, shared_workers())

    _, policy = load_lan_policy(path, LanMode.SHARED)

    assert policy.workers == ("shared-worker",)
    assert set(policy.service_owners) == set(FORMAL)
    assert policy.expected_gpu_count == 1
    assert policy.require_overlap is False


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST. ",
        "127.0.0.1",
        "127.1",
        "0.0.0.0",
        "::1",
        "[::1]",
        "::",
        "[::]",
    ],
)
def test_worker_host_rejects_loopback_unspecified_and_noncanonical_ipv4(
    tmp_path: Path, host: str
) -> None:
    workers = shared_workers()
    workers["shared-worker"]["host"] = host
    path = write_topology(tmp_path, workers)

    with pytest.raises(ValueError):
        load_lan_policy(path, LanMode.SHARED)


@pytest.mark.parametrize("duplicate", ["TTS-0.LAN", "tts-0.lan.", " TTS-0.LAN. "])
def test_distributed_host_normalization_prevents_duplicate_bypass(
    tmp_path: Path, duplicate: str
) -> None:
    workers = distributed_workers()
    workers["worker-1"]["host"] = duplicate
    path = write_topology(tmp_path, workers)

    with pytest.raises(ValueError, match="distinct worker hosts"):
        load_lan_policy(path, LanMode.DISTRIBUTED)


def test_every_app_node_must_have_no_services(tmp_path: Path) -> None:
    payload = topology_payload(shared_workers())
    payload["nodes"]["observer-app"] = {
        "role": "app",
        "host": "observer.lan",
        "bind_host": "127.0.0.1",
        "services": [FORMAL[0]],
        "resource_group": "observer",
        "capacity": 1,
    }
    path = write_payload(tmp_path, payload)

    with pytest.raises(ValueError, match="app node cannot own services"):
        load_lan_policy(path, LanMode.SHARED)


def test_invalid_mode_is_rejected_before_policy_derivation(tmp_path: Path) -> None:
    path = write_topology(tmp_path, shared_workers())

    with pytest.raises(ValueError, match="'invalid-mode' is not a valid LanMode"):
        load_lan_policy(path, "invalid-mode")


def test_distributed_policy_derives_three_distinct_workers(tmp_path: Path) -> None:
    path = write_topology(tmp_path, distributed_workers())

    _, policy = load_lan_policy(path, LanMode.DISTRIBUTED)

    assert policy.workers == ("worker-0", "worker-1", "worker-2")
    assert policy.service_owners == {
        service_id: f"worker-{index}" for index, service_id in enumerate(FORMAL)
    }
    assert policy.expected_gpu_count == 3
    assert policy.require_overlap is True


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update(app_node="missing"), "app_node is missing"),
        (
            lambda payload: payload["nodes"]["app-controller"].update(role="worker"),
            "app_node must have role app",
        ),
    ],
)
def test_designated_app_node_constraints_are_preserved(
    tmp_path: Path, mutate, message: str
) -> None:
    payload = topology_payload(shared_workers())
    mutate(payload)
    path = write_payload(tmp_path, payload)

    with pytest.raises(ValueError, match=message):
        load_lan_policy(path, LanMode.SHARED)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda workers: workers["worker-1"].update(
            resource_group=workers["worker-0"]["resource_group"]
        ),
        lambda workers: workers["worker-1"].update(capacity=2),
    ],
)
def test_distributed_policy_rejects_shared_resource_or_non_unit_capacity(
    tmp_path: Path, mutate
) -> None:
    workers = distributed_workers()
    mutate(workers)
    path = write_topology(tmp_path, workers)

    with pytest.raises(ValueError, match="distinct capacity-one resource groups"):
        load_lan_policy(path, LanMode.DISTRIBUTED)


def test_distributed_policy_rejects_duplicate_hosts(tmp_path: Path) -> None:
    workers = {
        f"worker-{index}": {
            "role": "worker",
            "host": "same-host.lan",
            "bind_host": "0.0.0.0",
            "services": [service_id],
            "resource_group": f"worker-{index}:cuda-0",
            "capacity": 1,
        }
        for index, service_id in enumerate(FORMAL)
    }
    path = write_topology(tmp_path, workers)

    with pytest.raises(ValueError, match="distinct worker hosts"):
        load_lan_policy(path, LanMode.DISTRIBUTED)
