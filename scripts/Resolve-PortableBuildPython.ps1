[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PackageRoot,
    [Parameter(Mandatory = $true)][string]$BuildToolsRoot,
    [Parameter(Mandatory = $true)][string]$BootstrapCondaPath,
    [Parameter(Mandatory = $true)][string]$ToolchainLockPath,
    [Parameter(Mandatory = $true)][string]$PortableInstallPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-RequiredFile {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    $resolved = [IO.Path]::GetFullPath($Path)
    if (!(Test-Path -LiteralPath $resolved -PathType Leaf)) { throw "$Label is missing: $resolved" }
    return $resolved
}

function Assert-NoReparsePathSegments {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd("\", "/")
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    $boundary = $resolvedRoot + [IO.Path]::DirectorySeparatorChar
    if (!$resolvedPath.StartsWith($boundary, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must remain below the package source root"
    }
    $relative = $resolvedPath.Substring($boundary.Length)
    $current = $resolvedRoot
    foreach ($segment in @($relative -split '[\\/]' | Where-Object { $_ })) {
        $current = Join-Path $current $segment
        if (!(Test-Path -LiteralPath $current)) { continue }
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label must not traverse a reparse point: $current"
        }
    }
}

function Assert-NoReparseTree {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    Assert-NoReparsePathSegments -Root $Root -Path $Path -Label $Label
    if (!(Test-Path -LiteralPath $Path)) { return }
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push([IO.Path]::GetFullPath($Path))
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a reparse point: $current"
        }
        if (!$item.PSIsContainer) { continue }
        foreach ($child in Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop) {
            if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label contains a reparse point: $($child.FullName)"
            }
            if ($child.PSIsContainer) { $pending.Push($child.FullName) }
        }
    }
}

function Resolve-PackageChildDirectory {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    $resolved = [IO.Path]::GetFullPath($Path)
    $boundary = $script:ResolvedPackageRoot.TrimEnd("\", "/") + [IO.Path]::DirectorySeparatorChar
    if (!$resolved.StartsWith($boundary, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must remain below the package source root"
    }
    Assert-NoReparsePathSegments -Root $script:ResolvedPackageRoot -Path $resolved -Label $Label
    return $resolved
}

function Test-Python311 {
    param([Parameter(Mandatory = $true)][string]$Python)
    if (!(Test-Path -LiteralPath $Python -PathType Leaf)) { return $false }
    & $Python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}

function Test-BuildPython {
    param([Parameter(Mandatory = $true)][string]$Python)
    if (!(Test-Python311 -Python $Python)) { return $false }
    & $Python -c "import jsonschema; from importlib.metadata import version; raise SystemExit(0 if version('jsonschema') == '4.26.0' else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}

function Test-LockedUv {
    param([Parameter(Mandatory = $true)][string]$UvExe)
    if (!(Test-Path -LiteralPath $UvExe -PathType Leaf)) { return $false }
    $versionOutput = @(& $UvExe --version 2>&1)
    return $LASTEXITCODE -eq 0 -and $versionOutput.Count -eq 1 -and [regex]::IsMatch([string]$versionOutput[0], "^uv 0\.11\.28(?:\s|$)")
}

function Remove-OwnedCacheDirectory {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$CacheRoot)
    if (!(Test-Path -LiteralPath $Path)) { return }
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    $resolvedCache = [IO.Path]::GetFullPath($CacheRoot).TrimEnd("\", "/")
    if (!$resolvedPath.StartsWith($resolvedCache + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "refusing to replace a build-tool cache outside the package-private cache root"
    }
    Assert-NoReparsePathSegments -Root $script:ResolvedPackageRoot -Path $resolvedCache -Label "build-tools cache root"
    Assert-NoReparsePathSegments -Root $script:ResolvedPackageRoot -Path $resolvedPath -Label "owned build-tools cache"
    Assert-NoReparseTree -Root $script:ResolvedPackageRoot -Path $resolvedPath -Label "owned build-tools cache"
    Remove-Item -LiteralPath $resolvedPath -Recurse -Force
}

$script:ResolvedPackageRoot = [IO.Path]::GetFullPath($PackageRoot)
if (!(Test-Path -LiteralPath $script:ResolvedPackageRoot -PathType Container)) {
    throw "package source root is missing: $script:ResolvedPackageRoot"
}
$resolvedBuildTools = Resolve-PackageChildDirectory -Path $BuildToolsRoot -Label "build-tools project"
$pyproject = Resolve-RequiredFile -Path (Join-Path $resolvedBuildTools "pyproject.toml") -Label "build-tools pyproject"
$uvLock = Resolve-RequiredFile -Path (Join-Path $resolvedBuildTools "uv.lock") -Label "build-tools uv.lock"
$bootstrapConda = Resolve-PackageChildDirectory -Path (Resolve-RequiredFile -Path $BootstrapCondaPath -Label "private Conda bootstrap") -Label "private Conda bootstrap"
$toolchainLock = Resolve-PackageChildDirectory -Path (Resolve-RequiredFile -Path $ToolchainLockPath -Label "portable toolchain lock") -Label "portable toolchain lock"
$portableInstall = Resolve-PackageChildDirectory -Path (Resolve-RequiredFile -Path $PortableInstallPath -Label "portable asset installer") -Label "portable asset installer"

if (![string]::IsNullOrWhiteSpace([string]$env:TTS_MORE_BUILD_PYTHON)) {
    $explicitPython = [IO.Path]::GetFullPath([string]$env:TTS_MORE_BUILD_PYTHON)
    if (!(Test-BuildPython -Python $explicitPython)) {
        throw "TTS_MORE_BUILD_PYTHON must be Python 3.11 with locked jsonschema 4.26.0"
    }
    Write-Output $explicitPython
    return
}

$toolchain = Get-Content -LiteralPath $toolchainLock -Raw | ConvertFrom-Json
if (
    [string]$toolchain.uv.version -ne "0.11.28" -or
    [string]::IsNullOrWhiteSpace([string]$toolchain.uv.url) -or
    [string]$toolchain.uv.sha256 -notmatch "^[0-9a-fA-F]{64}$"
) {
    throw "portable toolchain lock must pin uv 0.11.28 with URL and SHA-256"
}
$uvArchive = [string]$toolchain.uv.archive
if (
    [string]::IsNullOrWhiteSpace($uvArchive) -or
    [IO.Path]::IsPathRooted($uvArchive) -or
    $uvArchive -in @(".", "..") -or
    $uvArchive -ne [IO.Path]::GetFileName($uvArchive) -or
    $uvArchive.Contains("\") -or
    $uvArchive.Contains("/")
) {
    throw "portable toolchain lock uv.archive must be a single safe file name"
}
$uvSize = [int64]0
$uvSizeText = [string]$toolchain.uv.size_bytes
if (
    ![int64]::TryParse(
        $uvSizeText,
        [Globalization.NumberStyles]::None,
        [Globalization.CultureInfo]::InvariantCulture,
        [ref]$uvSize
    ) -or $uvSize -le 0
) {
    throw "portable toolchain lock uv.size_bytes must be a positive integer"
}

$cacheRoot = Resolve-PackageChildDirectory -Path (Join-Path $script:ResolvedPackageRoot "data\cache\portable\build-tools") -Label "build-tools cache"
$condaCache = Resolve-PackageChildDirectory -Path (Join-Path $script:ResolvedPackageRoot "data\cache\portable\conda") -Label "private Conda cache"
$condaPackageCache = Resolve-PackageChildDirectory -Path (Join-Path $condaCache "conda-pkgs") -Label "private Conda package cache"
New-Item -ItemType Directory -Force -Path $cacheRoot | Out-Null

$condaOutput = @(& $bootstrapConda -CacheRoot $condaCache -LockPath $toolchainLock -PackageRoot $script:ResolvedPackageRoot -PassThru)
if ($LASTEXITCODE -ne 0 -or $condaOutput.Count -eq 0) { throw "private Conda bootstrap failed for build tools" }
$conda = Resolve-PackageChildDirectory -Path ([string]$condaOutput[-1]) -Label "private Conda command"
if (!(Test-Path -LiteralPath $conda -PathType Leaf)) { throw "private Conda command is missing: $conda" }
$condaBasePython = Resolve-PackageChildDirectory -Path (Join-Path (Split-Path -Parent (Split-Path -Parent $conda)) "python.exe") -Label "private Miniforge base Python"
if (!(Test-Path -LiteralPath $condaBasePython -PathType Leaf)) { throw "private Miniforge base Python is missing" }

$assetRoot = Resolve-PackageChildDirectory -Path (Join-Path $cacheRoot "assets") -Label "build-tools asset cache"
New-Item -ItemType Directory -Force -Path $assetRoot | Out-Null
$uvAssetLock = Resolve-PackageChildDirectory -Path (Join-Path $assetRoot "uv.json") -Label "locked uv asset metadata"
$uvAsset = [ordered]@{
    id = "uv-0.11.28-windows-x64"
    urls = @([string]$toolchain.uv.url)
    sha256 = [string]$toolchain.uv.sha256
    size_bytes = $uvSize
}
$uvAsset | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $uvAssetLock -Encoding UTF8
$uvWheel = Resolve-PackageChildDirectory -Path (Join-Path $assetRoot $uvArchive) -Label "locked uv wheel"
if (![string]::Equals([IO.Path]::GetFullPath((Split-Path -Parent $uvWheel)), $assetRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "locked uv wheel must remain a direct child of the package asset cache"
}
& $condaBasePython $portableInstall ensure-asset --asset $uvAssetLock --path $uvWheel
if ($LASTEXITCODE -ne 0) { throw "locked uv 0.11.28 asset initialization failed" }

$uvBootstrap = Resolve-PackageChildDirectory -Path (Join-Path $cacheRoot "uv-bootstrap") -Label "uv bootstrap environment"
Assert-NoReparseTree -Root $script:ResolvedPackageRoot -Path $uvBootstrap -Label "uv bootstrap environment"
$uvBootstrapPython = Resolve-PackageChildDirectory -Path (Join-Path $uvBootstrap "python.exe") -Label "uv bootstrap Python"
$uvExe = Resolve-PackageChildDirectory -Path (Join-Path $uvBootstrap "Scripts\uv.exe") -Label "locked uv executable"
if (!(Test-Python311 -Python $uvBootstrapPython) -or !(Test-LockedUv -UvExe $uvExe)) {
    Remove-OwnedCacheDirectory -Path $uvBootstrap -CacheRoot $cacheRoot
    $uvBootstrapStaging = Resolve-PackageChildDirectory -Path (Join-Path $cacheRoot (".uv-bootstrap-" + $PID + "-" + [Guid]::NewGuid().ToString("N"))) -Label "uv bootstrap staging"
    try {
        & $conda create --yes --prefix $uvBootstrapStaging "python=3.11" pip
        if ($LASTEXITCODE -ne 0) { throw "private Conda failed to create the Python 3.11 uv bootstrap" }
        $stagingPython = Resolve-PackageChildDirectory -Path (Join-Path $uvBootstrapStaging "python.exe") -Label "uv bootstrap staging Python"
        & $stagingPython -m pip install --no-deps $uvWheel
        if ($LASTEXITCODE -ne 0) { throw "locked uv wheel installation failed" }
        $stagingUv = Resolve-PackageChildDirectory -Path (Join-Path $uvBootstrapStaging "Scripts\uv.exe") -Label "uv bootstrap staging executable"
        if (!(Test-Python311 -Python $stagingPython) -or !(Test-LockedUv -UvExe $stagingUv)) {
            throw "private uv bootstrap failed the Python 3.11/uv 0.11.28 probe"
        }
        Move-Item -LiteralPath $uvBootstrapStaging -Destination $uvBootstrap
    }
    finally {
        if (Test-Path -LiteralPath $uvBootstrapStaging) {
            Remove-OwnedCacheDirectory -Path $uvBootstrapStaging -CacheRoot $cacheRoot
        }
    }
}

$environment = Resolve-PackageChildDirectory -Path (Join-Path $cacheRoot "environment") -Label "build-tools environment"
Assert-NoReparseTree -Root $script:ResolvedPackageRoot -Path $environment -Label "build-tools environment"
$lockDigestBefore = (Get-FileHash -LiteralPath $uvLock -Algorithm SHA256).Hash
$previousProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    $env:UV_PROJECT_ENVIRONMENT = $environment
    $syncArguments = @("sync", "--locked", "--project", $resolvedBuildTools, "--python", $uvBootstrapPython)
    & $uvExe @syncArguments
    if ($LASTEXITCODE -ne 0) { throw "uv sync --locked failed for portable build tools" }
}
finally {
    $env:UV_PROJECT_ENVIRONMENT = $previousProjectEnvironment
}
if ((Get-FileHash -LiteralPath $uvLock -Algorithm SHA256).Hash -ne $lockDigestBefore) {
    throw "uv sync modified the locked portable build-tools dependency graph"
}

$buildPython = Resolve-PackageChildDirectory -Path (Join-Path $environment "Scripts\python.exe") -Label "build-tools Python"
if (!(Test-BuildPython -Python $buildPython)) {
    throw "portable build-tools Python 3.11/jsonschema probe failed"
}
Write-Output ([IO.Path]::GetFullPath($buildPython))
