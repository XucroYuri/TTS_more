[CmdletBinding()]
param(
    [string]$PackageRoot = "",
    [string]$OperationRoot = "",
    [ValidateRange(1, 65535)][Nullable[int]]$PortOverride = $null
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-PortablePackageRootChain {
    param([Parameter(Mandatory = $true)][string]$Root)
    if ([string]::IsNullOrWhiteSpace($Root)) { throw "portable package root is required" }
    $lexicalRoot = [IO.Path]::GetFullPath($Root)
    $pathRoot = [IO.Path]::GetPathRoot($lexicalRoot)
    if ([string]::IsNullOrWhiteSpace($pathRoot)) { throw "portable package root has no filesystem root" }
    $trimmedRoot = $lexicalRoot.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    if ($trimmedRoot.Length -ge $pathRoot.Length) { $lexicalRoot = $trimmedRoot }
    $current = [IO.Path]::GetFullPath($pathRoot)
    $chain = [Collections.Generic.List[string]]::new()
    [void]$chain.Add($current)
    $relative = $lexicalRoot.Substring($pathRoot.Length).TrimStart([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $segments = @($relative -split '[\\/]' | Where-Object { ![string]::IsNullOrWhiteSpace($_) })
    foreach ($segment in $segments) {
        $current = [IO.Path]::GetFullPath((Join-Path $current $segment))
        [void]$chain.Add($current)
    }
    foreach ($candidate in $chain) {
        if (!(Test-Path -LiteralPath $candidate -PathType Container)) { throw "portable package root or ancestor is missing" }
        if ((((Get-Item -LiteralPath $candidate -Force).Attributes) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "portable package root or ancestor cannot be a reparse point"
        }
    }
    return $lexicalRoot
}

$Root = if ([string]::IsNullOrWhiteSpace($PackageRoot)) { [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)) } else { [System.IO.Path]::GetFullPath($PackageRoot) }
$Root = Assert-PortablePackageRootChain -Root $Root
$ValidationScript = Join-Path $PSScriptRoot "Portable-Validation.ps1"
if (!(Test-Path -LiteralPath $ValidationScript -PathType Leaf)) { throw "Portable-Validation.ps1 is missing" }
. $ValidationScript
$Root = Assert-PortablePackageRoot -Root $Root
$Port = if ($null -ne $PortOverride) { [int]$PortOverride } elseif ($env:TTS_MORE_PORT) { [int]$env:TTS_MORE_PORT } else { 8000 }

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
$Python = Join-Path $Root "runtime\live\python.exe"
$RuntimeLockPath = Join-Path $Root "packaging\portable\runtime.lock.json"
$RuntimeLock = Get-Content -LiteralPath $RuntimeLockPath -Raw | ConvertFrom-Json
$ExpectedPython = if ([string]::IsNullOrWhiteSpace([string]$RuntimeLock.python_version)) { "3.11" } else { [string]$RuntimeLock.python_version }
$ImportProbe = if ($RuntimeLock.PSObject.Properties["import_probe"] -and ![string]::IsNullOrWhiteSpace([string]$RuntimeLock.import_probe)) { [string]$RuntimeLock.import_probe } else { "import fastapi,pydantic,uvicorn" }
[void](Assert-PortableRuntime -Root $Root -PythonPath $Python -ExpectedVersion $ExpectedPython -ImportProbe $ImportProbe)
$manifestPath = Join-Path $Root "package\tts-more-package.json"
$buildId = if (Test-Path -LiteralPath $manifestPath -PathType Leaf) { [string](Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json).build_id } else { "source-checkout" }
$arguments = @("-m", "uvicorn", "app.main:app", "--app-dir", $BackendRoot, "--host", "127.0.0.1", "--port", [string]$Port)
$recordPath = Join-Path $Root "data\local\run\worker.pid.json"
$Launcher = Join-Path $Root "scripts\portable_launcher.py"

$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
    $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
        $process = Get-Process -Id $_ -ErrorAction SilentlyContinue
        [pscustomobject]@{ pid = $_; name = if ($process) { $process.ProcessName } else { "unknown" }; path = if ($process) { $process.Path } else { "unknown" } }
    })
    $verifyArguments = @($Launcher, "verify-owned-listener", "--package-root", $Root, "--record-path", $recordPath, "--port", [string]$Port, "--build-id", $buildId, "--executable", $Python)
    foreach ($listenerPid in @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)) { $verifyArguments += @("--listener-pid", [string]$listenerPid) }
    $verifyArguments += "--"
    $verifyArguments += $arguments
    & $Python @verifyArguments *> $null
    $owned = $LASTEXITCODE -eq 0
    if ($owned) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
            if ($health.status -eq "ok") { Write-Host "TTS More ready: http://127.0.0.1:$Port"; exit 0 }
        } catch { }
    }
    throw "PORT_IN_USE: TTS More port $Port is already in use. Owner: $($owners | ConvertTo-Json -Compress). No process was terminated."
}

$env:TTS_MORE_STATIC_ROOT = $StaticRoot
$env:PATH = "$(Split-Path -Parent $Python);$env:PATH"
$process = $null
$processCreatedAt = ""
$startArgumentLine = ConvertTo-PortableWindowsArgumentLine -Arguments $arguments
try {
    $process = Start-Process -FilePath $Python -ArgumentList $startArgumentLine -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    $processCreatedAt = $process.StartTime.ToUniversalTime().ToString("o")
    & $Python $Launcher write-process-record `
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
        } catch { }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "TTS More did not become healthy within 60 seconds; run Stop.cmd and inspect the console log."
} catch {
    $startupFailure = $_.Exception.Message
    if ($null -ne $process -and ![string]::IsNullOrWhiteSpace($processCreatedAt)) {
        $rollbackArguments = @($Launcher, "rollback-started-process", "--package-root", $Root, "--pid", [string]$process.Id, "--parent-pid", [string]$PID, "--process-created-at", $processCreatedAt, "--executable", $Python, "--port", [string]$Port, "--build-id", $buildId, "--") + $arguments
        $rollbackOutput = @(& $Python @rollbackArguments 2>&1) -join [Environment]::NewLine
        $rollbackExitCode = $LASTEXITCODE
        if ($rollbackExitCode -ne 0) {
            throw "$startupFailure Rollback failed with exit code $rollbackExitCode. Evidence: $rollbackOutput"
        }
        throw "$startupFailure Startup process rollback completed."
    }
    throw
}
