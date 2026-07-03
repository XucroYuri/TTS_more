param(
    [Parameter(Mandatory = $true)]
    [string]$RepoPath,

    [int]$Port = 9872,

    [string]$Language = "zh_CN"
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $RepoPath).Path
$pythonRoot = Join-Path $repo "py312"
$pythonExe = Join-Path $pythonRoot "python.exe"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "GPT-SoVITS portable Python not found: $pythonExe"
}
if (-not (Test-Path -LiteralPath (Join-Path $repo "GPT_SoVITS\inference_webui.py"))) {
    throw "GPT-SoVITS inference WebUI entry not found in $repo"
}

$env:GRADIO_TEMP_DIR = Join-Path $repo "tmp"
$env:PYTHON_PATH = $pythonRoot
$env:PYTHON_EXECUTABLE = $pythonExe
$env:PYTHONEXECUTABLE = $pythonExe
$env:PYTHONWEXECUTABLE = Join-Path $pythonRoot "pythonw.exe"
$env:PYTHONW_EXECUTABLE = Join-Path $pythonRoot "pythonw.exe"
$env:PYTHON_BIN_PATH = $pythonExe
$env:PYTHON_LIB_PATH = Join-Path $pythonRoot "Lib\site-packages"
$env:PYTHONHOME = ""
$env:PYTHONPATH = ""
$env:DS_BUILD_AIO = "0"
$env:DS_BUILD_SPARSE_ATTN = "0"
$env:CU_PATH = Join-Path $pythonRoot "Lib\site-packages\torch\lib"
$env:cuda_PATH = Join-Path $pythonRoot "Library\bin"
$env:FFMPEG_PATH = Join-Path $pythonRoot "ffmpeg\bin"
$env:HF_ENDPOINT = "https://hf-mirror.com"
$env:HF_HOME = Join-Path $repo "hf_download"
$env:TRANSFORMERS_CACHE = Join-Path $repo "tf_download"
$env:XFORMERS_FORCE_DISABLE_TRITON = "1"
$env:GPT_SOVITS_PORTABLE_MODE = "1"
$env:infer_ttswebui = [string]$Port
$env:PATH = "$pythonRoot;$pythonRoot\Scripts;$env:FFMPEG_PATH;$env:CU_PATH;$env:cuda_PATH;$env:PATH"

Set-Location -LiteralPath $repo
& $pythonExe -s -u "tools\run_with_bootstrap.py" -- "GPT_SoVITS\inference_webui.py" $Language
