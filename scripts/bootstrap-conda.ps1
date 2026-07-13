[CmdletBinding()]
param(
    [string]$CacheRoot = "data/cache/portable/conda",
    [string]$LockPath = "packaging/portable/toolchain.lock.json",
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:RepoRoot = Split-Path -Parent $PSScriptRoot

function Resolve-RepoPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $script:RepoRoot $Path))
}

function Get-LockedMiniforge {
    param([Parameter(Mandatory = $true)][string]$ResolvedLockPath)

    if (!(Test-Path -LiteralPath $ResolvedLockPath -PathType Leaf)) {
        throw "Portable toolchain lock is missing: $ResolvedLockPath"
    }
    $lock = Get-Content -LiteralPath $ResolvedLockPath -Raw | ConvertFrom-Json
    $toolchain = $lock.miniforge
    foreach ($field in @("version", "archive", "url", "sha256")) {
        if ([string]::IsNullOrWhiteSpace([string]$toolchain.$field)) {
            throw "Portable toolchain lock field is missing: miniforge.$field"
        }
    }
    return $toolchain
}

function Test-LockedSha256 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )

    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    return $actual -eq $ExpectedSha256.ToLowerInvariant()
}

function Ensure-BuildConda {
    param(
        [string]$CacheRoot = "data/cache/portable/conda",
        [string]$LockPath = "packaging/portable/toolchain.lock.json",
        [switch]$DryRun
    )

    $cache = Resolve-RepoPath $CacheRoot
    $resolvedLockPath = Resolve-RepoPath $LockPath
    $toolchain = Get-LockedMiniforge $resolvedLockPath
    $archive = Join-Path $cache $toolchain.archive
    $installRoot = Join-Path $cache ("miniforge-" + $toolchain.version)
    $conda = Join-Path $installRoot "condabin/conda.bat"
    $packageCache = Join-Path $cache "conda-pkgs"

    $env:CONDA_PKGS_DIRS = $packageCache
    if (Test-Path -LiteralPath $conda -PathType Leaf) {
        Write-Host "[portable-conda] reuse private Conda: $conda"
        return $conda
    }
    if (Test-Path -LiteralPath $installRoot) {
        throw "Private Conda installation is incomplete: $installRoot. Delete only this cache directory, then rerun."
    }
    if ($DryRun) {
        Write-Host "[dry-run] cache root: $cache"
        Write-Host "[dry-run] pinned archive: $archive"
        Write-Host "[dry-run] pinned SHA-256: $($toolchain.sha256)"
        Write-Host "[dry-run] private install root: $installRoot"
        return $conda
    }

    New-Item -ItemType Directory -Force -Path $cache, $packageCache | Out-Null
    if (!(Test-Path -LiteralPath $archive -PathType Leaf)) {
        Write-Host "[portable-conda] downloading pinned Miniforge archive"
        Invoke-WebRequest -Uri $toolchain.url -OutFile $archive -UseBasicParsing
    }
    if (!(Test-LockedSha256 -Path $archive -ExpectedSha256 $toolchain.sha256)) {
        throw "Miniforge SHA-256 does not match toolchain.lock.json: $archive"
    }

    $arguments = @(
        "/InstallationType=JustMe",
        "/RegisterPython=0",
        "/AddToPath=0",
        "/S",
        "/D=$installRoot"
    )
    Write-Host "[portable-conda] installing private Miniforge below $cache"
    $process = Start-Process -FilePath $archive -ArgumentList $arguments -Wait -PassThru -NoNewWindow
    if ($process.ExitCode -ne 0) {
        throw "Private Miniforge installer failed with exit code $($process.ExitCode)"
    }
    if (!(Test-Path -LiteralPath $conda -PathType Leaf)) {
        throw "Private Miniforge did not provide conda.bat: $conda"
    }
    return $conda
}

$privateConda = Ensure-BuildConda -CacheRoot $CacheRoot -LockPath $LockPath -DryRun:$DryRun
Write-Host "[portable-conda] conda command: $privateConda"
