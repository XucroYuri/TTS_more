"""Operator-local configuration and lifecycle for the optional coordinator."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

from .coordinator import GPUCoordinator
from .transport import ControlClient, ControlServer, _endpoint


def validate_coordinator_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("coordinator configuration must be an object")
    control = config.get("control")
    if not isinstance(control, dict):
        raise ValueError("coordinator loopback control is required")
    # Constructs only a client and validates the endpoint; no network calls occur.
    ControlClient(control["url"], control["token"])
    participants = config.get("participants")
    groups = config.get("groups")
    if not isinstance(participants, dict) or not isinstance(groups, dict):
        raise ValueError("fixed participants and resource groups are required")
    coordinator = GPUCoordinator(groups)
    try:
        configured = set()
        for group, members in groups.items():
            if not isinstance(members, list):
                raise ValueError("group participants must be a JSON array")
            configured.update(members)
        if set(participants) != configured:
            raise ValueError("every registered participant needs exactly one private control endpoint")
        for endpoint in participants.values():
            if not isinstance(endpoint, dict):
                raise ValueError("invalid native participant endpoint")
            ControlClient(endpoint["url"], endpoint["token"])
        state_path = config.get("journal_path")
        if not isinstance(state_path, str) or not state_path:
            raise ValueError("a private persistent coordination journal is required")
        journal = Path(state_path)
        config["journal_path"] = str((path.resolve().parent / journal).resolve() if not journal.is_absolute() else journal.resolve())
        return config
    finally:
        coordinator.close()


def serve_coordinator(config: dict[str, Any], *, stop: threading.Event | None = None) -> None:
    coordinator = GPUCoordinator(config["groups"], journal_path=Path(config["journal_path"]))
    server = None
    try:
        for participant, endpoint in config["participants"].items():
            client = ControlClient(endpoint["url"], endpoint["token"])
            def offload(_participant, instance_id, revision, client=client):
                return client.call("offload_if_idle", {"instance_id": instance_id, "revision": revision})
            coordinator.set_offload_callback(participant, offload)
        control = config["control"]
        _endpoint(control["url"])
        endpoint = urlsplit(control["url"])
        server = ControlServer(coordinator.dispatch, control["token"], host=endpoint.hostname, port=endpoint.port or 80,
                               allowed_actions={"acquire_native", "release_native", "acquire_comfy", "check_comfy",
                                                "release_comfy", "update_native", "snapshot"})
        stop = stop or threading.Event()
        server.start()
        while not stop.wait(1):
            pass
    finally:
        try:
            if server is not None:
                server.close()
        finally:
            coordinator.close()
