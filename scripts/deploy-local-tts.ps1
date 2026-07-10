param(
    [ValidateSet("Auto", "ModelScope", "HF", "HF-Mirror")][string]$Source = "Auto",
    [ValidateSet("CU128", "CU126", "CPU", "ROCM", "MPS")][string]$Device = "CU128",
    [string[]]$Targets = @("default"),
    [string]$RepoPaths = "",
    [switch]$CleanRepos,
    [switch]$SkipAppInstall,
    [switch]$SkipRepoSync,
    [switch]$SkipRepoPrepare,
    [switch]$SkipInstall,
    [switch]$SkipDownloads,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$BasePython = if ($env:TTS_MORE_BASE_PYTHON) { $env:TTS_MORE_BASE_PYTHON } else { "python" }
$AppPython = Join-Path $Root ".venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $AppPython) { $AppPython } else { $BasePython }

function Invoke-Logged {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $Root
    )
    Write-Host "[run] $FilePath $($Arguments -join ' ')" -ForegroundColor Cyan
    if ($DryRun) { return }
    Push-Location $WorkingDirectory
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) { throw "Command failed: $FilePath $($Arguments -join ' ')" }
    } finally {
        Pop-Location
    }
}

function Refresh-Python {
    if (Test-Path -LiteralPath $AppPython) {
        $script:Python = $AppPython
    } else {
        $script:Python = $BasePython
    }
}

function Add-RepoPathsArg {
    param([string[]]$Arguments)
    if ($RepoPaths) {
        return $Arguments + @("--repo-paths", $RepoPaths)
    }
    return $Arguments
}

function Install-App {
    if ($SkipAppInstall) {
        Write-Host "[skip] app dependency install" -ForegroundColor Yellow
        return
    }
    if (!(Test-Path -LiteralPath $AppPython)) {
        Invoke-Logged $BasePython @("-m", "venv", (Join-Path $Root ".venv"))
    }
    Refresh-Python
    Invoke-Logged $Python @("-m", "pip", "install", "-e", (Join-Path $Root "backend[dev]"))
    $pnpm = (Get-Command pnpm -ErrorAction SilentlyContinue).Source
    if ($pnpm) {
        Invoke-Logged $pnpm @("install", "--frozen-lockfile") (Join-Path $Root "frontend")
    } else {
        Write-Warning "pnpm was not found; skipping frontend dependency install."
    }
}

Refresh-Python
Install-App
Refresh-Python

$targetList = $Targets -join ","
$validateArgs = Add-RepoPathsArg @((Join-Path $Root "scripts\tts_more_deploy.py"), "validate-repo-paths", "--service-ids", $targetList)
Invoke-Logged $Python $validateArgs

if (-not $SkipRepoSync) {
    $syncArgs = Add-RepoPathsArg @((Join-Path $Root "scripts\tts_more_deploy.py"), "sync-repos", "--service-ids", $targetList)
    if ($CleanRepos) { $syncArgs += "--clean" }
    if ($DryRun) { $syncArgs += "--dry-run" }
    Invoke-Logged $Python $syncArgs
} else {
    Write-Host "[skip] repo sync" -ForegroundColor Yellow
}

$bundleArgs = Add-RepoPathsArg @((Join-Path $Root "scripts\tts_more_deploy.py"), "install-repo-bundles", "--service-ids", $targetList)
if ($DryRun) { $bundleArgs += "--dry-run" }
Invoke-Logged $Python $bundleArgs

$updateScriptArgs = Add-RepoPathsArg @((Join-Path $Root "scripts\tts_more_deploy.py"), "install-update-scripts", "--service-ids", $targetList)
if ($DryRun) { $updateScriptArgs += "--dry-run" }
Invoke-Logged $Python $updateScriptArgs

if (-not $SkipRepoPrepare) {
    $prepareArgs = @("-Source", $Source, "-Device", $Device, "-Targets", $targetList)
    if ($RepoPaths) { $prepareArgs += @("-RepoPaths", $RepoPaths) }
    if ($SkipInstall) { $prepareArgs += "-SkipInstall" }
    if ($SkipDownloads) { $prepareArgs += "-SkipDownloads" }
    if ($DryRun) { $prepareArgs += "-DryRun" }
    $prepareCommandArgs = @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $Root "scripts\prepare-tts-repos.ps1"))
    $prepareCommandArgs += $prepareArgs
    Invoke-Logged "powershell" $prepareCommandArgs
} else {
    Write-Host "[skip] repo dependency/model prepare" -ForegroundColor Yellow
}

$renderArgs = Add-RepoPathsArg @((Join-Path $Root "scripts\tts_more_deploy.py"), "render-services", "--profile", "local-all", "--platform", "windows", "--service-ids", $targetList, "--output", "data\local\services.json")
Invoke-Logged $Python $renderArgs

$doctorArgs = Add-RepoPathsArg @((Join-Path $Root "scripts\tts_more_deploy.py"), "doctor", "--service-ids", $targetList)
Invoke-Logged $Python $doctorArgs

Write-Host "Local TTS deployment workflow complete." -ForegroundColor Green
