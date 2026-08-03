from __future__ import annotations

import json
import shutil
import subprocess
import ctypes
import time
import os
import sys
from pathlib import Path

import pytest

from app.comfyui import reliability_evidence
from app.comfyui import reliability_supervision


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
SUPERVISOR_SOURCE = (
    REPOSITORY_ROOT / "scripts" / "run-windows-comfyui-reliability-supervised.ps1"
)
INNER_SOURCE = REPOSITORY_ROOT / "scripts" / "run-windows-comfyui-reliability.ps1"
POWERSHELL = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


def _powershell_literal(value: Path) -> str:
    return str(value).replace("'", "''")


def _write_fake_inner(
    script_path: Path,
    *,
    start_count_path: Path,
    raw_run_id_path: Path,
    scenario: str,
    ready_directory: Path | None = None,
    release_path: Path | None = None,
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
    [switch] $AllowLan,
    [switch] $PreflightOnly
)
$ErrorActionPreference = 'Stop'
$scenario = '{scenario}'
$countPath = '{_powershell_literal(start_count_path)}'
$rawRunIdPath = '{_powershell_literal(raw_run_id_path)}'
$readyDirectory = '{_powershell_literal(ready_directory or script_path.parent)}'
$releasePath = '{_powershell_literal(release_path or script_path)}'
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
[IO.Directory]::CreateDirectory($runDirectory) | Out-Null
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
[IO.File]::WriteAllText((Join-Path $runDirectory 'preflight.json'), '{{"status":"passed"}}' + [Environment]::NewLine, $utf8)
$mode = if ($scenario -in @('preflight', 'privacy', 'extra-member', 'cas')) {{ 'preflight' }} else {{ 'matrix' }}
if ($scenario -in @('matrix', 'sparse-matrix')) {{
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
$validatorExit = if ($scenario -eq 'validator') {{ 7 }} elseif ($scenario -eq 'negative') {{ -7 }} else {{ 0 }}
$cleanupStatus = if ($scenario -in @('cleanup', 'cleanup-residual')) {{ 'failed' }} else {{ 'completed' }}
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
if ($scenario -eq 'cleanup-residual') {{
    [IO.Directory]::CreateDirectory((Join-Path $runDirectory '.p')) | Out-Null
    [IO.File]::WriteAllBytes(
        (Join-Path (Join-Path $runDirectory '.p') 'sentinel.bin'),
        [byte[]](0,1,127,128,254,255)
    )
    foreach ($privateName in @('.o', '.h', '.c')) {{
        [IO.File]::WriteAllText(
            (Join-Path $runDirectory $privateName),
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
    & $backendPython -m app.comfyui.reliability_supervision_cli record-inner `
        --output-root $OutputRoot --run-key $runKey --mode $mode `
        --validator-exit-code $validatorExit --cleanup-status $cleanupStatus
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
    _write_fake_inner(
        harness_scripts / "run-windows-comfyui-reliability.ps1",
        start_count_path=start_count,
        raw_run_id_path=raw_run_id,
        scenario=scenario,
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
                if scenario in {"preflight", "privacy", "extra-member"}
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
    committed_logs = b"".join(path.read_bytes() for path in (run_root / "logs").glob("*.log"))
    assert b"fake-inner-stdout" not in committed_logs
    assert b"fake-inner-stderr" not in committed_logs


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


def test_supervisor_commits_launcher_crash_without_fabricated_validator_exit(
    tmp_path: Path,
) -> None:
    completed, output_root, start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="launcher",
    )

    assert completed.returncode == 9, completed.stdout + completed.stderr
    assert start_count.read_text(encoding="utf-8") == "1"
    _assert_terminal(
        output_root,
        mode="matrix",
        outcome="failed",
        failure_source="launcher",
        launcher_exit_code=9,
        validator_exit_code=None,
        cleanup_status="not-started",
    )


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
        "logs/tts-more-stderr.log",
        "logs/tts-more-stdout.log",
    }
    for path in (run_root / "logs").glob("*.log"):
        assert b"PRIVATE-RECOVERY" not in path.read_bytes()


def test_cleanup_failure_with_private_recovery_residual_remains_orphan(
    tmp_path: Path,
) -> None:
    seeded_output_root = tmp_path / "evidence"
    seeded_output_root.mkdir()
    old_run_key = "a" * 64
    old_expected_token = _prepare_orphan_preflight_run(seeded_output_root, old_run_key)
    reliability_supervision.finalize_supervision(
        seeded_output_root,
        old_run_key,
        mode="preflight",
        expected_token=old_expected_token,
        launcher_exit_code=0,
        child_start_count=1,
    )
    old_pointer_bytes = (seeded_output_root / "current-terminal.json").read_bytes()
    old_current = reliability_evidence.verify_current(seeded_output_root)
    completed, output_root, start_count, _raw_run_id = _run_supervisor(
        tmp_path,
        scenario="cleanup-residual",
    )

    assert completed.returncode != 0
    assert start_count.read_text(encoding="utf-8") == "1"
    assert (output_root / "current-terminal.json").read_bytes() == old_pointer_bytes
    assert reliability_evidence.verify_current(output_root) == old_current
    run_roots = [
        path for path in (output_root / "runs").glob("*") if path.name != old_run_key
    ]
    assert len(run_roots) == 1
    run_root = run_roots[0]
    assert (run_root / ".p" / "sentinel.bin").read_bytes() == bytes(
        (0, 1, 127, 128, 254, 255)
    )
    for private_name in (".o", ".h", ".c"):
        assert (run_root / private_name).is_file()
    assert not (run_root / "terminal.json").exists()


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


def _prepare_orphan_preflight_run(output_root: Path, run_key: str) -> str:
    expected_token = reliability_evidence.snapshot_current(output_root)["token"]
    reliability_evidence.write_artifact(
        output_root,
        run_key,
        "preflight",
        b'{"status":"passed"}\n',
    )
    reliability_supervision.record_inner_result(
        output_root,
        run_key,
        mode="preflight",
        validator_exit_code=0,
        cleanup_status="completed",
    )
    return expected_token


def test_crash_before_terminal_preserves_old_current_and_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _completed, output_root, _start_count, _raw_run_id = _run_supervisor(tmp_path)
    old_pointer = (output_root / "current-terminal.json").read_bytes()
    old_current = reliability_evidence.verify_current(output_root)
    run_key = "a" * 64
    expected_token = _prepare_orphan_preflight_run(output_root, run_key)

    def crash_before_terminal(*_args: object, **_kwargs: object) -> None:
        raise SystemExit("intentional-before-terminal")

    monkeypatch.setattr(reliability_evidence, "write_terminal", crash_before_terminal)
    with pytest.raises(SystemExit, match="intentional-before-terminal"):
        reliability_supervision.finalize_supervision(
            output_root,
            run_key,
            mode="preflight",
            expected_token=expected_token,
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
    expected_token = _prepare_orphan_preflight_run(output_root, run_key)

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
    _write_fake_inner(
        harness_scripts / "run-windows-comfyui-reliability.ps1",
        start_count_path=tmp_path / "unused-count.txt",
        raw_run_id_path=tmp_path / "unused-run-id.txt",
        scenario="cas",
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
