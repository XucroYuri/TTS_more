from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LanNodePreflight(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    host_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    machine_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LanOrchestrationPreflight(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[2]
    mode: Literal["lan-shared", "lan-distributed"]
    topology_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    controller_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    controller_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    nodes: dict[str, LanNodePreflight]
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @field_validator("nodes")
    @classmethod
    def validate_node_names(
        cls, nodes: dict[str, LanNodePreflight]
    ) -> dict[str, LanNodePreflight]:
        if not nodes or any(not name.strip() for name in nodes):
            raise ValueError("nodes must contain nonempty node names")
        return nodes


def write_lan_preflight(path: Path, payload: LanOrchestrationPreflight) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        temporary_path.write_text(
            payload.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
