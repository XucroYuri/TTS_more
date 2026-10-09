param([string]$ConfigPath = (Join-Path $PSScriptRoot '../data/local/comfyui/source-config.json'))
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local-stack-config.ps1')
$taskRoot = Split-Path -Parent $PSScriptRoot
$config = Read-LocalStackConfig $ConfigPath
$endpoint = Get-LocalEndpoint $config.comfyui.base_url
$comfyRoot = Resolve-LocalStackPath $taskRoot $config.comfyui.source_root
$python = if ($config.comfyui.python_executable) { Resolve-LocalStackPath $comfyRoot $config.comfyui.python_executable } else { Join-Path $comfyRoot '.venv/Scripts/python.exe' }
if (!(Test-Path -LiteralPath $python)) { throw 'Configure an existing ComfyUI Python environment.' }
$integrationRoot = if ($config.integration_dir) { Resolve-LocalStackPath $taskRoot $config.integration_dir } else { Join-Path $taskRoot 'data/local/comfyui' }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:TTS_AUDIO_SUITE_RESOURCES = Join-Path $integrationRoot 'resources.yaml'
$logRoot = Join-Path $taskRoot 'data/local/run'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
if (Get-NetTCPConnection -State Listen -LocalPort $endpoint.Port -ErrorAction SilentlyContinue) { throw "ComfyUI port $($endpoint.Port) is in use." }
$process = Start-Process -FilePath $python -ArgumentList '-X','utf8','main.py','--listen',$endpoint.DnsSafeHost,'--port',$endpoint.Port -WorkingDirectory $comfyRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot 'comfyui.stdout.log') -RedirectStandardError (Join-Path $logRoot 'comfyui.stderr.log') -PassThru
Write-Output "ComfyUI PID: $($process.Id) $($config.comfyui.base_url)"
