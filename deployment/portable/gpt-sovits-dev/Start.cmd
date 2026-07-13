@echo off
setlocal EnableExtensions
for %%I in ("%~dp0.") do set "PACKAGE_ROOT=%%~fI"
rem GPT-SoVITS worker endpoint: http://127.0.0.1:9883
set "RUNTIME_ARCHIVE=%PACKAGE_ROOT%\runtime\runtime.zip"
set "RUNTIME_ROOT=%PACKAGE_ROOT%\runtime\live"
set "TTS_MORE_ARTIFACT_ROOT=%PACKAGE_ROOT%\data\local\artifacts"
if not exist "%RUNTIME_ROOT%\python.exe" (
  powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Expand-Archive -LiteralPath $env:RUNTIME_ARCHIVE -DestinationPath $env:RUNTIME_ROOT -Force"
  if errorlevel 1 exit /b %errorlevel%
)
"%RUNTIME_ROOT%\python.exe" "%PACKAGE_ROOT%\app\scripts\portable_launcher.py" prepare-runtime --package-root "%PACKAGE_ROOT%"
if errorlevel 1 exit /b %errorlevel%
call "%RUNTIME_ROOT%\Start-Worker-Runtime.cmd"
