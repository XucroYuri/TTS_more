[CmdletBinding()]
param(
    [ValidateSet("Bootstrap", "Full")][string]$Profile = "Bootstrap",
    [ValidateSet("Auto", "CU128", "CU126", "CPU")][string]$Device = "Auto",
    [string]$Version = "0.2.0",
    [string]$OutputRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Bundle = [System.IO.Path]::GetFullPath($PSScriptRoot)
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $Bundle))
$config = Get-Content -LiteralPath (Join-Path $Bundle "component.json") -Raw | ConvertFrom-Json
$profileName = $Profile.ToLowerInvariant()
if (!$OutputRoot) { $OutputRoot = Join-Path $Root "artifacts\portable\$profileName" }
$stage = Join-Path $Root "artifacts\portable\.work\$($config.component)-$profileName\$($config.component)-$Version-windows-x64-$profileName"
if (Test-Path -LiteralPath (Split-Path -Parent $stage)) { Remove-Item -LiteralPath (Split-Path -Parent $stage) -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
$excluded = @(".git", ".venv", "runtime", "data", "artifacts", "__pycache__")
Get-ChildItem -LiteralPath $Root -Force | Where-Object { $_.Name -notin $excluded } | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $stage -Recurse -Force }
if ($Profile -eq "Full") { & (Join-Path $stage "tts_more\Initialize.ps1") -Device $Device; if ($LASTEXITCODE -ne 0) { throw "full package initialization failed" } }
if ($Profile -eq "Bootstrap" -and (Get-ChildItem -LiteralPath $stage -Recurse -Force | Where-Object { $_.FullName -match "[\\/](\.venv|runtime[\\/]live|pretrained_models|checkpoints|cache)([\\/]|$)" })) { throw "bootstrap audit found forbidden runtime or model assets" }
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$zip = Join-Path $OutputRoot "$($config.component)-$Version-windows-x64-$profileName.zip"
Compress-Archive -LiteralPath $stage -DestinationPath $zip -Force
$hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $([IO.Path]::GetFileName($zip))" | Set-Content -LiteralPath "$zip.sha256" -Encoding ASCII
@{component=$config.component;version=$Version;profile=$profileName;sha256=$hash} | ConvertTo-Json | Set-Content -LiteralPath "$zip.provenance.json" -Encoding UTF8
Write-Host "Created $Profile package: $zip"
