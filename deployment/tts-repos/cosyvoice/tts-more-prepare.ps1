$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$Source = if ($env:TTS_MORE_MODEL_SOURCE) { $env:TTS_MORE_MODEL_SOURCE } else { "Auto" }
if ($Source -eq "Auto") {
    $Source = if ($env:TTS_MORE_RESOLVED_SOURCE) { $env:TTS_MORE_RESOLVED_SOURCE } else { "ModelScope" }
}
$BasePython = if ($env:TTS_MORE_BASE_PYTHON) { $env:TTS_MORE_BASE_PYTHON } else { "python" }

Set-Location $RepoRoot
git submodule update --init --recursive
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (!(Test-Path -LiteralPath $Python)) {
    & $BasePython -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& $Python -m pip install -U pip wheel setuptools
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($Source -eq "ModelScope") {
    Remove-Item Env:\HF_ENDPOINT -ErrorAction SilentlyContinue
    $Code = "from modelscope import snapshot_download; snapshot_download('iic/CosyVoice-300M', local_dir='pretrained_models/CosyVoice-300M')"
} else {
    if ($Source -eq "HF-Mirror") {
        $env:HF_ENDPOINT = "https://hf-mirror.com"
    } else {
        Remove-Item Env:\HF_ENDPOINT -ErrorAction SilentlyContinue
    }
    $Code = "from huggingface_hub import snapshot_download; snapshot_download('FunAudioLLM/CosyVoice-300M', local_dir='pretrained_models/CosyVoice-300M')"
}

Write-Host "[cosyvoice] download source=$Source model_dir=pretrained_models/CosyVoice-300M" -ForegroundColor Cyan
& $Python -c $Code
exit $LASTEXITCODE
