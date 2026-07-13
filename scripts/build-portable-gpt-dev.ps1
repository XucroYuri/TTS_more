[CmdletBinding()]
param(
    [ValidateSet("ModelScope", "HF", "HF-Mirror")][string]$Source = "ModelScope",
    [string]$WorkRoot = "packaging/portable/work",
    [string]$OutputRoot = "artifacts/portable",
    [switch]$DryRun,
    [switch]$ReuseRuntime
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:RepoRoot = Split-Path -Parent $PSScriptRoot
# Required lock identity: {"variant": "dev", "branch": "dev"}.
# The locked worker runtime requires onnxruntime-gpu==1.26.0 for CUDA 12.8.

function Resolve-RepoPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $script:RepoRoot $Path))
}

function Assert-ChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $prefix = $resolvedRoot + [System.IO.Path]::DirectorySeparatorChar
    if (!($resolvedPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase))) {
        throw "Refusing to modify a path outside the approved root: $resolvedPath"
    }
}

function Reset-BuildDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root
    )

    Assert-ChildPath -Path $Path -Root $Root
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][scriptblock]$Command
    )

    Write-Host "[portable-gpt] $Description" -ForegroundColor Cyan
    # Surface child-process output to the console without allowing it to become
    # this function's return value when Invoke-Checked is used inside a helper.
    & $Command | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

function Get-GptDevLock {
    $lockPath = Join-Path $script:RepoRoot "repo.lock.json"
    $lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
    $matches = @($lock.repositories | Where-Object { $_.provider_type -eq "gpt-sovits" -and $_.variant -eq "dev" })
    if ($matches.Count -ne 1) {
        throw "repo.lock.json must contain exactly one GPT-SoVITS dev entry"
    }
    $entry = $matches[0]
    foreach ($field in @("remote", "branch", "commit", "path", "port")) {
        if ([string]::IsNullOrWhiteSpace([string]$entry.$field)) {
            throw "GPT-SoVITS dev lock field is missing: $field"
        }
    }
    return $entry
}

function Ensure-LockedSource {
    param(
        [Parameter(Mandatory = $true)]$Lock,
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$WorkRoot
    )

    Assert-ChildPath -Path $SourcePath -Root $WorkRoot
    if (!(Test-Path -LiteralPath (Join-Path $SourcePath ".git"))) {
        Invoke-Checked "clone GPT-SoVITS dev source" {
            & git clone --filter=blob:none --branch $Lock.branch --single-branch $Lock.remote $SourcePath
        }
    }
    $origin = (& git -C $SourcePath remote get-url origin).Trim()
    if ($origin -ne $Lock.remote) {
        throw "GPT-SoVITS source remote does not match repo.lock.json: $origin"
    }
    Invoke-Checked "fetch locked GPT-SoVITS dev commit" { & git -C $SourcePath fetch --tags origin $Lock.branch }
    Invoke-Checked "checkout locked GPT-SoVITS dev commit" { & git -C $SourcePath checkout --detach $Lock.commit }
    $head = (& git -C $SourcePath rev-parse HEAD).Trim()
    if ($head -ne $Lock.commit) {
        throw "GPT-SoVITS source checkout is not the locked commit: $head"
    }
}

function Write-Utf8File {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $encoding = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Ensure-FullSharedFFmpeg {
    param([Parameter(Mandatory = $true)][string]$UpstreamRoot)

    $sharedBin = Join-Path $UpstreamRoot "ffmpeg-shared\bin"
    $ffmpeg = Join-Path $sharedBin "ffmpeg.exe"
    $ffprobe = Join-Path $sharedBin "ffprobe.exe"
    $sharedDll = Get-ChildItem -Path $sharedBin -Filter "avcodec-*.dll" -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ((Test-Path -LiteralPath $ffmpeg) -and (Test-Path -LiteralPath $ffprobe) -and $sharedDll) {
        return
    }
    $archive = Join-Path $UpstreamRoot "ffmpeg-full-shared.zip"
    $extractDir = Join-Path $UpstreamRoot ".tts-more-ffmpeg-tmp"
    try {
        Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "[portable-gpt] download full-shared FFmpeg runtime" -ForegroundColor Cyan
        Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/GyanD/codexffmpeg/releases/download/8.1.2/ffmpeg-8.1.2-full_build-shared.zip" -OutFile $archive -TimeoutSec 900
        Expand-Archive -LiteralPath $archive -DestinationPath $extractDir -Force
        $sourceBin = Get-ChildItem -LiteralPath $extractDir -Directory -Recurse |
            Where-Object { $_.Name -eq "bin" -and (Test-Path -LiteralPath (Join-Path $_.FullName "ffmpeg.exe")) } |
            Select-Object -First 1
        if ($null -eq $sourceBin) {
            throw "full-shared FFmpeg archive does not contain ffmpeg.exe"
        }
        New-Item -ItemType Directory -Path $sharedBin -Force | Out-Null
        Copy-Item -Path (Join-Path $sourceBin.FullName "*") -Destination $sharedBin -Recurse -Force
        Copy-Item -LiteralPath (Join-Path $sharedBin "ffmpeg.exe") -Destination (Join-Path $UpstreamRoot "ffmpeg.exe") -Force
        Copy-Item -LiteralPath (Join-Path $sharedBin "ffprobe.exe") -Destination (Join-Path $UpstreamRoot "ffprobe.exe") -Force
        $sharedDll = Get-ChildItem -Path $sharedBin -Filter "avcodec-*.dll" -File -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $sharedDll) {
            throw "full-shared FFmpeg DLLs were not found in $sharedBin"
        }
    } finally {
        Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Get-GptModelUrls {
    param([Parameter(Mandatory = $true)][string]$Source)

    switch ($Source) {
        "HF" { $base = "https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/resolve/main" }
        "HF-Mirror" { $base = "https://hf-mirror.com/XXXXRT/GPT-SoVITS-Pretrained/resolve/main" }
        "ModelScope" { $base = "https://www.modelscope.cn/models/XXXXRT/GPT-SoVITS-Pretrained/resolve/master" }
        default { throw "unsupported GPT model source: $Source" }
    }
    return [PSCustomObject]@{
        Pretrained = "$base/pretrained_models.zip"
        G2PW = "$base/G2PWModel.zip"
        Nltk = "$base/nltk_data.zip"
        OpenJTalk = "$base/open_jtalk_dic_utf_8-1.11.tar.gz"
    }
}

function Get-GptSourceFallbacks {
    param([Parameter(Mandatory = $true)][string]$Source)

    return @($Source, "ModelScope", "HF-Mirror", "HF" | Select-Object -Unique)
}

function Invoke-GptDownload {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    Write-Host "[portable-gpt] download $Url" -ForegroundColor Cyan
    $arguments = @("--fail", "--location", "--silent", "--show-error", "--connect-timeout", "15", "--speed-time", "60", "--speed-limit", "1024", "--range", "0-")
    $proxy = $env:HTTPS_PROXY
    if ([string]::IsNullOrWhiteSpace($proxy)) {
        $proxy = $env:HTTP_PROXY
    }
    if (![string]::IsNullOrWhiteSpace($proxy)) {
        $arguments += @("--proxy", $proxy)
    }
    $arguments += @("--output", $Destination, $Url)
    Invoke-Checked "download GPT payload" {
        & curl.exe @arguments
    }
}

function Test-ZipArchive {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
        $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
        try {
            return $archive.Entries.Count -gt 0
        } finally {
            $archive.Dispose()
        }
    } catch {
        return $false
    }
}

function Invoke-GptDownloadWithFallback {
    param(
        [Parameter(Mandatory = $true)][string]$Asset,
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [switch]$ZipArchive
    )

    $partial = "$Destination.partial"
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        $existing = Get-Item -LiteralPath $Destination
        if ((!$ZipArchive -and $existing.Length -gt 0) -or ($ZipArchive -and (Test-ZipArchive -Path $Destination))) {
            Write-Host "[portable-gpt] reuse validated GPT payload: $Destination" -ForegroundColor Green
            return
        }
        Remove-Item -LiteralPath $Destination -Force
    }
    foreach ($candidate in Get-GptSourceFallbacks -Source $Source) {
        $urls = Get-GptModelUrls -Source $candidate
        $url = switch ($Asset) {
            "pretrained_models.zip" { $urls.Pretrained }
            "G2PWModel.zip" { $urls.G2PW }
            "nltk_data.zip" { $urls.Nltk }
            "open_jtalk_dic_utf_8-1.11.tar.gz" { $urls.OpenJTalk }
            default { throw "unsupported GPT download asset: $Asset" }
        }
        Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
        try {
            Invoke-GptDownload -Url $url -Destination $partial
            if ($ZipArchive -and !(Test-ZipArchive -Path $partial)) {
                throw "downloaded payload is not a valid ZIP archive"
            }
            Move-Item -LiteralPath $partial -Destination $Destination -Force
            Write-Host "[portable-gpt] downloaded $Asset from $candidate" -ForegroundColor Green
            return
        } catch {
            Write-Warning "GPT download failed from $candidate for ${Asset}: $($_.Exception.Message)"
        } finally {
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
        }
    }
    throw "All configured sources failed for GPT payload: $Asset"
}

function Install-GptModelPayloads {
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$ModelSource
    )

    $gptRoot = Join-Path $SourcePath "GPT_SoVITS"
    $pretrainedReady = Join-Path $gptRoot "pretrained_models\sv"
    if (!(Test-Path -LiteralPath $pretrainedReady)) {
        $archive = Join-Path $SourcePath "pretrained_models.zip"
        try {
            Invoke-GptDownloadWithFallback -Asset "pretrained_models.zip" -Source $ModelSource -Destination $archive -ZipArchive
            Expand-Archive -LiteralPath $archive -DestinationPath $gptRoot -Force
        } finally {
            Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
        }
    }

    $g2pwReady = Join-Path $gptRoot "text\G2PWModel"
    if (!(Test-Path -LiteralPath $g2pwReady)) {
        $archive = Join-Path $SourcePath "G2PWModel.zip"
        try {
            Invoke-GptDownloadWithFallback -Asset "G2PWModel.zip" -Source $ModelSource -Destination $archive -ZipArchive
            Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $gptRoot "text") -Force
        } finally {
            Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
        }
    }

    $prefix = (& $Python -c "import sys; print(sys.prefix)").Trim()
    $nltkReady = Join-Path $prefix "nltk_data"
    if (!(Test-Path -LiteralPath $nltkReady)) {
        $archive = Join-Path $SourcePath "nltk_data.zip"
        try {
            Invoke-GptDownloadWithFallback -Asset "nltk_data.zip" -Source $ModelSource -Destination $archive -ZipArchive
            Expand-Archive -LiteralPath $archive -DestinationPath $prefix -Force
        } finally {
            Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
        }
    }

    $openJtalkTarget = (& $Python -c "import os, pyopenjtalk; print(os.path.dirname(pyopenjtalk.__file__))").Trim()
    $dictionaryReady = Join-Path $openJtalkTarget "open_jtalk_dic_utf_8-1.11"
    if (!(Test-Path -LiteralPath $dictionaryReady)) {
        $archive = Join-Path $SourcePath "open_jtalk_dic_utf_8-1.11.tar.gz"
        try {
            Invoke-GptDownloadWithFallback -Asset "open_jtalk_dic_utf_8-1.11.tar.gz" -Source $ModelSource -Destination $archive
            Invoke-Checked "extract Open JTalk dictionary" { & tar -xzf $archive -C $openJtalkTarget }
        } finally {
            Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
        }
    }
}

function Install-GptRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$PrivateConda,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$RequirementsLock,
        [Parameter(Mandatory = $true)][string]$WorkRoot,
        [Parameter(Mandatory = $true)][string]$ModelSource
    )

    Reset-BuildDirectory -Path $RuntimeRoot -Root $WorkRoot
    Invoke-Checked "create private Python 3.11 GPT runtime" {
        & $PrivateConda create --yes --prefix $RuntimeRoot python=3.11 pip
    }
    Invoke-Checked "install package-local FFmpeg and CMake" {
        & $PrivateConda install --yes --prefix $RuntimeRoot -c conda-forge ffmpeg cmake
    }

    $python = Join-Path $RuntimeRoot "python.exe"
    $cacheRoot = Resolve-RepoPath "data/cache/portable/pip"
    New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null
    $env:PIP_CACHE_DIR = $cacheRoot
    Invoke-Checked "install GPT extra requirements" {
        & $python -m pip install --no-deps -r (Join-Path $SourcePath "extra-req.txt")
    }
    Invoke-Checked "install GPT upstream requirements" {
        & $python -m pip install -r (Join-Path $SourcePath "requirements.txt")
    }
    # Keep the version in the shared lock, but leave ownership of setuptools with
    # Conda.  Pip would otherwise replace Conda's files and conda-pack rejects
    # the resulting environment as inconsistent.
    $pipRequirements = Join-Path $RuntimeRoot "gpt-dev-requirements.pip.txt"
    $pipRequirementLines = Get-Content -LiteralPath $RequirementsLock |
        Where-Object { $_ -notmatch '^\s*setuptools==' }
    Write-Utf8File -Path $pipRequirements -Content (($pipRequirementLines -join "`n") + "`n")
    try {
        Invoke-Checked "pin CUDA 12.8 torch, TorchCodec and worker runtime" {
            & $python -m pip install --upgrade --force-reinstall --no-deps --index-url "https://download.pytorch.org/whl/cu128" --extra-index-url "https://pypi.org/simple" -r $pipRequirements
        }
    } finally {
        Remove-Item -LiteralPath $pipRequirements -Force -ErrorAction SilentlyContinue
    }
    Invoke-Checked "pin Conda-owned setuptools for conda-pack" {
        & $PrivateConda install --yes --prefix $RuntimeRoot -c conda-forge setuptools=80.9.0
    }
    return $python
}

function Copy-UpstreamPayload {
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $excluded = @(".git", ".venv", "venv", "__pycache__", "logs", "output", "TEMP")
    Get-ChildItem -LiteralPath $SourcePath -Force | Where-Object { $excluded -notcontains $_.Name } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
    }
}

function Write-GptRuntimeLauncher {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][int]$Port
    )

    $content = @'
@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..\..") do set "PACKAGE_ROOT=%%~fI"
set "TTS_MORE_GPTSOVITS_REPO=%PACKAGE_ROOT%\upstream\GPT-SoVITS"
set "TTS_MORE_GPTSOVITS_CONFIG=GPT_SoVITS\configs\tts_infer.yaml"
set "TTS_MORE_ARTIFACT_ROOT=%PACKAGE_ROOT%\data\local\artifacts"
set "TTS_MORE_PACKAGE_ROOT=%PACKAGE_ROOT%"
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$root = $env:TTS_MORE_PACKAGE_ROOT; $python = Join-Path $root 'runtime\live\python.exe'; $appDir = Join-Path $root 'app'; $runDir = Join-Path $root 'data\local\run'; New-Item -ItemType Directory -Path $runDir -Force | Out-Null; $arguments = @('-m', 'uvicorn', 'app.workers.gpt_sovits_worker:app', '--app-dir', $appDir, '--host', '127.0.0.1', '--port', '__PORT__'); $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $root -PassThru -WindowStyle Hidden; @{ pid = [int]$process.Id; executable_path = $python; port = __PORT__; started_at = [DateTime]::UtcNow.ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runDir 'worker.pid.json') -Encoding UTF8"
exit /b %errorlevel%
'@
    Write-Utf8File -Path $Path -Content $content.Replace("__PORT__", [string]$Port)
}

function Test-StagedRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)][string]$UpstreamRoot
    )

    $oldPath = $env:PATH
    $oldPythonPath = $env:PYTHONPATH
    $oldRepo = $env:TTS_MORE_GPTSOVITS_REPO
    $env:PATH = "$(Join-Path $UpstreamRoot 'ffmpeg-shared\bin');$oldPath"
    $env:PYTHONPATH = Join-Path $StageRoot "app"
    $env:TTS_MORE_GPTSOVITS_REPO = $UpstreamRoot
    try {
        $probe = "import onnxruntime as ort; import torch; import torchaudio; assert ort.__version__ == '1.26.0'; assert torch.version.cuda == '12.8'; import app.workers.gpt_sovits_worker as worker; worker._bootstrap_repo(); from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config; print('portable GPT runtime import probe passed')"
        Invoke-Checked "probe packaged GPT runtime imports" { & $Python -c $probe }
    } finally {
        $env:PATH = $oldPath
        $env:PYTHONPATH = $oldPythonPath
        $env:TTS_MORE_GPTSOVITS_REPO = $oldRepo
    }
}

function Write-Sha256Sums {
    param([Parameter(Mandatory = $true)][string]$StageRoot)

    $output = Join-Path $StageRoot "SHA256SUMS.txt"
    $lines = Get-ChildItem -LiteralPath $StageRoot -File -Recurse |
        Where-Object { $_.FullName -ne $output } |
        Sort-Object FullName |
        ForEach-Object {
            $relative = $_.FullName.Substring($StageRoot.Length).TrimStart([char]'\', [char]'/').Replace("\\", "/")
            "$(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256 | Select-Object -ExpandProperty Hash) *$relative"
        }
    Write-Utf8File -Path $output -Content (($lines -join "`n") + "`n")
}

function Get-Private7Zip {
    param([Parameter(Mandatory = $true)][string]$PrivateConda)

    Invoke-Checked "install private ZIP64 packer" { & $PrivateConda install --yes --name base 7zip }
    $condaRoot = Split-Path -Parent (Split-Path -Parent $PrivateConda)
    $candidates = @(
        (Join-Path $condaRoot "bin\7z.exe"),
        (Join-Path $condaRoot "bin\7zz.exe"),
        (Join-Path $condaRoot "Library\bin\7z.exe"),
        (Join-Path $condaRoot "Library\bin\7zz.exe"),
        (Join-Path $condaRoot "Scripts\7z.exe")
    )
    $sevenZip = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if ($null -eq $sevenZip) {
        throw "Private Conda did not provide a ZIP64-capable 7-Zip executable"
    }
    return $sevenZip
}

function New-Zip64Package {
    param(
        [Parameter(Mandatory = $true)][string]$SevenZip,
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)][string]$OutputPath,
        [Parameter(Mandatory = $true)][string]$OutputRoot
    )

    Assert-ChildPath -Path $OutputPath -Root $OutputRoot
    New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
    Remove-Item -LiteralPath $OutputPath -Force -ErrorAction SilentlyContinue
    Push-Location $StageRoot
    try {
        Invoke-Checked "create ZIP64 portable package" { & $SevenZip a -tzip -mx=5 $OutputPath ".\*" }
    } finally {
        Pop-Location
    }
}

$lock = Get-GptDevLock
$work = Resolve-RepoPath $WorkRoot
$output = Resolve-RepoPath $OutputRoot
$sourcePath = Join-Path $work "GPT-SoVITS-dev-source"
$runtimeRoot = Join-Path $work "GPT-SoVITS-dev-runtime"
$stageRoot = Join-Path $work "GPT-SoVITS-dev-stage"
$packagePath = Join-Path $output "GPT-SoVITS-dev-win-x64.zip"
$bootstrap = Join-Path $script:RepoRoot "scripts\bootstrap-conda.ps1"
$requirements = Join-Path $script:RepoRoot "packaging\portable\gpt-dev-requirements.lock.txt"

if ($DryRun) {
    Write-Host "[dry-run] locked source: $($lock.remote)@$($lock.commit)" -ForegroundColor Cyan
    Write-Host "[dry-run] worker endpoint: http://127.0.0.1:$($lock.port)" -ForegroundColor Cyan
    Write-Host "[dry-run] runtime archive: runtime/runtime.zip" -ForegroundColor Cyan
    Write-Host "[dry-run] output package: $packagePath" -ForegroundColor Cyan
    & $bootstrap -DryRun
    $bootstrapExitCode = Get-Variable -Name LASTEXITCODE -ValueOnly -ErrorAction SilentlyContinue
    if ($null -ne $bootstrapExitCode -and $bootstrapExitCode -ne 0) {
        exit $bootstrapExitCode
    }
    exit 0
}

New-Item -ItemType Directory -Path $work -Force | Out-Null
Ensure-LockedSource -Lock $lock -SourcePath $sourcePath -WorkRoot $work
$privateConda = (& $bootstrap -PassThru)
if (!(Test-Path -LiteralPath $privateConda -PathType Leaf)) {
    throw "Private Conda bootstrap did not return conda.bat"
}
$env:CONDA_PKGS_DIRS = Join-Path (Resolve-RepoPath "data/cache/portable/conda") "conda-pkgs"
if ($ReuseRuntime) {
    $python = Join-Path $runtimeRoot "python.exe"
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Cannot reuse a missing private GPT runtime: $runtimeRoot"
    }
    Write-Host "[portable-gpt] reuse existing private GPT runtime" -ForegroundColor Cyan
} else {
    $python = Install-GptRuntime -PrivateConda $privateConda -RuntimeRoot $runtimeRoot -SourcePath $sourcePath -RequirementsLock $requirements -WorkRoot $work -ModelSource $Source
}
Install-GptModelPayloads -SourcePath $sourcePath -Python $python -ModelSource $Source

Reset-BuildDirectory -Path $stageRoot -Root $work
$upstreamStage = Join-Path $stageRoot "upstream\GPT-SoVITS"
Copy-UpstreamPayload -SourcePath $sourcePath -Destination $upstreamStage
Ensure-FullSharedFFmpeg -UpstreamRoot $upstreamStage

$appStage = Join-Path $stageRoot "app"
New-Item -ItemType Directory -Path (Join-Path $appStage "scripts") -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $script:RepoRoot "backend\app") -Destination (Join-Path $appStage "app") -Recurse -Force
Copy-Item -LiteralPath (Join-Path $script:RepoRoot "scripts\portable_launcher.py") -Destination (Join-Path $appStage "scripts\portable_launcher.py") -Force
Copy-Item -LiteralPath (Join-Path $script:RepoRoot "scripts\portable_packages.py") -Destination (Join-Path $appStage "scripts\portable_packages.py") -Force
Copy-Item -LiteralPath (Join-Path $script:RepoRoot "deployment\portable\gpt-sovits-dev\Start.cmd") -Destination (Join-Path $stageRoot "Start.cmd") -Force
Copy-Item -LiteralPath (Join-Path $script:RepoRoot "deployment\portable\gpt-sovits-dev\Stop.cmd") -Destination (Join-Path $stageRoot "Stop.cmd") -Force

$manifest = Get-Content -LiteralPath (Join-Path $script:RepoRoot "deployment\portable\gpt-sovits-dev\package\tts-more-package.json") -Raw | ConvertFrom-Json
$manifest.build_id = "gpt-sovits-dev-" + $lock.commit.Substring(0, 12)
Write-Utf8File -Path (Join-Path $stageRoot "package\tts-more-package.json") -Content ($manifest | ConvertTo-Json -Depth 8)

Write-GptRuntimeLauncher -Path (Join-Path $runtimeRoot "Start-Worker-Runtime.cmd") -Port ([int]$lock.port)
Test-StagedRuntime -Python $python -StageRoot $stageRoot -UpstreamRoot $upstreamStage

$runtimeArchive = Join-Path $stageRoot "runtime\runtime.zip"
New-Item -ItemType Directory -Path (Split-Path -Parent $runtimeArchive) -Force | Out-Null
$condaPack = Join-Path $runtimeRoot "Scripts\conda-pack.exe"
if (!(Test-Path -LiteralPath $condaPack -PathType Leaf)) {
    throw "conda-pack executable is missing from the private runtime: $condaPack"
}
Invoke-Checked "create relocatable runtime.zip" { & $condaPack --prefix $runtimeRoot --format zip --output $runtimeArchive }
$sevenZip = Get-Private7Zip -PrivateConda $privateConda
Push-Location $runtimeRoot
try {
    Invoke-Checked "add worker launcher to runtime.zip" { & $sevenZip a -tzip $runtimeArchive "Start-Worker-Runtime.cmd" }
} finally {
    Pop-Location
}
Invoke-Checked "validate staged portable manifest" { & $python (Join-Path $appStage "scripts\portable_packages.py") validate-manifest --manifest (Join-Path $stageRoot "package\tts-more-package.json") --package-root $stageRoot }
Write-Sha256Sums -StageRoot $stageRoot

New-Zip64Package -SevenZip $sevenZip -StageRoot $stageRoot -OutputPath $packagePath -OutputRoot $output
Write-Host "[portable-gpt] created: $packagePath" -ForegroundColor Green
