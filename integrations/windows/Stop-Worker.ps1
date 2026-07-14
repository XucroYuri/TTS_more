[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Bundle = [System.IO.Path]::GetFullPath($PSScriptRoot)
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $Bundle))
if (!(Test-Path -LiteralPath $Root -PathType Container)) { throw "portable package root is missing" }
if ((((Get-Item -LiteralPath $Root -Force).Attributes) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "portable package root cannot be a reparse point"
}
$ValidationScript = Join-Path $Bundle "Portable-Validation.ps1"
if (!(Test-Path -LiteralPath $ValidationScript -PathType Leaf)) { throw "Portable-Validation.ps1 is missing" }
. $ValidationScript
$Root = Assert-PortablePackageRoot -Root $Root
$recordPath = Resolve-PortablePackagePath -Root $Root -RelativePath "data\local\run\worker.pid.json" -Label "PID record"
if (!(Test-Path -LiteralPath $recordPath)) { Write-Host "worker is not running"; exit 0 }
$runtimeLockPath = Resolve-PortablePackagePath -Root $Root -RelativePath "tts_more\locks\runtime.lock.json" -Label "runtime lock" -MustExist
$runtimeLock = Get-Content -LiteralPath $runtimeLockPath -Raw | ConvertFrom-Json
$expectedPython = [string]$runtimeLock.python_version
if ($expectedPython -notin @("3.10", "3.11")) { throw "worker runtime lock has an unsupported Python version" }
$Python = Join-Path $Root "runtime\live\python.exe"
[void](Assert-PortableRuntime -Root $Root -PythonPath $Python -ExpectedVersion $expectedPython -ImportProbe "")
$Launcher = Resolve-PortablePackagePath -Root $Root -RelativePath "tts_more\portable_launcher.py" -Label "portable launcher" -MustExist
& $Python $Launcher stop-worker --package-root $Root
if ($LASTEXITCODE -eq 2) { throw "owned worker stopped but port release was not confirmed; PID record preserved" }
if ($LASTEXITCODE -ne 0) { throw "safe worker stop failed with exit code $LASTEXITCODE" }
Write-Host "worker stopped and port released"
