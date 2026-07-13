@echo off
setlocal EnableExtensions
set "PACKAGE_ROOT=%~dp0"
set "RUNTIME_ARCHIVE=%~dp0runtime\runtime.zip"
set "RUNTIME_ROOT=%~dp0runtime\live"
set "TTS_MORE_ARTIFACT_ROOT=%~dp0data\local\artifacts"
if not exist "%RUNTIME_ROOT%\.portable-build.json" (
  powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath $args[0] -DestinationPath $args[1] -Force" "%RUNTIME_ARCHIVE%" "%RUNTIME_ROOT%"
  if errorlevel 1 exit /b %errorlevel%
)
"%RUNTIME_ROOT%\python.exe" "%PACKAGE_ROOT%app\scripts\portable_launcher.py" prepare-runtime --package-root "%PACKAGE_ROOT%"
if errorlevel 1 exit /b %errorlevel%
call "%RUNTIME_ROOT%\Start-Worker-Runtime.cmd"
