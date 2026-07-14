[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$recordPath = Join-Path $Root "data\local\run\worker.pid.json"
if (!(Test-Path -LiteralPath $recordPath)) {
    Write-Host "TTS More is not running."
    exit 0
}
$record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
$Python = [System.IO.Path]::GetFullPath([string]$record.executable_path)
if (!(Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Recorded package Python is missing; preserving PID evidence: $Python"
}
& $Python (Join-Path $Root "scripts\portable_launcher.py") stop-worker --package-root $Root
if ($LASTEXITCODE -eq 2) {
    throw "Owned process was terminated but its port did not release; PID record was preserved."
}
if ($LASTEXITCODE -ne 0) {
    throw "TTS More safe stop failed with exit code $LASTEXITCODE"
}
Write-Host "TTS More stopped and its port is released."
