@echo off
setlocal EnableExtensions
set "PACKAGE_ROOT=%~dp0"
set "RUNTIME_ROOT=%~dp0runtime\live"
set "PID_RECORD=%~dp0data\local\run\worker.pid.json"
if not exist "%PID_RECORD%" exit /b 0
if not exist "%RUNTIME_ROOT%\python.exe" exit /b 1
"%RUNTIME_ROOT%\python.exe" "%PACKAGE_ROOT%app\scripts\portable_launcher.py" stop-worker --package-root "%PACKAGE_ROOT%"
exit /b %errorlevel%
