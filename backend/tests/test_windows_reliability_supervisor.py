from __future__ import annotations

import io
import json
import math
import shutil
import struct
import subprocess
import ctypes
import hashlib
import time
import os
import sys
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import soundfile

from app.comfyui import reliability_evidence
from app.comfyui import reliability_supervision
from app.comfyui import reliability_validation


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
SUPERVISOR_SOURCE = (
    REPOSITORY_ROOT / "scripts" / "run-windows-comfyui-reliability-supervised.ps1"
)
INNER_SOURCE = REPOSITORY_ROOT / "scripts" / "run-windows-comfyui-reliability.ps1"
POWERSHELL = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


def _make_windows_junction(junction: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("real junction behavior is Windows-specific")
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"Windows junction creation is unavailable: {completed.stderr}")


def _powershell_literal(value: Path) -> str:
    return str(value).replace("'", "''")


def _canonical_model_bytes(model: object) -> bytes:
    return (
        json.dumps(
            model.model_dump(mode="json"),  # type: ignore[attr-defined]
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _voiced_wav_bytes() -> bytes:
    buffer = io.BytesIO()
    frames = [int(8_000 * math.sin(index / 8)) for index in range(800)]
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"".join(struct.pack("<h", frame) for frame in frames))
    return buffer.getvalue()


def _voiced_container_bytes(container_format: str) -> bytes:
    buffer = io.BytesIO()
    samples = [0.25 * math.sin(index / 8) for index in range(800)]
    soundfile.write(buffer, samples, 16_000, format=container_format)
    return buffer.getvalue()


def _write_authoritative_public_fixture(directory: Path, *, matrix: bool) -> None:
    directory.mkdir(parents=True)
    timestamp = "2026-08-03T00:00:00Z"
    preflight = reliability_validation._PublicPreflightMarker.model_validate(
        {
            "status": "passed",
            "resources": [
                {
                    "engine": engine,
                    "ready": True,
                    "resource_id_hash": hashlib.sha256(engine.encode("utf-8")).hexdigest(),
                }
                for engine in sorted(reliability_validation.ENGINE_ORDER)
            ],
            "queue": {
                "tts_queued": 0,
                "tts_running": 0,
                "comfy_pending_prompt_ids": [],
                "comfy_running_prompt_ids": [],
            },
            "port_owners": {
                "8000": {
                    "pid": 8000,
                    "creation_time": timestamp,
                    "executable_name": "python.exe",
                    "ownership_hash": "1" * 64,
                },
                "8188": {
                    "pid": 8188,
                    "creation_time": timestamp,
                    "executable_name": "python.exe",
                    "ownership_hash": "2" * 64,
                },
            },
            "gpu_idle_baseline": {"used_mib": 100, "free_mib": 8000},
            "boundary": {
                "aggregate_hash": "b" * 64,
                "private_registry_hash": "c" * 64,
                "reference_hashes": {"reference": "d" * 64},
                "repositories": [
                    {
                        "label": label,
                        "head": "a" * 40,
                        "branch": "feature",
                        "porcelain_hash": "f" * 64,
                    }
                    for label in sorted(reliability_validation.REQUIRED_BOUNDARY_LABELS)
                ],
            },
        }
    )
    (directory / "preflight.json").write_bytes(_canonical_model_bytes(preflight))
    if not matrix:
        return

    cases_directory = directory / "cases"
    audio_directory = directory / "audio"
    cases_directory.mkdir()
    audio_directory.mkdir()
    wav_payload = _voiced_wav_bytes()
    audio_proof = reliability_validation._wav_proof_from_bytes(wav_payload)
    boundary = reliability_validation.BoundaryEvidence(
        before_hash="b" * 64,
        after_hash="b" * 64,
        private_registry_hash="c" * 64,
        reference_hashes={"reference": "d" * 64},
        repositories_before=[
            reliability_validation.RepositorySnapshot(
                label=label,
                head="a" * 40,
                branch="feature",
                porcelain_hash="f" * 64,
            )
            for label in reliability_validation.REQUIRED_BOUNDARY_LABELS
        ],
        repositories_after=[
            reliability_validation.RepositorySnapshot(
                label=label,
                head="a" * 40,
                branch="feature",
                porcelain_hash="f" * 64,
            )
            for label in reliability_validation.REQUIRED_BOUNDARY_LABELS
        ],
        private_registry_before_hash="c" * 64,
        private_registry_after_hash="c" * 64,
        reference_hashes_before={"reference": "d" * 64},
        reference_hashes_after={"reference": "d" * 64},
    )
    plan = reliability_validation.build_case_plan(rounds=10)
    cases: list[reliability_validation.CaseEvidence] = []
    for index, case_plan in enumerate(plan, start=1):
        started_at = datetime(2026, 8, 3, tzinfo=timezone.utc) + timedelta(
            minutes=index
        )
        finished_at = started_at + timedelta(seconds=10)
        prompt_id = f"prompt-{case_plan.case_id}"
        actual = case_plan.expected
        prompt_submitted = case_plan.action != "cancel-queued"
        version_id = None if not prompt_submitted else f"version-{case_plan.case_id}"
        comfyui = None
        tts_more = None
        termination = None
        if case_plan.action == "cancel-queued":
            tts_more = reliability_validation.TtsTerminalEvidence(
                job_status="cancelled",
                item_status="cancelled",
                version_status=None,
                manifest_version_absent=True,
                version_audio_absent=True,
            )
            prompt_id_value = None
        elif case_plan.action == "terminate-comfyui":
            tts_more = reliability_validation.TtsTerminalEvidence(
                job_status="failed",
                item_status="failed",
                version_status="failed",
                manifest_version_absent=False,
                version_audio_absent=True,
            )
            termination = reliability_validation.TerminationEvidence(
                endpoint_unavailable=True,
                prompt_id=prompt_id,
                queue_before_prompt_ids=[prompt_id],
                manifest_audio_absent=True,
            )
            prompt_id_value = prompt_id
        else:
            if case_plan.action in {"cancel-running", "timeout"}:
                terminal_status = (
                    "cancelled" if case_plan.action == "cancel-running" else "failed"
                )
                tts_more = reliability_validation.TtsTerminalEvidence(
                    job_status=terminal_status,
                    item_status=terminal_status,
                    version_status=terminal_status,
                    manifest_version_absent=False,
                    version_audio_absent=True,
                    control=reliability_validation.FaultControlEvidence(
                        control_code=(
                            "cancelled"
                            if case_plan.action == "cancel-running"
                            else "timeout"
                        ),
                        failure_stage=(
                            None if case_plan.action == "cancel-running" else "timeout"
                        ),
                        prompt_id=prompt_id,
                        initial_state="running",
                        final_state="interrupted",
                        actions=["interrupt"],
                        duration_seconds=0.5,
                        converged=True,
                    ),
                )
            comfyui = reliability_validation.ComfyQueueEvidence(
                queue_empty=True,
                history_present=True,
                prompt_id=prompt_id,
                queue_before_prompt_ids=[prompt_id],
                queue_after_prompt_ids=[],
                history_prompt_ids=[prompt_id],
                terminal_history_status=actual,
            )
            prompt_id_value = prompt_id
        case = reliability_validation.CaseEvidence(
            case_id=case_plan.case_id,
            phase=case_plan.phase,
            engine=case_plan.engine,
            expected=case_plan.expected,
            actual=actual,
            job_id=f"job-{case_plan.case_id}",
            prompt_id=prompt_id_value,
            version_id=version_id,
            prompt_submitted=prompt_submitted,
            tts_more=tts_more,
            termination=termination,
            started_at=started_at,
            finished_at=finished_at,
            audio=audio_proof if actual == "completed" else None,
            cleanup=reliability_validation.CleanupEvidence(
                ok=True,
                owned_processes_stopped=True,
                temp_paths_removed=True,
            ),
            processes=[
                reliability_validation.ProcessEvidence(
                    pid=10_000 + index,
                    ownership="validator-owned",
                    command_hash="a" * 64,
                    creation_time=started_at + timedelta(seconds=2),
                    parent_pid=8188,
                    parent_creation_time=started_at + timedelta(seconds=1),
                    stopped_at=finished_at - timedelta(seconds=1),
                    executable_name="python.exe",
                    executable_hash="a" * 64,
                    ownership_hash="b" * 64,
                    started=True,
                    stopped=True,
                    descendants_stopped=True,
                    alive_after=False,
                )
            ],
            comfyui=comfyui,
            gpu_before=reliability_validation.GpuSnapshot(used_mib=100, free_mib=8000),
            gpu_peak=reliability_validation.GpuSnapshot(used_mib=300, free_mib=7800),
            gpu_after=reliability_validation.GpuSnapshot(used_mib=100, free_mib=8000),
            boundary=boundary,
        )
        assert reliability_validation.validate_case(case).valid is True
        cases.append(case)
        (cases_directory / f"{case.case_id}.json").write_bytes(
            _canonical_model_bytes(case)
        )
        if case.audio is not None:
            (audio_directory / f"{case.case_id}.wav").write_bytes(wav_payload)

    fixture = reliability_validation.ReliabilityFixture.model_validate(
        {
            "version": 1,
            "base_urls": {
                "tts_more": "http://127.0.0.1:8000",
                "comfyui": "http://127.0.0.1:8188",
            },
            "resources": {
                engine: {
                    "resource_id": f"{engine}-resource",
                    "reference_audio": f"fixtures/{engine}.wav",
                    "reference_text": engine,
                }
                for engine in reliability_validation.ENGINE_ORDER
            },
            "rounds": 10,
        }
    )
    summary = reliability_validation.finalize_run(
        fixture,
        cases,
        required_cases=reliability_validation.required_case_specs(plan),
    )
    assert summary.status == "passed"
    (directory / "reliability-summary.json").write_bytes(
        _canonical_model_bytes(summary)
    )


def _write_fake_inner(
    script_path: Path,
    *,
    start_count_path: Path,
    raw_run_id_path: Path,
    scenario: str,
    evidence_fixture: Path | None = None,
    ready_directory: Path | None = None,
    release_path: Path | None = None,
    swap_outside: Path | None = None,
    swap_marker: Path | None = None,
) -> None:
    script_path.write_text(
        rf"""
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Fixture,
    [Parameter(Mandatory = $true)] [string] $OutputRoot,
    [Parameter(Mandatory = $true)] [string] $ComfyUiRoot,
    [Parameter(Mandatory = $true)] [string] $ComfyPython,
    [Parameter(Mandatory = $true)] [string] $TtsMoreRoot,
    [Parameter(Mandatory = $true)] [string] $RunId,
    [Parameter(Mandatory = $true)] [string] $OutputRootIdentity,
    [Parameter(Mandatory = $true)] [string] $RunRootIdentity,
    [string] $PrivateRecoveryRoot,
    [string] $PrivateRecoveryRootIdentity,
    [string] $PrivateRecoveryNamespaceIdentity,
    [switch] $AllowLan,
    [switch] $PreflightOnly
)
$ErrorActionPreference = 'Stop'
$scenario = '{scenario}'
$countPath = '{_powershell_literal(start_count_path)}'
$rawRunIdPath = '{_powershell_literal(raw_run_id_path)}'
$readyDirectory = '{_powershell_literal(ready_directory or script_path.parent)}'
$releasePath = '{_powershell_literal(release_path or script_path)}'
$evidenceFixture = '{_powershell_literal(evidence_fixture or script_path)}'
$swapOutside = '{_powershell_literal(swap_outside or script_path.parent)}'
$swapMarker = '{_powershell_literal(swap_marker or script_path)}'
if ($scenario -ne 'cas') {{
    $count = if (Test-Path -LiteralPath $countPath) {{
        [int] (Get-Content -LiteralPath $countPath -Raw)
    }} else {{ 0 }}
    Set-Content -LiteralPath $countPath -Value ([string] ($count + 1)) -NoNewline
    Set-Content -LiteralPath $rawRunIdPath -Value $RunId -NoNewline
}}
$sha = [Security.Cryptography.SHA256]::Create()
try {{
    $runKey = ([BitConverter]::ToString(
        $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($RunId))
    )).Replace('-', '').ToLowerInvariant()
}} finally {{ $sha.Dispose() }}
$backendRoot = Join-Path $TtsMoreRoot 'backend'
$backendPython = Join-Path $backendRoot '.venv\Scripts\python.exe'
$runDirectory = Join-Path (Join-Path $OutputRoot 'runs') $runKey
$privateDirectory = if ([string]::IsNullOrEmpty($PrivateRecoveryRoot)) {{
    $runDirectory
}} else {{
    $PrivateRecoveryRoot
}}
[IO.Directory]::CreateDirectory($runDirectory) | Out-Null
if ($scenario -eq 'lease-race') {{
    $parked = $runDirectory + '.parked'
    $junctionCreated = $false
    $swapSucceeded = $false
    try {{
        [IO.Directory]::Move($runDirectory, $parked)
        & cmd.exe /d /c mklink /J $runDirectory $swapOutside | Out-Null
        if ($LASTEXITCODE -ne 0) {{ throw 'junction swap failed' }}
        $junctionCreated = $true
        [IO.File]::WriteAllBytes(
            (Join-Path $runDirectory 'escaped.bin'),
            [byte[]](76,69,65,83,69)
        )
        $swapSucceeded = $true
    }} catch {{
        $swapSucceeded = $false
    }} finally {{
        if ($junctionCreated) {{ [IO.Directory]::Delete($runDirectory) }}
        if (Test-Path -LiteralPath $parked -PathType Container) {{
            [IO.Directory]::Move($parked, $runDirectory)
        }}
    }}
    [IO.File]::WriteAllText(
        $swapMarker,
        $(if ($swapSucceeded) {{ 'swapped' }} else {{ 'blocked' }})
    )
}}
if ($scenario -eq 'private-lease-race') {{
    $parked = $privateDirectory + '.parked'
    $swapSucceeded = $false
    try {{
        [IO.Directory]::Move($privateDirectory, $parked)
        [IO.Directory]::CreateDirectory($privateDirectory) | Out-Null
        [IO.File]::WriteAllBytes(
            (Join-Path $privateDirectory 'replacement.bin'),
            [byte[]](80,82,73,86,65,84,69)
        )
        $swapSucceeded = $true
    }} catch {{
        $swapSucceeded = $false
    }} finally {{
        if ($swapSucceeded -and (
                Test-Path -LiteralPath $privateDirectory -PathType Container
            )) {{
            [IO.Directory]::Delete($privateDirectory, $true)
        }}
        if (Test-Path -LiteralPath $parked -PathType Container) {{
            [IO.Directory]::Move($parked, $privateDirectory)
        }}
    }}
    [IO.File]::WriteAllText(
        $swapMarker,
        $(if ($swapSucceeded) {{ 'swapped' }} else {{ 'blocked' }})
    )
}}
if ($scenario -eq 'cas') {{
    [IO.File]::WriteAllText((Join-Path $readyDirectory ($RunId + '.ready')), $runKey)
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    while (-not (Test-Path -LiteralPath $releasePath -PathType Leaf)) {{
        if ([DateTime]::UtcNow -ge $deadline) {{ exit 91 }}
        Start-Sleep -Milliseconds 25
    }}
}}
$utf8 = New-Object Text.UTF8Encoding -ArgumentList $false
if ($scenario -eq 'launcher') {{
    [Console]::Error.WriteLine('fake-launcher-crash')
    exit 9
}}
if ($scenario -eq 'missing-result-zero') {{ exit 0 }}
if ($scenario -in @('junk-preflight', 'junk-matrix')) {{
    [IO.File]::WriteAllText((Join-Path $runDirectory 'preflight.json'), '{{"status":"passed"}}' + [Environment]::NewLine, $utf8)
}} else {{
    Get-ChildItem -LiteralPath $evidenceFixture -Force | Copy-Item -Destination $runDirectory -Recurse
}}
$mode = if ($scenario -in @(
    'preflight', 'privacy', 'extra-member', 'cas', 'junk-preflight',
    'launcher-completed', 'launcher-residual', 'lease-race'
    , 'private-lease-race'
)) {{ 'preflight' }} else {{ 'matrix' }}
if ($scenario -eq 'junk-matrix') {{
    [IO.Directory]::CreateDirectory((Join-Path $runDirectory 'cases')) | Out-Null
    [IO.Directory]::CreateDirectory((Join-Path $runDirectory 'audio')) | Out-Null
    $lastCase = if ($scenario -eq 'sparse-matrix') {{ 45 }} else {{ 46 }}
    foreach ($index in 0..$lastCase) {{
        $caseName = 'case-' + $index.ToString('00') + '.json'
        [IO.File]::WriteAllText((Join-Path (Join-Path $runDirectory 'cases') $caseName), '{{"status":"passed"}}' + [Environment]::NewLine, $utf8)
    }}
    foreach ($index in 0..38) {{
        $audioName = 'audio-' + $index.ToString('00') + '.wav'
        [IO.File]::WriteAllBytes((Join-Path (Join-Path $runDirectory 'audio') $audioName), [byte[]] (82, 73, 70, 70, $index))
    }}
    [IO.File]::WriteAllText((Join-Path $runDirectory 'reliability-summary.json'), '{{"status":"passed"}}' + [Environment]::NewLine, $utf8)
}}
if ($scenario -eq 'sparse-matrix') {{
    Remove-Item -LiteralPath ((Get-ChildItem -LiteralPath (Join-Path $runDirectory 'cases') -File | Select-Object -Last 1).FullName) -Force
}}
$validatorExit = if ($scenario -eq 'validator') {{ 7 }} elseif ($scenario -eq 'negative') {{ -7 }} else {{ 0 }}
$cleanupStatus = if ($scenario -in @('cleanup', 'cleanup-residual', 'launcher-residual')) {{ 'failed' }} else {{ 'completed' }}
if ($scenario -in @('validator', 'cleanup', 'cleanup-residual', 'negative')) {{
    $failureCode = if ($scenario -in @('cleanup', 'cleanup-residual')) {{ 'cleanup-failed' }} else {{ 'validator-failed' }}
    $failure = '{{"active_case_id":null,"failure":{{"code":"' + $failureCode + '","stage":"finalize"}},"run_key":"' + $runKey + '","schema_version":1,"status":"failed"}}' + [char] 10
    [IO.File]::WriteAllText((Join-Path $runDirectory 'failure.json'), $failure, $utf8)
}}
if ($scenario -in @('cleanup', 'cleanup-residual')) {{
    foreach ($rawName in @('.co', '.ce', '.bo', '.be', '.l', '.s')) {{
        [IO.File]::WriteAllText(
            (Join-Path $runDirectory $rawName),
            ('PRIVATE-RECOVERY ' + $rawName + ' C:\private\runtime'),
            $utf8
        )
    }}
}}
if ($scenario -in @('cleanup-residual', 'launcher-residual')) {{
    [IO.Directory]::CreateDirectory((Join-Path $privateDirectory '.p')) | Out-Null
    [IO.File]::WriteAllBytes(
        (Join-Path (Join-Path $privateDirectory '.p') 'sentinel.bin'),
        [byte[]](0,1,127,128,254,255)
    )
    foreach ($privateName in @('.o', '.h', '.c')) {{
        [IO.File]::WriteAllText(
            (Join-Path $privateDirectory $privateName),
            ('PRIVATE-IDENTITY ' + $privateName),
            $utf8
        )
    }}
}}
if ($scenario.StartsWith('invalid-')) {{
    $validatorFragment = switch ($scenario) {{
        'invalid-null' {{ '"validator_exit_code":null' }}
        'invalid-missing' {{ $null }}
        'invalid-string' {{ '"validator_exit_code":"7"' }}
        'invalid-boolean' {{ '"validator_exit_code":true' }}
        'invalid-float' {{ '"validator_exit_code":7.5' }}
        'invalid-out-of-range' {{ '"validator_exit_code":2147483648' }}
    }}
    $invalidResult = '{{"cleanup_status":"completed","failure_source":"validator","kind":"reliability-inner-run-result","mode":"matrix","outcome":"failed","reported_by":"inner","run_key":"' + $runKey + '","schema_version":1'
    if ($null -ne $validatorFragment) {{ $invalidResult += ',' + $validatorFragment }}
    $invalidResult += '}}' + [char] 10
    [IO.File]::WriteAllText((Join-Path $runDirectory 'run-result.json'), $invalidResult, $utf8)
    exit 7
}}
if ($scenario -eq 'extra-member') {{
    [IO.File]::WriteAllText((Join-Path $runDirectory 'rogue.txt'), 'unexpected', $utf8)
}}
Push-Location -LiteralPath $backendRoot
try {{
    if ($scenario -in @('launcher-completed', 'launcher-residual')) {{
        & $backendPython -m app.comfyui.reliability_supervision_cli record-inner `
            --output-root $OutputRoot --run-key $runKey --mode $mode `
            --failure-source launcher --cleanup-status $cleanupStatus
    }} else {{
        & $backendPython -m app.comfyui.reliability_supervision_cli record-inner `
            --output-root $OutputRoot --run-key $runKey --mode $mode `
            --validator-exit-code $validatorExit --cleanup-status $cleanupStatus
    }}
    $pythonExit = $LASTEXITCODE
}} finally {{
    Pop-Location
}}
if ($pythonExit -ne 0) {{ exit 95 }}
[Console]::Out.WriteLine('fake-inner-stdout')
[Console]::Error.WriteLine('fake-inner-stderr')
if ($scenario -eq 'privacy') {{
    [Console]::Out.WriteLine('token=TOP-SECRET C:\private\model.ckpt resource_id=voice-private')
    [Console]::Error.WriteLine('https://user:password@example.invalid/private')
}}
if ($scenario -in @('validator', 'cleanup', 'cleanup-residual')) {{ exit 7 }}
if ($scenario -eq 'launcher-completed') {{ exit 9 }}
if ($scenario -eq 'launcher-residual') {{ exit 7 }}
if ($scenario -eq 'negative') {{ exit -7 }}
exit 0
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _run_supervisor(
    tmp_path: Path,
    *,
    scenario: str = "preflight",
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    harness_scripts = tmp_path / "harness" / "scripts"
    harness_scripts.mkdir(parents=True)
    supervisor = harness_scripts / SUPERVISOR_SOURCE.name
    shutil.copy2(SUPERVISOR_SOURCE, supervisor)
    start_count = tmp_path / "start-count.txt"
    raw_run_id = tmp_path / "raw-run-id.txt"
    evidence_fixture = tmp_path / "authoritative-public-fixture"
    matrix_scenarios = {
        "matrix",
        "sparse-matrix",
        "wrong-case-ids",
        "mismatched-bindings",
        "non-wav",
        "flac-as-wav",
        "ogg-as-wav",
    }
    _write_authoritative_public_fixture(
        evidence_fixture,
        matrix=scenario in matrix_scenarios,
    )
    if scenario in {"launcher-completed", "launcher-residual"}:
        (evidence_fixture / "preflight.json").unlink()
    if scenario == "wrong-case-ids":
        first_case = sorted((evidence_fixture / "cases").glob("*.json"))[0]
        first_case.rename(first_case.with_name("wrong-case-id.json"))
    elif scenario == "mismatched-bindings":
        for case_path in sorted((evidence_fixture / "cases").glob("*.json")):
            document = json.loads(case_path.read_text(encoding="utf-8"))
            if document["audio"] is not None:
                document["audio"]["sha256"] = "0" * 64
                case_path.write_text(
                    json.dumps(
                        document,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="utf-8",
                )
                break
    elif scenario == "non-wav":
        first_audio = sorted((evidence_fixture / "audio").glob("*.wav"))[0]
        first_audio.write_bytes(b"RIFFx")
    elif scenario in {"flac-as-wav", "ogg-as-wav"}:
        first_audio = sorted((evidence_fixture / "audio").glob("*.wav"))[0]
        container_format = "FLAC" if scenario == "flac-as-wav" else "OGG"
        payload = _voiced_container_bytes(container_format)
        first_audio.write_bytes(payload)
        decoded, sample_rate = soundfile.read(
            io.BytesIO(payload),
            dtype="float32",
            always_2d=True,
        )
        proof = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
            "sample_rate": sample_rate,
            "frames": int(decoded.shape[0]),
            "peak": float(max(abs(float(decoded.min())), abs(float(decoded.max())))),
        }
        case_path = evidence_fixture / "cases" / f"{first_audio.stem}.json"
        case_document = json.loads(case_path.read_text(encoding="utf-8"))
        case_document["audio"] = proof
        case_path.write_bytes(
            (
                json.dumps(
                    case_document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
        summary_path = evidence_fixture / "reliability-summary.json"
        summary_document = json.loads(summary_path.read_text(encoding="utf-8"))
        for case in summary_document["cases"]:
            if case["case_id"] == first_audio.stem:
                case["audio"] = proof
                break
        summary_path.write_bytes(
            (
                json.dumps(
                    summary_document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
    swap_outside = tmp_path / "lease-race-outside"
    swap_marker = tmp_path / "lease-race.marker"
    if scenario == "lease-race":
        swap_outside.mkdir()
        (swap_outside / "sentinel.bin").write_bytes(b"outside-must-survive")
    _write_fake_inner(
        harness_scripts / "run-windows-comfyui-reliability.ps1",
        start_count_path=start_count,
        raw_run_id_path=raw_run_id,
        scenario=scenario,
        evidence_fixture=evidence_fixture,
        swap_outside=swap_outside,
        swap_marker=swap_marker,
    )
    fixture = tmp_path / "fixture.json"
    fixture.write_text("{}\n", encoding="utf-8")
    comfy_root = tmp_path / "comfyui"
    comfy_root.mkdir()
    comfy_python = tmp_path / "comfy-python.exe"
    comfy_python.write_bytes(b"test-only")
    output_root = tmp_path / "evidence"
    completed = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(supervisor),
            "-Fixture",
            str(fixture),
            "-OutputRoot",
            str(output_root),
            "-ComfyUiRoot",
            str(comfy_root),
            "-ComfyPython",
            str(comfy_python),
            "-TtsMoreRoot",
            str(REPOSITORY_ROOT),
            *(
                ["-PreflightOnly"]
                if scenario in {
                    "preflight",
                    "privacy",
                    "extra-member",
                    "junk-preflight",
                    "launcher-completed",
                    "launcher-residual",
                    "lease-race",
                    "private-lease-race",
                }
                else []
            ),
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    return completed, output_root, start_count, raw_run_id


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_f4_junction_ancestor_is_rejected_before_output_creation_or_child_start(
    tmp_path: Path,
) -> None:
    harness_scripts = tmp_path / "harness" / "scripts"
    harness_scripts.mkdir(parents=True)
    supervisor = harness_scripts / SUPERVISOR_SOURCE.name
    shutil.copy2(SUPERVISOR_SOURCE, supervisor)
    child_started = tmp_path / "child-started.txt"
    (harness_scripts / INNER_SOURCE.name).write_text(
        (
            "[CmdletBinding()]\n"
            "param([Parameter(ValueFromRemainingArguments = $true)] $Rest)\n"
            f"[IO.File]::WriteAllText('{_powershell_literal(child_started)}', 'started')\n"
            "exit 0\n"
        ),
        encoding="utf-8",
    )
    fixture = tmp_path / "fixture.json"
    fixture.write_text("{}\n", encoding="utf-8")
    comfy_root = tmp_path / "comfyui"
    comfy_root.mkdir()
    comfy_python = tmp_path / "comfy-python.exe"
    comfy_python.write_bytes(b"test-only")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"outside-must-survive")
    junction = tmp_path / "junction"
    _make_windows_junction(junction, outside)
    escaped_output = outside / "must-remain-absent"
    output_root = junction / escaped_output.name
    assert not escaped_output.exists()

    try:
        completed = subprocess.run(
            [
                str(POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(supervisor),
                "-Fixture",
                str(fixture),
                "-OutputRoot",
                str(output_root),
                "-ComfyUiRoot",
                str(comfy_root),
                "-ComfyPython",
                str(comfy_python),
                "-TtsMoreRoot",
                str(REPOSITORY_ROOT),
                "-PreflightOnly",
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

        assert completed.returncode != 0
        assert not escaped_output.exists()
        assert sentinel.read_bytes() == b"outside-must-survive"
        assert not child_started.exists()
        assert not (outside / "current-terminal.json").exists()
        assert not list(outside.glob("runs/*/terminal.json"))
    finally:
        junction.rmdir()


def test_r3_run_root_swap_is_blocked_for_the_full_supervised_lifecycle(
    tmp_path: Path,
) -> None:
    completed, output_root, start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="lease-race",
    )

    outside = tmp_path / "lease-race-outside"
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert start_count.read_text(encoding="utf-8") == "1"
    assert (tmp_path / "lease-race.marker").read_text(encoding="utf-8") == "blocked"
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel.bin"]
    assert (outside / "sentinel.bin").read_bytes() == b"outside-must-survive"
    current = reliability_evidence.verify_current(output_root)
    assert current.pointer.outcome == "passed"
    assert current.pointer.mode == "preflight"


def test_private_recovery_leaf_swap_is_blocked_for_the_full_supervised_lifecycle(
    tmp_path: Path,
) -> None:
    completed, output_root, start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="private-lease-race",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert start_count.read_text(encoding="utf-8") == "1"
    assert (tmp_path / "lease-race.marker").read_text(encoding="utf-8") == "blocked"
    current = reliability_evidence.verify_current(output_root)
    assert current.pointer.outcome == "passed"
    assert current.pointer.mode == "preflight"
    assert not (output_root / ".private-recovery" / current.pointer.run_key).exists()


def test_cleanup_success_holds_deleted_private_leaf_name_through_publication(
    tmp_path: Path,
) -> None:
    harness_scripts = tmp_path / "harness" / "scripts"
    harness_scripts.mkdir(parents=True)
    supervisor = harness_scripts / SUPERVISOR_SOURCE.name
    pause_marker = tmp_path / "private-delete-pending.marker"
    supervisor_source = SUPERVISOR_SOURCE.read_text(encoding="utf-8")
    deletion = "        $directoryLease.RemovePrivateRunDirectory()"
    assert supervisor_source.count(deletion) == 1
    supervisor.write_text(
        supervisor_source.replace(
            deletion,
            deletion
            + "\n"
            + f"        [IO.File]::WriteAllText('{_powershell_literal(pause_marker)}', 'pending')\n"
            + "        Start-Sleep -Milliseconds 1200",
        ),
        encoding="utf-8",
    )
    start_count = tmp_path / "start-count.txt"
    raw_run_id = tmp_path / "raw-run-id.txt"
    evidence_fixture = tmp_path / "authoritative-public-fixture"
    _write_authoritative_public_fixture(evidence_fixture, matrix=False)
    _write_fake_inner(
        harness_scripts / INNER_SOURCE.name,
        start_count_path=start_count,
        raw_run_id_path=raw_run_id,
        scenario="preflight",
        evidence_fixture=evidence_fixture,
    )
    fixture = tmp_path / "fixture.json"
    fixture.write_text("{}\n", encoding="utf-8")
    comfy_root = tmp_path / "comfyui"
    comfy_root.mkdir()
    comfy_python = tmp_path / "comfy-python.exe"
    comfy_python.write_bytes(b"test-only")
    output_root = tmp_path / "evidence"
    process = subprocess.Popen(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(supervisor),
            "-Fixture",
            str(fixture),
            "-OutputRoot",
            str(output_root),
            "-ComfyUiRoot",
            str(comfy_root),
            "-ComfyPython",
            str(comfy_python),
            "-TtsMoreRoot",
            str(REPOSITORY_ROOT),
            "-PreflightOnly",
        ],
        cwd=REPOSITORY_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        deadline = time.monotonic() + 20
        while not pause_marker.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(stdout + stderr)
            if time.monotonic() >= deadline:
                raise AssertionError("private deletion gate was not reached")
            time.sleep(0.01)
        run_key = hashlib.sha256(
            raw_run_id.read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest()
        private_leaf = output_root / ".private-recovery" / run_key
        try:
            private_leaf_observation = f"stat:{private_leaf.lstat()}"
        except OSError as exc:
            private_leaf_observation = f"error:{exc!r}"
        recreation_blocked = False
        try:
            private_leaf.mkdir()
        except OSError:
            recreation_blocked = True
        stdout, stderr = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert recreation_blocked, "delete-pending private leaf name was reusable"
    assert process.returncode == 0, private_leaf_observation + "\n" + stdout + stderr
    current = reliability_evidence.verify_current(output_root)
    assert not (output_root / ".private-recovery" / current.pointer.run_key).exists()


def test_r3_validation_failure_releases_the_directory_lease(tmp_path: Path) -> None:
    completed, output_root, _start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="junk-preflight",
    )

    assert completed.returncode != 0
    _assert_no_current(output_root)
    run_root = next((output_root / "runs").iterdir())
    assert not (run_root / "terminal.json").exists()
    moved = run_root.with_name(f"{run_root.name}.released")
    run_root.rename(moved)
    moved.rename(run_root)
    assert run_root.is_dir()


def test_supervisor_starts_inner_once_and_commits_preflight_passed_current(
    tmp_path: Path,
) -> None:
    completed, output_root, start_count, raw_run_id_path = _run_supervisor(tmp_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert start_count.read_text(encoding="utf-8") == "1"
    raw_run_id = raw_run_id_path.read_text(encoding="utf-8")
    current = reliability_evidence.verify_current(output_root)
    _assert_public_cli_verification(output_root, current.pointer.run_key)
    assert current.status == "current"
    assert current.pointer.mode == "preflight"
    assert current.pointer.outcome == "passed"
    assert current.run.terminal.launcher_exit_code == 0
    assert current.run.terminal.validator_exit_code == 0
    run_root = output_root / "runs" / current.pointer.run_key
    assert not (output_root / ".private-recovery" / current.pointer.run_key).exists()
    assert sorted(
        path.relative_to(run_root).as_posix()
        for path in run_root.rglob("*")
        if path.is_file()
    ) == [
        "logs/inner-stderr.log",
        "logs/inner-stdout.log",
        "preflight.json",
        "run-result.json",
        "supervisor.json",
        "terminal.json",
    ]
    public_documents = [
        json.loads(path.read_bytes())
        for path in (
            output_root / "current-terminal.json",
            run_root / "run-result.json",
            run_root / "supervisor.json",
            run_root / "terminal.json",
        )
    ]
    assert raw_run_id not in json.dumps(public_documents, sort_keys=True)
    committed_logs = b"".join(
        path.read_bytes() for path in (run_root / "logs").glob("*.log")
    )
    assert b"fake-inner-stdout" not in committed_logs
    assert b"fake-inner-stderr" not in committed_logs


def test_f1_status_only_preflight_cannot_become_current(tmp_path: Path) -> None:
    completed, output_root, _start_count, raw_run_id_path = _run_supervisor(
        tmp_path,
        scenario="junk-preflight",
    )

    raw_run_id = raw_run_id_path.read_text(encoding="utf-8")
    run_key = hashlib.sha256(raw_run_id.encode("utf-8")).hexdigest()
    run_root = output_root / "runs" / run_key
    assert completed.returncode != 0
    assert not (run_root / "terminal.json").exists()
    assert not (output_root / "current-terminal.json").exists()


@pytest.mark.parametrize(
    "scenario",
    ["junk-matrix", "wrong-case-ids", "mismatched-bindings", "non-wav"],
)
def test_f1_invalid_matrix_artifacts_cannot_become_current(
    tmp_path: Path,
    scenario: str,
) -> None:
    completed, output_root, _start_count, raw_run_id_path = _run_supervisor(
        tmp_path,
        scenario=scenario,
    )

    raw_run_id = raw_run_id_path.read_text(encoding="utf-8")
    run_key = hashlib.sha256(raw_run_id.encode("utf-8")).hexdigest()
    run_root = output_root / "runs" / run_key
    assert completed.returncode != 0
    assert not (run_root / "terminal.json").exists()
    assert not (output_root / "current-terminal.json").exists()


@pytest.mark.parametrize("scenario", ["flac-as-wav", "ogg-as-wav"])
def test_r2_decodable_non_wav_container_cannot_become_current(
    tmp_path: Path,
    scenario: str,
) -> None:
    completed, output_root, _start_count, raw_run_id_path = _run_supervisor(
        tmp_path,
        scenario=scenario,
    )

    raw_run_id = raw_run_id_path.read_text(encoding="utf-8")
    run_key = hashlib.sha256(raw_run_id.encode("utf-8")).hexdigest()
    run_root = output_root / "runs" / run_key
    assert completed.returncode != 0
    assert not (run_root / "terminal.json").exists()
    assert not (output_root / "current-terminal.json").exists()


def _assert_terminal(
    output_root: Path,
    *,
    mode: str,
    outcome: str,
    failure_source: str,
    launcher_exit_code: int,
    validator_exit_code: int | None,
    cleanup_status: str,
) -> reliability_evidence.CurrentVerification:
    current = reliability_evidence.verify_current(output_root)
    _assert_public_cli_verification(output_root, current.pointer.run_key)
    terminal = current.run.terminal
    assert current.pointer.mode == mode
    assert current.pointer.outcome == outcome
    assert terminal.failure_source == failure_source
    assert terminal.launcher_exit_code == launcher_exit_code
    assert terminal.validator_exit_code == validator_exit_code
    assert terminal.cleanup_status == cleanup_status
    return current


def _assert_public_cli_verification(output_root: Path, run_key: str) -> None:
    for command in (
        ("verify-run", "--run-key", run_key),
        ("verify-current",),
    ):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.comfyui.reliability_evidence_cli",
                command[0],
                "--output-root",
                str(output_root),
                *command[1:],
            ],
            cwd=BACKEND_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout)
        assert payload["ok"] is True
        assert payload["verification"]["status"] in {"verified", "current"}


def _prepare_direct_finalization(
    output_root: Path,
    prepared_run: reliability_supervision.PreparedRun,
    *,
    mode: str,
    launcher_exit_code: int,
    child_start_count: int = 1,
) -> None:
    prepared = reliability_supervision.prepare_private_finalization(
        output_root,
        prepared_run.run_key,
        mode=mode,  # type: ignore[arg-type]
        expected_root_identity=prepared_run.root_identity,
        expected_run_root_identity=prepared_run.run_root_identity,
        expected_private_root_identity=prepared_run.private_root_identity,
        expected_private_namespace_identity=(
            prepared_run.private_namespace_identity
        ),
        launcher_exit_code=launcher_exit_code,
        child_start_count=child_start_count,
    )
    if prepared.cleanup_status == "completed":
        Path(prepared_run.private_root).rmdir()


def test_supervisor_commits_exact_matrix_success(tmp_path: Path) -> None:
    completed, output_root, start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="matrix",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert start_count.read_text(encoding="utf-8") == "1"
    current = _assert_terminal(
        output_root,
        mode="matrix",
        outcome="passed",
        failure_source="none",
        launcher_exit_code=0,
        validator_exit_code=0,
        cleanup_status="completed",
    )
    assert len(current.run.terminal.cases) == 47
    assert sum(
        item.relative_name.startswith("audio/")
        for item in current.run.terminal.artifacts
    ) == 39


def test_supervisor_commits_validator_failure_with_exact_exit_7(tmp_path: Path) -> None:
    completed, output_root, start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="validator",
    )

    assert completed.returncode == 7, completed.stdout + completed.stderr
    assert start_count.read_text(encoding="utf-8") == "1"
    _assert_terminal(
        output_root,
        mode="matrix",
        outcome="failed",
        failure_source="validator",
        launcher_exit_code=7,
        validator_exit_code=7,
        cleanup_status="completed",
    )


@pytest.mark.parametrize(
    (
        "terminal_source",
        "validator_exit_code",
        "cleanup_status",
        "marker_code",
        "marker_stage",
        "expected_current",
    ),
    [
        ("launcher", None, "completed", "launcher-failed", "preflight", True),
        ("launcher", None, "completed", "validator-failed", "finalize", False),
        ("validator", 7, "completed", "validator-failed", "finalize", True),
        ("validator", 7, "completed", "cleanup-failed", "finalize", False),
        ("cleanup", 0, "failed", "cleanup-failed", "finalize", True),
        ("cleanup", 0, "failed", "validator-failed", "finalize", False),
        ("cleanup", 7, "failed", "validator-failed", "finalize", True),
        ("cleanup", 7, "failed", "cleanup-failed", "finalize", False),
    ],
)
def test_r1_failed_artifacts_are_cross_bound_to_terminal_and_primary_facts(
    tmp_path: Path,
    terminal_source: str,
    validator_exit_code: int | None,
    cleanup_status: str,
    marker_code: str,
    marker_stage: str,
    expected_current: bool,
) -> None:
    output_root = tmp_path / (
        f"{terminal_source}-{validator_exit_code}-{cleanup_status}-{marker_code}"
    )
    run_key = hashlib.sha256(str(output_root).encode("utf-8")).hexdigest()
    prepared_root = reliability_supervision.prepare_output_root(output_root)
    prepared_run = reliability_supervision.prepare_run(
        output_root,
        run_key,
        expected_root_identity=prepared_root.root_identity,
    )
    expected_token = reliability_evidence.snapshot_current(output_root)["token"]
    if terminal_source == "launcher":
        reliability_supervision.record_inner_result(
            output_root,
            run_key,
            mode="preflight",
            validator_exit_code=None,
            cleanup_status=cleanup_status,  # type: ignore[arg-type]
            failure_source="launcher",
        )
        launcher_exit_code = 9
    else:
        reliability_supervision.record_inner_result(
            output_root,
            run_key,
            mode="preflight",
            validator_exit_code=validator_exit_code,
            cleanup_status=cleanup_status,  # type: ignore[arg-type]
        )
        launcher_exit_code = 7
    marker = reliability_validation.ReliabilityRunFailure(
        run_key=run_key,
        failure=reliability_validation.FailureMarker(
            code=marker_code,
            stage=marker_stage,  # type: ignore[arg-type]
        ),
    )
    reliability_evidence.write_artifact(
        output_root,
        run_key,
        "failure",
        _canonical_model_bytes(marker),
    )
    _prepare_direct_finalization(
        output_root,
        prepared_run,
        mode="preflight",
        launcher_exit_code=launcher_exit_code,
    )

    if expected_current:
        finalized = reliability_supervision.finalize_supervision(
            output_root,
            run_key,
            mode="preflight",
            expected_token=expected_token,
            expected_root_identity=prepared_run.root_identity,
            expected_run_root_identity=prepared_run.run_root_identity,
            expected_private_root_identity=prepared_run.private_root_identity,
            expected_private_namespace_identity=(
                prepared_run.private_namespace_identity
            ),
            launcher_exit_code=launcher_exit_code,
            child_start_count=1,
        )
        assert finalized.status == "current"
        current = reliability_evidence.verify_current(output_root)
        assert current.run.terminal.failure_source == terminal_source
        assert current.run.terminal.validator_exit_code == validator_exit_code
        assert current.run.terminal.cleanup_status == cleanup_status
    else:
        with pytest.raises(
            reliability_supervision.SupervisionError,
            match="run artifacts are invalid",
        ):
            reliability_supervision.finalize_supervision(
                output_root,
                run_key,
                mode="preflight",
                expected_token=expected_token,
                expected_root_identity=prepared_run.root_identity,
                expected_run_root_identity=prepared_run.run_root_identity,
                expected_private_root_identity=prepared_run.private_root_identity,
                expected_private_namespace_identity=(
                    prepared_run.private_namespace_identity
                ),
                launcher_exit_code=launcher_exit_code,
                child_start_count=1,
            )
        _assert_no_current(output_root)
        assert not (output_root / "runs" / run_key / "terminal.json").exists()


def test_f3_started_child_missing_result_fails_closed(
    tmp_path: Path,
) -> None:
    completed, output_root, start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="launcher",
    )

    assert completed.returncode != 0
    assert start_count.read_text(encoding="utf-8") == "1"
    _assert_no_current(output_root)
    assert not list((output_root / "runs").glob("*/terminal.json"))


def _run_actual_inner_prevalidator_failure(
    tmp_path: Path,
    *,
    cleanup_unproven: bool,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    run_id = "4" * 32
    run_key = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    output_root = tmp_path / "evidence"
    prepared_root = reliability_supervision.prepare_output_root(output_root)
    prepared_run = reliability_supervision.prepare_run(
        output_root,
        run_key,
        expected_root_identity=prepared_root.root_identity,
    )
    run_root = Path(prepared_run.run_root)
    private_root = Path(prepared_run.private_root)
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    resources: dict[str, dict[str, str]] = {}
    for engine in reliability_validation.ENGINE_ORDER:
        reference_name = f"{engine}.wav"
        (fixture_root / reference_name).write_bytes(b"test-reference")
        resources[engine] = {"reference_audio": reference_name}
    fixture_path = fixture_root / "fixture.json"
    fixture_path.write_text(json.dumps({"resources": resources}), encoding="utf-8")
    comfy_root = tmp_path / "ComfyUI"
    (comfy_root / "custom_nodes" / "TTS-Audio-Suite").mkdir(parents=True)
    started_marker = tmp_path / "fake-comfy-started.marker"
    control_path = private_root / ".c"
    (comfy_root / "main.py").write_text(
        "from pathlib import Path\n"
        "import os, time\n"
        "Path(os.environ['TTS_MORE_F3_STARTED']).write_text('started')\n"
        "time.sleep(0.35)\n"
        + (
            "Path(os.environ['TTS_MORE_F3_CONTROL']).unlink(missing_ok=True)\n"
            if cleanup_unproven
            else ""
        )
        + "raise SystemExit(23)\n",
        encoding="utf-8",
    )
    model_roots: dict[str, Path] = {}
    for name in ("gpt", "index", "cosy"):
        model_root = tmp_path / name
        model_root.mkdir()
        model_roots[name] = model_root
    registry_path = tmp_path / "resources.json"
    registry_path.write_text("{}\n", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "TTS_MORE_RELIABILITY_GPT_SOVITS_ROOT": str(model_roots["gpt"]),
            "TTS_MORE_RELIABILITY_INDEXTTS_ROOT": str(model_roots["index"]),
            "TTS_MORE_RELIABILITY_COSYVOICE_ROOT": str(model_roots["cosy"]),
            "TTS_AUDIO_SUITE_RESOURCES": str(registry_path),
            "TTS_MORE_F3_STARTED": str(started_marker),
            "TTS_MORE_F3_CONTROL": str(control_path),
        }
    )
    completed = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(INNER_SOURCE),
            "-Fixture",
            str(fixture_path),
            "-OutputRoot",
            str(output_root),
            "-ComfyUiRoot",
            str(comfy_root),
            "-ComfyPython",
            sys.executable,
            "-TtsMoreRoot",
            str(REPOSITORY_ROOT),
            "-RunId",
            run_id,
            "-OutputRootIdentity",
            prepared_root.root_identity,
            "-RunRootIdentity",
            prepared_run.run_root_identity,
            "-PrivateRecoveryRoot",
            prepared_run.private_root,
            "-PrivateRecoveryRootIdentity",
            prepared_run.private_root_identity,
            "-PrivateRecoveryNamespaceIdentity",
            prepared_run.private_namespace_identity,
            "-PreflightOnly",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=30,
        check=False,
    )
    return completed, run_root, private_root, started_marker


@pytest.mark.parametrize(
    ("cleanup_unproven", "expected_cleanup"),
    [(False, "completed"), (True, "failed")],
)
def test_f3_inner_prevalidator_failure_records_truthful_started_service_cleanup(
    tmp_path: Path,
    cleanup_unproven: bool,
    expected_cleanup: str,
) -> None:
    completed, run_root, private_root, started_marker = _run_actual_inner_prevalidator_failure(
        tmp_path,
        cleanup_unproven=cleanup_unproven,
    )

    assert completed.returncode != 0
    assert started_marker.read_text(encoding="utf-8") == "started"
    result = reliability_supervision.InnerRunResult.model_validate_json(
        (run_root / "run-result.json").read_bytes(),
        strict=True,
    )
    assert result.reported_by == "inner"
    assert result.failure_source == "launcher"
    assert result.validator_exit_code is None
    assert result.cleanup_status == expected_cleanup
    if cleanup_unproven:
        assert any((private_root / name).exists() for name in (".p", ".o", ".h", ".c"))
    else:
        assert not any((private_root / name).exists() for name in (".p", ".o", ".h", ".c"))
    assert not any((run_root / name).exists() for name in (".p", ".o", ".h", ".c"))


def test_f3_supervisor_commits_inner_launcher_cleanup_completed(tmp_path: Path) -> None:
    completed, output_root, start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="launcher-completed",
    )

    assert completed.returncode == 9, completed.stdout + completed.stderr
    assert start_count.read_text(encoding="utf-8") == "1"
    current = _assert_terminal(
        output_root,
        mode="preflight",
        outcome="failed",
        failure_source="launcher",
        launcher_exit_code=9,
        validator_exit_code=None,
        cleanup_status="completed",
    )
    assert not (output_root / ".private-recovery" / current.pointer.run_key).exists()


def test_f3_supervisor_publishes_inner_launcher_private_recovery_residual(
    tmp_path: Path,
) -> None:
    completed, output_root, start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="launcher-residual",
    )

    assert completed.returncode == 7, completed.stdout + completed.stderr
    assert start_count.read_text(encoding="utf-8") == "1"
    current = _assert_terminal(
        output_root,
        mode="preflight",
        outcome="failed",
        failure_source="launcher",
        launcher_exit_code=7,
        validator_exit_code=None,
        cleanup_status="failed",
    )
    run_root = output_root / "runs" / current.pointer.run_key
    result = reliability_supervision.InnerRunResult.model_validate_json(
        (run_root / "run-result.json").read_bytes(),
        strict=True,
    )
    assert result.reported_by == "inner"
    assert result.failure_source == "launcher"
    assert result.validator_exit_code is None
    assert result.cleanup_status == "failed"
    assert not any((run_root / name).exists() for name in (".p", ".o", ".h", ".c"))
    private_root = output_root / ".private-recovery" / current.pointer.run_key
    assert (private_root / ".p" / "sentinel.bin").read_bytes() == bytes(
        (0, 1, 127, 128, 254, 255)
    )
    assert all((private_root / name).is_file() for name in (".o", ".h", ".c"))
    assert (run_root / "logs" / "private-recovery.log").is_file()


def test_supervisor_commits_cleanup_failure(tmp_path: Path) -> None:
    completed, output_root, start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="cleanup",
    )

    assert completed.returncode == 7, completed.stdout + completed.stderr
    assert start_count.read_text(encoding="utf-8") == "1"
    current = _assert_terminal(
        output_root,
        mode="matrix",
        outcome="failed",
        failure_source="cleanup",
        launcher_exit_code=7,
        validator_exit_code=0,
        cleanup_status="failed",
    )
    run_root = output_root / "runs" / current.pointer.run_key
    assert not any((run_root / name).exists() for name in (".co", ".ce", ".bo", ".be", ".l", ".s"))
    committed_log_names = {
        commitment.relative_name
        for commitment in current.run.terminal.artifacts
        if commitment.relative_name.startswith("logs/")
    }
    assert committed_log_names == {
        "logs/comfyui-stderr.log",
        "logs/comfyui-stdout.log",
        "logs/inner-stderr.log",
        "logs/inner-stdout.log",
        "logs/launcher-lifecycle-secondary.log",
        "logs/launcher-lifecycle.log",
        "logs/private-recovery.log",
        "logs/tts-more-stderr.log",
        "logs/tts-more-stdout.log",
    }
    for path in (run_root / "logs").glob("*.log"):
        assert b"PRIVATE-RECOVERY" not in path.read_bytes()


def test_cleanup_failure_publishes_private_recovery_residual_as_current(
    tmp_path: Path,
) -> None:
    seeded_output_root = tmp_path / "evidence"
    seeded_output_root.mkdir()
    old_run_key = "a" * 64
    old_expected_token, old_prepared = _prepare_orphan_preflight_run(
        seeded_output_root, old_run_key
    )
    _prepare_direct_finalization(
        seeded_output_root,
        old_prepared,
        mode="preflight",
        launcher_exit_code=0,
    )
    reliability_supervision.finalize_supervision(
        seeded_output_root,
        old_run_key,
        mode="preflight",
        expected_token=old_expected_token,
        expected_root_identity=old_prepared.root_identity,
        expected_run_root_identity=old_prepared.run_root_identity,
        expected_private_root_identity=old_prepared.private_root_identity,
        expected_private_namespace_identity=old_prepared.private_namespace_identity,
        launcher_exit_code=0,
        child_start_count=1,
    )
    old_pointer_bytes = (seeded_output_root / "current-terminal.json").read_bytes()
    old_current = reliability_evidence.verify_current(seeded_output_root)
    completed, output_root, start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="cleanup-residual",
    )

    assert completed.returncode == 7, completed.stdout + completed.stderr
    assert start_count.read_text(encoding="utf-8") == "1"
    assert (output_root / "current-terminal.json").read_bytes() != old_pointer_bytes
    current = reliability_evidence.verify_current(output_root)
    assert current != old_current
    assert current.run.terminal.failure_source == "cleanup"
    assert current.run.terminal.cleanup_status == "failed"
    run_root = output_root / "runs" / current.pointer.run_key
    assert not any((run_root / name).exists() for name in (".p", ".o", ".h", ".c"))
    assert (run_root / "logs" / "private-recovery.log").is_file()
    private_root = output_root / ".private-recovery" / current.pointer.run_key
    assert (private_root / ".p" / "sentinel.bin").read_bytes() == bytes(
        (0, 1, 127, 128, 254, 255)
    )
    for private_name in (".o", ".h", ".c"):
        assert (private_root / private_name).is_file()
    assert (run_root / "terminal.json").is_file()


def test_private_recovery_snapshot_crash_preserves_old_current_and_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _completed, output_root, _start_count, _raw_run_id = _run_supervisor(tmp_path)
    old_pointer_bytes = (output_root / "current-terminal.json").read_bytes()
    old_current = reliability_evidence.verify_current(output_root)
    run_key = "c" * 64
    prepared_root = reliability_supervision.prepare_output_root(output_root)
    prepared_run = reliability_supervision.prepare_run(
        output_root,
        run_key,
        expected_root_identity=prepared_root.root_identity,
    )
    reliability_supervision.record_inner_result(
        output_root,
        run_key,
        mode="preflight",
        validator_exit_code=0,
        cleanup_status="failed",
    )
    private_root = Path(prepared_run.private_root)
    (private_root / ".o").write_bytes(b"private-owner")

    def crash_before_snapshot(*_args: object, **_kwargs: object) -> None:
        raise SystemExit("intentional-before-private-snapshot")

    monkeypatch.setattr(
        reliability_supervision,
        "write_private_recovery_snapshot",
        crash_before_snapshot,
        raising=False,
    )
    with pytest.raises(SystemExit, match="intentional-before-private-snapshot"):
        reliability_supervision.prepare_private_finalization(
            output_root,
            run_key,
            mode="preflight",
            expected_root_identity=prepared_run.root_identity,
            expected_run_root_identity=prepared_run.run_root_identity,
            expected_private_root_identity=prepared_run.private_root_identity,
            expected_private_namespace_identity=(
                prepared_run.private_namespace_identity
            ),
            launcher_exit_code=7,
            child_start_count=1,
        )

    assert (output_root / "current-terminal.json").read_bytes() == old_pointer_bytes
    assert reliability_evidence.verify_current(output_root) == old_current
    assert (private_root / ".o").read_bytes() == b"private-owner"
    run_root = output_root / "runs" / run_key
    assert not (run_root / "logs" / "private-recovery.log").exists()
    assert not (run_root / "terminal.json").exists()


def test_post_delete_private_leaf_recreation_fails_closed_without_deleting_replacement(
    tmp_path: Path,
) -> None:
    _completed, output_root, _start_count, _raw_run_id = _run_supervisor(tmp_path)
    old_pointer_bytes = (output_root / "current-terminal.json").read_bytes()
    old_current = reliability_evidence.verify_current(output_root)
    run_key = "d" * 64
    expected_token, prepared_run = _prepare_orphan_preflight_run(output_root, run_key)
    _prepare_direct_finalization(
        output_root,
        prepared_run,
        mode="preflight",
        launcher_exit_code=0,
    )
    replacement = Path(prepared_run.private_root)
    replacement.mkdir()
    sentinel = replacement / "replacement-sentinel.bin"
    sentinel.write_bytes(b"replacement-must-survive")

    with pytest.raises(
        reliability_supervision.SupervisionError,
        match="private recovery cleanup is incomplete",
    ):
        reliability_supervision.finalize_supervision(
            output_root,
            run_key,
            mode="preflight",
            expected_token=expected_token,
            expected_root_identity=prepared_run.root_identity,
            expected_run_root_identity=prepared_run.run_root_identity,
            expected_private_root_identity=prepared_run.private_root_identity,
            expected_private_namespace_identity=(
                prepared_run.private_namespace_identity
            ),
            launcher_exit_code=0,
            child_start_count=1,
        )

    assert (output_root / "current-terminal.json").read_bytes() == old_pointer_bytes
    assert reliability_evidence.verify_current(output_root) == old_current
    assert sentinel.read_bytes() == b"replacement-must-survive"
    assert not (output_root / "runs" / run_key / "terminal.json").exists()


def _assert_no_current(output_root: Path) -> None:
    snapshot = reliability_evidence.snapshot_current(output_root)
    assert snapshot["status"] == "absent"
    assert not (output_root / "current-terminal.json").exists()


def test_zero_exit_without_inner_result_fails_closed(tmp_path: Path) -> None:
    completed, output_root, start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="missing-result-zero",
    )

    assert completed.returncode != 0
    assert start_count.read_text(encoding="utf-8") == "1"
    _assert_no_current(output_root)
    assert list((output_root / "runs").glob("*"))


def test_sparse_passing_matrix_cannot_become_current(tmp_path: Path) -> None:
    completed, output_root, _start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="sparse-matrix",
    )

    assert completed.returncode != 0
    _assert_no_current(output_root)
    assert not list((output_root / "runs").glob("*/terminal.json"))


def test_uncommitted_extra_member_prevents_terminal_and_current(tmp_path: Path) -> None:
    completed, output_root, _start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="extra-member",
    )

    assert completed.returncode != 0
    _assert_no_current(output_root)
    assert list((output_root / "runs").glob("*/rogue.txt"))
    assert not list((output_root / "runs").glob("*/terminal.json"))


def test_signed_negative_child_exit_is_not_reinterpreted_in_evidence(
    tmp_path: Path,
) -> None:
    completed, output_root, start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="negative",
    )

    assert ctypes.c_int32(completed.returncode).value == -7
    assert start_count.read_text(encoding="utf-8") == "1"
    _assert_terminal(
        output_root,
        mode="matrix",
        outcome="failed",
        failure_source="validator",
        launcher_exit_code=-7,
        validator_exit_code=-7,
        cleanup_status="completed",
    )


def test_adversarial_child_output_is_only_committed_as_a_hash(tmp_path: Path) -> None:
    completed, output_root, _start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="privacy",
    )

    assert completed.returncode == 0
    current = reliability_evidence.verify_current(output_root)
    run_root = output_root / "runs" / current.pointer.run_key
    public_bytes = b"\n".join(
        path.read_bytes()
        for path in [output_root / "current-terminal.json", *run_root.rglob("*")]
        if path.is_file()
    )
    for private_value in (
        b"TOP-SECRET",
        b"C:\\private\\model.ckpt",
        b"voice-private",
        b"user:password",
    ):
        assert private_value not in public_bytes


def test_malformed_inner_result_types_never_advance_current(tmp_path: Path) -> None:
    for scenario in (
        "invalid-null",
        "invalid-missing",
        "invalid-string",
        "invalid-boolean",
        "invalid-float",
        "invalid-out-of-range",
    ):
        scenario_root = tmp_path / scenario
        scenario_root.mkdir()
        completed, output_root, start_count, _raw_run_id = _run_supervisor(
            scenario_root,
            scenario=scenario,
        )
        assert completed.returncode != 0, scenario
        assert start_count.read_text(encoding="utf-8") == "1"
        _assert_no_current(output_root)
        assert not list((output_root / "runs").glob("*/terminal.json"))


def _prepare_orphan_preflight_run(
    output_root: Path, run_key: str
) -> tuple[str, reliability_supervision.PreparedRun]:
    expected_token = reliability_evidence.snapshot_current(output_root)["token"]
    prepared_root = reliability_supervision.prepare_output_root(output_root)
    prepared_run = reliability_supervision.prepare_run(
        output_root,
        run_key,
        expected_root_identity=prepared_root.root_identity,
    )
    fixture_directory = output_root.parent / f"authoritative-{run_key[:12]}"
    _write_authoritative_public_fixture(fixture_directory, matrix=False)
    reliability_evidence.write_artifact(
        output_root,
        run_key,
        "preflight",
        (fixture_directory / "preflight.json").read_bytes(),
    )
    reliability_supervision.record_inner_result(
        output_root,
        run_key,
        mode="preflight",
        validator_exit_code=0,
        cleanup_status="completed",
    )
    return expected_token, prepared_run


def test_crash_before_terminal_preserves_old_current_and_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _completed, output_root, _start_count, _raw_run_id = _run_supervisor(tmp_path)
    old_pointer = (output_root / "current-terminal.json").read_bytes()
    old_current = reliability_evidence.verify_current(output_root)
    run_key = "a" * 64
    expected_token, prepared_run = _prepare_orphan_preflight_run(output_root, run_key)
    _prepare_direct_finalization(
        output_root,
        prepared_run,
        mode="preflight",
        launcher_exit_code=0,
    )

    def crash_before_terminal(*_args: object, **_kwargs: object) -> None:
        raise SystemExit("intentional-before-terminal")

    monkeypatch.setattr(reliability_evidence, "write_terminal", crash_before_terminal)
    with pytest.raises(SystemExit, match="intentional-before-terminal"):
        reliability_supervision.finalize_supervision(
            output_root,
            run_key,
            mode="preflight",
            expected_token=expected_token,
            expected_root_identity=prepared_run.root_identity,
            expected_run_root_identity=prepared_run.run_root_identity,
            expected_private_root_identity=prepared_run.private_root_identity,
            expected_private_namespace_identity=(
                prepared_run.private_namespace_identity
            ),
            launcher_exit_code=0,
            child_start_count=1,
        )

    assert (output_root / "current-terminal.json").read_bytes() == old_pointer
    assert reliability_evidence.verify_current(output_root) == old_current
    orphan = output_root / "runs" / run_key
    assert orphan.is_dir()
    assert (orphan / "supervisor.json").is_file()
    assert not (orphan / "terminal.json").exists()


def test_crash_after_terminal_before_cas_preserves_verified_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _completed, output_root, _start_count, _raw_run_id = _run_supervisor(tmp_path)
    old_pointer = (output_root / "current-terminal.json").read_bytes()
    old_current = reliability_evidence.verify_current(output_root)
    run_key = "b" * 64
    expected_token, prepared_run = _prepare_orphan_preflight_run(output_root, run_key)
    _prepare_direct_finalization(
        output_root,
        prepared_run,
        mode="preflight",
        launcher_exit_code=0,
    )

    def crash_before_cas(*_args: object, **_kwargs: object) -> None:
        raise SystemExit("intentional-before-cas")

    monkeypatch.setattr(
        reliability_evidence,
        "compare_and_swap_current",
        crash_before_cas,
    )
    with pytest.raises(SystemExit, match="intentional-before-cas"):
        reliability_supervision.finalize_supervision(
            output_root,
            run_key,
            mode="preflight",
            expected_token=expected_token,
            expected_root_identity=prepared_run.root_identity,
            expected_run_root_identity=prepared_run.run_root_identity,
            expected_private_root_identity=prepared_run.private_root_identity,
            expected_private_namespace_identity=(
                prepared_run.private_namespace_identity
            ),
            launcher_exit_code=0,
            child_start_count=1,
        )

    assert (output_root / "current-terminal.json").read_bytes() == old_pointer
    assert reliability_evidence.verify_current(output_root) == old_current
    orphan = reliability_evidence.verify_run(output_root, run_key)
    assert orphan.status == "verified"
    assert orphan.terminal.outcome == "passed"


def test_two_real_supervisors_from_one_token_have_one_cas_winner(
    tmp_path: Path,
) -> None:
    harness_scripts = tmp_path / "harness" / "scripts"
    harness_scripts.mkdir(parents=True)
    supervisor = harness_scripts / SUPERVISOR_SOURCE.name
    shutil.copy2(SUPERVISOR_SOURCE, supervisor)
    ready_directory = tmp_path / "ready"
    ready_directory.mkdir()
    release_path = tmp_path / "release"
    evidence_fixture = tmp_path / "authoritative-public-fixture"
    _write_authoritative_public_fixture(evidence_fixture, matrix=False)
    _write_fake_inner(
        harness_scripts / "run-windows-comfyui-reliability.ps1",
        start_count_path=tmp_path / "unused-count.txt",
        raw_run_id_path=tmp_path / "unused-run-id.txt",
        scenario="cas",
        evidence_fixture=evidence_fixture,
        ready_directory=ready_directory,
        release_path=release_path,
    )
    fixture = tmp_path / "fixture.json"
    fixture.write_text("{}\n", encoding="utf-8")
    comfy_root = tmp_path / "comfyui"
    comfy_root.mkdir()
    comfy_python = tmp_path / "comfy-python.exe"
    comfy_python.write_bytes(b"test-only")
    output_root = tmp_path / "evidence"
    command = [
        str(POWERSHELL),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(supervisor),
        "-Fixture",
        str(fixture),
        "-OutputRoot",
        str(output_root),
        "-ComfyUiRoot",
        str(comfy_root),
        "-ComfyPython",
        str(comfy_python),
        "-TtsMoreRoot",
        str(REPOSITORY_ROOT),
        "-PreflightOnly",
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for _ in range(2)
    ]
    try:
        deadline = time.monotonic() + 20
        while len(list(ready_directory.glob("*.ready"))) != 2:
            if time.monotonic() >= deadline:
                raise AssertionError("both supervisors did not reach the one-child gate")
            time.sleep(0.025)
        release_path.write_text("release\n", encoding="utf-8")
        completed = [process.communicate(timeout=30) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    assert sorted(process.returncode for process in processes) == [0, 1], completed
    run_keys = {
        marker.read_text(encoding="utf-8")
        for marker in ready_directory.glob("*.ready")
    }
    assert len(run_keys) == 2
    assert {path.name for path in (output_root / "runs").iterdir()} == run_keys
    for run_key in run_keys:
        assert reliability_evidence.verify_run(output_root, run_key).status == "verified"
    current = reliability_evidence.verify_current(output_root)
    assert current.pointer.run_key in run_keys


@pytest.mark.parametrize("run_id", [None, "A" * 32, "a" * 31, "../unsafe"])
def test_direct_inner_missing_or_invalid_run_id_fails_before_any_mutation(
    tmp_path: Path,
    run_id: str | None,
) -> None:
    output_root = tmp_path / "must-remain-absent"
    command = [
        str(POWERSHELL),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(INNER_SOURCE),
        "-Fixture",
        str(tmp_path / "private-fixture-does-not-exist.json"),
        "-OutputRoot",
        str(output_root),
        "-ComfyUiRoot",
        str(tmp_path / "comfy-does-not-exist"),
        "-ComfyPython",
        str(tmp_path / "python-does-not-exist.exe"),
        "-TtsMoreRoot",
        str(tmp_path / "tts-does-not-exist"),
    ]
    if run_id is not None:
        command.extend(["-RunId", run_id])
    completed = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert completed.returncode == 7
    assert completed.stdout == ""
    assert completed.stderr.strip() == "Supervised reliability contract is invalid"
    assert not output_root.exists()


def test_both_reliability_scripts_parse_under_windows_powershell_51() -> None:
    script = r"""
$errors = $null
foreach ($path in $args) {
    $tokens = $null
    $localErrors = $null
    [void] [Management.Automation.Language.Parser]::ParseFile(
        $path,
        [ref] $tokens,
        [ref] $localErrors
    )
    if ($localErrors.Count -ne 0) {
        $localErrors | ForEach-Object { [Console]::Error.WriteLine($_.Message) }
        exit 1
    }
}
exit 0
"""
    completed = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            str(SUPERVISOR_SOURCE),
            str(INNER_SOURCE),
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _run_supervisor_function_contract(command: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["TTS_MORE_SUPERVISOR_SOURCE"] = str(SUPERVISOR_SOURCE)
    return subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=30,
        check=False,
    )


def test_supervisor_bounded_stream_reader_drains_before_strict_decode() -> None:
    command = r"""
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_SUPERVISOR_SOURCE,[ref]$tokens,[ref]$errors
)
if($errors.Count -ne 0){throw($errors|Out-String)}
$function=$ast.Find({param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Read-StrictBoundedProcessStreams'
},$true)
if($null -eq $function){throw 'Read-StrictBoundedProcessStreams is missing'}
Invoke-Expression $function.Extent.Text

$stdoutBytes=[Text.Encoding]::UTF8.GetBytes('stdout-safe')
$stderrBytes=[Text.Encoding]::UTF8.GetBytes('stderr-safe')
$stdout=New-Object IO.MemoryStream(,$stdoutBytes)
$stderr=New-Object IO.MemoryStream(,$stderrBytes)
$captured=Read-StrictBoundedProcessStreams -StandardOutput $stdout `
    -StandardError $stderr -MaximumBytes 32
if([Convert]::ToBase64String($captured.stdout_bytes) -cne
       [Convert]::ToBase64String($stdoutBytes) -or
   [Convert]::ToBase64String($captured.stderr_bytes) -cne
       [Convert]::ToBase64String($stderrBytes) -or
   $stdout.Position -ne $stdout.Length -or $stderr.Position -ne $stderr.Length){
    throw 'bounded reader did not preserve and drain valid streams'
}

$oversizedBytes=New-Object byte[] 33
for($index=0;$index -lt $oversizedBytes.Length;$index+=1){
    $oversizedBytes[$index]=[byte]65
}
$oversized=New-Object IO.MemoryStream(,$oversizedBytes)
$empty=New-Object IO.MemoryStream
$caught=$null
try {
    Read-StrictBoundedProcessStreams -StandardOutput $oversized `
        -StandardError $empty -MaximumBytes 32|Out-Null
} catch { $caught=$_ }
if($null -eq $caught -or $oversized.Position -ne $oversized.Length){
    throw 'oversized stream was not fully drained and rejected'
}
if(([string]$caught).Contains('AAAA')){throw 'oversized bytes leaked in the error'}

$invalid=New-Object IO.MemoryStream(,[byte[]](255,254,253))
$empty=New-Object IO.MemoryStream
$caught=$null
try {
    Read-StrictBoundedProcessStreams -StandardOutput $invalid `
        -StandardError $empty -MaximumBytes 32|Out-Null
} catch { $caught=$_ }
if($null -eq $caught -or $invalid.Position -ne $invalid.Length){
    throw 'invalid UTF-8 stream was not fully drained and rejected'
}
Write-Output 'SUPERVISOR_BOUNDED_STREAM_READER_OK'
"""
    completed = _run_supervisor_function_contract(command)
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert "SUPERVISOR_BOUNDED_STREAM_READER_OK" in completed.stdout


def test_supervisor_exit_reader_accepts_only_runtime_int32() -> None:
    command = r"""
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile(
    $env:TTS_MORE_SUPERVISOR_SOURCE,[ref]$tokens,[ref]$errors
)
if($errors.Count -ne 0){throw($errors|Out-String)}
$function=$ast.Find({param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Get-StrictProcessExitCode'
},$true)
if($null -eq $function){throw 'Get-StrictProcessExitCode is missing'}
Invoke-Expression $function.Extent.Text
$valid=[pscustomobject]@{ExitCode=[int]-7}
if((Get-StrictProcessExitCode -Process $valid) -ne -7){
    throw 'signed Int32 exit was not preserved'
}
$invalid=@(
    [pscustomobject]@{},
    [pscustomobject]@{ExitCode=$null},
    [pscustomobject]@{ExitCode=$true},
    [pscustomobject]@{ExitCode='7'},
    [pscustomobject]@{ExitCode=[double]7},
    [pscustomobject]@{ExitCode=[long]7}
)
foreach($candidate in $invalid){
    $caught=$null
    try{Get-StrictProcessExitCode -Process $candidate|Out-Null}catch{$caught=$_}
    if($null -eq $caught){throw 'non-Int32 exit unexpectedly passed'}
}
Write-Output 'SUPERVISOR_STRICT_INT32_EXIT_OK'
"""
    completed = _run_supervisor_function_contract(command)
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert "SUPERVISOR_STRICT_INT32_EXIT_OK" in completed.stdout
