@echo off
setlocal EnableExtensions
set "TTS_MORE_ROOT=%~dp0"
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\start-dev.ps1"
exit /b %errorlevel%
