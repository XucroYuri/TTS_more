$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$BackendPython = Join-Path $Root ".venv\Scripts\python.exe"

function Assert-PortAvailable {
  param([int]$Port, [string]$Label)
  $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
  if ($listeners.Count -gt 0) {
    $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
    throw "$Label port $Port is already in use; stop the existing process before starting this checkout. PID(s): $owners"
  }
}

if (!(Test-Path -LiteralPath $BackendPython)) {
  throw "Backend virtual environment not found. Create .venv and install backend[dev] first."
}

Assert-PortAvailable 8000 "Backend"
Assert-PortAvailable 5173 "Frontend"

$Backend = Start-Process -FilePath $BackendPython `
  -ArgumentList "-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", "8000", "--reload" `
  -WorkingDirectory $Root `
  -WindowStyle Hidden `
  -PassThru

$Frontend = Start-Process -FilePath "pnpm" `
  -ArgumentList "dev" `
  -WorkingDirectory (Join-Path $Root "frontend") `
  -WindowStyle Hidden `
  -PassThru

Write-Host "Backend PID: $($Backend.Id)  http://127.0.0.1:8000"
Write-Host "Frontend PID: $($Frontend.Id) http://127.0.0.1:5173"

