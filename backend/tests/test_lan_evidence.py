from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.lan_evidence import (
    LanNodePreflight,
    LanOrchestrationPreflight,
    write_lan_preflight,
)


def _preflight() -> LanOrchestrationPreflight:
    return LanOrchestrationPreflight(
        schema_version=2,
        mode="lan-shared",
        topology_sha256="a" * 64,
        fixture_sha256="b" * 64,
        controller_commit="c" * 40,
        controller_id_sha256="d" * 64,
        nodes={
            "shared-worker": LanNodePreflight(
                commit="c" * 40,
                host_key_sha256="e" * 64,
                machine_id_sha256="f" * 64,
            )
        },
        token_sha256="0" * 64,
        created_at=datetime.now(timezone.utc),
    )


def test_lan_preflight_writer_uses_schema_two_and_atomic_replacement(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "preflight.json"

    write_lan_preflight(path, _preflight())

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["mode"] == "lan-shared"
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", 1), ("mode", "distributed"), ("topology_sha256", "short")],
)
def test_lan_preflight_schema_rejects_downgrade_and_invalid_bindings(
    field: str, value: object
) -> None:
    payload = _preflight().model_dump()
    payload[field] = value

    with pytest.raises(ValidationError):
        LanOrchestrationPreflight.model_validate(payload)
