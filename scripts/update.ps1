$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (!(Test-Path -LiteralPath $Python)) {
  $Python = "python"
}

& $Python (Join-Path $Root "scripts\tts_more_deploy.py") update @args
exit $LASTEXITCODE
