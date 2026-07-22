@echo off
setlocal EnableExtensions
for %%I in ("%~dp0.") do set "PACKAGE_ROOT=%%~fI"
set "RUNTIME_ROOT=%PACKAGE_ROOT%\runtime\live"
set "PID_RECORD=%PACKAGE_ROOT%\data\local\run\worker.pid.json"
if not exist "%PID_RECORD%" exit /b 0
if not exist "%RUNTIME_ROOT%\python.exe" exit /b 1
"%RUNTIME_ROOT%\python.exe" "%PACKAGE_ROOT%\app\scripts\portable_launcher.py" stop-worker --package-root "%PACKAGE_ROOT%"
exit /b %errorlevel%
