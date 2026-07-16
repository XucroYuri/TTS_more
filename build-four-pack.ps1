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

function Assert-CleanSourceRepository {
    param([Parameter(Mandatory = $true)]$Target)
    $status = @(& git -C $Target.root status --porcelain=v1 --untracked-files=no 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "$($Target.component) source is not a readable Git repository" }
    if (@($status | Where-Object { ![string]::IsNullOrWhiteSpace([string]$_) }).Count -ne 0) {
        throw "$($Target.component) source is dirty; tracked content is not revision-bound"
    }
    $allUntracked = @(& git -C $Target.root ls-files --others --directory 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "$($Target.component) untracked source inventory failed" }
    $copiedUntracked = @($allUntracked | Where-Object {
        $relative = ([string]$_).Replace("\", "/")
        if ([string]::IsNullOrWhiteSpace($relative)) { return $false }
        $parts = @($relative.TrimEnd("/").Split("/"))
        if ($Target.component -eq "tts-more") {
            if (@($parts | Where-Object { $_ -in @("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache") }).Count -ne 0) { return $false }
            $controllerFiles = @(
                "backend/pyproject.toml", "backend/uv.lock", "backend/.python-version",
                "Initialize.cmd", "Start.cmd", "Stop.cmd", "Repair.cmd", "LICENSE", "NOTICE", "repo.lock.json",
                "packaging/portable/toolchain.lock.json", "packaging/portable/runtime.lock.json", "packaging/portable/models.lock.json",
                "packaging/portable/tts-more-package.schema.json", "packaging/portable/error-catalog.zh-CN.json", "packaging/portable/使用说明-先看这里.txt"
            )
            $controllerScripts = @(
                "bootstrap-conda.ps1", "initialize-portable.ps1", "repair-portable.ps1", "start-production.ps1", "stop-production.ps1",
                "Invoke-PortableStart.ps1", "Show-PortableProgress.ps1", "Portable-Validation.ps1", "select-portable-folder.ps1",
                "export-portable-diagnostics.py", "import-portable-data.py", "import_portable_data.py", "portable_install.py",
                "portable_launcher.py", "portable_operations.py", "portable_packages.py", "portable_package_runner.py"
            )
            return $relative.StartsWith("backend/app/", [StringComparison]::OrdinalIgnoreCase) -or
                $relative.StartsWith("frontend/dist/", [StringComparison]::OrdinalIgnoreCase) -or
                $relative -in $controllerFiles -or
                ($relative.StartsWith("scripts/", [StringComparison]::OrdinalIgnoreCase) -and $relative.Substring(8) -in $controllerScripts)
        }
        $workerRootExcluded = @(".git", ".venv", "runtime", "data", "artifacts", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
        $workerRecursiveExcluded = @(".git", ".venv", "artifacts", "cache", ".cache", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
        $modelDirectories = @("models", "pretrained_models", "checkpoints", "SoVITS_weights", "GPT_weights")
        if ($parts[0] -in $workerRootExcluded -or @($parts | Where-Object { $_ -in $workerRecursiveExcluded }).Count -ne 0) { return $false }
        if (@($parts | Where-Object { $_ -in $modelDirectories }).Count -ne 0 -or $relative -match "\.(safetensors|ckpt|pth|pt|t7|onnx|bin)/?$") { return $false }
        if ($parts[-1] -match '^\.env(?:\..+)?$') { return $false }
        return $true
    })
    if ($copiedUntracked.Count -ne 0) {
        throw "$($Target.component) source is dirty; copied untracked content is not revision-bound: $($copiedUntracked[0])"
    }
    $submoduleStatus = @(& git -C $Target.root submodule status --recursive 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "$($Target.component) recursive submodule status failed" }
    if (@($submoduleStatus | Where-Object { ([string]$_) -match "^[+-U]" }).Count -ne 0) {
        throw "$($Target.component) recursive submodule revision is not bound to HEAD"
    }
    $dirtySubmodules = @(& git -C $Target.root submodule foreach --quiet --recursive "git status --porcelain=v1 --untracked-files=all" 2>&1)
    if ($LASTEXITCODE -ne 0 -or @($dirtySubmodules | Where-Object { ![string]::IsNullOrWhiteSpace([string]$_) }).Count -ne 0) {
        throw "$($Target.component) recursive submodule source is dirty"
    }
}

function Invoke-MetadataPython {
    param(
        [Parameter(Mandatory = $true)]$MetadataPython,
        [Parameter(Mandatory = $true)][string]$PortablePackages,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Failure
    )
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = @(& $MetadataPython.command @($MetadataPython.prefix) $PortablePackages @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($exitCode -ne 0) { throw "$Failure`: $($output -join [Environment]::NewLine)" }
    return $output
}

function Get-ValidatedFullPackage {
    param(
        [Parameter(Mandatory = $true)]$Target,
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [Parameter(Mandatory = $true)]$MetadataPython,
        [Parameter(Mandatory = $true)][string]$PortablePackages
    )
    $selectArguments = @(
        "select-full-package",
        "--zip", $ZipPath,
        "--expected-component", $Target.component,
        "--expected-version", $Version,
        "--requested-profile", ([string]$Target.device).ToLowerInvariant(),
        "--expected-source-revision", [string]$Target.expected_revision
    )
    $selectionOutput = @(Invoke-MetadataPython -MetadataPython $MetadataPython -PortablePackages $PortablePackages -Arguments $selectArguments -Failure "$($Target.component) full package metadata verification failed")
    if ($selectionOutput.Count -ne 1) { throw "$($Target.component) full package metadata verification failed" }
    $selection = ([string]$selectionOutput[0]) | ConvertFrom-Json
    if (!$selection.valid) { throw "$($Target.component) full package metadata verification failed" }
    return $selection
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

$metadataPython = Resolve-MetadataPython
$portablePackages = Join-Path $Root "scripts\portable_packages.py"
$controllerRevision = (& git -C $Root rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $controllerRevision -notmatch "^[0-9a-f]{40}$") {
    throw "tts-more source revision is not an exact Git commit"
}
$targets[0].expected_revision = $controllerRevision
foreach ($target in $targets) {
    if ([string]$target.expected_revision -notmatch "^[0-9a-f]{40}$") {
        throw "$($target.component) expected source revision is invalid"
    }
    $actualRevision = (& git -C $target.root rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $actualRevision -ne $target.expected_revision) {
        throw "$($target.component) source revision drift: expected $($target.expected_revision), found $actualRevision"
    }
    Assert-CleanSourceRepository -Target $target
}

$pathArguments = @("validate-transaction-paths", "--output-root", $OutputRoot, "--controller-root", $Root)
foreach ($target in $targets) { $pathArguments += @("--source-root", [string]$target.root) }
Invoke-MetadataPython -MetadataPython $metadataPython -PortablePackages $portablePackages -Arguments $pathArguments -Failure "four-pack OutputRoot/source-root safety validation failed" | Out-Null

if (Test-Path -LiteralPath $OutputRoot) { throw "OutputRoot already exists; use a new version directory so previous packages remain unchanged" }
$outputParent = [System.IO.Path]::GetDirectoryName($OutputRoot)
if ([string]::IsNullOrWhiteSpace($outputParent)) { throw "OutputRoot must have a parent directory" }
New-Item -ItemType Directory -Force -Path $outputParent | Out-Null
$parentIdentity = [string](@(Invoke-MetadataPython -MetadataPython $metadataPython -PortablePackages $portablePackages -Arguments @("directory-identity", "--path", $outputParent) -Failure "four-pack output parent identity capture failed")[0])
$transactionRoot = Join-Path $outputParent (".tts-more-four-pack-transaction-$PID-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $transactionRoot | Out-Null
$transactionIdentity = [string](@(Invoke-MetadataPython -MetadataPython $metadataPython -PortablePackages $portablePackages -Arguments @("directory-identity", "--path", $transactionRoot) -Failure "four-pack transaction identity capture failed")[0])
$published = $false
try {
    $builtPackages = @()
    foreach ($target in $targets) {
        $before = Get-ZipSnapshot -Directory $transactionRoot
        & (Join-Path $target.root "Build-Package.ps1") -Profile Full -Device $target.device -Version $Version -OutputRoot $transactionRoot
        if ($LASTEXITCODE -ne 0) { throw "$($target.component) full package build failed" }
        $after = Get-ZipSnapshot -Directory $transactionRoot
        $changed = @(Get-ChildItem -LiteralPath $transactionRoot -Filter "*.zip" -File | Where-Object {
            !$before.ContainsKey($_.FullName) -or $before[$_.FullName] -ne $after[$_.FullName]
        })
        if ($changed.Count -ne 1) { throw "$($target.component) did not produce exactly one changed full ZIP" }
        $selection = Get-ValidatedFullPackage -Target $target -ZipPath $changed[0].FullName -MetadataPython $metadataPython -PortablePackages $portablePackages
        $builtPackages += [pscustomobject]@{
            target=$target
            path=$changed[0].FullName
        }
    }

    $expectedAssetNames = @()
    foreach ($built in $builtPackages) {
        $zipName = [System.IO.Path]::GetFileName([string]$built.path)
        foreach ($suffix in @("", ".sha256", ".spdx.json", ".licenses.json", ".provenance.json", ".acceptance.json")) {
            $expectedAssetNames += "$zipName$suffix"
        }
    }
    $actualAssetNames = @(Get-ChildItem -LiteralPath $transactionRoot | ForEach-Object { $_.Name })
    if ($expectedAssetNames.Count -ne 24 -or @($expectedAssetNames | Sort-Object -Unique).Count -ne 24 -or (Compare-Object ($expectedAssetNames | Sort-Object) ($actualAssetNames | Sort-Object))) {
        throw "four-pack final asset set must contain exactly four packages with six assets each"
    }

    $packages = @()
    foreach ($built in $builtPackages) {
        $selection = Get-ValidatedFullPackage -Target $built.target -ZipPath $built.path -MetadataPython $metadataPython -PortablePackages $portablePackages
        $packages += [ordered]@{
            component=[string]$built.target.component
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

    $finalAssetNames = @(Get-ChildItem -LiteralPath $transactionRoot | ForEach-Object { $_.Name })
    $expectedFinalNames = @($expectedAssetNames + @("compatibility-matrix.json", "four-pack.provenance.json"))
    if ($expectedFinalNames.Count -ne 26 -or (Compare-Object ($expectedFinalNames | Sort-Object) ($finalAssetNames | Sort-Object))) {
        throw "four-pack final publication layout is not the exact 4x6 plus matrix/provenance set"
    }
    Invoke-MetadataPython -MetadataPython $metadataPython -PortablePackages $portablePackages -Arguments @("publish-directory", "--source", $transactionRoot, "--destination", $OutputRoot, "--expected-identity", $transactionIdentity, "--expected-parent-identity", $parentIdentity) -Failure "four-pack atomic no-replace publication failed" | Out-Null
    $published = $true
    Write-Host "Created local full four-pack: $OutputRoot"
}
finally {
    if (!$published -and (Test-Path -LiteralPath $transactionRoot)) {
        Invoke-MetadataPython -MetadataPython $metadataPython -PortablePackages $portablePackages -Arguments @("cleanup-directory", "--path", $transactionRoot, "--expected-identity", $transactionIdentity) -Failure "four-pack transaction cleanup refused because directory identity changed" | Out-Null
    }
}
