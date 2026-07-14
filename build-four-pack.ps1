[CmdletBinding()]
param(
    [string]$Version = "0.2.0",
    [ValidateSet("Auto", "CU128", "CU126", "CPU")][string]$Device = "Auto",
    [string]$OutputRoot = "",
    [string]$GptRoot = "",
    [string]$IndexRoot = "",
    [string]$CosyVoiceRoot = "",
    [switch]$PlanOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($env:GITHUB_ACTIONS -eq "true") { throw "the four-pack full builder is local-only and is prohibited in GitHub Actions" }
$Root = [System.IO.Path]::GetFullPath($PSScriptRoot)
if (!$OutputRoot) { $OutputRoot = Join-Path $Root "artifacts\portable\full-four\$Version" }
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)

function Resolve-ComponentRoot {
    param([string]$Explicit, [string]$EnvironmentName, [string]$LockedPath)
    $raw = if ($Explicit) { $Explicit } elseif ([Environment]::GetEnvironmentVariable($EnvironmentName)) { [Environment]::GetEnvironmentVariable($EnvironmentName) } else { Join-Path $Root $LockedPath }
    $resolved = [System.IO.Path]::GetFullPath($raw)
    if (!(Test-Path -LiteralPath (Join-Path $resolved "Build-Package.ps1") -PathType Leaf)) { throw "component package builder is missing: $resolved" }
    return $resolved
}

$repoLock = Get-Content -LiteralPath (Join-Path $Root "repo.lock.json") -Raw | ConvertFrom-Json
$gptLock = @($repoLock.repositories | Where-Object { $_.name -eq "GPT-SoVITS-main" })[0]
$indexLock = @($repoLock.repositories | Where-Object { $_.provider_type -eq "indextts" })[0]
$cosyLock = @($repoLock.repositories | Where-Object { $_.provider_type -eq "cosyvoice" })[0]
$targets = @(
    [pscustomobject]@{ component="tts-more"; root=$Root; expected_revision="" },
    [pscustomobject]@{ component="gpt-sovits"; root=(Resolve-ComponentRoot $GptRoot "TTS_MORE_GPT_ROOT" ([string]$gptLock.path)); expected_revision=[string]$gptLock.commit },
    [pscustomobject]@{ component="indextts"; root=(Resolve-ComponentRoot $IndexRoot "TTS_MORE_INDEX_ROOT" ([string]$indexLock.path)); expected_revision=[string]$indexLock.commit },
    [pscustomobject]@{ component="cosyvoice"; root=(Resolve-ComponentRoot $CosyVoiceRoot "TTS_MORE_COSYVOICE_ROOT" ([string]$cosyLock.path)); expected_revision=[string]$cosyLock.commit }
)
$componentOrder = @("tts-more", "gpt-sovits", "indextts", "cosyvoice")
if (($targets.component -join ',') -ne ($componentOrder -join ',')) { throw "four-pack component order drifted" }
foreach ($target in $targets) {
    if (!$target.expected_revision) { continue }
    $actualRevision = (& git -C $target.root rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $actualRevision -ne $target.expected_revision) {
        throw "$($target.component) source revision drift: expected $($target.expected_revision), found $actualRevision"
    }
}
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$plan = [ordered]@{ schema_version=1; version=$Version; profile="full"; device=$Device.ToLowerInvariant(); output_root=$OutputRoot; components=@($targets | ForEach-Object { @{component=$_.component;root=$_.root;expected_revision=$_.expected_revision} }) }
$plan | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $OutputRoot "four-pack.plan.json") -Encoding UTF8
if ($PlanOnly) { Write-Host "Validated four-pack plan: $OutputRoot"; exit 0 }

$packages = @()
foreach ($target in $targets) {
    $before = @((Get-ChildItem -LiteralPath $OutputRoot -Filter "*.zip" -File -ErrorAction SilentlyContinue).FullName)
    & (Join-Path $target.root "Build-Package.ps1") -Profile Full -Device $Device -Version $Version -OutputRoot $OutputRoot
    if ($LASTEXITCODE -ne 0) { throw "$($target.component) full package build failed" }
    $created = @(Get-ChildItem -LiteralPath $OutputRoot -Filter "*-$Version-windows-x64-full.zip" -File | Where-Object { $_.FullName -notin $before } | Sort-Object LastWriteTimeUtc -Descending)
    if ($created.Count -ne 1) { throw "$($target.component) did not produce exactly one new full ZIP" }
    $zip = $created[0]
    $provenancePath = "$($zip.FullName).provenance.json"
    if (!(Test-Path -LiteralPath $provenancePath)) { throw "$($target.component) provenance is missing" }
    $provenance = Get-Content -LiteralPath $provenancePath -Raw | ConvertFrom-Json
    if ([string]$provenance.profile -ne "full" -or [string]$provenance.version -ne $Version) { throw "$($target.component) full provenance mismatch" }
    $packages += [ordered]@{ component=$target.component; file=$zip.Name; sha256=(Get-FileHash -LiteralPath $zip.FullName -Algorithm SHA256).Hash.ToLowerInvariant(); source_revision=[string]$provenance.source_revision }
}

$matrix = [ordered]@{ schema_version=1; release_train=$Version; platform="windows-x64"; profile="full"; device=$Device.ToLowerInvariant(); components=$packages; endpoints=@{"tts-more"=8000;"gpt-sovits"=9880;"indextts"=9881;"cosyvoice"=9882} }
$matrix | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $OutputRoot "compatibility-matrix.json") -Encoding UTF8
$material = ($packages | ForEach-Object { "$($_.component)`0$($_.source_revision)`0$($_.sha256)" }) -join "`n"
$sha = [System.Security.Cryptography.SHA256]::Create()
$trainSha = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($material)))).Replace("-", "").ToLowerInvariant()
[ordered]@{ schema_version=1; release_train=$Version; profile="full"; device=$Device.ToLowerInvariant(); package_set_sha256=$trainSha; generated_at=[DateTime]::UtcNow.ToString("o"); components=$packages } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $OutputRoot "four-pack.provenance.json") -Encoding UTF8
Write-Host "Created local full four-pack: $OutputRoot"
