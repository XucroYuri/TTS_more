param(
    [ValidateSet("Auto", "ModelScope", "HF", "HF-Mirror")][string]$Source = "Auto",
    [ValidateSet("CU128", "CU126", "CPU", "ROCM", "MPS")][string]$Device = "CU128",
    [ValidateSet("local-all", "app-only", "worker-node")][string]$Profile = "local-all",
    [string]$Topology = "",
    [string]$Node = "",
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
    $record = [ordered]@{ file = $FilePath; arguments = @($Arguments); working_directory = $WorkingDirectory }
    Write-Host "[run] $($record | ConvertTo-Json -Compress)" -ForegroundColor Cyan
    if ($DryRun) { return }
    Push-Location $WorkingDirectory
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) { throw "Command failed: $FilePath $($Arguments -join ' ')" }
    } finally {
        Pop-Location
    }
}

function Invoke-Plan {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $Root
    )
    $record = [ordered]@{ file = $FilePath; arguments = @($Arguments); working_directory = $WorkingDirectory }
    Write-Host "[plan] $($record | ConvertTo-Json -Compress)" -ForegroundColor Cyan
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

function Add-TopologyArgs {
    param([string[]]$Arguments)
    if ($Topology) {
        $Arguments += @("--topology", $Topology)
    }
    if ($Node) {
        $Arguments += @("--node", $Node)
    }
    return $Arguments
}

function Get-WorkerServiceIds {
    if ($Profile -ne "worker-node") {
        return @($Targets | ForEach-Object { $_ -split "," } | Where-Object { $_ } | ForEach-Object { $_.Trim() })
    }
    if (-not $Topology -or -not $Node) {
        throw "worker-node profile requires -Topology and -Node"
    }
    $topologyPath = if ([IO.Path]::IsPathRooted($Topology)) { $Topology } else { Join-Path $Root $Topology }
    if (!(Test-Path -LiteralPath $topologyPath)) { throw "Topology file not found: $topologyPath" }
    $manifest = Get-Content -LiteralPath $topologyPath -Raw | ConvertFrom-Json
    $nodeProperty = $manifest.nodes.PSObject.Properties[$Node]
    if ($null -eq $nodeProperty) { throw "Topology node not found: $Node" }
    if ($nodeProperty.Value.role -ne "worker") { throw "Topology node is not a worker: $Node" }
    return @($nodeProperty.Value.services)
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

$targetList = (Get-WorkerServiceIds) -join ","
$isAppOnly = $Profile -eq "app-only"
if (-not $isAppOnly) {
    $validateArgs = Add-RepoPathsArg @((Join-Path $Root "scripts\tts_more_deploy.py"), "validate-repo-paths", "--service-ids", $targetList)
    Invoke-Plan $Python $validateArgs

    if (-not $SkipRepoSync) {
        $syncArgs = Add-RepoPathsArg @((Join-Path $Root "scripts\tts_more_deploy.py"), "sync-repos", "--service-ids", $targetList)
        if ($CleanRepos) {
            $previewArgs = Add-RepoPathsArg @((Join-Path $Root "scripts\tts_more_deploy.py"), "list-repos", "--service-ids", $targetList, "--json-lines")
            Write-Host "Selected repository paths to clean:" -ForegroundColor Yellow
            $selectedRepositoryLines = @(& $Python @previewArgs)
            if ($LASTEXITCODE -ne 0) { throw "Unable to list selected repository paths before cleaning." }
            foreach ($repositoryLine in $selectedRepositoryLines) {
                if (-not $repositoryLine) { continue }
                $repository = $repositoryLine | ConvertFrom-Json
                Write-Host "  - $($repository.path)" -ForegroundColor Yellow
            }
            Write-Host "Models and repo-local venvs inside these selected paths will be removed." -ForegroundColor Yellow
            $syncArgs += "--clean"
        }
        if ($DryRun) { $syncArgs += "--dry-run" }
        Invoke-Plan $Python $syncArgs
    } else {
        Write-Host "[skip] repo sync" -ForegroundColor Yellow
    }

    $bundleArgs = Add-RepoPathsArg @((Join-Path $Root "scripts\tts_more_deploy.py"), "install-repo-bundles", "--service-ids", $targetList)
    if ($DryRun) { $bundleArgs += "--dry-run" }
    Invoke-Plan $Python $bundleArgs

    $updateScriptArgs = Add-RepoPathsArg @((Join-Path $Root "scripts\tts_more_deploy.py"), "install-update-scripts", "--service-ids", $targetList)
    if ($DryRun) { $updateScriptArgs += "--dry-run" }
    Invoke-Plan $Python $updateScriptArgs

    if (-not $SkipRepoPrepare) {
        $prepareArgs = @("-Source", $Source, "-Device", $Device, "-Profile", $Profile, "-Targets", $targetList)
        if ($Topology) { $prepareArgs += @("-Topology", $Topology) }
        if ($Node) { $prepareArgs += @("-Node", $Node) }
        if ($RepoPaths) { $prepareArgs += @("-RepoPaths", $RepoPaths) }
        if ($SkipInstall) { $prepareArgs += "-SkipInstall" }
        if ($SkipDownloads) { $prepareArgs += "-SkipDownloads" }
        if ($DryRun) { $prepareArgs += "-DryRun" }
        $prepareCommandArgs = @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $Root "scripts\prepare-tts-repos.ps1"))
        $prepareCommandArgs += $prepareArgs
        Invoke-Plan "powershell" $prepareCommandArgs
    } else {
        Write-Host "[skip] repo dependency/model prepare" -ForegroundColor Yellow
    }
} else {
    Write-Host "[skip] app-only profile does not prepare local TTS repositories" -ForegroundColor Yellow
}

$renderArgs = Add-TopologyArgs (Add-RepoPathsArg @((Join-Path $Root "scripts\tts_more_deploy.py"), "render-services", "--profile", $Profile, "--platform", "windows", "--service-ids", $targetList))
if (-not $DryRun) { $renderArgs += @("--output", "data\local\services.json") }
Invoke-Plan $Python $renderArgs

if (-not $isAppOnly) {
    $doctorArgs = Add-RepoPathsArg @((Join-Path $Root "scripts\tts_more_deploy.py"), "doctor", "--service-ids", $targetList)
    Invoke-Plan $Python $doctorArgs
}

Write-Host "Local TTS deployment workflow complete." -ForegroundColor Green
