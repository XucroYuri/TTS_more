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
if ($Version -notmatch "^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$") { throw "four-pack version is unsafe" }
if ($env:GITHUB_ACTIONS -eq "true") { throw "the four-pack full builder is local-only and is prohibited in GitHub Actions" }
$Root = [System.IO.Path]::GetFullPath($PSScriptRoot)
if (!$OutputRoot) { $OutputRoot = Join-Path $Root "artifacts\portable\full-four\$Version" }
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)

function Resolve-ComponentRoot {
    param([string]$Explicit, [string]$EnvironmentName, [string]$LockedPath)
    $environmentValue = [Environment]::GetEnvironmentVariable($EnvironmentName)
    $raw = if ($Explicit) { $Explicit } elseif ($environmentValue) { $environmentValue } else { Join-Path $Root $LockedPath }
    $resolved = [System.IO.Path]::GetFullPath($raw)
    if (!(Test-Path -LiteralPath (Join-Path $resolved "Build-Package.ps1") -PathType Leaf)) { throw "component package builder is missing: $resolved" }
    return $resolved
}

function Get-ZipSnapshot {
    param([Parameter(Mandatory = $true)][string]$Directory)
    $snapshot = @{}
    if (!(Test-Path -LiteralPath $Directory -PathType Container)) { return $snapshot }
    foreach ($zip in @(Get-ChildItem -LiteralPath $Directory -Filter "*.zip" -File -ErrorAction SilentlyContinue)) {
        $snapshot[$zip.FullName] = (Get-FileHash -LiteralPath $zip.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    return $snapshot
}

function Resolve-MetadataPython {
    if ($env:TTS_MORE_BUILD_PYTHON) { return [pscustomobject]@{ command=$env:TTS_MORE_BUILD_PYTHON; prefix=@() } }
    foreach ($candidate in @(
        (Join-Path $Root "runtime\live\python.exe"),
        (Join-Path $Root "backend\.venv\Scripts\python.exe"),
        (Join-Path $Root ".venv\Scripts\python.exe")
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return [pscustomobject]@{ command=$candidate; prefix=@() } }
    }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { return [pscustomobject]@{ command=$py.Source; prefix=@("-3.11") } }
    throw "Python 3.11 is required to validate four-pack metadata"
}

$repoLock = Get-Content -LiteralPath (Join-Path $Root "repo.lock.json") -Raw | ConvertFrom-Json
$gptLock = @($repoLock.repositories | Where-Object { $_.name -eq "GPT-SoVITS-main" })[0]
$indexLock = @($repoLock.repositories | Where-Object { $_.provider_type -eq "indextts" })[0]
$cosyLock = @($repoLock.repositories | Where-Object { $_.provider_type -eq "cosyvoice" })[0]
$targets = @(
    [pscustomobject]@{ component="tts-more"; root=$Root; expected_revision=""; device="CPU" },
    [pscustomobject]@{ component="gpt-sovits"; root=(Resolve-ComponentRoot $GptRoot "TTS_MORE_GPT_ROOT" ([string]$gptLock.path)); expected_revision=[string]$gptLock.commit; device=$Device },
    [pscustomobject]@{ component="indextts"; root=(Resolve-ComponentRoot $IndexRoot "TTS_MORE_INDEX_ROOT" ([string]$indexLock.path)); expected_revision=[string]$indexLock.commit; device=$Device },
    [pscustomobject]@{ component="cosyvoice"; root=(Resolve-ComponentRoot $CosyVoiceRoot "TTS_MORE_COSYVOICE_ROOT" ([string]$cosyLock.path)); expected_revision=[string]$cosyLock.commit; device=$Device }
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

$plan = [ordered]@{
    schema_version=1
    version=$Version
    profile="full"
    components=@($targets | ForEach-Object {
        [ordered]@{ component=$_.component; device=([string]$_.device).ToLowerInvariant(); expected_revision=$_.expected_revision }
    })
}
if ($PlanOnly) {
    [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
    Write-Output ($plan | ConvertTo-Json -Depth 6)
    exit 0
}

if (Test-Path -LiteralPath $OutputRoot) { throw "OutputRoot already exists; use a new version directory so previous packages remain unchanged" }
$outputParent = [System.IO.Path]::GetDirectoryName($OutputRoot)
if ([string]::IsNullOrWhiteSpace($outputParent)) { throw "OutputRoot must have a parent directory" }
New-Item -ItemType Directory -Force -Path $outputParent | Out-Null
$transactionRoot = Join-Path $outputParent (".tts-more-four-pack-transaction-$PID-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $transactionRoot | Out-Null
$published = $false
try {
    $metadataPython = Resolve-MetadataPython
    $portablePackages = Join-Path $Root "scripts\portable_packages.py"
    $packages = @()
    foreach ($target in $targets) {
        $before = Get-ZipSnapshot -Directory $transactionRoot
        & (Join-Path $target.root "Build-Package.ps1") -Profile Full -Device $target.device -Version $Version -OutputRoot $transactionRoot
        if ($LASTEXITCODE -ne 0) { throw "$($target.component) full package build failed" }
        $after = Get-ZipSnapshot -Directory $transactionRoot
        $changed = @(Get-ChildItem -LiteralPath $transactionRoot -Filter "*.zip" -File | Where-Object {
            !$before.ContainsKey($_.FullName) -or $before[$_.FullName] -ne $after[$_.FullName]
        })
        if ($changed.Count -ne 1) { throw "$($target.component) did not produce exactly one changed full ZIP" }
        $selectArguments = @(
            "select-full-package",
            "--zip", $changed[0].FullName,
            "--expected-component", $target.component,
            "--expected-version", $Version,
            "--requested-profile", ([string]$target.device).ToLowerInvariant()
        )
        $selectionOutput = @(& $metadataPython.command @($metadataPython.prefix) $portablePackages @selectArguments 2>&1)
        $selectionExit = $LASTEXITCODE
        if ($selectionExit -ne 0 -or $selectionOutput.Count -ne 1) { throw "$($target.component) full package metadata verification failed" }
        $selection = ([string]$selectionOutput[0]) | ConvertFrom-Json
        if (!$selection.valid) { throw "$($target.component) full package metadata verification failed" }
        $packages += [ordered]@{
            component=$target.component
            file=[string]$selection.filename
            resolved_profile=[string]$selection.resolved_profile
            sha256=[string]$selection.sha256
            source_revision=[string]$selection.source_revision
        }
    }

    $matrix = [ordered]@{
        schema_version=1; release_train=$Version; platform="windows-x64"; profile="full"
        requested_device=$Device.ToLowerInvariant(); components=$packages
        endpoints=@{"tts-more"=8000;"gpt-sovits"=9880;"indextts"=9881;"cosyvoice"=9882}
    }
    $matrix | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $transactionRoot "compatibility-matrix.json") -Encoding UTF8
    $material = ($packages | ForEach-Object { "$($_.component)`0$($_.source_revision)`0$($_.resolved_profile)`0$($_.sha256)" }) -join "`n"
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $trainSha = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($material)))).Replace("-", "").ToLowerInvariant() }
    finally { $sha.Dispose() }
    [ordered]@{
        schema_version=1; release_train=$Version; profile="full"; requested_device=$Device.ToLowerInvariant()
        package_set_sha256=$trainSha; generated_at=[DateTime]::UtcNow.ToString("o"); components=$packages
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $transactionRoot "four-pack.provenance.json") -Encoding UTF8

    Move-Item -LiteralPath $transactionRoot -Destination $OutputRoot
    $published = $true
    Write-Host "Created local full four-pack: $OutputRoot"
}
finally {
    if (!$published -and (Test-Path -LiteralPath $transactionRoot)) {
        Remove-Item -LiteralPath $transactionRoot -Recurse -Force
    }
}
