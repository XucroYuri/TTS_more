param(
    [string]$Services = "",
    [switch]$Detach
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (!(Test-Path -LiteralPath $Python)) {
    $Python = "python"
}

$argsList = @((Join-Path $Root "scripts\tts_more_deploy.py"), "start-workers", "--platform", "windows")
if ($Services) {
    $argsList += @("--service-ids", $Services)
}
if ($Detach) {
    $argsList += "--detach"
}

& $Python @argsList
exit $LASTEXITCODE
