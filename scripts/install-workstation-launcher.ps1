[CmdletBinding()]
param([string]$ProjectRoot = '', [string]$Target = '')
$ErrorActionPreference = 'Stop'
if (!$ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
if (!(Test-Path -LiteralPath (Join-Path $ProjectRoot 'Start-TTSMore.bat'))) {
    throw "Repository launcher is missing: $ProjectRoot"
}
if (!$Target) { $Target = Join-Path (Split-Path -Parent $ProjectRoot) 'Start-TTSMore.bat' }
$Target = [IO.Path]::GetFullPath($Target)
if ($Target -ieq (Join-Path $ProjectRoot 'Start-TTSMore.bat')) {
    throw 'The external wrapper must not overwrite the repository launcher.'
}
if (Test-Path -LiteralPath $Target) {
    Copy-Item -LiteralPath $Target -Destination "$Target.backup-$(Get-Date -Format 'yyyyMMdd-HHmmss-fff')"
}
$batchRoot = $ProjectRoot.Replace('%', '%%')
$body = @"
@echo off
setlocal DisableDelayedExpansion
if not defined TTS_MORE_PROJECT_ROOT set "TTS_MORE_PROJECT_ROOT=$batchRoot"
if not exist "%TTS_MORE_PROJECT_ROOT%\Start-TTSMore.bat" exit /b 1
call "%TTS_MORE_PROJECT_ROOT%\Start-TTSMore.bat" %*
exit /b %errorlevel%
"@
# UTF-8 paths require the matching console code page when cmd reads this wrapper.
$body = $body.Replace('@echo off', "@echo off`r`nchcp 65001 >nul")
[IO.File]::WriteAllText($Target, $body, [Text.UTF8Encoding]::new($false))
Write-Host "Launcher installed: $Target"
