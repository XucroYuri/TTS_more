$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (!(Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Missing project Python environment"
}

& $Python (Join-Path $Root "scripts\run-lan-validation.py") @args
exit $LASTEXITCODE
