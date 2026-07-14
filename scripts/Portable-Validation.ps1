Set-StrictMode -Version Latest

function Test-PortablePathWithinRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $candidate = [IO.Path]::GetFullPath($Path)
    if ([string]::Equals($rootPath, $candidate, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    return $candidate.StartsWith($rootPath + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
}

function Resolve-PortablePackagePath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$MustExist
    )
    if ([string]::IsNullOrWhiteSpace($RelativePath) -or [IO.Path]::IsPathRooted($RelativePath) -or $RelativePath.Contains(":")) {
        throw "$Label must be a package-relative path"
    }
    $segments = @($RelativePath -split '[\\/]')
    if ($segments -contains "..") { throw "$Label cannot escape the package" }
    $resolvedRoot = [IO.Path]::GetFullPath($Root)
    $resolved = [IO.Path]::GetFullPath((Join-Path $resolvedRoot $RelativePath))
    if (!(Test-PortablePathWithinRoot -Root $resolvedRoot -Path $resolved)) { throw "$Label resolves outside the package" }
    $current = $resolvedRoot
    foreach ($segment in $segments) {
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -eq ".") { continue }
        $current = Join-Path $current $segment
        if (Test-Path -LiteralPath $current) {
            if (([IO.File]::GetAttributes($current) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label traverses a reparse point"
            }
        }
    }
    if ($MustExist -and !(Test-Path -LiteralPath $resolved)) { throw "$Label is missing: $RelativePath" }
    return $resolved
}

function Assert-PortableExactOperationContract {
    param(
        [Parameter(Mandatory = $true)][string]$OperationsRoot,
        [Parameter(Mandatory = $true)][string]$OperationRoot,
        [Parameter(Mandatory = $true)][string]$CancelFile,
        [switch]$RequireOperation
    )
    $resolvedOperations = [IO.Path]::GetFullPath($OperationsRoot)
    $resolvedOperation = [IO.Path]::GetFullPath($OperationRoot)
    if (![string]::Equals((Split-Path -Parent $resolvedOperation), $resolvedOperations, [StringComparison]::OrdinalIgnoreCase)) {
        throw "OperationRoot must be a UUID-named direct child of the package operations root"
    }
    $parsed = [guid]::Empty
    if (![guid]::TryParse((Split-Path -Leaf $resolvedOperation), [ref]$parsed)) { throw "OperationRoot name must be a valid UUID" }
    if ($RequireOperation -and !(Test-Path -LiteralPath $resolvedOperation -PathType Container)) { throw "OperationRoot is missing" }
    if (Test-Path -LiteralPath $resolvedOperation) {
        if (([IO.File]::GetAttributes($resolvedOperation) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "OperationRoot cannot be a reparse point"
        }
    }
    $resolvedCancel = [IO.Path]::GetFullPath($CancelFile)
    $expectedCancel = [IO.Path]::GetFullPath((Join-Path $resolvedOperation "cancel.requested"))
    if (![string]::Equals($resolvedCancel, $expectedCancel, [StringComparison]::OrdinalIgnoreCase)) {
        throw "CancelFile must resolve exactly to OperationRoot/cancel.requested"
    }
    return [pscustomobject]@{ OperationRoot = $resolvedOperation; CancelFile = $resolvedCancel }
}

function Assert-PortableRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [string]$ImportProbe = "import sys"
    )
    $expectedPath = [IO.Path]::GetFullPath((Join-Path $Root "runtime\live\python.exe"))
    $resolvedPython = [IO.Path]::GetFullPath($PythonPath)
    if (![string]::Equals($resolvedPython, $expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "package runtime must be runtime/live/python.exe"
    }
    $relative = $resolvedPython.Substring([IO.Path]::GetFullPath($Root).TrimEnd('\', '/').Length).TrimStart('\', '/')
    [void](Resolve-PortablePackagePath -Root $Root -RelativePath $relative -Label "package runtime" -MustExist)
    if (!(Test-Path -LiteralPath $resolvedPython -PathType Leaf)) { throw "package runtime is missing" }
    $versionOutput = @(& $resolvedPython -c "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>&1)
    if ($LASTEXITCODE -ne 0 -or ($versionOutput -join "").Trim() -ne $ExpectedVersion) {
        throw "package runtime Python version must be $ExpectedVersion"
    }
    if (![string]::IsNullOrWhiteSpace($ImportProbe)) {
        & $resolvedPython -c $ImportProbe *> $null
        if ($LASTEXITCODE -ne 0) { throw "package runtime import probe failed" }
    }
    return $resolvedPython
}

function Test-PortableRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [string]$ImportProbe = "import sys"
    )
    try {
        [void](Assert-PortableRuntime -Root $Root -PythonPath $PythonPath -ExpectedVersion $ExpectedVersion -ImportProbe $ImportProbe)
        return $true
    } catch { return $false }
}

function Test-PortableLockedAssets {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ModelLock
    )
    try {
        if (!(Test-Path -LiteralPath $ModelLock -PathType Leaf)) { return $false }
        $lock = Get-Content -LiteralPath $ModelLock -Raw | ConvertFrom-Json
        $requiredPaths = if ($lock.PSObject.Properties["required_paths"]) { @($lock.required_paths) } else { @() }
        foreach ($relative in $requiredPaths) {
            if ([string]::IsNullOrWhiteSpace([string]$relative)) { continue }
            $required = Resolve-PortablePackagePath -Root $Root -RelativePath ([string]$relative) -Label "model required path" -MustExist
            if (!(Test-Path -LiteralPath $required)) { return $false }
        }
        $assets = if ($lock.PSObject.Properties["assets"]) { @($lock.assets) } else { @() }
        foreach ($asset in $assets) {
            if ($null -eq $asset -or [string]::IsNullOrWhiteSpace([string]$asset.target)) { continue }
            $target = Resolve-PortablePackagePath -Root $Root -RelativePath ([string]$asset.target) -Label "model asset" -MustExist
            if (!(Test-Path -LiteralPath $target -PathType Leaf)) { return $false }
            if ($null -ne $asset.PSObject.Properties["size_bytes"] -and [int64]$asset.size_bytes -ne (Get-Item -LiteralPath $target).Length) { return $false }
            if ($null -ne $asset.PSObject.Properties["sha256"] -and ![string]::IsNullOrWhiteSpace([string]$asset.sha256)) {
                if (![string]::Equals((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash, [string]$asset.sha256, [StringComparison]::OrdinalIgnoreCase)) { return $false }
            }
        }
        return $true
    } catch { return $false }
}

function Assert-PortableSha256Manifest {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [string[]]$RequiredCoverage = @()
    )
    if (!(Test-Path -LiteralPath $ManifestPath -PathType Leaf)) { throw "SHA256SUMS manifest is missing" }
    $covered = @{}
    foreach ($line in [IO.File]::ReadAllLines($ManifestPath)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line -notmatch '^([0-9a-fA-F]{64})\s{2}(.+)$') { throw "SHA256SUMS contains an invalid record" }
        $relative = $Matches[2].Replace('/', '\')
        $path = Resolve-PortablePackagePath -Root $Root -RelativePath $relative -Label "SHA256SUMS entry" -MustExist
        $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        if (![string]::Equals($actual, $Matches[1], [StringComparison]::OrdinalIgnoreCase)) { throw "SHA256SUMS hash mismatch: $relative" }
        $covered[[IO.Path]::GetFullPath($path).ToLowerInvariant()] = $true
    }
    foreach ($path in $RequiredCoverage) {
        if ([string]::IsNullOrWhiteSpace($path)) { continue }
        $key = [IO.Path]::GetFullPath($path).ToLowerInvariant()
        if (!$covered.ContainsKey($key)) { throw "SHA256SUMS does not cover required package file: $path" }
    }
}

function Test-PortableInstallStateComplete {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)][string]$Component,
        [Parameter(Mandatory = $true)][string]$BuildId,
        [Parameter(Mandatory = $true)][string]$RuntimeLock,
        [Parameter(Mandatory = $true)][string]$ModelLock,
        [Parameter(Mandatory = $true)][string]$ExpectedPython,
        [string]$ImportProbe = "import sys",
        [switch]$ValidateAssets,
        [string]$Sha256Manifest = "",
        [string[]]$RequiredCoverage = @()
    )
    try {
        if (!(Test-Path -LiteralPath $StatePath -PathType Leaf)) { return $false }
        $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
        if ([int]$state.schema_version -ne 1 -or !$state.ready) { return $false }
        if ([string]$state.component -ne $Component -or [string]$state.build_id -ne $BuildId) { return $false }
        if ([string]::IsNullOrWhiteSpace([string]$state.profile)) { return $false }
        if (Test-Path -LiteralPath $RuntimeLock -PathType Leaf) {
            $runtimePayload = Get-Content -LiteralPath $RuntimeLock -Raw | ConvertFrom-Json
            if ($ValidateAssets -and (!$runtimePayload.PSObject.Properties["component"] -or [string]$runtimePayload.component -ne $Component)) { return $false }
            if ($runtimePayload.PSObject.Properties["python_version"] -and [string]$runtimePayload.python_version -ne $ExpectedPython) { return $false }
            if ($runtimePayload.PSObject.Properties["profiles"] -and !$runtimePayload.profiles.PSObject.Properties[[string]$state.profile]) { return $false }
            $runtimeHash = (Get-FileHash -LiteralPath $RuntimeLock -Algorithm SHA256).Hash.ToLowerInvariant()
            if ([string]$state.runtime_lock_sha256 -ne $runtimeHash) { return $false }
        }
        if (Test-Path -LiteralPath $ModelLock -PathType Leaf) {
            $modelPayload = Get-Content -LiteralPath $ModelLock -Raw | ConvertFrom-Json
            if ($ValidateAssets -and (!$modelPayload.PSObject.Properties["component"] -or [string]$modelPayload.component -ne $Component)) { return $false }
            $modelHash = (Get-FileHash -LiteralPath $ModelLock -Algorithm SHA256).Hash.ToLowerInvariant()
            if ([string]$state.model_lock_sha256 -ne $modelHash) { return $false }
        }
        $python = Join-Path $Root "runtime\live\python.exe"
        if (!(Test-PortableRuntime -Root $Root -PythonPath $python -ExpectedVersion $ExpectedPython -ImportProbe $ImportProbe)) { return $false }
        if ($ValidateAssets -and !(Test-PortableLockedAssets -Root $Root -ModelLock $ModelLock)) { return $false }
        if ($ValidateAssets -and $Sha256Manifest) {
            Assert-PortableSha256Manifest -Root $Root -ManifestPath $Sha256Manifest -RequiredCoverage $RequiredCoverage
        }
        return $true
    } catch { return $false }
}
