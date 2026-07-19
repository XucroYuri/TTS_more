@echo off
setlocal EnableExtensions
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\repair-portable.ps1" %*
exit /b %errorlevel%
