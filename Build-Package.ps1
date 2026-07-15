[CmdletBinding()]
param(
    [ValidateSet("Bootstrap", "Full")][string]$Profile = "Bootstrap",
    [ValidateSet("Auto", "CU128", "CU126", "CPU")][string]$Device = "Auto",
    [string]$Version = "0.2.0",
    [string]$OutputRoot = "",
    [string]$WorkRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($Profile -eq "Full" -and $env:GITHUB_ACTIONS -eq "true") { throw "profile=full is local-only and cannot be built by a GitHub upload workflow" }
if ($Version -notmatch "^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$") { throw "package Version must contain only ASCII letters, digits, dot, underscore, or hyphen (maximum 128 characters)" }

function Assert-PortableWorkPath {
    param([Parameter(Mandatory = $true)][string]$CandidatePath)
    try {
        $fullPath = [IO.Path]::GetFullPath($CandidatePath)
        $volumeRoot = [IO.Path]::GetPathRoot($fullPath)
    }
    catch {
        throw "WorkRoot path validation failed closed. Choose a different -WorkRoot path. Error: $($_.Exception.Message)"
    }
    if ([string]::IsNullOrWhiteSpace($volumeRoot)) { throw "WorkRoot path validation failed closed. Choose an absolute -WorkRoot path." }
    $pathsToCheck = @($volumeRoot)
    $currentPath = $volumeRoot
    foreach ($segment in @($fullPath.Substring($volumeRoot.Length) -split '[\\/]' | Where-Object { $_ })) {
        $currentPath = Join-Path $currentPath $segment
        $pathsToCheck += $currentPath
    }
    foreach ($currentPath in $pathsToCheck) {
        try { $item = Get-Item -LiteralPath $currentPath -Force -ErrorAction Stop }
        catch [System.Management.Automation.ItemNotFoundException] { return $false }
        catch { throw "WorkRoot path validation failed closed. Choose a different -WorkRoot path. Path: $currentPath. Error: $($_.Exception.Message)" }
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "WorkRoot path must not traverse a reparse point. Choose -WorkRoot outside junctions and symbolic links. Path: $currentPath"
        }
        if (!$item.PSIsContainer) { throw "WorkRoot path contains an existing non-directory segment. Choose a different -WorkRoot path. Path: $currentPath" }
    }
    return $true
}

function Update-PortablePathBudget {
    param([string]$ProjectedPath, [ref]$MaximumLength, [ref]$MaximumPath)
    if ($ProjectedPath.Length -gt $MaximumLength.Value) {
        $MaximumLength.Value = $ProjectedPath.Length
        $MaximumPath.Value = $ProjectedPath
    }
}

function Measure-PortableCopyTree {
    param([string]$Source, [string]$Destination, [ref]$MaximumLength, [ref]$MaximumPath)
    Update-PortablePathBudget -ProjectedPath $Destination -MaximumLength $MaximumLength -MaximumPath $MaximumPath
    foreach ($entry in Get-ChildItem -LiteralPath $Source -Force) {
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "controller package source cannot contain a reparse point: $($entry.FullName)" }
        $target = Join-Path $Destination $entry.Name
        Update-PortablePathBudget -ProjectedPath $target -MaximumLength $MaximumLength -MaximumPath $MaximumPath
        if ($entry.PSIsContainer) { Measure-PortableCopyTree -Source $entry.FullName -Destination $target -MaximumLength $MaximumLength -MaximumPath $MaximumPath }
    }
}

$Root = [System.IO.Path]::GetFullPath($PSScriptRoot)
$profileName = $Profile.ToLowerInvariant()
if ([string]::IsNullOrWhiteSpace($OutputRoot)) { $OutputRoot = Join-Path $Root "artifacts\portable\$profileName" }
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
$packageName = "TTS-More-$Version-windows-x64-$profileName"
$workBase = if ($WorkRoot) { [IO.Path]::GetFullPath($WorkRoot) } else { [IO.Path]::GetFullPath([IO.Path]::GetTempPath()) }
$normalizedSourceRoot = $Root.TrimEnd("\", "/")
$normalizedWorkBase = $workBase.TrimEnd("\", "/")
if (
    [string]::Equals($normalizedWorkBase, $normalizedSourceRoot, [StringComparison]::OrdinalIgnoreCase) -or
    $normalizedWorkBase.StartsWith($normalizedSourceRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
) { throw "WorkRoot must be outside source checkout. Set -WorkRoot to a directory outside '$Root' (for example C:\tm)." }
[void](Assert-PortableWorkPath -CandidatePath $workBase)
$workIdentity = "tts-more-controller-$PID-$([Guid]::NewGuid().ToString('N').Substring(0, 12))"
$work = [IO.Path]::GetFullPath((Join-Path $workBase $workIdentity))
$stage = Join-Path $work $packageName

if (!(Test-Path -LiteralPath (Join-Path $Root "frontend\dist\index.html"))) {
    & pnpm --dir (Join-Path $Root "frontend") build
    if ($LASTEXITCODE -ne 0) { throw "frontend production build failed" }
}
$maximumPathLength = 0
$maximumProjectedPath = ""
Measure-PortableCopyTree -Source (Join-Path $Root "backend\app") -Destination (Join-Path $stage "app\backend\app") -MaximumLength ([ref]$maximumPathLength) -MaximumPath ([ref]$maximumProjectedPath)
Measure-PortableCopyTree -Source (Join-Path $Root "frontend\dist") -Destination (Join-Path $stage "app\frontend") -MaximumLength ([ref]$maximumPathLength) -MaximumPath ([ref]$maximumProjectedPath)
foreach ($projected in @(
    (Join-Path $stage "package\tts-more-package.json"),
    (Join-Path $stage "licenses\THIRD_PARTY_NOTICES.json"),
    (Join-Path $stage "packaging\portable\tts-more-package.schema.json"),
    (Join-Path $stage "scripts\import_portable_data.py"),
    (Join-Path $stage "SHA256SUMS.txt")
)) { Update-PortablePathBudget -ProjectedPath $projected -MaximumLength ([ref]$maximumPathLength) -MaximumPath ([ref]$maximumProjectedPath) }
if ($maximumPathLength -gt 240) {
    throw "controller package staging path budget exceeded before copy: projected path length $maximumPathLength exceeds the safe Windows limit 240. Use -WorkRoot with a shorter external directory (for example C:\tm). Projected path: $maximumProjectedPath"
}
try {
New-Item -ItemType Directory -Force -Path $stage, (Join-Path $stage "app\backend"), (Join-Path $stage "package"), (Join-Path $stage "scripts"), (Join-Path $stage "packaging\portable"), (Join-Path $stage "licenses") | Out-Null
[void](Assert-PortableWorkPath -CandidatePath $stage)

Copy-Item -LiteralPath (Join-Path $Root "backend\app") -Destination (Join-Path $stage "app\backend\app") -Recurse
foreach ($file in @("pyproject.toml", "uv.lock", ".python-version")) { Copy-Item -LiteralPath (Join-Path $Root "backend\$file") -Destination (Join-Path $stage "app\backend\$file") }
Copy-Item -LiteralPath (Join-Path $Root "frontend\dist") -Destination (Join-Path $stage "app\frontend") -Recurse
foreach ($file in @("bootstrap-conda.ps1", "initialize-portable.ps1", "repair-portable.ps1", "start-production.ps1", "stop-production.ps1", "Invoke-PortableStart.ps1", "Show-PortableProgress.ps1", "Portable-Validation.ps1", "select-portable-folder.ps1", "export-portable-diagnostics.py", "import-portable-data.py", "import_portable_data.py", "portable_install.py", "portable_launcher.py", "portable_operations.py", "portable_packages.py", "portable_package_runner.py")) { Copy-Item -LiteralPath (Join-Path $Root "scripts\$file") -Destination (Join-Path $stage "scripts\$file") }
foreach ($file in @("toolchain.lock.json", "runtime.lock.json", "models.lock.json", "tts-more-package.schema.json", "error-catalog.zh-CN.json")) { Copy-Item -LiteralPath (Join-Path $Root "packaging\portable\$file") -Destination (Join-Path $stage "packaging\portable\$file") }
@(Get-ChildItem -LiteralPath $stage -Directory -Recurse -Force | Where-Object { $_.Name -in @("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache") } | Sort-Object FullName -Descending) | ForEach-Object {
    $resolved = [System.IO.Path]::GetFullPath($_.FullName)
    if (!$resolved.StartsWith([System.IO.Path]::GetFullPath($stage), [System.StringComparison]::OrdinalIgnoreCase)) { throw "refusing to clean outside package stage: $resolved" }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
foreach ($file in @("Initialize.cmd", "Start.cmd", "Stop.cmd", "Repair.cmd")) {
    Copy-Item -LiteralPath (Join-Path $Root $file) -Destination (Join-Path $stage $file)
}
Copy-Item -LiteralPath (Join-Path $Root "LICENSE") -Destination (Join-Path $stage "licenses\LICENSE")
Copy-Item -LiteralPath (Join-Path $Root "NOTICE") -Destination (Join-Path $stage "licenses\NOTICE")
Copy-Item -LiteralPath (Join-Path $Root "repo.lock.json") -Destination (Join-Path $stage "package\repo.lock.json")
Copy-Item -LiteralPath (Join-Path $Root "packaging\portable\使用说明-先看这里.txt") -Destination (Join-Path $stage "使用说明-先看这里.txt")
@'
throw "This delivered portable package cannot rebuild itself. Use the corresponding source checkout and its Build-Package.ps1."
'@ | Set-Content -LiteralPath (Join-Path $stage "Build-Package.ps1") -Encoding ASCII

$revision = (& git -C $Root rev-parse HEAD).Trim()
$integrationFiles = @(Get-ChildItem -LiteralPath (Join-Path $stage "scripts") -File | Sort-Object FullName)
$integrationDigestText = ($integrationFiles | ForEach-Object { (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }) -join "`n"
$sha256 = [System.Security.Cryptography.SHA256]::Create()
$bundleSha = ([BitConverter]::ToString($sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($integrationDigestText)))).Replace("-", "").ToLowerInvariant()
$manifest = [ordered]@{
    schema_version = 2; component = "tts-more"; package_id = "tts-more"; release_version = $Version
    version = $Version; build_id = "tts-more-$Version-$($revision.Substring(0, 12))"
    package_profile = $profileName; platform = "windows-x64"; api_contract = "tts-more-v1"
    protocol = @{ name = "tts-more-v1"; version = "1.0"; controller_range = ">=0.2.0,<0.3.0" }
    source = @{ repository = "https://github.com/XucroYuri/TTS_more.git"; revision = $revision }
    integration = @{ version = "2.0.0"; source_revision = $revision; bundle_sha256 = $bundleSha }
    runtime = @{ python_version = "3.11"; device_profiles = @($Device.ToLowerInvariant()); lock = "packaging/portable/runtime.lock.json"; state_path = "data/local/install-state.json" }
    models = @{ lock = "packaging/portable/models.lock.json"; required = $false }
    data_root = "data/local"
    data = @{ user = "data/user"; local = "data/local"; cache = "data/cache"; operations = "data/local/operations" }
    launchers = @{ initialize = "Initialize.cmd"; start = "Start.cmd"; stop = "Stop.cmd"; repair = "Repair.cmd"; build = "Build-Package.ps1" }
    endpoint = @{ default_url = "http://127.0.0.1:8000"; port = 8000; health_path = "/api/health"; capabilities_path = "/api/open-source-tts/catalog"; bind_policy = "loopback" }
    capabilities = @("orchestrator", "package-discovery", "artifact-transfer", "trusted-lan-registration")
    sha256_manifest = "SHA256SUMS.txt"; licenses = "licenses/THIRD_PARTY_NOTICES.json"
}
$manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $stage "package\tts-more-package.json") -Encoding UTF8
@{ schema_version = 1; component = "tts-more"; packages = @(); upstream_repositories = @("GPT-SoVITS", "IndexTTS", "CosyVoice") } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $stage "licenses\THIRD_PARTY_NOTICES.json") -Encoding UTF8

if ($Profile -eq "Full") {
    & (Join-Path $stage "scripts\initialize-portable.ps1") -Device $Device
    if ($LASTEXITCODE -ne 0) { throw "full package initialization failed" }
}

$sumPath = Join-Path $stage "SHA256SUMS.txt"
@(Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object { $_.FullName -ne $sumPath } | Sort-Object FullName | ForEach-Object {
    $relative = $_.FullName.Substring($stage.Length).TrimStart("\", "/").Replace("\", "/")
    "$((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant())  $relative"
}) | Set-Content -LiteralPath $sumPath -Encoding UTF8

if ($Profile -eq "Bootstrap") {
    $forbidden = @(Get-ChildItem -LiteralPath $stage -Recurse -Force | Where-Object {
        $_.FullName -match "[\\/](\.venv|runtime[\\/]live|models?|cache|projects?)([\\/]|$)" -or $_.Name -match "\.(safetensors|ckpt|pth|pt)$"
    })
    if ($forbidden.Count -gt 0) { throw "bootstrap package contains forbidden local/full assets: $($forbidden.FullName -join ', ')" }
}
$machinePaths = @(
    $Root,
    $env:USERPROFILE,
    "$($env:HOMEDRIVE)$($env:HOMEPATH)"
) | Where-Object { ![string]::IsNullOrWhiteSpace([string]$_) -and ([string]$_).Length -ge 4 } | Select-Object -Unique
$machineNames = @($env:USERNAME, $env:COMPUTERNAME) | Where-Object { ![string]::IsNullOrWhiteSpace([string]$_) -and ([string]$_).Length -ge 4 } | Select-Object -Unique
$machinePathLeak = @(Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object { $_.Length -lt 5MB } | Select-String -SimpleMatch -Pattern $machinePaths -ErrorAction SilentlyContinue)
$generatedMetadata = @((Join-Path $stage "package\tts-more-package.json"), (Join-Path $stage "licenses\THIRD_PARTY_NOTICES.json"))
$generatedMetadata = @($generatedMetadata | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
$machineNameLeak = @()
if (@($machineNames).Count -gt 0 -and $generatedMetadata.Count -gt 0) { $machineNameLeak = @(Select-String -LiteralPath $generatedMetadata -SimpleMatch -Pattern $machineNames -ErrorAction SilentlyContinue) }
if ($machinePathLeak.Count -gt 0) { throw "package contains build-machine identity or path data: $($machinePathLeak[0].Path)" }
if ($machineNameLeak.Count -gt 0) { throw "package metadata contains build-machine identity data: $($machineNameLeak[0].Path)" }

$python = if ($env:TTS_MORE_BUILD_PYTHON) {
    $env:TTS_MORE_BUILD_PYTHON
} elseif (Test-Path -LiteralPath (Join-Path $Root "runtime\live\python.exe")) {
    Join-Path $Root "runtime\live\python.exe"
} elseif (Test-Path -LiteralPath (Join-Path $Root ".venv\Scripts\python.exe")) {
    Join-Path $Root ".venv\Scripts\python.exe"
} else {
    $conda = (& (Join-Path $Root "scripts\bootstrap-conda.ps1") -CacheRoot "data/cache/portable/conda" -LockPath "packaging/portable/toolchain.lock.json" -PassThru | Select-Object -Last 1)
    Join-Path (Split-Path -Parent (Split-Path -Parent $conda)) "python.exe"
}
& $python (Join-Path $stage "scripts\portable_packages.py") validate-manifest --manifest (Join-Path $stage "package\tts-more-package.json") --package-root $stage
if ($LASTEXITCODE -ne 0) { throw "staged package manifest failed schema v2 validation" }

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$zip = Join-Path $OutputRoot "$packageName.zip"
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
& $python (Join-Path $stage "scripts\portable_packages.py") create-zip --package-root $stage --output $zip
if ($LASTEXITCODE -ne 0) { throw "ZIP64 package creation failed" }
$auditPassed = $false
if ($Profile -eq "Bootstrap") {
    & $python (Join-Path $stage "scripts\portable_packages.py") audit-release --zip $zip
    if ($LASTEXITCODE -ne 0) { throw "GitHub bootstrap release audit failed" }
    $auditPassed = $true
}
$zipSha = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
"$zipSha  $([System.IO.Path]::GetFileName($zip))" | Set-Content -LiteralPath "$zip.sha256" -Encoding ASCII
@{ schema_version = 1; component = "tts-more"; version = $Version; profile = $profileName; source_revision = $revision; sha256 = $zipSha } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath "$zip.provenance.json" -Encoding UTF8
$packages = @()
$lockText = Get-Content -LiteralPath (Join-Path $Root "backend\uv.lock") -Raw
foreach ($match in [regex]::Matches($lockText, '(?ms)\[\[package\]\]\s+name\s*=\s*"([^"]+)"\s+version\s*=\s*"([^"]+)"')) {
    $spdxId = ($match.Groups[1].Value -replace '[^A-Za-z0-9.-]', '-')
    $packages += @{ SPDXID="SPDXRef-Package-$spdxId"; name=$match.Groups[1].Value; versionInfo=$match.Groups[2].Value; downloadLocation="NOASSERTION"; filesAnalyzed=$false }
}
@{ spdxVersion="SPDX-2.3"; dataLicense="CC0-1.0"; SPDXID="SPDXRef-DOCUMENT"; name=$packageName; documentNamespace="https://tts-more.local/spdx/tts-more/$Version/$zipSha"; creationInfo=@{created=[DateTime]::UtcNow.ToString("o");creators=@("Tool: TTS-More-Build-Package-2.0.0")}; packages=$packages } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath "$zip.spdx.json" -Encoding UTF8
Copy-Item -LiteralPath (Join-Path $stage "licenses\THIRD_PARTY_NOTICES.json") -Destination "$zip.licenses.json"
@{ schema_version=1; component="tts-more"; profile=$profileName; manifest_valid=$true; bootstrap_audit=$auditPassed; machine_path_scan=$true; generated_at=[DateTime]::UtcNow.ToString("o") } | ConvertTo-Json | Set-Content -LiteralPath "$zip.acceptance.json" -Encoding UTF8
Write-Host "Created $Profile package: $zip"
}
finally {
    $workPathExists = Assert-PortableWorkPath -CandidatePath $work
    if ($workPathExists) {
        $resolvedWork = [IO.Path]::GetFullPath($work)
        $resolvedParent = [IO.Path]::GetFullPath((Split-Path -Parent $resolvedWork))
        $resolvedLeaf = Split-Path -Leaf $resolvedWork
        if (![string]::Equals($resolvedParent.TrimEnd("\", "/"), $workBase.TrimEnd("\", "/"), [StringComparison]::OrdinalIgnoreCase) -or $resolvedLeaf -ne $workIdentity) {
            throw "refusing to clean a controller package staging directory that is not the unique directory created by this build: $resolvedWork"
        }
        Remove-Item -LiteralPath $resolvedWork -Recurse -Force
    }
}
