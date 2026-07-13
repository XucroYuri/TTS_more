param(
    [string]$Services = "",
    [string]$RepoPaths = "",
    [string]$Topology = "",
    [string]$Node = "",
    [string]$PidManifest = "",
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
if ($RepoPaths) {
    $argsList += @("--repo-paths", $RepoPaths)
}
if ($Topology) {
    $argsList += @("--topology", $Topology)
}
if ($Node) {
    $argsList += @("--node", $Node)
}
if (-not $PidManifest) {
    $PidManifest = $env:TTS_MORE_CUDA_PID_MANIFEST
}
if ($PidManifest) {
    $argsList += @("--pid-manifest", $PidManifest)
}
if ($Detach) {
    $argsList += "--detach"
}

& $Python @argsList
exit $LASTEXITCODE
