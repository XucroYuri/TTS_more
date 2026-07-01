$ErrorActionPreference = "Stop"

param(
  [switch]$StartGPTSoVITS
)

$Root = Split-Path -Parent $PSScriptRoot
$BackendPython = Join-Path $Root ".venv\Scripts\python.exe"

if (!(Test-Path -LiteralPath $BackendPython)) {
  throw "Backend virtual environment not found. Create .venv and install backend[dev] first."
}

$IndexWorker = Start-Process -FilePath $BackendPython `
  -ArgumentList "-m", "uvicorn", "app.workers.indextts_worker:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", "9881" `
  -WorkingDirectory $Root `
  -WindowStyle Hidden `
  -PassThru

Write-Host "IndexTTS worker PID: $($IndexWorker.Id)  http://127.0.0.1:9881"

if ($StartGPTSoVITS) {
  $GptRepo = Join-Path $Root "repo\GPT-SoVITS"
  $GptPython = Join-Path $GptRepo "runtime\python.exe"
  if (!(Test-Path -LiteralPath $GptPython)) {
    $GptPython = "python"
  }
  $Gpt = Start-Process -FilePath $GptPython `
    -ArgumentList "api_v2.py", "-a", "127.0.0.1", "-p", "9880", "-c", "GPT_SoVITS/configs/tts_infer.yaml" `
    -WorkingDirectory $GptRepo `
    -WindowStyle Hidden `
    -PassThru
  Write-Host "GPT-SoVITS API PID: $($Gpt.Id) http://127.0.0.1:9880"
}
