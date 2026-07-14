[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if (!(Test-Path -LiteralPath $Root -PathType Container)) { throw "portable package root is missing" }
if ((((Get-Item -LiteralPath $Root -Force).Attributes) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "portable package root cannot be a reparse point"
}
$ValidationScript = Join-Path $PSScriptRoot "Portable-Validation.ps1"
if (!(Test-Path -LiteralPath $ValidationScript -PathType Leaf)) { throw "Portable-Validation.ps1 is missing" }
. $ValidationScript
$Root = Assert-PortablePackageRoot -Root $Root
$recordPath = Resolve-PortablePackagePath -Root $Root -RelativePath "data\local\run\worker.pid.json" -Label "PID record"
if (!(Test-Path -LiteralPath $recordPath)) {
    Write-Host "TTS More is not running."
    exit 0
}
$runtimeLockPath = Resolve-PortablePackagePath -Root $Root -RelativePath "packaging\portable\runtime.lock.json" -Label "runtime lock" -MustExist
$runtimeLock = Get-Content -LiteralPath $runtimeLockPath -Raw | ConvertFrom-Json
$expectedPython = [string]$runtimeLock.python_version
if ($expectedPython -ne "3.11") { throw "TTS More runtime lock must require Python 3.11" }
$Python = Join-Path $Root "runtime\live\python.exe"
[void](Assert-PortableRuntime -Root $Root -PythonPath $Python -ExpectedVersion $expectedPython -ImportProbe "")
$Launcher = Resolve-PortablePackagePath -Root $Root -RelativePath "scripts\portable_launcher.py" -Label "portable launcher" -MustExist
& $Python $Launcher stop-worker --package-root $Root
if ($LASTEXITCODE -eq 2) {
    throw "Owned process was terminated but its port did not release; PID record was preserved."
}
if ($LASTEXITCODE -ne 0) {
    throw "TTS More safe stop failed with exit code $LASTEXITCODE"
}
Write-Host "TTS More stopped and its port is released."
