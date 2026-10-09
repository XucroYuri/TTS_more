param([string]$ConfigPath = (Join-Path $PSScriptRoot '../data/local/comfyui/source-config.json'))
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local-stack-config.ps1')
$taskRoot = Split-Path -Parent $PSScriptRoot
$config = Read-LocalStackConfig $ConfigPath
$endpoint = Get-LocalEndpoint $config.backend_url
$python = if ($config.backend.python_executable) { Resolve-LocalStackPath $taskRoot $config.backend.python_executable } elseif (Test-Path (Join-Path $taskRoot '.venv311/Scripts/python.exe')) { Join-Path $taskRoot '.venv311/Scripts/python.exe' } else { Join-Path $taskRoot '.venv/Scripts/python.exe' }
if (!(Test-Path -LiteralPath $python)) { throw 'Python 3.11 backend environment is missing.' }
$runtimeVersion = & $python -c 'import sys; print(sys.version_info.major, sys.version_info.minor, sep=chr(46))'
if ($LASTEXITCODE -ne 0 -or $runtimeVersion.Trim() -ne '3.11') { throw 'TTSMore backend requires Python 3.11.' }
if (Get-NetTCPConnection -State Listen -LocalPort $endpoint.Port -ErrorAction SilentlyContinue) { throw "TTSMore port $($endpoint.Port) is in use." }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:TTS_MORE_STATIC_ROOT = Join-Path $taskRoot 'frontend/dist'
if (!(Test-Path -LiteralPath (Join-Path $env:TTS_MORE_STATIC_ROOT 'index.html'))) { throw 'Build the frontend with pnpm run build before starting.' }
$logRoot = Join-Path $taskRoot 'data/local/run'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$process = Start-Process -FilePath $python -ArgumentList '-m','uvicorn','app.main:app','--app-dir','backend','--host',$endpoint.DnsSafeHost,'--port',$endpoint.Port -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot 'backend.stdout.log') -RedirectStandardError (Join-Path $logRoot 'backend.stderr.log') -PassThru
$recordPath = Join-Path $logRoot 'tts-more.pid.json'
$record = if (Test-Path -LiteralPath $recordPath) { Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json } else { [pscustomobject]@{backend_pid=0;frontend_pid=0;backend_port=$endpoint.Port;frontend_port=5173;started_at=[DateTime]::UtcNow.ToString('o')} }
$record.backend_pid = $process.Id
$record.backend_port = $endpoint.Port
$record | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding utf8
Write-Output "TTSMore PID: $($process.Id) $($config.backend_url)"
