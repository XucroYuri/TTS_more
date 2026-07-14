[CmdletBinding()]
param(
    [ValidateSet("Auto", "CU128", "CU126", "CPU")][string]$Device = "Auto",
    [switch]$Repair
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$RuntimeLock = Join-Path $Root "packaging\portable\runtime.lock.json"
$ModelLock = Join-Path $Root "packaging\portable\models.lock.json"
$StatePath = Join-Path $Root "data\local\install-state.json"
$Live = Join-Path $Root "runtime\live"
$Staging = Join-Path $Root "runtime\staging"
$BackendRoot = if (Test-Path -LiteralPath (Join-Path $Root "backend\uv.lock")) { Join-Path $Root "backend" } else { Join-Path $Root "app\backend" }

foreach ($required in @($RuntimeLock, $ModelLock, (Join-Path $BackendRoot "uv.lock"), (Join-Path $Root "scripts\portable_install.py"))) {
    if (!(Test-Path -LiteralPath $required -PathType Leaf)) { throw "required locked package input is missing: $required" }
}
if ($Root.Length -gt 180) { throw "package path is too long for reliable Windows model tooling ($($Root.Length) characters): $Root" }
$drive = Get-PSDrive -Name ([System.IO.Path]::GetPathRoot($Root).Substring(0, 1)) -ErrorAction SilentlyContinue
if ($drive -and $drive.Free -lt 3GB) { throw "at least 3 GB free space is required for transactional initialization" }

function Test-LiveRuntime {
    $python = Join-Path $Live "python.exe"
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) { return $false }
    & $python -m pip check *> $null
    if ($LASTEXITCODE -ne 0) { return $false }
    & $python -c "import fastapi,pydantic,uvicorn" *> $null
    return $LASTEXITCODE -eq 0
}

if ((Test-Path -LiteralPath $StatePath) -and (Test-LiveRuntime)) {
    Write-Host "TTS More package runtime is already verified."
    exit 0
}
if ($Repair) { Write-Host "Repairing only the missing or failed runtime transaction; user data is preserved." }

$controllersPath = Join-Path $Root "data\cache\portable\video-controllers.json"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $controllersPath) | Out-Null
@(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | ForEach-Object {
    [pscustomobject]@{ name = [string]$_.Name; driver_version = [string]$_.DriverVersion }
}) | ConvertTo-Json | Set-Content -LiteralPath $controllersPath -Encoding UTF8

$bootstrap = Join-Path $Root "scripts\bootstrap-conda.ps1"
$Conda = (& $bootstrap -CacheRoot "data/cache/portable/conda" -LockPath "packaging/portable/toolchain.lock.json" -PassThru | Select-Object -Last 1)
if (!(Test-Path -LiteralPath $Conda -PathType Leaf)) { throw "private package Conda bootstrap did not return conda.bat" }
$CondaRoot = Split-Path -Parent (Split-Path -Parent $Conda)
$BootstrapPython = Join-Path $CondaRoot "python.exe"
if (!(Test-Path -LiteralPath $BootstrapPython -PathType Leaf)) { throw "private Miniforge Python is missing: $BootstrapPython" }

$selected = (& $BootstrapPython (Join-Path $Root "scripts\portable_install.py") select-device --runtime-lock $RuntimeLock --requested $Device.ToLowerInvariant() --controllers $controllersPath).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($selected)) { throw "device profile selection failed" }
Write-Host "Selected device profile: $selected"

$runtimeLockPayload = Get-Content -LiteralPath $RuntimeLock -Raw | ConvertFrom-Json
$uvAssetPath = Join-Path $Root "data\cache\portable\assets\$($runtimeLockPayload.assets.uv.id).json"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $uvAssetPath) | Out-Null
$runtimeLockPayload.assets.uv | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $uvAssetPath -Encoding UTF8
$uvWheel = Join-Path $Root "data\cache\portable\assets\$($runtimeLockPayload.assets.uv.id).whl"
& $BootstrapPython (Join-Path $Root "scripts\portable_install.py") ensure-asset --asset $uvAssetPath --path $uvWheel
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
& $StagePython -c "import fastapi,pydantic,uvicorn; print('TTS More runtime import probe passed')"
if ($LASTEXITCODE -ne 0) { throw "core import probe failed in temporary runtime" }

$backup = Join-Path $Root "runtime\previous"
if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
if (Test-Path -LiteralPath $Live) { Move-Item -LiteralPath $Live -Destination $backup }
Move-Item -LiteralPath $Staging -Destination $Live
if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }

$manifestPath = Join-Path $Root "package\tts-more-package.json"
$buildId = if (Test-Path -LiteralPath $manifestPath) { [string](Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json).build_id } else { "source-checkout" }
$runtimeSha = (Get-FileHash -LiteralPath $RuntimeLock -Algorithm SHA256).Hash.ToLowerInvariant()
$modelSha = (Get-FileHash -LiteralPath $ModelLock -Algorithm SHA256).Hash.ToLowerInvariant()
& (Join-Path $Live "python.exe") (Join-Path $Root "scripts\portable_install.py") write-state --path $StatePath --component tts-more --build-id $buildId --profile $selected --runtime-lock-sha256 $runtimeSha --model-lock-sha256 $modelSha
if ($LASTEXITCODE -ne 0) { throw "failed to commit install-state.json" }
Write-Host "TTS More portable initialization completed."
