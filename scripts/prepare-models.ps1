param(
    [ValidateSet("ModelScope", "HF", "HF-Mirror")]
    [string]$Source = "ModelScope",
    [ValidateSet("CU128", "CU126", "CPU")]
    [string]$Device = "CU128",
    [string]$Python = "",
    [switch]$SkipInstall,
    [switch]$SkipGPTSoVITS,
    [switch]$SkipIndexTTS,
    [switch]$SkipDownloads
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")

function Write-Step($Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok($Message) {
    Write-Host "[ok] $Message" -ForegroundColor Green
}

function Write-Warn($Message) {
    Write-Host "[warn] $Message" -ForegroundColor Yellow
}

function Resolve-BasePython {
    if ($Python -and (Test-Path $Python)) {
        return (Resolve-Path $Python).Path
    }
    if ($env:TTS_MORE_BASE_PYTHON -and (Test-Path $env:TTS_MORE_BASE_PYTHON)) {
        return (Resolve-Path $env:TTS_MORE_BASE_PYTHON).Path
    }
    $uvPython = Join-Path $env:APPDATA "uv\python\cpython-3.10.20-windows-x86_64-none\python.exe"
    if (Test-Path $uvPython) {
        return $uvPython
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        return "py -3.10"
    }
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        return $pythonCmd.Source
    }
    throw "Python 3.10 was not found. Set TTS_MORE_BASE_PYTHON to a Python 3.10 executable."
}

function Invoke-Python {
    param(
        [string]$PythonCommand,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $ProjectRoot
    )
    Push-Location $WorkingDirectory
    try {
        if ($PythonCommand -like "py *") {
            $parts = $PythonCommand.Split(" ", 2)
            & $parts[0] $parts[1] @Arguments
        } else {
            & $PythonCommand @Arguments
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Python command failed: $PythonCommand $($Arguments -join ' ')"
        }
    } finally {
        Pop-Location
    }
}

function Ensure-Venv {
    param(
        [string]$RepoPath,
        [string]$BasePython
    )
    $venvPython = Join-Path $RepoPath ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Write-Step "Creating venv: $RepoPath\.venv"
        Invoke-Python -PythonCommand $BasePython -Arguments @("-m", "venv", ".venv") -WorkingDirectory $RepoPath
    }
    $pipOutput = & $venvPython -m pip install -U pip wheel
    $pipOutput | Out-Host
    $setuptoolsOutput = & $venvPython -m pip install "setuptools<82"
    $setuptoolsOutput | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upgrade pip in $RepoPath"
    }
    return (Resolve-Path $venvPython).Path
}

function Install-Torch {
    param(
        [string]$PythonExe,
        [string]$DeviceName,
        [string[]]$Extra = @(),
        [switch]$Force,
        [string]$TorchPackage = "torch",
        [string]$TorchaudioPackage = "torchaudio"
    )
    $installArgs = @("-m", "pip", "install", $TorchPackage, $TorchaudioPackage)
    if ($Force) {
        $installArgs += "--force-reinstall"
    }
    if ($DeviceName -eq "CPU") {
        & $PythonExe @($installArgs + @("--index-url", "https://download.pytorch.org/whl/cpu"))
    } elseif ($DeviceName -eq "CU126") {
        & $PythonExe @($installArgs + @("--index-url", "https://download.pytorch.org/whl/cu126"))
    } else {
        & $PythonExe @($installArgs + @("--index-url", "https://download.pytorch.org/whl/cu128"))
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install PyTorch for $DeviceName"
    }
    if ($Extra.Count -gt 0) {
        & $PythonExe -m pip install @Extra
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install extra PyTorch packages: $($Extra -join ', ')"
        }
    }
}

function Invoke-Download {
    param(
        [string]$Uri,
        [string]$OutFile,
        [int]$TimeoutSec = 300
    )
    if (Test-Path $OutFile) {
        $existing = Get-Item $OutFile
        if ($existing.Length -gt 0) {
            Write-Ok "Already downloaded $OutFile"
            return
        }
        Remove-Item $OutFile -Force
    }
    Write-Host "Downloading $Uri"
    Invoke-WebRequest -Uri $Uri -OutFile $OutFile -MaximumRedirection 10 -TimeoutSec $TimeoutSec
    $downloaded = Get-Item $OutFile -ErrorAction SilentlyContinue
    if (-not $downloaded -or $downloaded.Length -eq 0) {
        throw "Downloaded file is empty: $Uri"
    }
}

function Test-IndexTTSBigVGANReady {
    param([string]$ModelDir)
    $bigvganDir = Join-Path $ModelDir "hf_cache\bigvgan"
    $configPath = Join-Path $bigvganDir "config.json"
    $weightsPath = Join-Path $bigvganDir "bigvgan_generator.pt"
    if (-not (Test-Path $configPath) -or -not (Test-Path $weightsPath)) {
        return $false
    }
    $config = Get-Item $configPath
    $weights = Get-Item $weightsPath
    return $config.Length -gt 100 -and $weights.Length -gt 1MB
}

function Prepare-IndexTTSBigVGAN {
    param([string]$ModelDir)

    if (Test-IndexTTSBigVGANReady -ModelDir $ModelDir) {
        Write-Ok "IndexTTS BigVGAN already prepared"
        return
    }

    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) {
        throw "git is required to fetch IndexTTS BigVGAN from the Gitee HF mirror"
    }
    & git lfs version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "git-lfs is required to fetch IndexTTS BigVGAN. Install git-lfs and retry."
    }

    Write-Step "Downloading IndexTTS BigVGAN from Gitee HF mirror"
    $bigvganDir = Join-Path $ModelDir "hf_cache\bigvgan"
    New-Item -ItemType Directory -Force -Path $bigvganDir | Out-Null

    $tempRoot = [System.IO.Path]::GetTempPath()
    $tmp = Join-Path $tempRoot ("tts-more-bigvgan-" + [guid]::NewGuid().ToString("N"))
    $previousSkipSmudge = $env:GIT_LFS_SKIP_SMUDGE
    try {
        $env:GIT_LFS_SKIP_SMUDGE = "1"
        & git clone --depth 1 "https://gitee.com/hf-models/bigvgan_v2_22khz_80band_256x.git" $tmp
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to clone BigVGAN mirror from Gitee"
        }

        if ($null -eq $previousSkipSmudge) {
            Remove-Item Env:\GIT_LFS_SKIP_SMUDGE -ErrorAction SilentlyContinue
        } else {
            $env:GIT_LFS_SKIP_SMUDGE = $previousSkipSmudge
        }
        & git -C $tmp lfs pull --include="bigvgan_generator.pt" --exclude=""
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to pull BigVGAN LFS weights from Gitee"
        }

        foreach ($file in @("config.json", "bigvgan_generator.pt")) {
            $src = Join-Path $tmp $file
            if (-not (Test-Path $src)) {
                throw "BigVGAN mirror is missing $file"
            }
            Copy-Item -Path $src -Destination (Join-Path $bigvganDir $file) -Force
        }
    } finally {
        if ($null -eq $previousSkipSmudge) {
            Remove-Item Env:\GIT_LFS_SKIP_SMUDGE -ErrorAction SilentlyContinue
        } else {
            $env:GIT_LFS_SKIP_SMUDGE = $previousSkipSmudge
        }
        if (Test-Path $tmp) {
            $resolvedTmp = (Resolve-Path $tmp).Path
            $resolvedTempRoot = (Resolve-Path $tempRoot).Path.TrimEnd("\")
            if ($resolvedTmp.StartsWith($resolvedTempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                Remove-Item $resolvedTmp -Recurse -Force
            } else {
                Write-Warn "Refusing to remove unexpected temp path: $resolvedTmp"
            }
        }
    }

    if (-not (Test-IndexTTSBigVGANReady -ModelDir $ModelDir)) {
        throw "IndexTTS BigVGAN files are incomplete in $bigvganDir"
    }
    Write-Ok "IndexTTS BigVGAN prepared"
}

function Expand-ZipOnce {
    param(
        [string]$ZipPath,
        [string]$Destination
    )
    Expand-Archive -Path $ZipPath -DestinationPath $Destination -Force
    Remove-Item $ZipPath -Force
}

function Test-GPTPretrainedReady {
    param([string]$PretrainedDir)
    $required = @(
        "s1v3.ckpt",
        "v2Pro\s2Gv2ProPlus.pth",
        "v2Pro\s2Dv2ProPlus.pth",
        "sv\pretrained_eres2netv2w24s4ep4.ckpt",
        "chinese-roberta-wwm-ext-large\config.json",
        "chinese-hubert-base\config.json",
        "fast_langdetect\lid.176.bin"
    )
    foreach ($item in $required) {
        if (-not (Test-Path (Join-Path $PretrainedDir $item))) {
            return $false
        }
    }
    return $true
}

function Download-GPTPretrainedSnapshot {
    param(
        [string]$PythonExe,
        [string]$PretrainedDir
    )
    Write-Warn "Falling back to Hugging Face snapshot download for GPT-SoVITS v2ProPlus resources"
    $downloadCode = @"
import os
from huggingface_hub import snapshot_download
last_error = None
for endpoint in ("https://hf-mirror.com", "https://huggingface.co"):
    os.environ["HF_ENDPOINT"] = endpoint
    try:
        print(f"Using HF endpoint: {endpoint}")
        snapshot_download(
            repo_id="lj1995/GPT-SoVITS",
            local_dir=r"$PretrainedDir",
            allow_patterns=[
                "s1v3.ckpt",
                "v2Pro/s2Gv2ProPlus.pth",
                "v2Pro/s2Dv2ProPlus.pth",
                "sv/pretrained_eres2netv2w24s4ep4.ckpt",
                "chinese-roberta-wwm-ext-large/*",
                "chinese-hubert-base/*",
            ],
        )
        break
    except Exception as exc:
        last_error = exc
        print(f"HF endpoint failed: {endpoint}: {exc}")
else:
    raise last_error
"@
    $downloadCode | & $PythonExe -
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to download GPT-SoVITS pretrained snapshot"
    }
}

function Download-GPTPretrainedModelScope {
    param(
        [string]$PythonExe,
        [string]$RepoPath
    )
    Write-Step "Downloading GPT-SoVITS pretrained resources from ModelScope snapshot"
    $targetRoot = Join-Path $RepoPath "GPT_SoVITS"
    New-Item -ItemType Directory -Force -Path (Join-Path $targetRoot "pretrained_models") | Out-Null
    $downloadCode = @"
from modelscope.hub.snapshot_download import snapshot_download
snapshot_download(
    model_id="XXXXRT/GPT-SoVITS-Pretrained",
    local_dir=r"$targetRoot",
    allow_patterns=[
        "pretrained_models/s1v3.ckpt",
        "pretrained_models/v2Pro/s2Gv2ProPlus.pth",
        "pretrained_models/v2Pro/s2Dv2ProPlus.pth",
        "pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt",
        "pretrained_models/chinese-roberta-wwm-ext-large/*",
        "pretrained_models/chinese-hubert-base/*",
    ],
    max_workers=2,
)
"@
    $downloadCode | & $PythonExe -
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to download GPT-SoVITS pretrained resources from ModelScope"
    }
}

function Download-G2PW {
    param(
        [string]$PythonExe,
        [string]$G2PWDir,
        [string]$SourceBase,
        [string]$RepoPath
    )
    if (Test-Path $G2PWDir) {
        return
    }
    if ($Source -eq "ModelScope") {
        try {
            Write-Step "Downloading G2PWModel from ModelScope"
            $downloadCode = @"
from modelscope.hub.file_download import model_file_download
model_file_download(
    model_id="XXXXRT/GPT-SoVITS-Pretrained",
    file_path="G2PWModel.zip",
    local_dir=r"$RepoPath",
)
"@
            $downloadCode | & $PythonExe -
            if ($LASTEXITCODE -ne 0) {
                throw "ModelScope SDK exited with code $LASTEXITCODE"
            }
            $zipFromSdk = Join-Path $RepoPath "G2PWModel.zip"
            if (-not (Test-Path $zipFromSdk)) {
                $nested = Get-ChildItem -Path $RepoPath -Filter "G2PWModel.zip" -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($nested) { $zipFromSdk = $nested.FullName }
            }
            Expand-ZipOnce -ZipPath $zipFromSdk -Destination (Join-Path $RepoPath "GPT_SoVITS\text")
            if (-not (Test-Path $G2PWDir)) {
                $versioned = Join-Path $RepoPath "GPT_SoVITS\text\G2PWModel_1.1"
                if (Test-Path $versioned) {
                    Rename-Item -Path $versioned -NewName "G2PWModel"
                }
            }
            if (Test-Path $G2PWDir) {
                return
            }
        } catch {
            Write-Warn "G2PW ModelScope SDK download failed: $($_.Exception.Message)"
        }
    }
    $candidates = @(
        "$SourceBase/G2PWModel.zip",
        "https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/resolve/main/G2PWModel.zip",
        "https://hf-mirror.com/XXXXRT/GPT-SoVITS-Pretrained/resolve/main/G2PWModel.zip",
        "https://www.modelscope.cn/models/kamiorinn/g2pw/resolve/master/G2PWModel_1.1.zip"
    )
    $zip = Join-Path $RepoPath "G2PWModel.zip"
    foreach ($uri in $candidates) {
        try {
            if (Test-Path $zip) { Remove-Item $zip -Force }
            Invoke-Download -Uri $uri -OutFile $zip
            Expand-ZipOnce -ZipPath $zip -Destination (Join-Path $RepoPath "GPT_SoVITS\text")
            if (-not (Test-Path $G2PWDir)) {
                $versioned = Join-Path $RepoPath "GPT_SoVITS\text\G2PWModel_1.1"
                if (Test-Path $versioned) {
                    Rename-Item -Path $versioned -NewName "G2PWModel"
                }
            }
            if (Test-Path $G2PWDir) {
                return
            }
        } catch {
            Write-Warn "G2PW download failed from ${uri}: $($_.Exception.Message)"
        }
    }
    throw "Failed to prepare G2PWModel"
}

function Download-FastLangDetect {
    param([string]$PretrainedDir)
    $targetDir = Join-Path $PretrainedDir "fast_langdetect"
    $target = Join-Path $targetDir "lid.176.bin"
    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    if ((Test-Path $target) -and ((Get-Item $target).Length -gt 100MB)) {
        return
    }
    Invoke-Download -Uri "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.bin" -OutFile $target -TimeoutSec 900
}

function Prepare-GPTFFmpeg {
    param([string]$RepoPath)
    $ffmpegTarget = Join-Path $RepoPath "ffmpeg.exe"
    $ffprobeTarget = Join-Path $RepoPath "ffprobe.exe"
    $sharedBin = Join-Path $RepoPath "ffmpeg-shared\bin"
    $sharedDll = Get-ChildItem -Path $sharedBin -Filter "avcodec-*.dll" -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ((Test-Path $ffmpegTarget) -and (Test-Path $ffprobeTarget) -and $sharedDll) {
        return
    }

    $zipPath = Join-Path $RepoPath "ffmpeg-full-shared.zip"
    $extractDir = Join-Path $RepoPath ".ffmpeg-tmp"
    try {
        if (Test-Path $extractDir) { Remove-Item $extractDir -Recurse -Force }
        Invoke-Download -Uri "https://github.com/GyanD/codexffmpeg/releases/download/8.1.2/ffmpeg-8.1.2-full_build-shared.zip" -OutFile $zipPath -TimeoutSec 900
        Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force
        $foundBin = Get-ChildItem -Path $extractDir -Directory -Recurse | Where-Object { $_.Name -eq "bin" -and (Test-Path (Join-Path $_.FullName "ffmpeg.exe")) } | Select-Object -First 1
        if (-not $foundBin) {
            throw "ffmpeg shared bin directory not found in full-shared archive"
        }
        New-Item -ItemType Directory -Force -Path $sharedBin | Out-Null
        Copy-Item -Path (Join-Path $foundBin.FullName "*") -Destination $sharedBin -Recurse -Force
        foreach ($tool in @("ffmpeg.exe", "ffprobe.exe")) {
            Copy-Item -Path (Join-Path $sharedBin $tool) -Destination (Join-Path $RepoPath $tool) -Force
        }
        $sharedDll = Get-ChildItem -Path $sharedBin -Filter "avcodec-*.dll" -File -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $sharedDll) {
            throw "ffmpeg full-shared DLLs were not found in $sharedBin"
        }
    } finally {
        if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
        if (Test-Path $extractDir) { Remove-Item $extractDir -Recurse -Force }
    }
}

function Prepare-GPTSoVITS {
    if ($SkipGPTSoVITS) { return }
    $repo = Join-Path $ProjectRoot "repo\GPT-SoVITS"
    if (-not (Test-Path $repo)) { throw "GPT-SoVITS repo not found: $repo" }
    $pythonExe = Ensure-Venv -RepoPath $repo -BasePython $BasePython

    if ($SkipInstall) {
        Write-Warn "Skipping GPT-SoVITS dependency installation"
    } else {
        Write-Step "Installing GPT-SoVITS dependencies"
        Install-Torch -PythonExe $pythonExe -DeviceName $Device -Extra @("torchcodec")
        & $pythonExe -m pip install -r (Join-Path $repo "requirements.txt")
        if ($LASTEXITCODE -ne 0) { throw "Failed to install GPT-SoVITS requirements" }
    }

    if ($SkipDownloads) {
        Write-Warn "Skipping GPT-SoVITS model downloads"
        return
    }

    Write-Step "Downloading GPT-SoVITS pretrained resources"
    $pretrainedDir = Join-Path $repo "GPT_SoVITS\pretrained_models"
    $g2pwDir = Join-Path $repo "GPT_SoVITS\text\G2PWModel"
    New-Item -ItemType Directory -Force -Path $pretrainedDir | Out-Null
    $base = if ($Source -eq "HF") {
        "https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/resolve/main"
    } elseif ($Source -eq "HF-Mirror") {
        "https://hf-mirror.com/XXXXRT/GPT-SoVITS-Pretrained/resolve/main"
    } else {
        "https://www.modelscope.cn/models/XXXXRT/GPT-SoVITS-Pretrained/resolve/master"
    }
    if (-not (Test-GPTPretrainedReady -PretrainedDir $pretrainedDir)) {
        $zip = Join-Path $repo "pretrained_models.zip"
        if ($Source -eq "ModelScope") {
            Download-GPTPretrainedModelScope -PythonExe $pythonExe -RepoPath $repo
        } else {
            try {
                Invoke-Download -Uri "$base/pretrained_models.zip" -OutFile $zip
                Expand-ZipOnce -ZipPath $zip -Destination (Join-Path $repo "GPT_SoVITS")
            } catch {
                Write-Warn "GPT-SoVITS pretrained zip failed: $($_.Exception.Message)"
                if (Test-Path $zip) { Remove-Item $zip -Force }
                Download-GPTPretrainedSnapshot -PythonExe $pythonExe -PretrainedDir $pretrainedDir
            }
        }
        if (-not (Test-GPTPretrainedReady -PretrainedDir $pretrainedDir)) {
            throw "GPT-SoVITS v2ProPlus pretrained resources are incomplete in $pretrainedDir"
        }
    }
    Download-G2PW -PythonExe $pythonExe -G2PWDir $g2pwDir -SourceBase $base -RepoPath $repo
    Download-FastLangDetect -PretrainedDir $pretrainedDir
    Prepare-GPTFFmpeg -RepoPath $repo
    Write-Ok "GPT-SoVITS prepared"
}

function Prepare-IndexTTS {
    if ($SkipIndexTTS) { return }
    $repo = Join-Path $ProjectRoot "repo\index-tts"
    if (-not (Test-Path $repo)) { throw "index-tts repo not found: $repo" }
    $pythonExe = Ensure-Venv -RepoPath $repo -BasePython $BasePython

    if ($SkipInstall) {
        Write-Warn "Skipping IndexTTS dependency installation"
    } else {
        Write-Step "Installing IndexTTS dependencies"
        $torchPackage = "torch==2.8.0"
        $torchaudioPackage = "torchaudio==2.8.0"
        if ($Device -eq "CU126") {
            $torchPackage = "torch==2.8.0+cu126"
            $torchaudioPackage = "torchaudio==2.8.0+cu126"
        } elseif ($Device -eq "CU128") {
            $torchPackage = "torch==2.8.0+cu128"
            $torchaudioPackage = "torchaudio==2.8.0+cu128"
        }
        Install-Torch -PythonExe $pythonExe -DeviceName $Device -TorchPackage $torchPackage -TorchaudioPackage $torchaudioPackage -Force
        & $pythonExe -m pip install -e $repo
        if ($LASTEXITCODE -ne 0) { throw "Failed to install IndexTTS" }
        Install-Torch -PythonExe $pythonExe -DeviceName $Device -TorchPackage $torchPackage -TorchaudioPackage $torchaudioPackage
    }

    if ($SkipDownloads) {
        Write-Warn "Skipping IndexTTS model downloads"
        return
    }

    Write-Step "Downloading IndexTTS-2 checkpoints"
    $modelDir = Join-Path $repo "checkpoints"
    New-Item -ItemType Directory -Force -Path $modelDir | Out-Null
    if ($Source -eq "ModelScope") {
        Prepare-IndexTTSBigVGAN -ModelDir $modelDir
    }
    $previousUseModelScope = $env:USE_MODELSCOPE
    try {
        if ($Source -eq "ModelScope") {
            $env:USE_MODELSCOPE = "true"
        }
        & $pythonExe (Join-Path $repo "indextts\cli_v2.py") download --source modelscope --model-dir $modelDir
        if ($LASTEXITCODE -ne 0) { throw "Failed to download IndexTTS checkpoints" }
        & $pythonExe (Join-Path $repo "indextts\cli_v2.py") config set model_dir $modelDir
    } finally {
        if ($null -eq $previousUseModelScope) {
            Remove-Item Env:\USE_MODELSCOPE -ErrorAction SilentlyContinue
        } else {
            $env:USE_MODELSCOPE = $previousUseModelScope
        }
    }
    Write-Ok "IndexTTS prepared"
}

function Write-EnvLocal {
    $envPath = Join-Path $ProjectRoot ".env.local"
    $content = @"
TTS_MORE_SERVICE_MODE=real
TTS_MORE_PYTHON_EXE=.venv\\Scripts\\python.exe
TTS_MORE_INDEXTTS_MODEL_DIR=repo\\index-tts\\checkpoints
INDEXTTS2_MODEL_DIR=repo\\index-tts\\checkpoints
TTS_MORE_INDEXTTS_PYTHON=repo\\index-tts\\.venv\\Scripts\\python.exe
"@
    if (-not (Test-Path $envPath)) {
        Set-Content -Path $envPath -Value $content -Encoding UTF8
        Write-Ok "Created .env.local"
    } else {
        Write-Warn ".env.local already exists; verify real-mode variables manually"
    }
}

Write-Step "Preflight"
$BasePython = Resolve-BasePython
Write-Ok "Base Python: $BasePython"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git is required" }
if (-not (Get-Command git-lfs -ErrorAction SilentlyContinue)) { Write-Warn "git-lfs not found; some model downloads may fail" }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { Write-Warn "ffmpeg not on PATH; GPT-SoVITS local ffmpeg.exe will be downloaded" }
$NvidiaControllers = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | Where-Object {
    ([string]$_.Name) -match "NVIDIA" -or ([string]$_.AdapterCompatibility) -match "NVIDIA"
})
if ($NvidiaControllers.Count -gt 0) {
    Write-Ok "NVIDIA video controller detected through Windows CIM"
} else {
    Write-Warn "NVIDIA video controller not detected through Windows CIM; GPU readiness will be confirmed by the package runtime"
}

foreach ($port in 9880, 9881) {
    $busy = Test-NetConnection -ComputerName 127.0.0.1 -Port $port -InformationLevel Quiet
    if ($busy) { Write-Warn "Port $port is already listening" }
}

Prepare-GPTSoVITS
Prepare-IndexTTS
Write-EnvLocal
Write-Ok "Model preparation flow finished"
