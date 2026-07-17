@echo off
setlocal EnableExtensions
set "TTS_MORE_ROOT=%~dp0"
rem Starts the TTS More backend on 8000 and frontend on 5173.
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\start-dev.ps1"
exit /b %errorlevel%
