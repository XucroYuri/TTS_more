# Start the three non-invasive TTS workers (GPT-SoVITS, IndexTTS, CosyVoice).
#
# Windows equivalent of scripts/start-service-workers.sh. Each worker is a
# FastAPI app that imports the upstream model directly and exposes the
# tts-more-v1 contract — no Gradio scraping, no upstream file changes.
#
# The workers run in the upstream repo's own venv (so torch/CUDA resolve).
# Set TTS_MORE_GPTSOVITS_PYTHON / TTS_MORE_INDEXTTS_PYTHON /
# TTS_MORE_COSYVOICE_PYTHON to point at each repo's interpreter; if unset,
# falls back to the backend .venv\Scripts\python.exe.
#
# Ports: GPT-SoVITS 9880, IndexTTS 9881, CosyVoice 9882.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$BackendPython = Join-Path $Root ".venv\Scripts\python.exe"
if (!(Test-Path -LiteralPath $BackendPython)) { $BackendPython = "python" }

$GptPython   = $env:TTS_MORE_GPTSOVITS_PYTHON;   if (-not $GptPython)   { $GptPython = $BackendPython }
$IndexPython = $env:TTS_MORE_INDEXTTS_PYTHON;    if (-not $IndexPython) { $IndexPython = $BackendPython }
$CosyPython  = $env:TTS_MORE_COSYVOICE_PYTHON;   if (-not $CosyPython)  { $CosyPython = $BackendPython }

$workers = @(
  @{ Name = "GPT-SoVITS"; Py = $GptPython;   Module = "app.workers.gpt_sovits_worker:app"; Port = 9880 },
  @{ Name = "IndexTTS";   Py = $IndexPython; Module = "app.workers.indextts_worker:app";   Port = 9881 },
  @{ Name = "CosyVoice";  Py = $CosyPython;  Module = "app.workers.cosyvoice_worker:app";  Port = 9882 }
)

foreach ($w in $workers) {
  $proc = Start-Process -FilePath $w.Py `
    -ArgumentList "-m", "uvicorn", $w.Module, "--app-dir", "backend", "--host", "127.0.0.1", "--port", $w.Port `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -PassThru
  Write-Host "$($w.Name) worker PID: $($proc.Id)  http://127.0.0.1:$($w.Port)"
}

Write-Host ""
Write-Host "All workers started. Stop them via taskkill or closing the windows."
Write-Host "  GPT-SoVITS: http://127.0.0.1:9880/health"
Write-Host "  IndexTTS:   http://127.0.0.1:9881/health"
Write-Host "  CosyVoice:  http://127.0.0.1:9882/health"
