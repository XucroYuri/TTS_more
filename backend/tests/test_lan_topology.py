from pathlib import Path

import pytest

from app.lan_topology import LanMode, load_lan_policy


FORMAL = ["local-gpt-sovits-main", "local-indextts", "local-cosyvoice"]


def write_topology(tmp_path: Path, workers: dict) -> Path:
    import json

    path = tmp_path / "topology.json"
    path.write_text(
        json.dumps(
            {
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
        ),
        encoding="utf-8",
    )
    return path


def test_shared_policy_requires_one_capacity_one_worker(tmp_path: Path) -> None:
    path = write_topology(
        tmp_path,
        {
            "shared-worker": {
                "role": "worker",
                "host": "tts-shared.lan",
                "bind_host": "0.0.0.0",
                "services": FORMAL,
                "resource_group": "shared-worker:cuda-0",
                "capacity": 1,
            }
        },
    )

    _, policy = load_lan_policy(path, LanMode.SHARED)

    assert policy.workers == ("shared-worker",)
    assert set(policy.service_owners) == set(FORMAL)
    assert policy.expected_gpu_count == 1
    assert policy.require_overlap is False


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
