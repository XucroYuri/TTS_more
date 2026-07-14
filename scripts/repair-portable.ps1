[CmdletBinding()]
param([ValidateSet("Auto", "CU128", "CU126", "CPU")][string]$Device = "Auto")

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "initialize-portable.ps1") -Device $Device -Repair
exit $LASTEXITCODE
