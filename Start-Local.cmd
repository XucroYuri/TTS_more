@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-local-stack.ps1" %*
exit /b %errorlevel%
