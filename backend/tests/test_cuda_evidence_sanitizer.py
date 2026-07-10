from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import pytest


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
        writer.writerow(
            [
                "captured_at",
                "timestamp",
                "index",
                "uuid",
                "memory.total",
                "memory.free",
                "memory.used",
                "utilization.gpu",
            ]
        )
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
