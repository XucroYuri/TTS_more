[CmdletBinding()]
param(
    [ValidateSet("Bootstrap", "Full")][string]$Profile = "Bootstrap",
    [ValidateSet("Auto", "CU128", "CU126", "CPU")][string]$Device = "Auto",
    [string]$Version = "0.2.0",
    [string]$OutputRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = [System.IO.Path]::GetFullPath($PSScriptRoot)
$profileName = $Profile.ToLowerInvariant()
if ([string]::IsNullOrWhiteSpace($OutputRoot)) { $OutputRoot = Join-Path $Root "artifacts\portable\$profileName" }
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
$work = Join-Path $Root "artifacts\portable\.work\tts-more-$profileName"
$packageName = "TTS-More-$Version-windows-x64-$profileName"
$stage = Join-Path $work $packageName

if (!(Test-Path -LiteralPath (Join-Path $Root "frontend\dist\index.html"))) {
    & pnpm --dir (Join-Path $Root "frontend") build
    if ($LASTEXITCODE -ne 0) { throw "frontend production build failed" }
}
if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage, (Join-Path $stage "app\backend"), (Join-Path $stage "package"), (Join-Path $stage "scripts"), (Join-Path $stage "packaging\portable") | Out-Null

Copy-Item -LiteralPath (Join-Path $Root "backend\app") -Destination (Join-Path $stage "app\backend\app") -Recurse
foreach ($file in @("pyproject.toml", "uv.lock", ".python-version")) { Copy-Item -LiteralPath (Join-Path $Root "backend\$file") -Destination (Join-Path $stage "app\backend\$file") }
Copy-Item -LiteralPath (Join-Path $Root "frontend\dist") -Destination (Join-Path $stage "app\frontend") -Recurse
foreach ($file in @("bootstrap-conda.ps1", "initialize-portable.ps1", "repair-portable.ps1", "start-production.ps1", "stop-production.ps1", "portable_install.py", "portable_launcher.py", "portable_packages.py")) { Copy-Item -LiteralPath (Join-Path $Root "scripts\$file") -Destination (Join-Path $stage "scripts\$file") }
foreach ($file in @("toolchain.lock.json", "runtime.lock.json", "models.lock.json", "tts-more-package.schema.json")) { Copy-Item -LiteralPath (Join-Path $Root "packaging\portable\$file") -Destination (Join-Path $stage "packaging\portable\$file") }
@(Get-ChildItem -LiteralPath $stage -Directory -Recurse -Force | Where-Object { $_.Name -in @("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache") } | Sort-Object FullName -Descending) | ForEach-Object {
    $resolved = [System.IO.Path]::GetFullPath($_.FullName)
    if (!$resolved.StartsWith([System.IO.Path]::GetFullPath($stage), [System.StringComparison]::OrdinalIgnoreCase)) { throw "refusing to clean outside package stage: $resolved" }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
foreach ($file in @("Initialize.cmd", "Start.cmd", "Stop.cmd", "Repair.cmd", "LICENSE", "NOTICE", "repo.lock.json")) {
    Copy-Item -LiteralPath (Join-Path $Root $file) -Destination (Join-Path $stage $file)
}
Copy-Item -LiteralPath (Join-Path $Root "Build-Package.ps1") -Destination (Join-Path $stage "Build-Package.ps1")

$revision = (& git -C $Root rev-parse HEAD).Trim()
$integrationFiles = @(Get-ChildItem -LiteralPath (Join-Path $stage "scripts") -File | Sort-Object FullName)
$integrationDigestText = ($integrationFiles | ForEach-Object { (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }) -join "`n"
$sha256 = [System.Security.Cryptography.SHA256]::Create()
$bundleSha = ([BitConverter]::ToString($sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($integrationDigestText)))).Replace("-", "").ToLowerInvariant()
$manifest = [ordered]@{
    schema_version = 2; component = "tts-more"; version = $Version; build_id = "tts-more-$Version-$($revision.Substring(0, 12))"
    package_profile = $profileName; platform = "windows-x64"; api_contract = "tts-more-v1"
    source = @{ repository = "https://github.com/XucroYuri/TTS_more.git"; revision = $revision }
    integration = @{ version = "2.0.0"; source_revision = $revision; bundle_sha256 = $bundleSha }
    runtime = @{ python_version = "3.11"; device_profiles = @($Device.ToLowerInvariant()); lock = "packaging/portable/runtime.lock.json"; state_path = "data/local/install-state.json" }
    models = @{ lock = "packaging/portable/models.lock.json"; required = $false }
    data_root = "data/local"
    launchers = @{ initialize = "Initialize.cmd"; start = "Start.cmd"; stop = "Stop.cmd"; repair = "Repair.cmd"; build = "Build-Package.ps1" }
    endpoint = @{ default_url = "http://127.0.0.1:8000"; port = 8000; health_path = "/api/health"; capabilities_path = "/api/open-source-tts/catalog"; bind_policy = "loopback" }
    capabilities = @("orchestrator", "package-discovery", "artifact-transfer", "trusted-lan-registration")
    sha256_manifest = "SHA256SUMS.txt"; licenses = "THIRD_PARTY_NOTICES.json"
}
$manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $stage "package\tts-more-package.json") -Encoding UTF8
@{ schema_version = 1; component = "tts-more"; packages = @(); upstream_repositories = @("GPT-SoVITS", "IndexTTS", "CosyVoice") } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $stage "THIRD_PARTY_NOTICES.json") -Encoding UTF8

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

$python = if (Test-Path -LiteralPath (Join-Path $Root ".venv\Scripts\python.exe")) { Join-Path $Root ".venv\Scripts\python.exe" } else { "py" }
& $python (Join-Path $stage "scripts\portable_packages.py") validate-manifest --manifest (Join-Path $stage "package\tts-more-package.json") --package-root $stage
if ($LASTEXITCODE -ne 0) { throw "staged package manifest failed schema v2 validation" }

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$zip = Join-Path $OutputRoot "$packageName.zip"
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -LiteralPath $stage -DestinationPath $zip -CompressionLevel Optimal
$zipSha = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
"$zipSha  $([System.IO.Path]::GetFileName($zip))" | Set-Content -LiteralPath "$zip.sha256" -Encoding ASCII
@{ schema_version = 1; component = "tts-more"; version = $Version; profile = $profileName; source_revision = $revision; sha256 = $zipSha } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath "$zip.provenance.json" -Encoding UTF8
Write-Host "Created $Profile package: $zip"
