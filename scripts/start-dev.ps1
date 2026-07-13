$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$BackendPython = Join-Path $Root ".venv\Scripts\python.exe"
$BackendPort = if ($env:TTS_MORE_BACKEND_PORT) { [int]$env:TTS_MORE_BACKEND_PORT } else { 8000 }
$FrontendPort = if ($env:TTS_MORE_FRONTEND_PORT) { [int]$env:TTS_MORE_FRONTEND_PORT } else { 5173 }

function Assert-PortAvailable {
  param([int]$Port, [string]$Label)
  $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
  if ($listeners.Count -gt 0) {
    throw "$Label port $Port is already in use; confirm its ownership before taking any action."
  }
}

if (!(Test-Path -LiteralPath $BackendPython)) {
  throw "Backend virtual environment not found. Create .venv and install backend[dev] first."
}

Assert-PortAvailable $BackendPort "Backend"
Assert-PortAvailable $FrontendPort "Frontend"

$Backend = Start-Process -FilePath $BackendPython `
  -ArgumentList "-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", ([string]$BackendPort), "--reload" `
  -WorkingDirectory $Root `
  -WindowStyle Hidden `
  -PassThru

$Frontend = Start-Process -FilePath "pnpm" `
  -ArgumentList "dev", "--host", "127.0.0.1", "--port", ([string]$FrontendPort) `
  -WorkingDirectory (Join-Path $Root "frontend") `
  -WindowStyle Hidden `
  -PassThru

$RunDirectory = Join-Path $Root "data\local\run"
New-Item -ItemType Directory -Path $RunDirectory -Force | Out-Null
@{
  backend_pid = [int]$Backend.Id
  frontend_pid = [int]$Frontend.Id
  backend_port = $BackendPort
  frontend_port = $FrontendPort
  started_at = [DateTime]::UtcNow.ToString("o")
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $RunDirectory "tts-more.pid.json") -Encoding UTF8

Write-Host "Backend PID: $($Backend.Id)  http://127.0.0.1:$BackendPort"
Write-Host "Frontend PID: $($Frontend.Id) http://127.0.0.1:$FrontendPort"

