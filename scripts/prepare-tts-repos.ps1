param(
    [string[]]$Targets = @("all"),
    [ValidateSet("Auto", "ModelScope", "HF", "HF-Mirror")][string]$Source = "Auto",
    [ValidateSet("CU128", "CU126", "CPU", "ROCM", "MPS")][string]$Device = "CU128",
    [switch]$SyncRepos,
    [switch]$CleanRepos,
    [switch]$SkipInstall,
    [switch]$SkipDownloads,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (!(Test-Path -LiteralPath $Python)) { $Python = "python" }

function Invoke-Logged {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )
    $line = "$FilePath $($Arguments -join ' ')"
    Write-Host "[run] $line" -ForegroundColor Cyan
    if ($DryRun) { return }
    Push-Location $WorkingDirectory
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) { throw "Command failed: $line" }
    } finally {
        Pop-Location
    }
}

function Invoke-Captured {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )
    $line = "$FilePath $($Arguments -join ' ')"
    Write-Host "[run] $line" -ForegroundColor Cyan
    if ($DryRun) { return "{}" }
    Push-Location $WorkingDirectory
    try {
        $output = & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) { throw "Command failed: $line" }
        return ($output -join "`n")
    } finally {
        Pop-Location
    }
}

function Resolve-NetworkProfile {
    $args = @("scripts\tts_more_deploy.py", "probe-network", "--write", "--source", $Source)
    if ($DryRun) {
        Write-Host "[run] $Python $($args -join ' ')" -ForegroundColor Cyan
        return [pscustomobject]@{
            model_source = if ($Source -eq "Auto") { "ModelScope" } else { $Source }
            env = [pscustomobject]@{}
        }
    }
    $json = Invoke-Captured $Python $args $Root
    return $json | ConvertFrom-Json
}

function Set-NetworkProfileEnvironment {
    param($Profile)
    if ($null -eq $Profile.env) { return }
    foreach ($property in $Profile.env.PSObject.Properties) {
        [Environment]::SetEnvironmentVariable($property.Name, [string]$property.Value, "Process")
    }
}

function Get-SourceFallbacks {
    param([string]$PrimarySource)
    $ordered = @($PrimarySource, "ModelScope", "HF-Mirror", "HF")
    $seen = @{}
    $fallbacks = @()
    foreach ($item in $ordered) {
        if (-not $item) { continue }
        if ($seen.ContainsKey($item)) { continue }
        $seen[$item] = $true
        $fallbacks += $item
    }
    return $fallbacks
}

function Invoke-WithSourceFallback {
    param(
        [scriptblock]$Action,
        [string[]]$Sources,
        [string]$Description
    )
    $errors = @()
    foreach ($candidate in $Sources) {
        Write-Host "[source] $Description via $candidate" -ForegroundColor Cyan
        try {
            & $Action $candidate
            return
        } catch {
            $errors += "${candidate}: $($_.Exception.Message)"
            Write-Warning "$Description failed via ${candidate}: $($_.Exception.Message)"
            if ($DryRun) { return }
        }
    }
    throw "$Description failed for all sources: $($errors -join '; ')"
}

function Test-Target {
    param($Repo)
    if ($Targets -contains "all") { return $true }
    return (
        $Targets -contains $Repo.name -or
        $Targets -contains $Repo.provider_type -or
        $Targets -contains $Repo.service_id -or
        ($Repo.variant -and $Targets -contains $Repo.variant)
    )
}

function Resolve-RepoPython {
    param([string]$RepoPath)
    return Join-Path $RepoPath ".venv\Scripts\python.exe"
}

function Ensure-Venv {
    param([string]$RepoPath)
    $venvPython = Resolve-RepoPython $RepoPath
    if (Test-Path -LiteralPath $venvPython) { return $venvPython }
    $basePython = $env:TTS_MORE_BASE_PYTHON
    if (-not $basePython) { $basePython = "python" }
    Invoke-Logged $basePython @("-m", "venv", ".venv") $RepoPath
    Invoke-Logged $venvPython @("-m", "pip", "install", "-U", "pip", "wheel", "setuptools") $RepoPath
    return $venvPython
}

function Prepare-GPTSoVITS {
    param($Repo)
    $repoPath = Join-Path $Root $Repo.path
    if ($SkipInstall) {
        Write-Host "[skip] GPT-SoVITS install for $($Repo.name)"
        return
    }
    $installPs1 = Join-Path $repoPath "install.ps1"
    $installSh = Join-Path $repoPath "install.sh"
    if ($IsWindows -or $env:OS -eq "Windows_NT") {
        if (!(Test-Path -LiteralPath $installPs1)) { throw "Missing GPT-SoVITS installer: $installPs1" }
        if (!(Get-Command conda -ErrorAction SilentlyContinue)) {
            Write-Warning "conda was not found; GPT-SoVITS official installer requires conda. Install conda/micromamba or run upstream install manually for $($Repo.name)."
            return
        }
        Invoke-WithSourceFallback -Sources $SourceFallbacks -Description "GPT-SoVITS install for $($Repo.name)" -Action {
            param($CandidateSource)
            Invoke-Logged "powershell" @("-ExecutionPolicy", "Bypass", "-File", $installPs1, "-Device", $Device, "-Source", $CandidateSource) $repoPath
        }
    } else {
        if (!(Test-Path -LiteralPath $installSh)) { throw "Missing GPT-SoVITS installer: $installSh" }
        if (!(Get-Command conda -ErrorAction SilentlyContinue)) {
            Write-Warning "conda was not found; GPT-SoVITS official installer requires conda. Install conda/micromamba or run upstream install manually for $($Repo.name)."
            return
        }
        Invoke-WithSourceFallback -Sources $SourceFallbacks -Description "GPT-SoVITS install for $($Repo.name)" -Action {
            param($CandidateSource)
            Invoke-Logged "bash" @($installSh, "--device", $Device, "--source", $CandidateSource) $repoPath
        }
    }
}

function Prepare-IndexTTS {
    param($Repo)
    $repoPath = Join-Path $Root $Repo.path
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    $repoUv = Join-Path $repoPath ".uv-bootstrap\Scripts\uv.exe"
    if (-not $uv -and (Test-Path -LiteralPath $repoUv)) { $uv = $repoUv }
    if (-not $SkipInstall) {
        if ($uv) {
            Invoke-Logged $uv @("sync", "--all-extras") $repoPath
        } else {
            $repoPython = Ensure-Venv $repoPath
            Invoke-Logged $repoPython @("-m", "pip", "install", "-e", ".") $repoPath
        }
    }
    if (-not $SkipDownloads) {
        $repoPython = Resolve-RepoPython $repoPath
        if (!(Test-Path -LiteralPath $repoPython) -and $uv) {
            Invoke-Logged $uv @("sync", "--all-extras") $repoPath
        }
        Invoke-WithSourceFallback -Sources $SourceFallbacks -Description "IndexTTS model download" -Action {
            param($CandidateSource)
            $sourceArg = if ($CandidateSource -eq "ModelScope") { "modelscope" } else { "huggingface" }
            if ($CandidateSource -eq "HF-Mirror") {
                $env:HF_ENDPOINT = "https://hf-mirror.com"
            } else {
                Remove-Item Env:\HF_ENDPOINT -ErrorAction SilentlyContinue
            }
            Invoke-Logged $repoPython @("indextts\cli_v2.py", "download", "--source", $sourceArg, "--model-dir", "checkpoints") $repoPath
        }
        Invoke-Logged $repoPython @("indextts\cli_v2.py", "config", "set", "model_dir", "checkpoints") $repoPath
    }
}

function Prepare-CosyVoice {
    param($Repo)
    $repoPath = Join-Path $Root $Repo.path
    Invoke-Logged "git" @("-C", $repoPath, "submodule", "update", "--init", "--recursive") $Root
    $repoPython = Resolve-RepoPython $repoPath
    if (-not $SkipInstall) {
        $repoPython = Ensure-Venv $repoPath
        Invoke-Logged $repoPython @("-m", "pip", "install", "-r", "requirements.txt") $repoPath
    }
    if (-not $SkipDownloads) {
        $repoPython = Resolve-RepoPython $repoPath
        Invoke-WithSourceFallback -Sources $SourceFallbacks -Description "CosyVoice model download" -Action {
            param($CandidateSource)
            if ($CandidateSource -eq "ModelScope") {
                Remove-Item Env:\HF_ENDPOINT -ErrorAction SilentlyContinue
                $code = "from modelscope import snapshot_download; snapshot_download('iic/CosyVoice-300M', local_dir='pretrained_models/CosyVoice-300M')"
            } else {
                if ($CandidateSource -eq "HF-Mirror") {
                    $env:HF_ENDPOINT = "https://hf-mirror.com"
                } else {
                    Remove-Item Env:\HF_ENDPOINT -ErrorAction SilentlyContinue
                }
                $code = "from huggingface_hub import snapshot_download; snapshot_download('FunAudioLLM/CosyVoice-300M', local_dir='pretrained_models/CosyVoice-300M')"
            }
            Invoke-Logged $repoPython @("-c", $code) $repoPath
        }
    }
}

if ($SyncRepos) {
    $syncArgs = @("scripts\tts_more_deploy.py", "sync-repos")
    if ($CleanRepos) { $syncArgs += "--clean" }
    if ($DryRun) { $syncArgs += "--dry-run" }
    Invoke-Logged $Python $syncArgs $Root
}

$NetworkProfile = Resolve-NetworkProfile
Set-NetworkProfileEnvironment $NetworkProfile
$ResolvedSource = [string]$NetworkProfile.model_source
if (-not $ResolvedSource) { $ResolvedSource = if ($Source -eq "Auto") { "ModelScope" } else { $Source } }
$SourceFallbacks = Get-SourceFallbacks $ResolvedSource
Write-Host "[network] source=$ResolvedSource cache=$($NetworkProfile.cache_root)" -ForegroundColor Cyan

$lock = Get-Content -Raw (Join-Path $Root "repo.lock.json") | ConvertFrom-Json
foreach ($repo in $lock.repositories) {
    if (-not (Test-Target $repo)) { continue }
    switch ($repo.provider_type) {
        "gpt-sovits" { Prepare-GPTSoVITS $repo }
        "indextts" { Prepare-IndexTTS $repo }
        "cosyvoice" { Prepare-CosyVoice $repo }
    }
}

Invoke-Logged $Python @("scripts\tts_more_deploy.py", "render-services", "--profile", "local-all", "--platform", "windows", "--output", "data\local\services.json") $Root
Write-Host "Prepared selected TTS repositories. Rendered data\local\services.json." -ForegroundColor Green
