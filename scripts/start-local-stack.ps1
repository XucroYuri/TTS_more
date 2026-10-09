param([string]$ConfigPath = (Join-Path $PSScriptRoot '../data/local/comfyui/source-config.json'))
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local-stack-config.ps1')
$config = Read-LocalStackConfig $ConfigPath
$backendEndpoint = Get-LocalEndpoint $config.backend_url
$comfyEndpoint = Get-LocalEndpoint $config.comfyui.base_url
if (!(Get-NetTCPConnection -State Listen -LocalPort $comfyEndpoint.Port -ErrorAction SilentlyContinue)) { & (Join-Path $PSScriptRoot 'start-comfyui-local.ps1') -ConfigPath $ConfigPath }
if (!(Get-NetTCPConnection -State Listen -LocalPort $backendEndpoint.Port -ErrorAction SilentlyContinue)) { & (Join-Path $PSScriptRoot 'start-backend-local.ps1') -ConfigPath $ConfigPath }
$deadline = [DateTime]::UtcNow.AddSeconds(60)
do {
    try {
        $backend = Invoke-RestMethod ($config.backend_url.TrimEnd('/')+'/api/queue/status') -TimeoutSec 3
        $comfy = Invoke-RestMethod ($config.comfyui.base_url.TrimEnd('/')+'/api/tts-audio-suite/v1/capabilities') -TimeoutSec 3
        Write-Output "TTSMore: $($config.backend_url)"
        Write-Output "ComfyUI: $($config.comfyui.base_url) ($($comfy.resources.Count) resources)"
        return
    } catch { Start-Sleep -Milliseconds 500 }
} while ([DateTime]::UtcNow -lt $deadline)
throw 'Local TTS stack did not become ready. Inspect data/local/run logs.'
