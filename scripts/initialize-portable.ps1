[CmdletBinding()]
param(
    [ValidateSet("Auto", "CU128", "CU126", "CPU")][string]$Device = "Auto",
    [switch]$Repair,
    [string]$PackageRoot = "",
    [string]$OperationRoot = "",
    [string]$CancelFile = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ValidationScript = Join-Path $PSScriptRoot "Portable-Validation.ps1"
if (!(Test-Path -LiteralPath $ValidationScript -PathType Leaf)) { throw "Portable-Validation.ps1 is missing" }
. $ValidationScript
$Root = if ([string]::IsNullOrWhiteSpace($PackageRoot)) {
    [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
} else {
    [System.IO.Path]::GetFullPath($PackageRoot)
}
$RuntimeLock = Join-Path $Root "packaging\portable\runtime.lock.json"
$ModelLock = Join-Path $Root "packaging\portable\models.lock.json"
$StatePath = Join-Path $Root "data\local\install-state.json"
$Live = Join-Path $Root "runtime\live"
$Staging = Join-Path $Root "runtime\staging"
$BackendRoot = if (Test-Path -LiteralPath (Join-Path $Root "backend\uv.lock")) { Join-Path $Root "backend" } else { Join-Path $Root "app\backend" }

function Resolve-OperationContract {
    param([string]$PackageRoot, [string]$OperationRoot = "", [string]$CancelFile = "")

    $hasOperation = ![string]::IsNullOrWhiteSpace($OperationRoot)
    $hasCancel = ![string]::IsNullOrWhiteSpace($CancelFile)
    if ($hasOperation -ne $hasCancel) { throw "OperationRoot and CancelFile must be provided together" }
    $resolvedPackage = [System.IO.Path]::GetFullPath($PackageRoot)
    if (!$hasOperation) { return [pscustomobject]@{ OperationRoot = ""; CancelFile = "" } }
    $operationsRelative = "data\local\operations"
    $manifestPath = Join-Path $resolvedPackage "package\tts-more-package.json"
    if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        if ([int]$manifest.schema_version -eq 2) {
            $operationsRelative = [string]$manifest.data.operations
            $segments = @($operationsRelative -split '[\\/]')
            if ([string]::IsNullOrWhiteSpace($operationsRelative) -or [IO.Path]::IsPathRooted($operationsRelative) -or $operationsRelative.Contains(":") -or $segments -contains "..") {
                throw "manifest data.operations must be a package-relative path"
            }
        }
    }
    $operations = [System.IO.Path]::GetFullPath((Join-Path $resolvedPackage $operationsRelative))
    $packagePrefix = $resolvedPackage.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (!$operations.StartsWith($packagePrefix, [StringComparison]::OrdinalIgnoreCase)) { throw "manifest data.operations resolves outside the package" }
    $current = $resolvedPackage
    foreach ($segment in @($operationsRelative -split '[\\/]')) {
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -eq ".") { continue }
        $current = Join-Path $current $segment
        if ((Test-Path -LiteralPath $current) -and (([IO.File]::GetAttributes($current) -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw "manifest data.operations traverses a reparse point"
        }
    }
    $resolvedOperation = [System.IO.Path]::GetFullPath($OperationRoot)
    $operationParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $resolvedOperation))
    if (![string]::Equals($operationParent, $operations, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "OperationRoot must be a UUID-named direct child of the package operations root"
    }
    $parsedId = [guid]::Empty
    if (![guid]::TryParse((Split-Path -Leaf $resolvedOperation), [ref]$parsedId)) {
        throw "OperationRoot name must be a valid UUID"
    }
    $resolvedCancel = [System.IO.Path]::GetFullPath($CancelFile)
    $expectedCancel = [System.IO.Path]::GetFullPath((Join-Path $resolvedOperation "cancel.requested"))
    if (![string]::Equals($resolvedCancel, $expectedCancel, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "CancelFile must resolve exactly to OperationRoot/cancel.requested"
    }
    return Assert-PortableExactOperationContract -OperationsRoot $operations -OperationRoot $resolvedOperation -CancelFile $resolvedCancel -RequireOperation
}

$contract = Resolve-OperationContract -PackageRoot $Root -OperationRoot $OperationRoot -CancelFile $CancelFile
$OperationRoot = $contract.OperationRoot
$CancelFile = $contract.CancelFile
$DownloadArguments = @("--package-root", $Root)
if (![string]::IsNullOrWhiteSpace($OperationRoot)) {
    $DownloadArguments += @("--operation-root", $OperationRoot, "--cancel-file", $CancelFile)
}

function Assert-PortableNotCancelled {
    if (![string]::IsNullOrWhiteSpace($CancelFile) -and (Test-Path -LiteralPath $CancelFile -PathType Leaf)) {
        exit 20
    }
}

Assert-PortableNotCancelled

foreach ($required in @($RuntimeLock, $ModelLock, (Join-Path $BackendRoot "uv.lock"), (Join-Path $Root "scripts\portable_install.py"))) {
    if (!(Test-Path -LiteralPath $required -PathType Leaf)) { throw "required locked package input is missing: $required" }
}
if ($Root.Length -gt 180) { throw "package path is too long for reliable Windows model tooling ($($Root.Length) characters): $Root" }
$drive = Get-PSDrive -Name ([System.IO.Path]::GetPathRoot($Root).Substring(0, 1)) -ErrorAction SilentlyContinue
if ($drive -and $drive.Free -lt 3GB) { throw "at least 3 GB free space is required for transactional initialization" }

$ManifestPath = Join-Path $Root "package\tts-more-package.json"
$BuildId = if (Test-Path -LiteralPath $ManifestPath -PathType Leaf) { [string](Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json).build_id } else { "source-checkout" }
$RuntimePayload = Get-Content -LiteralPath $RuntimeLock -Raw | ConvertFrom-Json
$ExpectedPython = if ([string]::IsNullOrWhiteSpace([string]$RuntimePayload.python_version)) { "3.11" } else { [string]$RuntimePayload.python_version }
$ImportProbe = if ($RuntimePayload.PSObject.Properties["import_probe"] -and ![string]::IsNullOrWhiteSpace([string]$RuntimePayload.import_probe)) { [string]$RuntimePayload.import_probe } else { "import fastapi,pydantic,uvicorn" }
if (Test-PortableInstallStateComplete -Root $Root -StatePath $StatePath -Component "tts-more" -BuildId $BuildId -RuntimeLock $RuntimeLock -ModelLock $ModelLock -ExpectedPython $ExpectedPython -ImportProbe $ImportProbe -ValidateAssets) {
    Write-Host "TTS More package runtime is already verified."
    exit 0
}
if ((Test-PortableLockedAssets -Root $Root -ModelLock $ModelLock) -and (Test-PortableRuntime -Root $Root -PythonPath (Join-Path $Live "python.exe") -ExpectedVersion $ExpectedPython -ImportProbe $ImportProbe)) {
    $ExistingState = if (Test-Path -LiteralPath $StatePath -PathType Leaf) { try { Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json } catch { $null } } else { $null }
    $RequestedProfile = if ($ExistingState -and ![string]::IsNullOrWhiteSpace([string]$ExistingState.profile)) { [string]$ExistingState.profile } else { "" }
    $Profile = Resolve-PortableSupportedProfile -RuntimeLockPayload $RuntimePayload -RequestedProfile $RequestedProfile
    $RuntimeSha = Get-PortableFileSha256 -Path $RuntimeLock
    $ModelSha = Get-PortableFileSha256 -Path $ModelLock
    & (Join-Path $Live "python.exe") (Join-Path $Root "scripts\portable_install.py") write-state --path $StatePath --component tts-more --build-id $BuildId --profile $Profile --runtime-lock-sha256 $RuntimeSha --model-lock-sha256 $ModelSha
    if ($LASTEXITCODE -ne 0) { throw "failed to repair stale install-state.json" }
    if (!(Test-PortableInstallStateComplete -Root $Root -StatePath $StatePath -Component "tts-more" -BuildId $BuildId -RuntimeLock $RuntimeLock -ModelLock $ModelLock -ExpectedPython $ExpectedPython -ImportProbe $ImportProbe -ValidateAssets)) { throw "repaired install-state.json failed complete validation" }
    Write-Host "TTS More package install state was repaired from verified package-private assets."
    exit 0
}
if ($Repair) { Write-Host "Repairing only the missing or failed runtime transaction; user data is preserved." }

$controllersPath = Join-Path $Root "data\cache\portable\video-controllers.json"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $controllersPath) | Out-Null
$videoControllers = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | ForEach-Object {
    [pscustomobject]@{ name = [string]$_.Name; driver_version = [string]$_.DriverVersion }
})
ConvertTo-Json -InputObject $videoControllers | Set-Content -LiteralPath $controllersPath -Encoding UTF8

$bootstrap = Join-Path $Root "scripts\bootstrap-conda.ps1"
$Conda = (& $bootstrap -CacheRoot "data/cache/portable/conda" -LockPath "packaging/portable/toolchain.lock.json" -PackageRoot $Root -OperationRoot $OperationRoot -CancelFile $CancelFile -PassThru | Select-Object -Last 1)
$bootstrapExitCode = if (Get-Variable -Name LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue) { [int]$LASTEXITCODE } else { 0 }
if ($bootstrapExitCode -eq 20) { exit 20 }
if ($bootstrapExitCode -ne 0) { throw "private package Conda bootstrap failed with exit code $bootstrapExitCode" }
if (!(Test-Path -LiteralPath $Conda -PathType Leaf)) { throw "private package Conda bootstrap did not return conda.bat" }
$CondaRoot = Split-Path -Parent (Split-Path -Parent $Conda)
$BootstrapPython = Join-Path $CondaRoot "python.exe"
if (!(Test-Path -LiteralPath $BootstrapPython -PathType Leaf)) { throw "private Miniforge Python is missing: $BootstrapPython" }

$selected = (& $BootstrapPython (Join-Path $Root "scripts\portable_install.py") select-device --runtime-lock $RuntimeLock --requested $Device.ToLowerInvariant() --controllers $controllersPath).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($selected)) { throw "device profile selection failed" }
Write-Host "Selected device profile: $selected"

$runtimeLockPayload = Get-Content -LiteralPath $RuntimeLock -Raw | ConvertFrom-Json
$modelLockPayload = Get-Content -LiteralPath $ModelLock -Raw | ConvertFrom-Json
foreach ($asset in @($modelLockPayload.assets)) {
    $assetLock = Join-Path $Root "data\cache\portable\locks\$($asset.id).json"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $assetLock) | Out-Null
    $asset | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $assetLock -Encoding UTF8
    & $BootstrapPython (Join-Path $Root "scripts\portable_install.py") ensure-asset --asset $assetLock --path (Join-Path $Root ([string]$asset.target)) @DownloadArguments
    if ($LASTEXITCODE -eq 20) { exit 20 }
    if ($LASTEXITCODE -ne 0) { throw "locked model asset failed: $($asset.id)" }
}
foreach ($requiredModelPath in @($modelLockPayload.required_paths)) {
    if (!(Test-Path -LiteralPath (Join-Path $Root ([string]$requiredModelPath)))) {
        throw "required model asset is missing after locked initialization: $requiredModelPath"
    }
}

$uvAssetPath = Join-Path $Root "data\cache\portable\assets\$($runtimeLockPayload.assets.uv.id).json"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $uvAssetPath) | Out-Null
$runtimeLockPayload.assets.uv | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $uvAssetPath -Encoding UTF8
$uvWheel = Join-Path $Root "data\cache\portable\assets\$($runtimeLockPayload.assets.uv.id).whl"
& $BootstrapPython (Join-Path $Root "scripts\portable_install.py") ensure-asset --asset $uvAssetPath --path $uvWheel @DownloadArguments
if ($LASTEXITCODE -eq 20) { exit 20 }
if ($LASTEXITCODE -ne 0) { throw "locked uv asset initialization failed" }

if (Test-Path -LiteralPath $Staging) { Remove-Item -LiteralPath $Staging -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Staging) | Out-Null
& $Conda create --yes --prefix $Staging "python=3.11" pip
if ($LASTEXITCODE -ne 0) { throw "private Conda failed to create the temporary Python 3.11 runtime" }
$StagePython = Join-Path $Staging "python.exe"
& $StagePython -m pip install --no-deps $uvWheel
if ($LASTEXITCODE -ne 0) { throw "locked uv installation failed" }
$UvExe = Join-Path $Staging "Scripts\uv.exe"

# Frozen deployment contract: uv lock --check must never update uv.lock.
& $UvExe lock --check --project $BackendRoot
if ($LASTEXITCODE -ne 0) { throw "backend uv.lock drift detected" }
$requirements = Join-Path $Staging "tts-more-requirements.lock.txt"
& $UvExe export --frozen --no-dev --no-emit-project --project $BackendRoot --output-file $requirements
if ($LASTEXITCODE -ne 0) { throw "failed to export frozen backend dependencies" }
& $UvExe pip install --python $StagePython --requirement $requirements
if ($LASTEXITCODE -ne 0) { throw "failed to synchronize frozen backend dependencies" }
& $StagePython -m pip check
if ($LASTEXITCODE -ne 0) { throw "pip check failed in temporary runtime" }
& $StagePython -c $ImportProbe
if ($LASTEXITCODE -ne 0) { throw "core import probe failed in temporary runtime" }

$backup = Join-Path $Root "runtime\previous"
if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
if (Test-Path -LiteralPath $Live) { Move-Item -LiteralPath $Live -Destination $backup }
Move-Item -LiteralPath $Staging -Destination $Live
if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }

$runtimeSha = Get-PortableFileSha256 -Path $RuntimeLock
$modelSha = Get-PortableFileSha256 -Path $ModelLock
& (Join-Path $Live "python.exe") (Join-Path $Root "scripts\portable_install.py") write-state --path $StatePath --component tts-more --build-id $BuildId --profile $selected --runtime-lock-sha256 $runtimeSha --model-lock-sha256 $modelSha
if ($LASTEXITCODE -ne 0) { throw "failed to commit install-state.json" }
Write-Host "TTS More portable initialization completed."
