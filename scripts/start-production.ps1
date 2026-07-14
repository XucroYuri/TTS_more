[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$Port = if ($env:TTS_MORE_PORT) { [int]$env:TTS_MORE_PORT } else { 8000 }

function Resolve-ExistingPath {
    param([string[]]$Candidates, [string]$Label)
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    throw "$Label is missing. Run Initialize.cmd first."
}

$BackendRoot = Resolve-ExistingPath @((Join-Path $Root "backend"), (Join-Path $Root "app\backend")) "backend"
$StaticRoot = Resolve-ExistingPath @((Join-Path $Root "frontend\dist"), (Join-Path $Root "app\frontend")) "frontend static assets"
$Python = Resolve-ExistingPath @($env:TTS_MORE_PYTHON_EXE, (Join-Path $Root "runtime\live\python.exe"), (Join-Path $Root ".venv\Scripts\python.exe")) "package Python"

$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
    $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
        $process = Get-Process -Id $_ -ErrorAction SilentlyContinue
        [pscustomobject]@{ pid = $_; name = if ($process) { $process.ProcessName } else { "unknown" }; path = if ($process) { $process.Path } else { "unknown" } }
    })
    throw "TTS More port $Port is already in use. Owner: $($owners | ConvertTo-Json -Compress). No process was terminated."
}

$env:TTS_MORE_STATIC_ROOT = $StaticRoot
$arguments = @("-m", "uvicorn", "app.main:app", "--app-dir", $BackendRoot, "--host", "127.0.0.1", "--port", [string]$Port)
$process = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $Root -WindowStyle Hidden -PassThru
$processCreatedAt = $process.StartTime.ToUniversalTime().ToString("o")
$manifestPath = Join-Path $Root "package\tts-more-package.json"
$buildId = "source-checkout"
if (Test-Path -LiteralPath $manifestPath) {
    $buildId = [string](Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json).build_id
}
$recordPath = Join-Path $Root "data\local\run\worker.pid.json"
& $Python (Join-Path $Root "scripts\portable_launcher.py") write-process-record `
    --package-root $Root --record-path $recordPath --pid $process.Id --parent-pid $PID `
    --process-created-at $processCreatedAt --executable $Python --port $Port --build-id $buildId -- @arguments
if ($LASTEXITCODE -ne 0) {
    throw "failed to persist TTS More process ownership record"
}

$deadline = [DateTime]::UtcNow.AddSeconds(60)
do {
    if ($process.HasExited) {
        throw "TTS More exited during startup with code $($process.ExitCode)"
    }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
        if ($health.status -eq "ok") {
            Write-Host "TTS More ready: http://127.0.0.1:$Port"
            exit 0
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
} while ([DateTime]::UtcNow -lt $deadline)
throw "TTS More did not become healthy within 60 seconds; run Stop.cmd and inspect the console log."
