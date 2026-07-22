@echo off
setlocal EnableExtensions
set "TTS_MORE_ROOT=%~dp0"
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$record = Join-Path $env:TTS_MORE_ROOT 'data\local\run\tts-more.pid.json'; if (!(Test-Path -LiteralPath $record)) { exit 0 }; $payload = Get-Content -LiteralPath $record -Raw | ConvertFrom-Json; foreach ($name in @('backend_pid', 'frontend_pid')) { $processId = $payload.$name; if ($processId) { & taskkill /PID ([string]$processId) /T /F | Out-Null } }; Remove-Item -LiteralPath $record -Force"
exit /b %errorlevel%
