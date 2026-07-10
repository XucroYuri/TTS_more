from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


FORMAL_SERVICE_IDS = frozenset(
    {"local-gpt-sovits-main", "local-indextts", "local-cosyvoice"}
)


class LanMode(str, Enum):
    SHARED = "lan-shared"
    DISTRIBUTED = "lan-distributed"


class LanNode(BaseModel):
    model_config = ConfigDict(strict=True)

    role: Literal["app", "worker"]
    host: str = Field(min_length=1)
    bind_host: str = Field(min_length=1)
    services: list[str]
    resource_group: str = Field(min_length=1)
    capacity: int = Field(ge=1)

    @field_validator("host", "bind_host", "resource_group")
    @classmethod
    def validate_nonempty_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must be a nonempty string")
        return value

    @field_validator("services")
    @classmethod
    def validate_services(cls, services: list[str]) -> list[str]:
        if any(not service_id.strip() for service_id in services):
            raise ValueError("services must contain nonempty strings")
        return services


class LanTopology(BaseModel):
    model_config = ConfigDict(strict=True)

    schema_version: int
    name: str = Field(min_length=1)
    app_node: str = Field(min_length=1)
    nodes: dict[str, LanNode]

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("schema_version must be 1")
        return value

    @field_validator("name", "app_node")
    @classmethod
    def validate_nonempty_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must be a nonempty string")
        return value

    @field_validator("nodes")
    @classmethod
    def validate_node_names(cls, nodes: dict[str, LanNode]) -> dict[str, LanNode]:
        if any(not name.strip() for name in nodes):
            raise ValueError("node names must be nonempty strings")
        return nodes


@dataclass(frozen=True)
class LanPolicy:
    mode: LanMode
    app_node: str
    workers: tuple[str, ...]
    service_owners: dict[str, str]
    expected_gpu_count: int
    require_overlap: bool


def _normalize_host(host: str) -> str:
    return host.strip().casefold().rstrip(".")


def _validate_worker_host(name: str, host: str) -> str:
    normalized = _normalize_host(host)
    if not normalized:
        raise ValueError(f"worker {name} must use a nonempty host")
    if normalized in {"localhost", "ip6-localhost"}:
        raise ValueError(f"worker {name} must use a non-loopback, specified host")
    candidate_ip = normalized.strip("[]")
    try:
        address = ipaddress.ip_address(candidate_ip)
    except ValueError:
        if "." in candidate_ip and candidate_ip.replace(".", "").isdigit():
            raise ValueError(f"worker {name} must use a canonical numeric IP address") from None
    else:
        if address.is_loopback or address.is_unspecified:
            raise ValueError(f"worker {name} must use a non-loopback, specified host")
    return normalized


def load_lan_policy(path: Path, mode: LanMode | str) -> tuple[LanTopology, LanPolicy]:
    mode = LanMode(mode)
    topology = LanTopology.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if topology.app_node not in topology.nodes:
        raise ValueError("topology app_node is missing")
    if topology.nodes[topology.app_node].role != "app":
        raise ValueError("topology app_node must have role app")
    if topology.nodes[topology.app_node].services:
        raise ValueError("topology app node cannot own services")
    if any(node.role == "app" and node.services for node in topology.nodes.values()):
        raise ValueError("topology app node cannot own services")

    workers = {name: node for name, node in topology.nodes.items() if node.role == "worker"}
    owners: dict[str, str] = {}
    worker_hosts: dict[str, str] = {}
    for name, node in workers.items():
        worker_hosts[name] = _validate_worker_host(name, node.host)
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
    hosts = set(worker_hosts.values())
    groups = {node.resource_group for node in workers.values()}
    if len(hosts) != 3:
        raise ValueError("lan-distributed requires distinct worker hosts")
    if len(groups) != 3 or any(node.capacity != 1 for node in workers.values()):
        raise ValueError("lan-distributed requires distinct capacity-one resource groups")
    return topology, LanPolicy(mode, topology.app_node, tuple(workers), owners, 3, True)
