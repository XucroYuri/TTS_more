@echo off
setlocal EnableExtensions
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\stop-production.ps1"
exit /b %errorlevel%
