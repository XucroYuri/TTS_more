#!/usr/bin/env python3
"""Build a fail-closed, shareable CUDA validation evidence bundle.

The raw validation directory is controlled evidence. This script never copies raw
files; it constructs a small allowlisted bundle from safe fields only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


ALLOWED_MODES = {"single-clean", "single-release", "distributed"}
ALLOWED_OUTCOMES = {"success", "failure", "skipped", "cancelled"}
BUNDLE_FILES = {
    "summary.json",
    "junit.xml",
    "nvidia-smi.csv",
    "worker-log-references.json",
    "human-review-state.json",
    "automatic-gate.json",
    "manifest.json",
}
MANIFEST_CONTENT_FILES = BUNDLE_FILES - {"manifest.json"}
ALLOWED_CERTIFICATION_STATUSES = {
    "blocked",
    "core_failed",
    "diagnostic_core_passed",
    "core_passed_ui_pending",
    "automatic_passed_human_pending",
}
FORMAL_SERVICE_IDS = {
    "local-gpt-sovits-main",
    "local-indextts",
    "local-cosyvoice",
}
FORMAL_CASE_IDS = {
    "gpt-v2ProPlus",
    "gpt-v2Pro",
    "gpt-v2ProPlus-artifact",
    "index-emotion-text",
    "cosyvoice-zero-shot",
    "cosyvoice-cross-lingual",
}
SAFE_STAGES = {
    "host-preflight",
    "input-preflight",
    "argument-validation",
    "deployment",
    "orchestration-preflight",
    "worker-wait",
    "cuda-validation",
    "fault-recovery",
    "evidence-collection",
    "workflow-finalizer",
}
SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\)"),
    re.compile(r"GPU-[0-9a-f-]{8,}", re.IGNORECASE),
    re.compile(r"Bearer\s+", re.IGNORECASE),
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(
        r"(?i)(?:\.wav\b|\.ckpt\b|\.pth\b|controller\.log|worker-logs|trace\.zip|\.webm\b|\.png\b)"
    ),
)


class EvidenceSanitizationError(RuntimeError):
    """Raised when a shareable bundle cannot be proven safe."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _safe_identifier(
    value: Any, *, prefix: str, allowlist: set[str] | None = None
) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9._-]{1,96}", text) and (
        allowlist is None or text in allowlist
    ):
        return text
    digest = _sha256_bytes(text.encode("utf-8"))[:12]
    return f"{prefix}-{digest}"


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _safe_numeric_tree(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    number = _safe_number(value)
    if number is not None:
        return number
    if isinstance(value, list):
        return [item for item in (_safe_numeric_tree(entry) for entry in value) if item is not None]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,96}", key):
                continue
            safe_value = _safe_numeric_tree(raw_value)
            if safe_value is not None:
                result[key] = safe_value
        return result
    return None


def _safe_performance(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed: dict[str, set[str]] = {
        "checks": {
            "no_oom",
            "free_memory_reserve",
            "unload_memory_return",
            "cold_load",
            "short_synthesis",
            "warm_p95_regression",
        },
        "metrics": {
            "oom",
            "minimum_free_mib",
            "baseline_memory_mib",
            "unload_memory_mib",
            "unload_recovery_seconds",
            "cold_load_seconds",
            "short_synthesis_seconds",
            "warm_synthesis_seconds",
            "warm_p95_seconds",
            "maximum_reserved_mib",
            "maximum_allocated_mib",
        },
        "baseline": {"warm_p95_seconds"},
        "thresholds": {
            "minimum_free_mib",
            "unload_memory_delta_mib",
            "unload_seconds",
            "cold_load_seconds",
            "short_synthesis_seconds",
            "warm_p95_regression",
        },
    }
    result: dict[str, Any] = {"passed": bool(value.get("passed"))}
    for section, keys in allowed.items():
        raw_section = value.get(section)
        if not isinstance(raw_section, dict):
            result[section] = {}
            continue
        safe_section: dict[str, Any] = {}
        for key in keys:
            safe_value = _safe_numeric_tree(raw_section.get(key))
            if safe_value is not None:
                safe_section[key] = safe_value
        result[section] = safe_section
    return result


def _load_source_summary(raw_dir: Path) -> tuple[dict[str, Any], bool, bool]:
    path = raw_dir / "summary.json"
    if not path.is_file():
        return {}, False, False
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, True, False
    return (payload, True, True) if isinstance(payload, dict) else ({}, True, False)


def _xml_evidence_state(path: Path) -> tuple[bool, bool]:
    if not path.is_file():
        return False, False
    try:
        ElementTree.parse(path)
    except (OSError, ElementTree.ParseError):
        return True, False
    return True, True


def _safe_summary(
    raw: dict[str, Any],
    *,
    mode: str,
    source_present: bool,
    source_valid: bool,
    source_junit_present: bool,
    source_junit_valid: bool,
    source_playwright_junit_present: bool,
    source_playwright_junit_valid: bool,
    shareable_evidence_complete: bool,
) -> dict[str, Any]:
    stage = _safe_identifier(
        raw.get("stage") or "workflow-finalizer",
        prefix="stage",
        allowlist=SAFE_STAGES,
    )
    source_status = str(raw.get("certification_status") or "blocked")
    status = source_status if source_status in ALLOWED_CERTIFICATION_STATUSES else "blocked"
    fixture_sha256 = raw.get("fixture_sha256")
    if not isinstance(fixture_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", fixture_sha256):
        fixture_sha256 = None

    service_results = []
    for item in raw.get("services") or []:
        if not isinstance(item, dict):
            continue
        errors = item.get("errors") if isinstance(item.get("errors"), list) else []
        service_results.append(
            {
                "service_id": _safe_identifier(
                    item.get("service_id"), prefix="service", allowlist=FORMAL_SERVICE_IDS
                ),
                "passed": bool(item.get("passed")),
                "error_count": len(errors),
            }
        )

    case_results = []
    for item in raw.get("cases") or []:
        if not isinstance(item, dict):
            continue
        errors = item.get("errors") if isinstance(item.get("errors"), list) else []
        safe_case: dict[str, Any] = {
            "name": _safe_identifier(
                item.get("name"), prefix="case", allowlist=FORMAL_CASE_IDS
            ),
            "service_id": _safe_identifier(
                item.get("service_id"), prefix="service", allowlist=FORMAL_SERVICE_IDS
            ),
            "passed": bool(item.get("passed")),
            "error_count": len(errors),
        }
        for field in ("synthesis_seconds", "load_seconds", "unload_recovery_seconds"):
            number = _safe_number(item.get(field))
            if number is not None:
                safe_case[field] = number
        warm = _safe_numeric_tree(item.get("warm_synthesis_seconds"))
        if isinstance(warm, list):
            safe_case["warm_synthesis_seconds"] = warm
        asr = item.get("asr")
        if isinstance(asr, dict):
            cer = _safe_number(asr.get("cer"))
            if cer is not None:
                safe_case["cer"] = cer
        case_results.append(safe_case)

    cer_raw = raw.get("cer") if isinstance(raw.get("cer"), dict) else {}
    performance_raw = raw.get("performance") if isinstance(raw.get("performance"), dict) else {}
    monitor_raw = raw.get("gpu_monitor") if isinstance(raw.get("gpu_monitor"), dict) else {}
    summary = {
        "schema_version": 1,
        "name": "cuda-e2e-validation-sanitized",
        "stage": stage,
        "mode": mode,
        "passed": bool(raw.get("passed")) if source_valid else False,
        "certifiable": False,
        "certification_status": status if source_valid else "blocked",
        "fixture_sha256": fixture_sha256,
        "source_summary_present": source_present,
        "source_summary_valid": source_valid,
        "source_junit_present": source_junit_present,
        "source_junit_valid": source_junit_valid,
        "source_playwright_junit_present": source_playwright_junit_present,
        "source_playwright_junit_valid": source_playwright_junit_valid,
        "shareable_evidence_complete": shareable_evidence_complete,
        "blocker_count": int(raw.get("blocker_count") or 0) if source_valid else 1,
        "service_results": service_results,
        "case_results": case_results,
        "cer": {
            "required": bool(cer_raw.get("required")),
            "aggregate_cer": _safe_number(cer_raw.get("aggregate_cer")),
            "passed": bool(cer_raw.get("passed")),
        },
        "performance": _safe_performance(performance_raw),
        "gpu_monitor": {
            "healthy": bool(monitor_raw.get("healthy")),
            "sample_count": int(monitor_raw.get("sample_count") or 0),
        },
    }
    return summary


def _automatic_gate(
    *,
    source_present: bool,
    source_valid: bool,
    source_status: str,
    core_outcome: str,
    playwright_outcome: str,
    shareable_evidence_complete: bool,
) -> dict[str, Any]:
    if not source_present or not source_valid or source_status == "blocked":
        automatic_result = "阻塞"
        overall_result = "阻塞"
        status = "blocked"
    elif core_outcome != "success":
        automatic_result = "失败" if core_outcome == "failure" else "阻塞"
        overall_result = automatic_result
        status = "core_failed" if core_outcome == "failure" else "blocked"
    elif playwright_outcome == "failure":
        automatic_result = "失败"
        overall_result = "失败"
        status = "core_failed"
    elif playwright_outcome != "success" or not shareable_evidence_complete:
        automatic_result = "阻塞"
        overall_result = "阻塞"
        status = "core_passed_ui_pending"
    else:
        automatic_result = "通过"
        overall_result = "自动门禁通过，人工待完成"
        status = "automatic_passed_human_pending"
    return {
        "schema_version": 1,
        "automatic_result": automatic_result,
        "overall_result": overall_result,
        "certification_status": status,
        "core_outcome": core_outcome,
        "playwright_outcome": playwright_outcome,
    }


def _write_junit(path: Path, summary: dict[str, Any], gate: dict[str, Any]) -> None:
    failed = gate["automatic_result"] != "通过"
    suite = ElementTree.Element(
        "testsuite",
        {
            "name": "cuda-sanitized-evidence",
            "tests": "1",
            "failures": "1" if failed else "0",
        },
    )
    testcase = ElementTree.SubElement(
        suite,
        "testcase",
        {"classname": "cuda.workflow", "name": str(summary["stage"])},
    )
    if failed:
        failure = ElementTree.SubElement(
            testcase,
            "failure",
            {"message": str(gate["automatic_result"])},
        )
        failure.text = f"workflow automatic gate: {gate['automatic_result']}"
    ElementTree.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def _number_text(value: str) -> str:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite numeric field")
    return str(int(number)) if number.is_integer() else format(number, ".12g")


def _write_gpu_csv(raw_dir: Path, output_path: Path) -> int:
    header = [
        "sample",
        "index",
        "memory_total_mib",
        "memory_free_mib",
        "memory_used_mib",
        "utilization_percent",
    ]
    rows: list[list[str]] = []
    source = raw_dir / "nvidia-smi.csv"
    if source.is_file():
        try:
            with source.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                required = {
                    "index",
                    "uuid",
                    "memory.total",
                    "memory.free",
                    "memory.used",
                    "utilization.gpu",
                }
                if not reader.fieldnames or not required.issubset(reader.fieldnames):
                    raise EvidenceSanitizationError("GPU evidence has an unexpected schema")
                for sample, row in enumerate(reader, start=1):
                    rows.append(
                        [
                            str(sample),
                            _number_text(str(row["index"])),
                            _number_text(str(row["memory.total"])),
                            _number_text(str(row["memory.free"])),
                            _number_text(str(row["memory.used"])),
                            _number_text(str(row["utilization.gpu"])),
                        ]
                    )
        except (OSError, UnicodeError, csv.Error, TypeError, ValueError) as exc:
            raise EvidenceSanitizationError("GPU evidence cannot be sanitized") from exc
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    return len(rows)


def _write_worker_references(raw_dir: Path, output_path: Path) -> set[str]:
    source = raw_dir / "worker-log-references.json"
    sanitized = []
    if source.is_file():
        try:
            payload = json.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EvidenceSanitizationError("worker references cannot be sanitized") from exc
        if not isinstance(payload, list):
            raise EvidenceSanitizationError("worker references must be a list")
        for item in payload:
            if not isinstance(item, dict):
                raise EvidenceSanitizationError("worker reference entry is invalid")
            configured_log = str(item.get("configured_log") or "")
            status_path = str(item.get("status_path") or "")
            sanitized.append(
                {
                    "service_id": _safe_identifier(
                        item.get("service_id"),
                        prefix="service",
                        allowlist=FORMAL_SERVICE_IDS,
                    ),
                    "configured_log_sha256": _sha256_bytes(configured_log.encode("utf-8")),
                    "status_path_sha256": _sha256_bytes(status_path.encode("utf-8")),
                }
            )
    _write_json(output_path, sanitized)
    return {str(item["service_id"]) for item in sanitized}


def _write_human_review_state(
    output_path: Path, *, mode: str, case_count: int
) -> None:
    required_reviewers = 2 if mode == "single-clean" else 1
    _write_json(
        output_path,
        {
            "schema_version": 1,
            "mode": mode,
            "state": "pending",
            "required_reviewers": required_reviewers,
            "case_count": case_count,
            "required_review_rows": required_reviewers * case_count,
            "identities_included": False,
        },
    )


def _assert_no_sensitive_content(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise EvidenceSanitizationError(f"shareable file is not UTF-8 text: {path.name}") from exc
    for pattern in SENSITIVE_PATTERNS:
        if pattern.search(text):
            raise EvidenceSanitizationError(
                f"sensitive content detected in shareable file: {path.name}"
            )


def verify_sanitized_bundle(output_dir: Path) -> dict[str, Any]:
    if not output_dir.is_dir():
        raise EvidenceSanitizationError("sanitized bundle directory is missing")
    actual = {path.name for path in output_dir.iterdir() if path.is_file()}
    if actual != BUNDLE_FILES or any(path.is_dir() for path in output_dir.iterdir()):
        raise EvidenceSanitizationError("sanitized bundle does not match the allowlist")
    try:
        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceSanitizationError("sanitized manifest is invalid") from exc
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or set(files) != MANIFEST_CONTENT_FILES:
        raise EvidenceSanitizationError("sanitized manifest does not match the allowlist")
    for name, metadata in files.items():
        path = output_dir / name
        if not isinstance(metadata, dict) or metadata.get("sha256") != _sha256_file(path):
            raise EvidenceSanitizationError(f"sanitized file hash mismatch: {name}")
        if metadata.get("size") != path.stat().st_size:
            raise EvidenceSanitizationError(f"sanitized file size mismatch: {name}")
        _assert_no_sensitive_content(path)
    _assert_no_sensitive_content(output_dir / "manifest.json")
    return manifest


def sanitize_evidence(
    raw_dir: Path,
    output_dir: Path,
    *,
    mode: str,
    core_outcome: str,
    playwright_outcome: str,
) -> dict[str, Any]:
    raw_dir = Path(raw_dir)
    output_dir = Path(output_dir)
    if mode not in ALLOWED_MODES:
        raise EvidenceSanitizationError("unsupported CUDA validation mode")
    if core_outcome not in ALLOWED_OUTCOMES or playwright_outcome not in ALLOWED_OUTCOMES:
        raise EvidenceSanitizationError("unsupported workflow outcome")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise EvidenceSanitizationError("sanitized output directory must be empty")
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_summary, source_present, source_valid = _load_source_summary(raw_dir)
    source_junit_present, source_junit_valid = _xml_evidence_state(raw_dir / "junit.xml")
    source_playwright_junit_present, source_playwright_junit_valid = _xml_evidence_state(
        raw_dir / "playwright-junit.xml"
    )
    gpu_sample_count = _write_gpu_csv(raw_dir, output_dir / "nvidia-smi.csv")
    worker_reference_services = _write_worker_references(
        raw_dir, output_dir / "worker-log-references.json"
    )
    shareable_evidence_complete = bool(
        source_junit_valid
        and source_playwright_junit_valid
        and gpu_sample_count > 0
        and worker_reference_services == FORMAL_SERVICE_IDS
    )
    summary = _safe_summary(
        raw_summary,
        mode=mode,
        source_present=source_present,
        source_valid=source_valid,
        source_junit_present=source_junit_present,
        source_junit_valid=source_junit_valid,
        source_playwright_junit_present=source_playwright_junit_present,
        source_playwright_junit_valid=source_playwright_junit_valid,
        shareable_evidence_complete=shareable_evidence_complete,
    )
    source_status = str(raw_summary.get("certification_status") or "blocked")
    gate = _automatic_gate(
        source_present=source_present,
        source_valid=source_valid,
        source_status=source_status,
        core_outcome=core_outcome,
        playwright_outcome=playwright_outcome,
        shareable_evidence_complete=shareable_evidence_complete,
    )

    _write_json(output_dir / "summary.json", summary)
    _write_junit(output_dir / "junit.xml", summary, gate)
    _write_human_review_state(
        output_dir / "human-review-state.json",
        mode=mode,
        case_count=len(summary["case_results"]),
    )
    _write_json(output_dir / "automatic-gate.json", gate)

    manifest = {
        "schema_version": 1,
        "bundle": "cuda-evidence-sanitized",
        "files": {
            name: {
                "sha256": _sha256_file(output_dir / name),
                "size": (output_dir / name).stat().st_size,
            }
            for name in sorted(MANIFEST_CONTENT_FILES)
        },
    }
    _write_json(output_dir / "manifest.json", manifest)
    verify_sanitized_bundle(output_dir)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=sorted(ALLOWED_MODES))
    parser.add_argument("--core-outcome", choices=sorted(ALLOWED_OUTCOMES))
    parser.add_argument("--playwright-outcome", choices=sorted(ALLOWED_OUTCOMES))
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.verify_only:
            verify_sanitized_bundle(args.output)
        else:
            if args.raw is None or args.mode is None or args.core_outcome is None or args.playwright_outcome is None:
                parser.error("--raw, --mode, --core-outcome and --playwright-outcome are required")
            sanitize_evidence(
                args.raw,
                args.output,
                mode=args.mode,
                core_outcome=args.core_outcome,
                playwright_outcome=args.playwright_outcome,
            )
    except EvidenceSanitizationError as exc:
        parser.exit(2, f"evidence sanitization blocked: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
