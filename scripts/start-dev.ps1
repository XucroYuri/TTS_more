$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$BackendPython = Join-Path $Root ".venv\Scripts\python.exe"

if (!(Test-Path -LiteralPath $BackendPython)) {
  throw "Backend virtual environment not found. Create .venv and install backend[dev] first."
}

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

