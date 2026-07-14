@echo off
setlocal EnableExtensions
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\start-production.ps1"
exit /b %errorlevel%
