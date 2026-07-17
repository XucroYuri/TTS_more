from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.lan_evidence import (
    LanEvidenceManifest,
    LanNodeEvidence,
    LanNodePreflight,
    LanOrchestrationPreflight,
    assert_required_evidence,
    write_lan_evidence,
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


def _manifest() -> LanEvidenceManifest:
    return LanEvidenceManifest(
        schema_version=1,
        mode="lan-shared",
        deployment="clean",
        controller_commit="a" * 40,
        topology_sha256="b" * 64,
        fixture_sha256="c" * 64,
        service_owners={
            "local-gpt-sovits-main": "shared-worker",
            "local-indextts": "shared-worker",
            "local-cosyvoice": "shared-worker",
        },
        nodes={
            "shared-worker": LanNodeEvidence(
                commit="a" * 40,
                host_key_sha256="d" * 64,
                machine_id_sha256="e" * 64,
                gpu_uuid_sha256=["f" * 64],
                gpu_log="worker-logs/shared-worker/nvidia-smi.csv",
                service_logs={
                    "local-gpt-sovits-main": "worker-logs/shared-worker/local-gpt-sovits-main.log",
                    "local-indextts": "worker-logs/shared-worker/local-indextts.log",
                    "local-cosyvoice": "worker-logs/shared-worker/local-cosyvoice.log",
                },
            )
        },
        fault_recovery="fault-recovery.json",
        human_review_status="pending",
    )


def test_lan_evidence_manifest_is_strict_private_free_and_atomic(tmp_path: Path) -> None:
    path = tmp_path / "run" / "distributed-evidence.json"

    write_lan_evidence(path, _manifest())

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["human_review_status"] == "pending"
    assert payload["nodes"]["shared-worker"]["gpu_log"].startswith("worker-logs/")
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []
    invalid = _manifest().model_dump()
    invalid["host"] = "private.example.test"
    with pytest.raises(ValidationError):
        LanEvidenceManifest.model_validate(invalid)
    private_owner = _manifest().model_dump()
    private_owner["service_owners"] = {
        service_id: "192.0.2.10" for service_id in private_owner["service_owners"]
    }
    private_owner["nodes"] = {"192.0.2.10": next(iter(private_owner["nodes"].values()))}
    with pytest.raises(ValidationError):
        LanEvidenceManifest.model_validate(private_owner)


def _write_required_bundle(output: Path) -> datetime:
    started_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    output.mkdir()
    for relative in (
        "summary.json",
        "junit.xml",
        "controller.log",
        "orchestration-preflight.json",
        "fault-recovery.json",
        "distributed-evidence.json",
        "human-listening-review.md",
        "playwright-junit.xml",
        "recovery/summary.json",
        "recovery/junit.xml",
    ):
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.name == "summary.json":
            content = '{"passed":true}\n'
        elif path.suffix == ".xml":
            content = '<testsuite tests="1" failures="0" errors="0"/>\n'
        elif path.name == "fault-recovery.json":
            content = json.dumps(
                {
                    "mode": "lan-shared",
                    "schema_version": 1,
                    "fault_node": "shared-worker",
                    "service_id": "local-gpt-sovits-main",
                    "degraded_within_seconds": 1.0,
                    "restart_seconds": 2.0,
                    "other_services_ready": True,
                    "all_services_degraded": True,
                    "all_services_degraded_within_seconds": 1.5,
                    "all_services_restart_seconds": 2.5,
                    "application_survived": True,
                    "retry_passed": True,
                    "retry_seconds": 3.0,
                    "recovery_passed": True,
                    "recovery_seconds": 4.0,
                }
            )
        elif path.name == "distributed-evidence.json":
            content = _manifest().model_dump_json()
        else:
            content = "evidence\n"
        path.write_text(content, encoding="utf-8")
    for directory in (output / "wav", output / "recovery" / "wav"):
        directory.mkdir(parents=True)
        for index in range(5):
            (directory / f"case-{index}.wav").write_bytes(b"RIFFevidence")
    worker = output / "worker-logs" / "shared-worker"
    worker.mkdir(parents=True)
    (worker / "nvidia-smi.csv").write_text("header\nsample\n", encoding="utf-8")
    for service_id in (
        "local-gpt-sovits-main",
        "local-indextts",
        "local-cosyvoice",
    ):
        (worker / f"{service_id}.log").write_text("worker evidence\n", encoding="utf-8")
    return started_at


def test_required_evidence_accepts_complete_automatic_bundle_without_human_approval(
    tmp_path: Path,
) -> None:
    output = tmp_path / "run"
    started_at = _write_required_bundle(output)
    (output / "human-listening-review.md").write_text(
        "Template only; Decision: PASS / FAIL\n", encoding="utf-8"
    )

    assert_required_evidence(
        output,
        {
            "local-gpt-sovits-main": "shared-worker",
            "local-indextts": "shared-worker",
            "local-cosyvoice": "shared-worker",
        },
        started_at=started_at,
    )


@pytest.mark.parametrize("failure", ["empty", "stale", "symlink"])
def test_required_evidence_fails_closed_for_invalid_worker_evidence(
    tmp_path: Path, failure: str
) -> None:
    output = tmp_path / "run"
    started_at = _write_required_bundle(output)
    gpu_log = output / "worker-logs" / "shared-worker" / "nvidia-smi.csv"
    if failure == "empty":
        gpu_log.write_bytes(b"")
    elif failure == "stale":
        old = (started_at - timedelta(seconds=60)).timestamp()
        os.utime(gpu_log, (old, old))
    else:
        target = tmp_path / "outside.csv"
        target.write_text("header\nsample\n", encoding="utf-8")
        gpu_log.unlink()
        gpu_log.symlink_to(target)

    with pytest.raises(RuntimeError, match="evidence is incomplete"):
        assert_required_evidence(
            output,
            {
                "local-gpt-sovits-main": "shared-worker",
                "local-indextts": "shared-worker",
                "local-cosyvoice": "shared-worker",
            },
            started_at=started_at,
        )
