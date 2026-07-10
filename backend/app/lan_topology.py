from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


FORMAL_SERVICE_IDS = frozenset(
    {"local-gpt-sovits-main", "local-indextts", "local-cosyvoice"}
)


class LanMode(str, Enum):
    SHARED = "lan-shared"
    DISTRIBUTED = "lan-distributed"


class LanNode(BaseModel):
    role: Literal["app", "worker"]
    host: str = Field(min_length=1)
    bind_host: str = Field(min_length=1)
    services: list[str]
    resource_group: str = Field(min_length=1)
    capacity: int = Field(ge=1)


class LanTopology(BaseModel):
    schema_version: Literal[1]
    name: str = Field(min_length=1)
    app_node: str = Field(min_length=1)
    nodes: dict[str, LanNode]


@dataclass(frozen=True)
class LanPolicy:
    mode: LanMode
    app_node: str
    workers: tuple[str, ...]
    service_owners: dict[str, str]
    expected_gpu_count: int
    require_overlap: bool


def _is_loopback(host: str) -> bool:
    normalized = host.casefold().rstrip(".").strip("[]")
    if normalized in {"localhost", "ip6-localhost"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def load_lan_policy(path: Path, mode: LanMode) -> tuple[LanTopology, LanPolicy]:
    topology = LanTopology.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if topology.app_node not in topology.nodes:
        raise ValueError("topology app_node is missing")
    if topology.nodes[topology.app_node].role != "app":
        raise ValueError("topology app_node must have role app")
    if topology.nodes[topology.app_node].services:
        raise ValueError("topology app node cannot own services")

    workers = {name: node for name, node in topology.nodes.items() if node.role == "worker"}
    owners: dict[str, str] = {}
    for name, node in workers.items():
        if _is_loopback(node.host):
            raise ValueError(f"worker {name} must use a non-loopback host")
        for service_id in node.services:
            if service_id in owners:
                raise ValueError(f"service {service_id} has multiple owners")
            owners[service_id] = name
    if set(owners) != FORMAL_SERVICE_IDS:
        raise ValueError("topology must assign every formal service exactly once")

    if mode is LanMode.SHARED:
        if len(workers) != 1:
            raise ValueError("lan-shared requires exactly one worker")
        worker = next(iter(workers.values()))
        if worker.capacity != 1 or set(worker.services) != FORMAL_SERVICE_IDS:
            raise ValueError("lan-shared worker must own all formal services at capacity 1")
        return topology, LanPolicy(mode, topology.app_node, tuple(workers), owners, 1, False)

    if len(workers) != 3 or any(len(node.services) != 1 for node in workers.values()):
        raise ValueError("lan-distributed requires three one-service workers")
    hosts = {node.host.casefold().rstrip(".") for node in workers.values()}
    groups = {node.resource_group for node in workers.values()}
    if len(hosts) != 3:
        raise ValueError("lan-distributed requires distinct worker hosts")
    if len(groups) != 3 or any(node.capacity != 1 for node in workers.values()):
        raise ValueError("lan-distributed requires distinct capacity-one resource groups")
    return topology, LanPolicy(mode, topology.app_node, tuple(workers), owners, 3, True)
