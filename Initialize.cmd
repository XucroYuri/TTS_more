@echo off
setlocal EnableExtensions
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\initialize-portable.ps1" %*
exit /b %errorlevel%
