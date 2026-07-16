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

function Resolve-PackageChildDirectory {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    $resolved = [IO.Path]::GetFullPath($Path)
    $boundary = $script:ResolvedPackageRoot.TrimEnd("\", "/") + [IO.Path]::DirectorySeparatorChar
    if (!$resolved.StartsWith($boundary, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must remain below the package source root"
    }
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
    $item = Get-Item -LiteralPath $resolvedPath -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "refusing to replace a reparse-point build-tool cache: $resolvedPath"
    }
    Remove-Item -LiteralPath $resolvedPath -Recurse -Force
}

$script:ResolvedPackageRoot = [IO.Path]::GetFullPath($PackageRoot)
if (!(Test-Path -LiteralPath $script:ResolvedPackageRoot -PathType Container)) {
    throw "package source root is missing: $script:ResolvedPackageRoot"
}
$resolvedBuildTools = Resolve-PackageChildDirectory -Path $BuildToolsRoot -Label "build-tools project"
$pyproject = Resolve-RequiredFile -Path (Join-Path $resolvedBuildTools "pyproject.toml") -Label "build-tools pyproject"
$uvLock = Resolve-RequiredFile -Path (Join-Path $resolvedBuildTools "uv.lock") -Label "build-tools uv.lock"
$bootstrapConda = Resolve-RequiredFile -Path $BootstrapCondaPath -Label "private Conda bootstrap"
$toolchainLock = Resolve-RequiredFile -Path $ToolchainLockPath -Label "portable toolchain lock"
$portableInstall = Resolve-RequiredFile -Path $PortableInstallPath -Label "portable asset installer"

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

$cacheRoot = Resolve-PackageChildDirectory -Path (Join-Path $script:ResolvedPackageRoot "data\cache\portable\build-tools") -Label "build-tools cache"
$condaCache = Resolve-PackageChildDirectory -Path (Join-Path $script:ResolvedPackageRoot "data\cache\portable\conda") -Label "private Conda cache"
New-Item -ItemType Directory -Force -Path $cacheRoot | Out-Null

$condaOutput = @(& $bootstrapConda -CacheRoot $condaCache -LockPath $toolchainLock -PackageRoot $script:ResolvedPackageRoot -PassThru)
if ($LASTEXITCODE -ne 0 -or $condaOutput.Count -eq 0) { throw "private Conda bootstrap failed for build tools" }
$conda = [IO.Path]::GetFullPath([string]$condaOutput[-1])
if (!(Test-Path -LiteralPath $conda -PathType Leaf)) { throw "private Conda command is missing: $conda" }
$condaBasePython = Join-Path (Split-Path -Parent (Split-Path -Parent $conda)) "python.exe"
if (!(Test-Path -LiteralPath $condaBasePython -PathType Leaf)) { throw "private Miniforge base Python is missing" }

$assetRoot = Join-Path $cacheRoot "assets"
New-Item -ItemType Directory -Force -Path $assetRoot | Out-Null
$uvAssetLock = Join-Path $assetRoot "uv.json"
$uvAsset = [ordered]@{
    id = "uv-0.11.28-windows-x64"
    urls = @([string]$toolchain.uv.url)
    sha256 = [string]$toolchain.uv.sha256
    size_bytes = [int64]$toolchain.uv.size_bytes
}
$uvAsset | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $uvAssetLock -Encoding UTF8
$uvWheel = Join-Path $assetRoot ([string]$toolchain.uv.archive)
& $condaBasePython $portableInstall ensure-asset --asset $uvAssetLock --path $uvWheel
if ($LASTEXITCODE -ne 0) { throw "locked uv 0.11.28 asset initialization failed" }

$uvBootstrap = Join-Path $cacheRoot "uv-bootstrap"
$uvBootstrapPython = Join-Path $uvBootstrap "python.exe"
$uvExe = Join-Path $uvBootstrap "Scripts\uv.exe"
if (!(Test-Python311 -Python $uvBootstrapPython) -or !(Test-LockedUv -UvExe $uvExe)) {
    Remove-OwnedCacheDirectory -Path $uvBootstrap -CacheRoot $cacheRoot
    $uvBootstrapStaging = Join-Path $cacheRoot (".uv-bootstrap-" + $PID + "-" + [Guid]::NewGuid().ToString("N"))
    try {
        & $conda create --yes --prefix $uvBootstrapStaging "python=3.11" pip
        if ($LASTEXITCODE -ne 0) { throw "private Conda failed to create the Python 3.11 uv bootstrap" }
        $stagingPython = Join-Path $uvBootstrapStaging "python.exe"
        & $stagingPython -m pip install --no-deps $uvWheel
        if ($LASTEXITCODE -ne 0) { throw "locked uv wheel installation failed" }
        $stagingUv = Join-Path $uvBootstrapStaging "Scripts\uv.exe"
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

$environment = Join-Path $cacheRoot "environment"
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

$buildPython = Join-Path $environment "Scripts\python.exe"
if (!(Test-BuildPython -Python $buildPython)) {
    throw "portable build-tools Python 3.11/jsonschema probe failed"
}
Write-Output ([IO.Path]::GetFullPath($buildPython))
