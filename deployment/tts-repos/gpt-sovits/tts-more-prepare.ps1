$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$Device = if ($env:TTS_MORE_DEVICE) { $env:TTS_MORE_DEVICE } else { "CU128" }
$Source = if ($env:TTS_MORE_MODEL_SOURCE) { $env:TTS_MORE_MODEL_SOURCE } else { "Auto" }
if ($Source -eq "Auto") {
    $Source = if ($env:TTS_MORE_RESOLVED_SOURCE) { $env:TTS_MORE_RESOLVED_SOURCE } else { "ModelScope" }
}

if (!(Get-Command conda -ErrorAction SilentlyContinue)) {
    if (Get-Command micromamba -ErrorAction SilentlyContinue) {
        throw "micromamba is installed but is not currently supported by the TTS More GPT-SoVITS prepare workflow; install conda."
    }
    throw "supported conda executable was not found; GPT-SoVITS dependency preparation cannot continue. Install conda."
}

$InstallPs1 = Join-Path $RepoRoot "install.ps1"
$InstallSh = Join-Path $RepoRoot "install.sh"
Write-Host "[gpt-sovits] install device=$Device source=$Source" -ForegroundColor Cyan

if (Test-Path -LiteralPath $InstallPs1) {
    & powershell -ExecutionPolicy Bypass -File $InstallPs1 -Device $Device -Source $Source
    exit $LASTEXITCODE
}
if (Test-Path -LiteralPath $InstallSh) {
    & bash $InstallSh --device $Device --source $Source
    exit $LASTEXITCODE
}
throw "Missing upstream installer: $InstallPs1 or $InstallSh"
