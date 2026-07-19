@echo off
setlocal EnableExtensions
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\Invoke-PortableStart.ps1" %*
exit /b %errorlevel%
