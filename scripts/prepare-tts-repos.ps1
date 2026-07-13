param(
    [string[]]$Targets = @("default"),
    [ValidateSet("Auto", "ModelScope", "HF", "HF-Mirror")][string]$Source = "Auto",
    [ValidateSet("CU128", "CU126", "CPU", "ROCM", "MPS")][string]$Device = "CU128",
    [ValidateSet("local-all", "app-only", "worker-node")][string]$Profile = "local-all",
    [string]$Topology = "",
    [string]$Node = "",
    [string]$RepoPaths = "",
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

$script:TargetItems = @(Get-WorkerServiceIds)

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
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & $FilePath @Arguments 2>&1 | Out-Host
            $exitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($exitCode -ne 0) { throw "Command failed: $line" }
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

function Get-PackageIndexFallbacks {
    param([string]$PrimaryIndexUrl)
    $ordered = @($PrimaryIndexUrl, "https://mirrors.aliyun.com/pypi/simple", "https://pypi.org/simple")
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

function Set-PackageIndexEnvironment {
    param([string]$IndexUrl)
    if ($IndexUrl) {
        $env:PIP_INDEX_URL = $IndexUrl
        $env:UV_INDEX_URL = $IndexUrl
    } else {
        Remove-Item Env:\PIP_INDEX_URL -ErrorAction SilentlyContinue
        Remove-Item Env:\UV_INDEX_URL -ErrorAction SilentlyContinue
    }
}

function Invoke-WithPackageIndexFallback {
    param(
        [scriptblock]$Action,
        [string[]]$Indexes,
        [string]$Description
    )
    $errors = @()
    foreach ($candidate in $Indexes) {
        Write-Host "[package-index] $Description via $candidate" -ForegroundColor Cyan
        Set-PackageIndexEnvironment $candidate
        try {
            & $Action $candidate
            return
        } catch {
            $errors += "${candidate}: $($_.Exception.Message)"
            Write-Warning "$Description failed via ${candidate}: $($_.Exception.Message)"
            if ($DryRun) { return }
        }
    }
    throw "$Description failed for all package indexes: $($errors -join '; ')"
}

function Test-Target {
    param($Repo)
    if ($TargetItems -contains "all") { return $true }
    if ($TargetItems -contains "default" -and $Repo.default_selected -ne $false) { return $true }
    return (
        $TargetItems -contains $Repo.name -or
        $TargetItems -contains $Repo.provider_type -or
        $TargetItems -contains $Repo.service_id -or
        ($Repo.variant -and $TargetItems -contains $Repo.variant)
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
    Invoke-WithPackageIndexFallback -Indexes $PackageIndexFallbacks -Description "base Python package upgrade" -Action {
        param($IndexUrl)
        Invoke-Logged $venvPython @("-m", "pip", "install", "-U", "pip", "wheel", "setuptools") $RepoPath
    }
    return $venvPython
}

function Install-WorkerRuntime {
    param([string]$RepoPath)
    if ($SkipInstall) { return }
    $repoPython = Resolve-RepoPython $RepoPath
    if (!(Test-Path -LiteralPath $repoPython) -and -not $DryRun) {
        throw "Repository Python environment not found: $repoPython"
    }
    Invoke-WithPackageIndexFallback -Indexes $PackageIndexFallbacks -Description "TTS More worker runtime install" -Action {
        param($IndexUrl)
        Invoke-Logged $repoPython @(
            "-m", "pip", "install",
            "fastapi>=0.115.0",
            "uvicorn[standard]>=0.30.0",
            "pydantic>=2.8.0",
            "python-multipart>=0.0.9"
        ) $RepoPath
    }
}

function Install-CU128TorchRuntime {
    param(
        [string]$RepoPython,
        [string]$RepoPath,
        [string]$TorchVersion
    )
    if ($Device -ne "CU128") { return }
    $indexUrl = "https://download.pytorch.org/whl/cu128"
    Invoke-Logged $RepoPython @(
        "-m", "pip", "install", "--upgrade",
        "torch==${TorchVersion}+cu128",
        "torchaudio==${TorchVersion}+cu128",
        "--index-url", $indexUrl
    ) $RepoPath
    $probe = "import torch; assert torch.version.cuda == '12.8', torch.version.cuda; assert torch.cuda.is_available()"
    Invoke-Logged $RepoPython @("-c", $probe) $RepoPath
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
    $repoPython = Ensure-Venv $repoPath
    if ($IsWindows -or $env:OS -eq "Windows_NT") {
        if (!(Test-Path -LiteralPath $installPs1)) { throw "Missing GPT-SoVITS installer: $installPs1" }
        if (!(Get-Command conda -ErrorAction SilentlyContinue)) {
            throw "conda was not found; GPT-SoVITS official installer requires conda for $($Repo.name)."
        }
        Invoke-WithPackageIndexFallback -Indexes $PackageIndexFallbacks -Description "GPT-SoVITS torchcodec bootstrap" -Action {
            param($IndexUrl)
            Invoke-Logged $repoPython @("-m", "pip", "install", "--no-deps", "torchcodec==0.13") $repoPath
        }
        $previousPath = $env:PATH
        $env:PATH = "$(Split-Path -Parent $repoPython);$previousPath"
        try {
            Invoke-WithSourceFallback -Sources $SourceFallbacks -Description "GPT-SoVITS install for $($Repo.name)" -Action {
                param($CandidateSource)
                Invoke-Logged "powershell" @("-ExecutionPolicy", "Bypass", "-File", $installPs1, "-Device", $Device, "-Source", $CandidateSource) $repoPath
            }
        } finally {
            $env:PATH = $previousPath
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

function Install-IndexTTSModelScopeAuxiliaryModels {
    param(
        [string]$RepoPython,
        [string]$RepoPath
    )
    $code = @'
from hashlib import sha256
from pathlib import Path
from shutil import copy2

from modelscope.hub.file_download import model_file_download

resources = (
    ('AI-ModelScope/w2v-bert-2.0', 'config.json', 'checkpoints/hf_cache/w2v-bert-2.0/config.json', None),
    ('AI-ModelScope/w2v-bert-2.0', 'preprocessor_config.json', 'checkpoints/hf_cache/w2v-bert-2.0/preprocessor_config.json', None),
    ('AI-ModelScope/w2v-bert-2.0', 'model.safetensors', 'checkpoints/hf_cache/w2v-bert-2.0/model.safetensors', 'eb890c9660ed6e3414b6812e27257b8ce5454365d5490d3ad581ea60b93be043'),
    ('amphion/MaskGCT', 'semantic_codec/model.safetensors', 'checkpoints/hf_cache/semantic_codec_model.safetensors', None),
    ('iic/speech_campplus_sv_zh-cn_16k-common', 'campplus_cn_common.bin', 'checkpoints/hf_cache/campplus_cn_common.bin', None),
    ('nv-community/bigvgan_v2_22khz_80band_256x', 'config.json', 'checkpoints/hf_cache/bigvgan/config.json', None),
    ('nv-community/bigvgan_v2_22khz_80band_256x', 'bigvgan_generator.pt', 'checkpoints/hf_cache/bigvgan/bigvgan_generator.pt', 'e95ba25972d3de0628d99cd156e9315a9c018899bf739988959ebe3544080ced'),
)


def file_hash(path):
    digest = sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


for repo_id, remote_file, destination, expected_hash in resources:
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size > 0:
        if expected_hash is None or file_hash(target) == expected_hash:
            print(f'[skip] verified IndexTTS auxiliary resource: {target}', flush=True)
            continue
        target.unlink()
    print(f'[download] {repo_id}/{remote_file}', flush=True)
    downloaded = Path(model_file_download(model_id=repo_id, file_path=remote_file, local_dir=str(target.parent)))
    if downloaded.resolve() != target.resolve():
        copy2(downloaded, target)
    if not target.is_file() or target.stat().st_size <= 0:
        raise RuntimeError(f'IndexTTS auxiliary resource is missing after download: {target}')
    if expected_hash is not None and file_hash(target) != expected_hash:
        raise RuntimeError(f'IndexTTS auxiliary resource hash mismatch: {target}')
'@
    Invoke-Logged $RepoPython @("-c", $code) $RepoPath
}

function Prepare-IndexTTS {
    param($Repo)
    $repoPath = Join-Path $Root $Repo.path
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    $repoUv = Join-Path $repoPath ".uv-bootstrap\Scripts\uv.exe"
    if (-not $uv -and (Test-Path -LiteralPath $repoUv)) { $uv = $repoUv }
    if (-not $SkipInstall) {
        if ($uv) {
            Invoke-WithPackageIndexFallback -Indexes $PackageIndexFallbacks -Description "IndexTTS dependency install" -Action {
                param($IndexUrl)
                Invoke-Logged $uv @("sync", "--all-extras") $repoPath
            }
        } else {
            $repoPython = Ensure-Venv $repoPath
            Invoke-WithPackageIndexFallback -Indexes $PackageIndexFallbacks -Description "IndexTTS editable install" -Action {
                param($IndexUrl)
                Invoke-Logged $repoPython @("-m", "pip", "install", "-e", ".") $repoPath
            }
        }
        $repoPython = Resolve-RepoPython $repoPath
        Install-CU128TorchRuntime $repoPython $repoPath "2.8.0"
    }
    if (-not $SkipDownloads) {
        $repoPython = Resolve-RepoPython $repoPath
        if (!(Test-Path -LiteralPath $repoPython) -and $uv) {
            Invoke-WithPackageIndexFallback -Indexes $PackageIndexFallbacks -Description "IndexTTS dependency install" -Action {
                param($IndexUrl)
                Invoke-Logged $uv @("sync", "--all-extras") $repoPath
            }
        }
        Invoke-WithSourceFallback -Sources $SourceFallbacks -Description "IndexTTS model download" -Action {
            param($CandidateSource)
            $sourceArg = if ($CandidateSource -eq "ModelScope") { "modelscope" } else { "huggingface" }
            if ($CandidateSource -eq "ModelScope") {
                Install-IndexTTSModelScopeAuxiliaryModels $repoPython $repoPath
            }
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

function Get-CosyVoiceRequirementsWithoutTorch {
    param([string]$RepoPath)
    $requirementsPath = Join-Path $RepoPath "requirements.txt"
    return @(
        Get-Content -LiteralPath $requirementsPath |
            ForEach-Object { $_.Trim() } |
            Where-Object {
                $_ -and
                -not $_.StartsWith("#") -and
                -not $_.StartsWith("--") -and
                $_ -notmatch '^(torch|torchaudio)=='
            }
    )
}

function Prepare-CosyVoice {
    param($Repo)
    $repoPath = Join-Path $Root $Repo.path
    Invoke-Logged "git" @("-C", $repoPath, "submodule", "update", "--init", "--recursive") $Root
    $repoPython = Resolve-RepoPython $repoPath
    if (-not $SkipInstall) {
        $repoPython = Ensure-Venv $repoPath
        Invoke-WithPackageIndexFallback -Indexes $PackageIndexFallbacks -Description "CosyVoice legacy Whisper bootstrap" -Action {
            param($IndexUrl)
            Invoke-Logged $repoPython @("-m", "pip", "install", "setuptools<81") $repoPath
            Invoke-Logged $repoPython @(
                "-m", "pip", "install",
                "--no-build-isolation", "--no-deps", "openai-whisper==20231117"
            ) $repoPath
        }
        Invoke-WithPackageIndexFallback -Indexes $PackageIndexFallbacks -Description "CosyVoice dependency install" -Action {
            param($IndexUrl)
            if ($Device -eq "CU128") {
                $cosyRequirements = @(Get-CosyVoiceRequirementsWithoutTorch $repoPath)
                Invoke-Logged $repoPython (@("-m", "pip", "install") + $cosyRequirements) $repoPath
            } else {
                Invoke-Logged $repoPython @("-m", "pip", "install", "-r", "requirements.txt") $repoPath
            }
        }
        Install-CU128TorchRuntime $repoPython $repoPath "2.7.1"
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
    $syncArgs = @("scripts\tts_more_deploy.py", "sync-repos", "--service-ids", ($TargetItems -join ","))
    if ($RepoPaths) { $syncArgs += @("--repo-paths", $RepoPaths) }
    if ($CleanRepos) { $syncArgs += "--clean" }
    if ($DryRun) { $syncArgs += "--dry-run" }
    Invoke-Logged $Python $syncArgs $Root
}

$NetworkProfile = Resolve-NetworkProfile
Set-NetworkProfileEnvironment $NetworkProfile
$ResolvedSource = [string]$NetworkProfile.model_source
if (-not $ResolvedSource) { $ResolvedSource = if ($Source -eq "Auto") { "ModelScope" } else { $Source } }
$SourceFallbacks = Get-SourceFallbacks $ResolvedSource
$PackageIndexFallbacks = Get-PackageIndexFallbacks ([string]$NetworkProfile.pip_index_url)
Write-Host "[network] source=$ResolvedSource cache=$($NetworkProfile.cache_root)" -ForegroundColor Cyan

if ($RepoPaths) {
    $repoArgs = @((Join-Path $Root "scripts\tts_more_deploy.py"), "--root", $Root, "list-repos", "--service-ids", ($TargetItems -join ","), "--repo-paths", $RepoPaths)
    $reposJson = & $Python @repoArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $parsedRepositories = $reposJson | ConvertFrom-Json
    $repositories = @($parsedRepositories)
} else {
    $lock = Get-Content -Raw (Join-Path $Root "repo.lock.json") | ConvertFrom-Json
    $repositories = @($lock.repositories)
}
foreach ($repo in $repositories) {
    if (-not (Test-Target $repo)) { continue }
    $repoPath = Join-Path $Root $repo.path
    switch ($repo.provider_type) {
        "gpt-sovits" { Prepare-GPTSoVITS $repo }
        "indextts" { Prepare-IndexTTS $repo }
        "cosyvoice" { Prepare-CosyVoice $repo }
    }
    Install-WorkerRuntime $repoPath
}

$renderArgs = @("scripts\tts_more_deploy.py", "render-services", "--profile", $Profile, "--platform", "windows", "--service-ids", ($TargetItems -join ","), "--output", "data\local\services.json")
if ($RepoPaths) { $renderArgs += @("--repo-paths", $RepoPaths) }
if ($Topology) { $renderArgs += @("--topology", $Topology) }
if ($Node) { $renderArgs += @("--node", $Node) }
Invoke-Logged $Python $renderArgs $Root
Write-Host "Prepared selected TTS repositories. Rendered data\local\services.json." -ForegroundColor Green
