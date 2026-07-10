$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$Source = if ($env:TTS_MORE_MODEL_SOURCE) { $env:TTS_MORE_MODEL_SOURCE } else { "Auto" }
if ($Source -eq "Auto") {
    $Source = if ($env:TTS_MORE_RESOLVED_SOURCE) { $env:TTS_MORE_RESOLVED_SOURCE } else { "ModelScope" }
}

Set-Location $RepoRoot
$Uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if ($Uv) {
    Write-Host "[indextts] uv sync --all-extras" -ForegroundColor Cyan
    & $Uv sync --all-extras
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} else {
    if (!(Test-Path -LiteralPath $Python)) {
        & python -m venv .venv
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    & $Python -m pip install -U pip wheel setuptools
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python -m pip install -e .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$SourceArg = if ($Source -eq "ModelScope") { "modelscope" } else { "huggingface" }
if ($Source -eq "HF-Mirror") {
    $env:HF_ENDPOINT = "https://hf-mirror.com"
} else {
    Remove-Item Env:\HF_ENDPOINT -ErrorAction SilentlyContinue
}

Write-Host "[indextts] download source=$Source model_dir=checkpoints" -ForegroundColor Cyan
& $Python indextts\cli_v2.py download --source $SourceArg --model-dir checkpoints
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python indextts\cli_v2.py config set model_dir checkpoints
exit $LASTEXITCODE
