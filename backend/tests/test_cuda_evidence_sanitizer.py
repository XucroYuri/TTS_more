from __future__ import annotations

import csv
import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

from app.cuda_validation import NvidiaSmiMonitor


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "sanitize-cuda-evidence.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sanitize_cuda_evidence", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_malicious_raw_run(raw: Path) -> dict[str, str]:
    secrets = {
        "user": "runner-private-user",
        "reviewer": "Private Reviewer Name",
        "uuid": "GPU-86b51e30-3faf-38a7-b083-dc74af4df579",
        "token": "Bearer secret-validation-token",
        "path": r"C:\Users\runner-private-user\weights\voice.ckpt",
    }
    raw.mkdir(parents=True)
    summary = {
        "schema_version": 1,
        "stage": "cuda-validation",
        "mode": "single-release",
        "passed": True,
        "certifiable": False,
        "certification_status": "core_passed_ui_pending",
        "fixture_sha256": "a" * 64,
        "topology": secrets["path"],
        "services": [
            {
                "service_id": "local-gpt-sovits-main",
                "passed": True,
                "errors": [],
                "status": {"device_uuid": secrets["uuid"], "model": secrets["path"]},
            }
        ],
        "cases": [
            {
                "name": "gpt-v2ProPlus",
                "service_id": "local-gpt-sovits-main",
                "passed": True,
                "errors": [],
                "output_path": "wav/private-reference.wav",
                "asr": {"reference": "private text", "hypothesis": "private text", "cer": 0.0},
                "warm_synthesis_seconds": [1.0, 1.1],
            }
        ],
        "cer": {"required": True, "aggregate_cer": 0.0, "passed": True},
        "performance": {
            "passed": True,
            "checks": {"warm_p95_regression": True},
            "metrics": {"warm_p95_seconds": 1.095, "maximum_reserved_mib": 512.0},
        },
        "gpu_monitor": {"healthy": True, "sample_count": 1, "error": ""},
    }
    (raw / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (raw / "junit.xml").write_text(
        f'<testsuite><testcase name="case"><failure>{secrets["token"]} {secrets["path"]}</failure></testcase></testsuite>',
        encoding="utf-8",
    )
    (raw / "playwright-junit.xml").write_text(
        '<testsuite tests="1" failures="0"><testcase name="cuda-ui" /></testsuite>',
        encoding="utf-8",
    )
    with (raw / "nvidia-smi.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(NvidiaSmiMonitor.HEADER)
        writer.writerow(
            ["2026-07-10T00:00:00Z", "2026/07/10", "0", secrets["uuid"], "16380", "12000", "4380", "25"]
        )
    (raw / "worker-log-references.json").write_text(
        json.dumps(
            [
                {
                    "service_id": "local-gpt-sovits-main",
                    "configured_log": secrets["path"],
                    "status_path": "/status",
                },
                {
                    "service_id": "local-indextts",
                    "configured_log": secrets["path"] + "-index",
                    "status_path": "/status",
                },
                {
                    "service_id": "local-cosyvoice",
                    "configured_log": secrets["path"] + "-cosy",
                    "status_path": "/status",
                },
            ]
        ),
        encoding="utf-8",
    )
    (raw / "human-listening-review.md").write_text(
        f"### Reviewer `{secrets['reviewer']}`\n\n| private-case | wav/private.wav | reviewer |\n",
        encoding="utf-8",
    )
    (raw / "controller.log").write_text(f"{secrets['user']} {secrets['token']}", encoding="utf-8")
    (raw / "wav").mkdir()
    (raw / "wav" / "private.wav").write_bytes(b"RIFF-private")
    return secrets


def test_sanitizer_builds_a_strict_shareable_allowlist_without_private_values(tmp_path: Path) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    output = tmp_path / "sanitized"
    secrets = _write_malicious_raw_run(raw)

    manifest = module.sanitize_evidence(
        raw,
        output,
        mode="single-release",
        core_outcome="success",
        playwright_outcome="success",
    )

    expected = {
        "summary.json",
        "junit.xml",
        "nvidia-smi.csv",
        "worker-log-references.json",
        "human-review-state.json",
        "automatic-gate.json",
        "manifest.json",
    }
    assert {path.name for path in output.iterdir()} == expected
    assert set(manifest["files"]) == expected - {"manifest.json"}
    combined = "\n".join(path.read_text(encoding="utf-8") for path in output.iterdir())
    for private_value in secrets.values():
        assert private_value not in combined
    for forbidden in ("private-reference.wav", "controller.log", "device_uuid", "output_path", "Bearer "):
        assert forbidden not in combined

    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["core_passed"] is True
    assert summary["passed"] is True
    assert summary["pass_scope"] == "automatic_only"
    assert summary["overall_result"] == "自动门禁通过，人工待完成"
    assert summary["certification_status"] == "automatic_passed_human_pending"
    assert summary["certifiable"] is False
    assert summary["fixture_sha256"] == "a" * 64
    assert summary["service_results"] == [
        {"service_id": "local-gpt-sovits-main", "passed": True, "error_count": 0}
    ]
    assert summary["case_results"][0]["warm_synthesis_seconds"] == [1.0, 1.1]
    assert summary["case_results"][0]["cer"] == 0.0
    gate = json.loads((output / "automatic-gate.json").read_text(encoding="utf-8"))
    assert gate == {
        "schema_version": 1,
        "automatic_result": "通过",
        "overall_result": "自动门禁通过，人工待完成",
        "certification_status": "automatic_passed_human_pending",
        "core_outcome": "success",
        "playwright_outcome": "success",
        "cleanup_outcome": "success",
    }
    module.verify_sanitized_bundle(output)


def test_sanitizer_generates_structured_blocker_when_raw_summary_and_junit_are_missing(tmp_path: Path) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    raw.mkdir()
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode="distributed",
        core_outcome="failure",
        playwright_outcome="skipped",
    )

    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["passed"] is False
    assert summary["certification_status"] == "blocked"
    assert summary["stage"] == "workflow-finalizer"
    assert summary["source_summary_present"] is False
    assert "failure" not in (output / "junit.xml").read_text(encoding="utf-8").casefold() or "workflow" in (
        output / "junit.xml"
    ).read_text(encoding="utf-8").casefold()
    gate = json.loads((output / "automatic-gate.json").read_text(encoding="utf-8"))
    assert gate["automatic_result"] == "阻塞"
    assert gate["overall_result"] == "阻塞"
    module.verify_sanitized_bundle(output)


def test_automatic_success_requires_both_junits_gpu_samples_and_three_worker_references(
    tmp_path: Path,
) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    _write_malicious_raw_run(raw)
    (raw / "playwright-junit.xml").unlink()
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode="single-release",
        core_outcome="success",
        playwright_outcome="success",
    )

    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    gate = json.loads((output / "automatic-gate.json").read_text(encoding="utf-8"))
    assert summary["shareable_evidence_complete"] is False
    assert summary["source_playwright_junit_present"] is False
    assert gate["automatic_result"] == "阻塞"
    assert gate["certification_status"] == "core_passed_ui_pending"


def test_sanitizer_removes_gpu_uuid_and_hashes_worker_references(tmp_path: Path) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    secrets = _write_malicious_raw_run(raw)
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode="single-release",
        core_outcome="failure",
        playwright_outcome="skipped",
    )

    gpu_text = (output / "nvidia-smi.csv").read_text(encoding="utf-8")
    assert "uuid" not in gpu_text.casefold()
    assert secrets["uuid"] not in gpu_text
    rows = list(csv.DictReader(gpu_text.splitlines()))
    assert rows == [
        {
            "source": "controller",
            "sample": "1",
            "index": "0",
            "memory_total_mib": "16380",
            "memory_free_mib": "12000",
            "memory_used_mib": "4380",
            "utilization_percent": "25",
        }
    ]
    references = json.loads((output / "worker-log-references.json").read_text(encoding="utf-8"))
    assert references[0]["service_id"] == "local-gpt-sovits-main"
    assert set(references[0]) == {"service_id", "configured_log_sha256", "status_path_sha256"}
    assert secrets["path"] not in json.dumps(references)


def test_distributed_bundle_requires_and_anonymizes_three_remote_gpu_sources(tmp_path: Path) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    _write_malicious_raw_run(raw)
    worker_root = raw / "worker-logs"
    private_nodes = ["private-gpt-host", "private-index-host", "private-cosy-host"]
    for index, node in enumerate(private_nodes):
        node_dir = worker_root / node
        node_dir.mkdir(parents=True)
        (node_dir / "nvidia-smi.csv").write_text(
            f"2026/07/10 01:00:0{index}, {index}, GPU-private-{index}, 24576, 22000, 2576, 31\n",
            encoding="utf-8",
        )
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode="distributed",
        core_outcome="success",
        playwright_outcome="success",
    )

    rows = list(csv.DictReader((output / "nvidia-smi.csv").read_text(encoding="utf-8").splitlines()))
    assert {row["source"] for row in rows if row["source"] != "controller"} == {
        "worker-1",
        "worker-2",
        "worker-3",
    }
    combined = (output / "nvidia-smi.csv").read_text(encoding="utf-8")
    for node in private_nodes:
        assert node not in combined
    assert "GPU-private" not in combined
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["gpu_monitor"]["remote_source_count"] == 3
    assert summary["shareable_evidence_complete"] is True


def test_distributed_bundle_blocks_when_any_remote_gpu_source_is_missing(tmp_path: Path) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    _write_malicious_raw_run(raw)
    worker_root = raw / "worker-logs"
    for index in range(2):
        node_dir = worker_root / f"node-{index}"
        node_dir.mkdir(parents=True)
        (node_dir / "nvidia-smi.csv").write_text(
            f"2026/07/10 01:00:0{index}, {index}, GPU-private-{index}, 24576, 22000, 2576, 31\n",
            encoding="utf-8",
        )
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode="distributed",
        core_outcome="success",
        playwright_outcome="success",
    )

    gate = json.loads((output / "automatic-gate.json").read_text(encoding="utf-8"))
    assert gate["automatic_result"] == "阻塞"
    assert gate["certification_status"] == "core_passed_ui_pending"


def test_distributed_bundle_blocks_when_controller_gpu_source_is_missing(tmp_path: Path) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    _write_malicious_raw_run(raw)
    (raw / "nvidia-smi.csv").unlink()
    for index in range(3):
        node_dir = raw / "worker-logs" / f"node-{index}"
        node_dir.mkdir(parents=True)
        (node_dir / "nvidia-smi.csv").write_text(
            f"2026/07/10 01:00:0{index}, {index}, GPU-private-{index}, 24576, 22000, 2576, 31\n",
            encoding="utf-8",
        )
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode="distributed",
        core_outcome="success",
        playwright_outcome="success",
    )

    gate = json.loads((output / "automatic-gate.json").read_text(encoding="utf-8"))
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert gate["automatic_result"] == "阻塞"
    assert summary["gpu_monitor"]["controller_sample_count"] == 0


LAN_WORKERS = {
    "lan-shared": ["shared-worker"],
    "lan-distributed": ["worker-0", "worker-1", "worker-2"],
}


def _bind_lan_summary(
    raw: Path,
    *,
    mode: str,
    verified: object = True,
    workers: list[str] | None = None,
) -> None:
    summary_path = raw / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["mode"] = mode
    if verified is not ...:
        summary["orchestration_verified"] = verified
    summary["orchestration_workers"] = workers or LAN_WORKERS[mode]
    summary_path.write_text(json.dumps(summary), encoding="utf-8")


def _write_remote_gpu_sources(
    raw: Path, nodes: int | list[str], *, duplicate_content: bool = False
) -> None:
    names = [f"private-node-{index}" for index in range(nodes)] if isinstance(nodes, int) else nodes
    for index, node in enumerate(names):
        node_dir = raw / "worker-logs" / node
        node_dir.mkdir(parents=True)
        evidence_index = 0 if duplicate_content else index
        (node_dir / "nvidia-smi.csv").write_text(
            f"2026/07/10 01:00:0{evidence_index}, {evidence_index}, "
            f"GPU-private-{evidence_index}, 24576, 22000, 2576, 31\n",
            encoding="utf-8",
        )


@pytest.mark.parametrize(
    ("mode", "remote_count"),
    [("lan-shared", 1), ("lan-distributed", 3)],
)
def test_lan_bundle_accepts_exact_policy_remote_gpu_cardinality(
    tmp_path: Path, mode: str, remote_count: int
) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    _write_malicious_raw_run(raw)
    _bind_lan_summary(raw, mode=mode)
    _write_remote_gpu_sources(raw, LAN_WORKERS[mode])
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode=mode,
        core_outcome="success",
        playwright_outcome="success",
    )

    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in output.iterdir()
    )
    assert summary["shareable_evidence_complete"] is True
    assert summary["source_orchestration_verified"] is True
    assert summary["gpu_monitor"]["remote_node_set_verified"] is True
    assert summary["gpu_monitor"]["remote_source_count"] == remote_count
    rows = list(
        csv.DictReader((output / "nvidia-smi.csv").read_text(encoding="utf-8").splitlines())
    )
    assert {row["source"] for row in rows if row["source"] != "controller"} == {
        f"remote-{index}" for index in range(1, remote_count + 1)
    }
    assert all(node not in combined for node in LAN_WORKERS[mode])
    assert "GPU-private" not in combined


@pytest.mark.parametrize(
    ("mode", "remote_count"),
    [("lan-shared", 0), ("lan-shared", 2), ("lan-distributed", 2), ("lan-distributed", 4)],
)
def test_lan_bundle_fails_closed_on_remote_gpu_cardinality_mismatch(
    tmp_path: Path, mode: str, remote_count: int
) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    _write_malicious_raw_run(raw)
    _bind_lan_summary(raw, mode=mode)
    _write_remote_gpu_sources(raw, remote_count)
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode=mode,
        core_outcome="success",
        playwright_outcome="success",
    )

    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    gate = json.loads((output / "automatic-gate.json").read_text(encoding="utf-8"))
    assert summary["shareable_evidence_complete"] is False
    assert gate["automatic_result"] == "阻塞"


@pytest.mark.parametrize("verified", [..., False, None, "true"])
def test_lan_bundle_requires_source_orchestration_verified_exactly_true(
    tmp_path: Path, verified: object
) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    _write_malicious_raw_run(raw)
    _bind_lan_summary(raw, mode="lan-shared", verified=verified)
    _write_remote_gpu_sources(raw, LAN_WORKERS["lan-shared"])
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode="lan-shared",
        core_outcome="success",
        playwright_outcome="success",
    )

    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    gate = json.loads((output / "automatic-gate.json").read_text(encoding="utf-8"))
    assert summary["shareable_evidence_complete"] is False
    assert summary["source_orchestration_verified"] is False
    assert gate["automatic_result"] == "阻塞"


@pytest.mark.parametrize(
    "actual_nodes",
    [
        ["worker-0", "worker-1"],
        ["worker-0", "worker-1", "worker-2", "worker-extra"],
        ["substitute-0", "substitute-1", "substitute-2"],
    ],
)
def test_lan_distributed_bundle_rejects_nonexact_worker_directory_set(
    tmp_path: Path, actual_nodes: list[str]
) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    _write_malicious_raw_run(raw)
    _bind_lan_summary(raw, mode="lan-distributed")
    _write_remote_gpu_sources(raw, actual_nodes)
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode="lan-distributed",
        core_outcome="success",
        playwright_outcome="success",
    )

    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    gate = json.loads((output / "automatic-gate.json").read_text(encoding="utf-8"))
    assert summary["shareable_evidence_complete"] is False
    assert summary["gpu_monitor"]["remote_node_set_verified"] is False
    assert gate["automatic_result"] == "阻塞"
    assert not any(node in json.dumps(summary) for node in LAN_WORKERS["lan-distributed"])


def test_lan_distributed_bundle_rejects_copied_gpu_evidence_under_expected_nodes(
    tmp_path: Path,
) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    _write_malicious_raw_run(raw)
    _bind_lan_summary(raw, mode="lan-distributed")
    _write_remote_gpu_sources(
        raw, LAN_WORKERS["lan-distributed"], duplicate_content=True
    )
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode="lan-distributed",
        core_outcome="success",
        playwright_outcome="success",
    )

    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    gate = json.loads((output / "automatic-gate.json").read_text(encoding="utf-8"))
    assert summary["shareable_evidence_complete"] is False
    assert summary["gpu_monitor"]["remote_node_set_verified"] is False
    assert gate["automatic_result"] == "阻塞"


def test_lan_bundle_rejects_source_summary_mode_mismatch(tmp_path: Path) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    _write_malicious_raw_run(raw)
    _bind_lan_summary(raw, mode="lan-distributed")
    summary_path = raw / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["mode"] = "lan-shared"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    _write_remote_gpu_sources(raw, LAN_WORKERS["lan-distributed"])
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode="lan-distributed",
        core_outcome="success",
        playwright_outcome="success",
    )

    sanitized = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    gate = json.loads((output / "automatic-gate.json").read_text(encoding="utf-8"))
    assert sanitized["source_orchestration_verified"] is False
    assert sanitized["shareable_evidence_complete"] is False
    assert gate["automatic_result"] == "阻塞"


def test_sanitizer_accepts_the_production_gpu_monitor_schema(tmp_path: Path) -> None:
    module = _load_module()
    assert NvidiaSmiMonitor.HEADER == [
        "captured_at",
        "gpu_timestamp",
        "index",
        "uuid",
        "memory_total_mib",
        "memory_free_mib",
        "memory_used_mib",
        "utilization_gpu_percent",
    ]
    raw = tmp_path / "raw"
    _write_malicious_raw_run(raw)
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode="single-release",
        core_outcome="success",
        playwright_outcome="success",
    )

    rows = list(csv.DictReader((output / "nvidia-smi.csv").read_text(encoding="utf-8").splitlines()))
    assert rows[0]["memory_total_mib"] == "16380"
    assert rows[0]["utilization_percent"] == "25"


def test_sanitizer_rejects_nonempty_output_and_verifier_rejects_tampering(tmp_path: Path) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    raw.mkdir()
    output = tmp_path / "sanitized"
    output.mkdir()
    (output / "unknown.txt").write_text("must not survive", encoding="utf-8")

    with pytest.raises(module.EvidenceSanitizationError, match="must be empty"):
        module.sanitize_evidence(
            raw,
            output,
            mode="single-clean",
            core_outcome="failure",
            playwright_outcome="skipped",
        )

    (output / "unknown.txt").unlink()
    output.rmdir()
    module.sanitize_evidence(
        raw,
        output,
        mode="single-clean",
        core_outcome="failure",
        playwright_outcome="skipped",
    )
    (output / "extra.log").write_text("unexpected", encoding="utf-8")
    with pytest.raises(module.EvidenceSanitizationError, match="allowlist"):
        module.verify_sanitized_bundle(output)
    (output / "extra.log").unlink()
    (output / "summary.json").write_text(r'{"leak":"C:\\private\\voice.pth"}', encoding="utf-8")
    with pytest.raises(module.EvidenceSanitizationError, match="hash mismatch|sensitive"):
        module.verify_sanitized_bundle(output)


def test_sensitive_rejection_never_leaves_a_publishable_partial_directory(tmp_path: Path) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    _write_malicious_raw_run(raw)
    summary_path = raw / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["performance"]["metrics"]["warm_p95_seconds"] = {
        "GPU-86b51e30-3faf-38a7-b083-dc74af4df579": 1.0
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    output = tmp_path / "sanitized"

    with pytest.raises(module.EvidenceSanitizationError, match="sensitive content"):
        module.sanitize_evidence(
            raw,
            output,
            mode="single-release",
            core_outcome="success",
            playwright_outcome="success",
        )

    assert not output.exists()


def test_verifier_rejects_hash_consistent_but_semantically_invalid_gate(tmp_path: Path) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    _write_malicious_raw_run(raw)
    output = tmp_path / "sanitized"
    module.sanitize_evidence(
        raw,
        output,
        mode="single-release",
        core_outcome="success",
        playwright_outcome="success",
    )

    gate_path = output / "automatic-gate.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    gate["overall_result"] = "通过"
    gate_path.write_text(json.dumps(gate, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["automatic-gate.json"] = {
        "sha256": hashlib.sha256(gate_path.read_bytes()).hexdigest(),
        "size": gate_path.stat().st_size,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(module.EvidenceSanitizationError, match="automatic gate semantics"):
        module.verify_sanitized_bundle(output)


@pytest.mark.parametrize(
    ("core", "playwright", "automatic_result", "overall", "status"),
    [
        ("success", "failure", "失败", "失败", "core_failed"),
        ("success", "skipped", "阻塞", "阻塞", "core_passed_ui_pending"),
        ("failure", "skipped", "失败", "失败", "core_failed"),
    ],
)
def test_automatic_gate_never_claims_overall_pass_before_human_review(
    tmp_path: Path,
    core: str,
    playwright: str,
    automatic_result: str,
    overall: str,
    status: str,
) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "summary.json").write_text(
        json.dumps(
            {
                "mode": "single-release",
                "stage": "cuda-validation",
                "passed": core == "success",
                "certification_status": "core_passed_ui_pending" if core == "success" else "core_failed",
                "services": [],
                "cases": [],
                "cer": {},
                "performance": {},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode="single-release",
        core_outcome=core,
        playwright_outcome=playwright,
    )

    gate = json.loads((output / "automatic-gate.json").read_text(encoding="utf-8"))
    assert gate["automatic_result"] == automatic_result
    assert gate["overall_result"] == overall
    assert gate["certification_status"] == status
    assert gate["overall_result"] != "通过"


def test_cleanup_failure_prevents_automatic_pass(tmp_path: Path) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    _write_malicious_raw_run(raw)
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode="single-release",
        core_outcome="success",
        playwright_outcome="success",
        cleanup_outcome="failure",
    )

    gate = json.loads((output / "automatic-gate.json").read_text(encoding="utf-8"))
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert gate["automatic_result"] == "失败"
    assert gate["overall_result"] == "失败"
    assert gate["cleanup_outcome"] == "failure"
    assert summary["core_passed"] is True
    assert summary["passed"] is False
    assert summary["pass_scope"] == "automatic_only"
    assert summary["overall_result"] == "失败"
    assert summary["certification_status"] == "core_failed"


def test_playwright_failure_is_not_demoted_by_skipped_cleanup(tmp_path: Path) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    _write_malicious_raw_run(raw)
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode="single-release",
        core_outcome="success",
        playwright_outcome="failure",
        cleanup_outcome="skipped",
    )

    gate = json.loads((output / "automatic-gate.json").read_text(encoding="utf-8"))
    assert gate["automatic_result"] == "失败"
    assert gate["certification_status"] == "core_failed"


def test_diagnostic_core_cannot_be_promoted_by_successful_workflow_outcomes(tmp_path: Path) -> None:
    module = _load_module()
    raw = tmp_path / "raw"
    _write_malicious_raw_run(raw)
    summary_path = raw / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["certification_status"] = "diagnostic_core_passed"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    output = tmp_path / "sanitized"

    module.sanitize_evidence(
        raw,
        output,
        mode="single-release",
        core_outcome="success",
        playwright_outcome="success",
    )

    gate = json.loads((output / "automatic-gate.json").read_text(encoding="utf-8"))
    sanitized_summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert gate["automatic_result"] == "阻塞"
    assert gate["certification_status"] == "diagnostic_core_passed"
    assert sanitized_summary["passed"] is False
    assert sanitized_summary["certification_status"] == "diagnostic_core_passed"
