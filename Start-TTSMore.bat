@echo off
setlocal
if not defined TTS_MORE_PROJECT_ROOT set "TTS_MORE_PROJECT_ROOT=%~dp0"
set "TTS_MORE_LAUNCH_OPTIONS=%*"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$options=if($env:TTS_MORE_LAUNCH_OPTIONS){@($env:TTS_MORE_LAUNCH_OPTIONS.Split(' ',[StringSplitOptions]::RemoveEmptyEntries))}else{@()}; & (Join-Path $env:TTS_MORE_PROJECT_ROOT 'scripts\start-workstation.ps1') -ProjectRoot $env:TTS_MORE_PROJECT_ROOT -Options $options"
set "TTS_MORE_LAUNCH_RESULT=%errorlevel%"
if not "%TTS_MORE_LAUNCH_RESULT%"=="0" (
  echo Startup failed. Review the error above and data\local\run\one-click logs.
  if "%TTS_MORE_LAUNCH_OPTIONS%"=="" pause
)
exit /b %TTS_MORE_LAUNCH_RESULT%
