[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string[]]$Packages,
    [Parameter(Mandatory = $true)][string]$Output
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Portable-Validation.ps1")

$ExpectedComponents = @("tts-more", "gpt-sovits", "indextts", "cosyvoice")
$AllowedEvidenceFields = @("component", "scenario", "result", "duration", "error_code", "worker_real_initialization", "controller_real_initialization", "fixture_runtime_preseeded", "direct_downloader", "operation_progress_python")
$ForbiddenEvidenceFields = @("absolute_path", "username", "hostname", "ip_address", "pid", "command", "secret", "token")
$FixturePython = [string]$env:TTS_MORE_FIRST_RUN_PYTHON
$FixtureBasePython = ""
$FixtureBasePrefix = ""
$SinglePackageSmoke = [string]$env:TTS_MORE_FIRST_RUN_SINGLE_PACKAGE_SMOKE -eq "1"
$PortablePackagesScript = Join-Path $PSScriptRoot "portable_packages.py"
$FixtureServerScript = Join-Path $PSScriptRoot "serve-portable-fixtures.py"
$SystemPath = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\WindowsPowerShell\v1.0"
$OriginalPath = $env:PATH
$Evidence = [Collections.Generic.List[object]]::new()
$WorkerLifecycleSucceeded = @{}
$LockedPythonAssetCache = @{}
$ExpandedPackages = [Collections.Generic.List[object]]::new()
$OwnedProcesses = [Collections.Generic.List[object]]::new()
$OwnedProcessKeys = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$ServerProcess = $null
$AssetsRoot = ""
$ServerRequestLog = ""
$FixtureEndpoint = ""
$ServerRestartIndex = 0
$WorkRoot = ""
$WorkIdentity = [guid]::NewGuid().ToString("N")
$OwnerStartedAt = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString("o")
$Utf8NoBom = New-Object Text.UTF8Encoding($false)

function Get-PortableFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "").ToLowerInvariant() }
    finally {
        $stream.Dispose()
        $sha256.Dispose()
    }
}

function Throw-HarnessError {
    param([Parameter(Mandatory = $true)][string]$Code, [Parameter(Mandatory = $true)][string]$Message)
    throw "${Code}: $Message"
}

function Get-ErrorCode {
    param([Parameter(Mandatory = $true)][System.Management.Automation.ErrorRecord]$ErrorRecord)
    $message = [string]$ErrorRecord.Exception.Message
    if ($message -match '^([A-Z][A-Z0-9_]{2,63}):') { return $Matches[1] }
    if ($message -match 'PORT_IN_USE') { return "PORT_IN_USE" }
    if ($message -match '(?i)download|network|HTTP') { return "DOWNLOAD_NETWORK_INTERRUPTED" }
    return "HARNESS_FAILURE"
}

function Add-Evidence {
    param(
        [Parameter(Mandatory = $true)][string]$Component,
        [Parameter(Mandatory = $true)][string]$Scenario,
        [Parameter(Mandatory = $true)][ValidateSet("pass", "fail")][string]$Result,
        [Parameter(Mandatory = $true)][double]$Duration,
        [string]$ErrorCode = ""
    )
    $record = [ordered]@{
        component = $Component
        scenario = $Scenario
        result = $Result
        duration = [Math]::Round([Math]::Max(0.0, $Duration), 3)
        error_code = $ErrorCode
        worker_real_initialization = $false
        controller_real_initialization = ($Component -eq "tts-more" -and $Scenario -eq "controller_real_initialize")
        fixture_runtime_preseeded = $false
        direct_downloader = $false
        operation_progress_python = ""
    }
    $names = @($record.Keys | Sort-Object)
    $expected = @($AllowedEvidenceFields | Sort-Object)
    if (($names -join "`n") -ne ($expected -join "`n")) { Throw-HarnessError "EVIDENCE_SCHEMA" "acceptance record is not allowlisted" }
    foreach ($name in $ForbiddenEvidenceFields) {
        if ($record.Contains($name)) { Throw-HarnessError "EVIDENCE_SCHEMA" "acceptance record contains a forbidden field" }
    }
    $Evidence.Add([pscustomobject]$record)
}

function Finalize-WorkerInitializationEvidence {
    foreach ($component in @("gpt-sovits", "indextts", "cosyvoice")) {
        if (!$script:WorkerLifecycleSucceeded.ContainsKey($component)) {
            Throw-HarnessError "WORKER_ACCEPTANCE_INCOMPLETE" "worker lifecycle acceptance is incomplete"
        }
        $result = $script:WorkerLifecycleSucceeded[$component]
        foreach ($record in @($Evidence | Where-Object { $_.component -eq $component })) {
            $record.worker_real_initialization = $true
            $record.operation_progress_python = [string]$result.OperationProgressPython
        }
    }
}

function Invoke-Scenario {
    param(
        [Parameter(Mandatory = $true)][string]$Component,
        [Parameter(Mandatory = $true)][string]$Scenario,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    $clock = [Diagnostics.Stopwatch]::StartNew()
    try {
        & $Action
        $clock.Stop()
        Add-Evidence -Component $Component -Scenario $Scenario -Result pass -Duration $clock.Elapsed.TotalSeconds
    }
    catch {
        $clock.Stop()
        $code = Get-ErrorCode -ErrorRecord $_
        Add-Evidence -Component $Component -Scenario $Scenario -Result fail -Duration $clock.Elapsed.TotalSeconds -ErrorCode $code
        throw
    }
}

function Assert-SanitizedEvidence {
    param([Parameter(Mandatory = $true)][string]$Text)
    if ($Text -match '(?i)[A-Z]:\\' -or $Text -match '(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])') {
        Throw-HarnessError "EVIDENCE_IDENTITY_LEAK" "acceptance evidence contains a machine path or address"
    }
    foreach ($value in @($env:USERNAME, $env:COMPUTERNAME, $env:USERPROFILE, $FixturePython)) {
        if (![string]::IsNullOrWhiteSpace([string]$value) -and [string]$value -ne "." -and $Text.IndexOf([string]$value, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            Throw-HarnessError "EVIDENCE_IDENTITY_LEAK" "acceptance evidence contains machine identity"
        }
    }
}

function Write-AcceptanceEvidence {
    param([Parameter(Mandatory = $true)][string]$Directory)
    New-Item -ItemType Directory -Force -Path $Directory | Out-Null
    foreach ($record in $Evidence) {
        $names = @($record.PSObject.Properties.Name | Sort-Object)
        $expected = @($AllowedEvidenceFields | Sort-Object)
        if (($names -join "`n") -ne ($expected -join "`n")) { Throw-HarnessError "EVIDENCE_SCHEMA" "acceptance record changed shape" }
    }
    $json = ConvertTo-Json -InputObject @($Evidence) -Depth 4
    Assert-SanitizedEvidence -Text $json
    [IO.File]::WriteAllText((Join-Path $Directory "acceptance.json"), $json + "`n", $Utf8NoBom)

    $document = New-Object Xml.XmlDocument
    $testsuites = $document.CreateElement("testsuites")
    [void]$document.AppendChild($testsuites)
    $testsuite = $document.CreateElement("testsuite")
    $testsuite.SetAttribute("name", "portable-first-run")
    $testsuite.SetAttribute("tests", [string]$Evidence.Count)
    $testsuite.SetAttribute("failures", [string]@($Evidence | Where-Object { $_.result -eq "fail" }).Count)
    [void]$testsuites.AppendChild($testsuite)
    foreach ($record in $Evidence) {
        $testcase = $document.CreateElement("testcase")
        $testcase.SetAttribute("classname", [string]$record.component)
        $testcase.SetAttribute("name", [string]$record.scenario)
        $testcase.SetAttribute("time", ([double]$record.duration).ToString("0.000", [Globalization.CultureInfo]::InvariantCulture))
        if ($record.result -eq "fail") {
            $failure = $document.CreateElement("failure")
            $failure.SetAttribute("type", [string]$record.error_code)
            [void]$testcase.AppendChild($failure)
        }
        [void]$testsuite.AppendChild($testcase)
    }
    $settings = New-Object Xml.XmlWriterSettings
    $settings.Encoding = $Utf8NoBom
    $settings.Indent = $true
    $stream = New-Object IO.MemoryStream
    $writer = [Xml.XmlWriter]::Create($stream, $settings)
    try { $document.Save($writer) } finally { $writer.Dispose() }
    $junitBytes = $stream.ToArray()
    $stream.Dispose()
    $junit = $Utf8NoBom.GetString($junitBytes)
    Assert-SanitizedEvidence -Text $junit
    [IO.File]::WriteAllBytes((Join-Path $Directory "acceptance.junit.xml"), $junitBytes)
}

function Assert-FixturePython {
    if ([string]::IsNullOrWhiteSpace($FixturePython) -or ![IO.Path]::IsPathRooted($FixturePython) -or !(Test-Path -LiteralPath $FixturePython -PathType Leaf)) {
        Throw-HarnessError "FIXTURE_RUNTIME_MISSING" "TTS_MORE_FIRST_RUN_PYTHON must name the explicit Python 3.11 fixture runtime"
    }
    if (!(Test-Path -LiteralPath $PortablePackagesScript -PathType Leaf) -or !(Test-Path -LiteralPath $FixtureServerScript -PathType Leaf)) {
        Throw-HarnessError "HARNESS_INPUT_MISSING" "portable fixture scripts are missing"
    }
    $version = @(& $FixturePython -c "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>&1)
    if ($LASTEXITCODE -ne 0 -or ($version -join "").Trim() -ne "3.11") {
        Throw-HarnessError "FIXTURE_RUNTIME_INVALID" "the explicit fixture runtime must be Python 3.11"
    }
    $script:FixtureBasePrefix = (@(& $FixturePython -c "import sys;print(sys.base_prefix)" 2>&1) -join "").Trim()
    $baseExecutable = (@(& $FixturePython -c "import sys;print(sys._base_executable)" 2>&1) -join "").Trim()
    $basePythonCandidates = [Collections.Generic.List[string]]::new()
    if (![string]::IsNullOrWhiteSpace($baseExecutable) -and [IO.Path]::IsPathRooted($baseExecutable)) {
        [void]$basePythonCandidates.Add($baseExecutable)
    }
    foreach ($name in @("python.exe", "python3.exe")) {
        [void]$basePythonCandidates.Add((Join-Path $FixtureBasePrefix $name))
    }
    $script:FixtureBasePython = ""
    foreach ($candidate in $basePythonCandidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $script:FixtureBasePython = [IO.Path]::GetFullPath($candidate)
            break
        }
    }
    if (![IO.Path]::IsPathRooted($FixtureBasePython) -or !(Test-Path -LiteralPath $FixtureBasePython -PathType Leaf)) {
        Throw-HarnessError "FIXTURE_RUNTIME_INVALID" "the explicit fixture runtime has no Python 3.11 base executable"
    }
}

function Assert-RestrictedChildPath {
    $env:PATH = $SystemPath
    foreach ($name in @("python", "conda", "node", "git")) {
        $found = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
        if ($found) { Throw-HarnessError "COMMAND_LEAK" "restricted child PATH can discover a forbidden developer command" }
    }
}

function Get-ArchiveManifest {
    param([Parameter(Mandatory = $true)][string]$Zip)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($Zip)
    try {
        $entries = @($archive.Entries | Where-Object { $_.FullName.Replace("\\", "/") -match '^[^/]+/package/tts-more-package\.json$' })
        if ($entries.Count -ne 1) { Throw-HarnessError "PACKAGE_AUDIT_FAILED" "ZIP must contain exactly one package v2 manifest" }
        $reader = New-Object IO.StreamReader($entries[0].Open(), [Text.Encoding]::UTF8, $true)
        try { return ($reader.ReadToEnd() | ConvertFrom-Json) } finally { $reader.Dispose() }
    }
    finally { $archive.Dispose() }
}

function Assert-ZipHashSidecar {
    param([Parameter(Mandatory = $true)][string]$Zip)
    $sidecar = "$Zip.sha256"
    if (!(Test-Path -LiteralPath $sidecar -PathType Leaf)) { Throw-HarnessError "ZIP_HASH_MISSING" "ZIP SHA-256 sidecar is missing" }
    $line = (Get-Content -LiteralPath $sidecar -Raw -Encoding ASCII).Trim()
    if ($line -notmatch '^([0-9a-fA-F]{64})  ([^/\\]+)$') { Throw-HarnessError "ZIP_HASH_INVALID" "ZIP SHA-256 sidecar is invalid" }
    if ($Matches[2] -ne (Split-Path -Leaf $Zip)) { Throw-HarnessError "ZIP_HASH_INVALID" "ZIP SHA-256 sidecar names another archive" }
    $actual = Get-PortableFileSha256 -Path $Zip
    if (![string]::Equals($actual, $Matches[1], [StringComparison]::OrdinalIgnoreCase)) { Throw-HarnessError "ZIP_HASH_INVALID" "ZIP SHA-256 does not match" }
}

function Assert-InputPackages {
    $requiredCount = if ($SinglePackageSmoke) { 1 } else { 4 }
    if ($Packages.Count -ne $requiredCount) {
        if ($SinglePackageSmoke) { Throw-HarnessError "PACKAGE_SET_INVALID" "single-package smoke requires exactly one TTS More ZIP" }
        Throw-HarnessError "PACKAGE_SET_INVALID" "clean Windows acceptance requires exactly four Bootstrap ZIPs"
    }
    $seen = @{}
    foreach ($candidate in $Packages) {
        $zip = [IO.Path]::GetFullPath($candidate)
        if (!(Test-Path -LiteralPath $zip -PathType Leaf)) { Throw-HarnessError "PACKAGE_SET_INVALID" "an input ZIP is missing" }
        & $FixturePython $PortablePackagesScript audit-release --zip $zip *> $null
        if ($LASTEXITCODE -ne 0) { Throw-HarnessError "PACKAGE_AUDIT_FAILED" "Bootstrap release ZIP audit failed" }
        Assert-ZipHashSidecar -Zip $zip
        $manifest = Get-ArchiveManifest -Zip $zip
        $schemaVersion = 0
        if (![int]::TryParse([string]$manifest.schema_version, [ref]$schemaVersion) -or $schemaVersion -ne 2 -or [string]$manifest.package_profile -ne "bootstrap") {
            Throw-HarnessError "PACKAGE_AUDIT_FAILED" "input must be a schema v2 Bootstrap ZIP"
        }
        $component = [string]$manifest.component
        if ($component -notin $ExpectedComponents -or $seen.ContainsKey($component)) { Throw-HarnessError "PACKAGE_SET_INVALID" "component set is unsupported or duplicated" }
        $seen[$component] = [pscustomobject]@{ Zip = $zip; Manifest = $manifest }
    }
    $expected = if ($SinglePackageSmoke) { @("tts-more") } else { $ExpectedComponents }
    if ((@($seen.Keys | Sort-Object) -join "`n") -ne (@($expected | Sort-Object) -join "`n")) {
        Throw-HarnessError "PACKAGE_SET_INVALID" "input ZIPs do not exactly cover tts-more/gpt-sovits/indextts/cosyvoice"
    }
    return $seen
}

function Get-RandomLoopbackPort {
    $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return [int]$listener.LocalEndpoint.Port
    }
    finally { $listener.Stop() }
}

function Test-LoopbackPort {
    param([Parameter(Mandatory = $true)][int]$Port)
    $client = New-Object Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync("127.0.0.1", $Port)
        return $task.Wait(500) -and $client.Connected
    }
    catch { return $false }
    finally { $client.Dispose() }
}

function Wait-LoopbackPort {
    param([Parameter(Mandatory = $true)][int]$Port, [Parameter(Mandatory = $true)][bool]$Listening, [int]$Seconds = 15)
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        if ((Test-LoopbackPort -Port $Port) -eq $Listening) { return }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    Throw-HarnessError "PORT_STATE_INVALID" "loopback listener did not reach the expected state"
}

function Write-FixtureSha256Manifest {
    param([Parameter(Mandatory = $true)][string]$Root)
    # fixture-only copy mutation: keep the delivered ZIP SHA256SUMS.txt immutable.
    # Runtime/cache assets created during first-run validation are tracked in a
    # separate manifest so the release audit boundary never absorbs data/cache
    # or runtime/live files.
    $rootFull = [IO.Path]::GetFullPath($Root)
    $manifestRelative = "data/local/fixture/fixture-sha256.json"
    $manifestPath = Join-Path $rootFull $manifestRelative
    $seen = @{}
    $entries = New-Object System.Collections.Generic.List[object]
    $fixtureDirectories = @(
        "data/cache/fixture",
        "data/cache/portable/assets",
        "data/cache/portable/conda"
    )
    $fixtureFiles = @(
        "data/local/fixture/asset.lock.json",
        "data/local/fixture/fixture-service.py",
        "data/local/fixture/python-seed/python.exe",
        "data/local/fixture/python-seed/Scripts/uv.exe",
        "data/local/fixture/python-seed/Lib/site-packages/pip/__main__.py",
        "data/local/fixture/python-seed/Lib/site-packages/uvicorn/__main__.py",
        "data/local/fixture/python-seed/Lib/site-packages/fastapi/__init__.py",
        "data/local/fixture/python-seed/Lib/site-packages/pydantic/__init__.py",
        "runtime/live/python.exe",
        "runtime/live/Scripts/uv.exe",
        "runtime/live/Lib/site-packages/pip/__main__.py",
        "runtime/live/Lib/site-packages/uvicorn/__main__.py",
        "runtime/live/Lib/site-packages/fastapi/__init__.py",
        "runtime/live/Lib/site-packages/pydantic/__init__.py",
        "app/tts_more/component.json",
        "package/tts-more-package.json",
        "packaging/portable/runtime.lock.json",
        "packaging/portable/models.lock.json",
        "packaging/portable/toolchain.lock.json",
        "app/tts_more/locks/runtime.lock.json",
        "app/tts_more/locks/models.lock.json",
        "app/tts_more/locks/toolchain.lock.json",
        "app/tts_more/locks/requirements-cpu.lock.txt",
        "tts_more/locks/runtime.lock.json",
        "tts_more/locks/models.lock.json",
        "tts_more/locks/toolchain.lock.json",
        "tts_more/locks/requirements-cpu.lock.txt"
    )
    $releaseSourceFiles = @(
        "app/tts_more/component.json",
        "package/tts-more-package.json",
        "packaging/portable/runtime.lock.json",
        "packaging/portable/models.lock.json",
        "packaging/portable/toolchain.lock.json",
        "app/tts_more/locks/runtime.lock.json",
        "app/tts_more/locks/models.lock.json",
        "app/tts_more/locks/toolchain.lock.json",
        "app/tts_more/locks/requirements-cpu.lock.txt",
        "tts_more/locks/runtime.lock.json",
        "tts_more/locks/models.lock.json",
        "tts_more/locks/toolchain.lock.json",
        "tts_more/locks/requirements-cpu.lock.txt"
    )
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($relativeDirectory in $fixtureDirectories) {
        $directory = Join-Path $rootFull $relativeDirectory
        if (!(Test-Path -LiteralPath $directory -PathType Container)) { continue }
        foreach ($file in @(Get-ChildItem -LiteralPath $directory -File -Recurse -Force)) {
            $candidates.Add($file.FullName)
        }
    }
    foreach ($relativeFile in $fixtureFiles) {
        $file = Join-Path $rootFull $relativeFile
        if (Test-Path -LiteralPath $file -PathType Leaf) { $candidates.Add($file) }
    }
    foreach ($candidate in @($candidates | Sort-Object -Unique)) {
        $full = [IO.Path]::GetFullPath($candidate)
        if ([string]::Equals($full, $manifestPath, [StringComparison]::OrdinalIgnoreCase)) { continue }
        if (!$full.StartsWith($rootFull.TrimEnd('\') + "\", [StringComparison]::OrdinalIgnoreCase)) { Throw-HarnessError "FIXTURE_COPY_HASH_INVALID" "fixture integrity path escapes package root" }
        $relative = $full.Substring($rootFull.Length).TrimStart('\', '/').Replace('\', '/')
        if ($relative -match '^data/cache/portable/conda/[^/]+/(DLLs|Lib)/') { continue }
        if (!(Test-Path -LiteralPath $full -PathType Leaf)) { continue }
        $item = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
        if ($null -eq $item) { continue }
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Throw-HarnessError "FIXTURE_COPY_HASH_INVALID" "fixture integrity path is an unsafe reparse point" }
        $canonical = $relative.ToLowerInvariant()
        if ($seen.ContainsKey($canonical)) { continue }
        $seen[$canonical] = $true
        $digest = $null
        $lastHashError = $null
        foreach ($attempt in 1..6) {
            try {
                if (!(Test-Path -LiteralPath $full -PathType Leaf)) { throw "fixture integrity file disappeared before hashing" }
                $digest = Get-PortableFileSha256 -Path $full
                break
            }
            catch {
                $lastHashError = $_
                Start-Sleep -Milliseconds ([Math]::Min(500, 50 * $attempt))
            }
        }
        if ([string]::IsNullOrWhiteSpace($digest)) {
            if (!(Test-Path -LiteralPath $full -PathType Leaf)) { continue }
            if ([string]$env:TTS_MORE_FIRST_RUN_DEBUG -eq "1" -and $lastHashError) { [Console]::Error.WriteLine("FIXTURE_HASH_RETRY_FAILED=$full`n$lastHashError") }
            Throw-HarnessError "FIXTURE_COPY_HASH_INVALID" "fixture runtime file could not be hashed"
        }
        $entries.Add([ordered]@{
            path = $relative
            sha256 = $digest
        })
    }
    $payload = [ordered]@{
        schema_version = 1
        manifest = "fixture-runtime"
        release_manifest = "SHA256SUMS.txt"
        files = @($entries | Sort-Object { $_.path })
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $manifestPath) | Out-Null
    [IO.File]::WriteAllText($manifestPath, ($payload | ConvertTo-Json -Depth 8), $Utf8NoBom)
    Write-FixtureReleaseSha256SourceEntries -Root $Root -RelativeFiles $releaseSourceFiles
}

function Write-FixtureReleaseSha256SourceEntries {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string[]]$RelativeFiles)
    $rootFull = [IO.Path]::GetFullPath($Root)
    $sum = Join-Path $rootFull "SHA256SUMS.txt"
    $targets = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($relative in $RelativeFiles) {
        if ($relative -match '^(data/cache|runtime/live)(/|$)') { Throw-HarnessError "FIXTURE_COPY_HASH_INVALID" "fixture release SHA whitelist included runtime data" }
        [void]$targets.Add($relative.Replace('\', '/'))
    }
    $entries = @{}
    if (Test-Path -LiteralPath $sum -PathType Leaf) {
        foreach ($line in [IO.File]::ReadAllLines($sum)) {
            if ($line -notmatch '^([0-9A-Fa-f]{64})\s+\*?(.+)$') { continue }
            $relative = $Matches[2].Trim().Replace('\', '/')
            if ($targets.Contains($relative)) { continue }
            $entries[$relative] = $Matches[1].ToLowerInvariant()
        }
    }
    foreach ($relative in @($targets | Sort-Object)) {
        $full = [IO.Path]::GetFullPath((Join-Path $rootFull $relative))
        if (!$full.StartsWith($rootFull.TrimEnd('\') + "\", [StringComparison]::OrdinalIgnoreCase)) { Throw-HarnessError "FIXTURE_COPY_HASH_INVALID" "fixture release SHA path escapes package root" }
        if (!(Test-Path -LiteralPath $full -PathType Leaf)) { continue }
        $entries[$relative] = Get-PortableFileSha256 -Path $full
    }
    $lines = @(
        $entries.Keys |
            Sort-Object |
            ForEach-Object { "$($entries[$_])  $_" }
    )
    [IO.File]::WriteAllLines($sum, $lines, $Utf8NoBom)
}

function Test-FixtureSha256Manifest {
    param([Parameter(Mandatory = $true)][string]$Root)
    $rootFull = [IO.Path]::GetFullPath($Root)
    $manifestPath = Join-Path $rootFull "data/local/fixture/fixture-sha256.json"
    if (!(Test-Path -LiteralPath $manifestPath -PathType Leaf)) { Throw-HarnessError "FIXTURE_COPY_HASH_INVALID" "fixture runtime SHA-256 manifest is missing" }
    $payload = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ([int]$payload.schema_version -ne 1 -or [string]$payload.manifest -ne "fixture-runtime") { Throw-HarnessError "FIXTURE_COPY_HASH_INVALID" "fixture runtime SHA-256 manifest is invalid" }
    foreach ($entry in @($payload.files)) {
        $relative = [string]$entry.path
        if ([string]::IsNullOrWhiteSpace($relative) -or $relative.StartsWith("/") -or $relative -match '^[A-Za-z]:' -or $relative.Contains("..")) { Throw-HarnessError "FIXTURE_COPY_HASH_INVALID" "fixture runtime manifest contains an unsafe path" }
        $file = [IO.Path]::GetFullPath((Join-Path $rootFull $relative))
        if (!$file.StartsWith($rootFull.TrimEnd('\') + "\", [StringComparison]::OrdinalIgnoreCase)) { Throw-HarnessError "FIXTURE_COPY_HASH_INVALID" "fixture runtime manifest path escapes package root" }
        if (!(Test-Path -LiteralPath $file -PathType Leaf)) { Throw-HarnessError "FIXTURE_COPY_HASH_INVALID" "fixture runtime manifest path is missing" }
        $actual = Get-PortableFileSha256 -Path $file
        if ($actual -ne ([string]$entry.sha256).ToLowerInvariant()) { Throw-HarnessError "FIXTURE_COPY_HASH_INVALID" "fixture runtime manifest hash mismatch" }
    }
}

function Write-FixtureService {
    param([Parameter(Mandatory = $true)][string]$Path)
    $source = @'
from __future__ import annotations
import argparse, json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/crash":
            self.send_response(204); self.end_headers(); self.wfile.flush(); os._exit(91)
        if self.path in {"/health", "/api/health"}:
            body = json.dumps({"status": "ok", "ready": True}).encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()
    def log_message(self, format, *args):
        pass

parser = argparse.ArgumentParser()
parser.add_argument("--port", required=True, type=int)
args = parser.parse_args()
ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
'@
    [IO.File]::WriteAllText($Path, $source, $Utf8NoBom)
}

function Write-FixtureUvExe {
    param([Parameter(Mandatory = $true)][string]$Path)
    $source = @'
using System;
using System.IO;
using System.Linq;

public static class FixtureUv {
  private static void CopyTree(string source, string destination) {
    Directory.CreateDirectory(destination);
    foreach (string file in Directory.GetFiles(source)) File.Copy(file, Path.Combine(destination, Path.GetFileName(file)), true);
    foreach (string directory in Directory.GetDirectories(source)) CopyTree(directory, Path.Combine(destination, Path.GetFileName(directory)));
  }
  public static int Main(string[] args) {
    if (args.Length == 0) return 0;
    string command = args[0].ToLowerInvariant();
    if (command == "lock") return 0;
    if (command == "export") {
      for (int i = 0; i + 1 < args.Length; i++) {
        if (args[i] == "--output-file") {
          Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(args[i + 1])));
          File.WriteAllText(args[i + 1], "# fixture-only frozen requirements\n");
          return 0;
        }
      }
      return 2;
    }
    if (command == "pip" && args.Length > 1 && args[1].ToLowerInvariant() == "install") {
      string source = Environment.GetEnvironmentVariable("TTS_MORE_FIXTURE_PACKAGES");
      int targetIndex = Array.IndexOf(args, "--target");
      if (String.IsNullOrWhiteSpace(source) || targetIndex < 0 || targetIndex + 1 >= args.Length || !Directory.Exists(source)) return 3;
      CopyTree(source, args[targetIndex + 1]);
      return 0;
    }
    return 0;
  }
}
'@
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    if (Test-Path -LiteralPath $Path -PathType Leaf) { Remove-Item -LiteralPath $Path -Force }
    Add-Type -TypeDefinition $source -Language CSharp -OutputAssembly $Path -OutputType ConsoleApplication
}

function Write-FixturePythonPackages {
    param([Parameter(Mandatory = $true)][string]$RuntimeRoot)
    $site = Join-Path $RuntimeRoot "Lib\site-packages"
    New-Item -ItemType Directory -Force -Path $site | Out-Null
    $pip = Join-Path $site "pip"
    New-Item -ItemType Directory -Force -Path $pip | Out-Null
    [IO.File]::WriteAllText((Join-Path $pip "__init__.py"), "__version__ = 'fixture'`n", $Utf8NoBom)
    [IO.File]::WriteAllText((Join-Path $pip "__main__.py"), @'
from __future__ import annotations
import sys

def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in {"install", "check"}:
        return 0
    return 0

raise SystemExit(main())
'@, $Utf8NoBom)

    $uvicorn = Join-Path $site "uvicorn"
    New-Item -ItemType Directory -Force -Path $uvicorn | Out-Null
    [IO.File]::WriteAllText((Join-Path $uvicorn "__init__.py"), "__version__ = 'fixture'`n", $Utf8NoBom)
    [IO.File]::WriteAllText((Join-Path $uvicorn "__main__.py"), @'
from __future__ import annotations
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def _arg(name: str, default: str = "") -> str:
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return default

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/crash":
            self.send_response(204)
            self.end_headers()
            self.wfile.flush()
            os._exit(91)
        if self.path in {"/health", "/api/health"}:
            body = json.dumps({"status": "ok", "ready": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, _format, *args):
        pass

port = int(_arg("--port", "8000"))
ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
'@, $Utf8NoBom)

    $fastapi = Join-Path $site "fastapi"
    New-Item -ItemType Directory -Force -Path $fastapi | Out-Null
    [IO.File]::WriteAllText((Join-Path $fastapi "__init__.py"), @'
class FastAPI:
    def __init__(self, *args, **kwargs):
        pass
'@, $Utf8NoBom)

    $pydantic = Join-Path $site "pydantic"
    New-Item -ItemType Directory -Force -Path $pydantic | Out-Null
    [IO.File]::WriteAllText((Join-Path $pydantic "__init__.py"), @'
class BaseModel:
    pass
'@, $Utf8NoBom)
}

function Get-LockedEmbeddedPythonAsset {
    param([Parameter(Mandatory = $true)][string]$Root)
    $manifest = Get-Content -LiteralPath (Join-Path $Root "package\tts-more-package.json") -Raw | ConvertFrom-Json
    $runtimeLockPath = Join-Path $Root ([string]$manifest.runtime.lock)
    $runtime = Get-Content -LiteralPath $runtimeLockPath -Raw | ConvertFrom-Json
    $version = [string]$runtime.python_version
    $known = @{
        "3.11.9" = [pscustomobject]@{ Url="https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"; Sha256="009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b"; Size=11249023; Stdlib="python311.zip"; Pth="python311._pth" }
        "3.10.11" = [pscustomobject]@{ Url="https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip"; Sha256="608619f8619075629c9c69f361352a0da6ed7e62f83a0e19c63e0ea32eb7629d"; Size=8629277; Stdlib="python310.zip"; Pth="python310._pth" }
    }
    if (!$known.ContainsKey($version)) { Throw-HarnessError "FIXTURE_RUNTIME_INVALID" "component runtime lock has an unsupported exact Python version" }
    $expected = $known[$version]
    $asset = $runtime.assets.python
    if (
        [string]$asset.sha256 -ne $expected.Sha256 -or
        [int64]$asset.size_bytes -ne [int64]$expected.Size -or
        [string]$asset.archive_entry -ne "python.exe" -or
        @($asset.urls) -notcontains $expected.Url
    ) { Throw-HarnessError "FIXTURE_RUNTIME_INVALID" "component runtime lock does not pin the official embeddable Python asset" }
    return [pscustomobject]@{ Version=$version; Url=$expected.Url; Sha256=$expected.Sha256; Size=[int64]$expected.Size; Stdlib=$expected.Stdlib; Pth=$expected.Pth; RuntimeLock=$runtimeLockPath }
}

function Get-OrDownloadLockedAsset {
    param([Parameter(Mandatory = $true)][object]$Asset)
    $cacheKey = [string]$Asset.Sha256
    if ($script:LockedPythonAssetCache.ContainsKey($cacheKey)) { return [string]$script:LockedPythonAssetCache[$cacheKey] }
    $destination = Join-Path $AssetsRoot ("official-python-" + $cacheKey + ".zip")
    $partial = "$destination.partial"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $client = New-Object Net.WebClient
        try { $client.DownloadFile([string]$Asset.Url, $partial) } finally { $client.Dispose() }
        if ((Get-Item -LiteralPath $partial).Length -ne [int64]$Asset.Size -or (Get-PortableFileSha256 -Path $partial) -ne [string]$Asset.Sha256) {
            Throw-HarnessError "FIXTURE_RUNTIME_INVALID" "downloaded official Python asset failed its immutable lock"
        }
        Move-Item -LiteralPath $partial -Destination $destination -Force
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [IO.Compression.ZipFile]::OpenRead($destination)
        try {
            $names = @($archive.Entries | ForEach-Object { $_.FullName })
            if ($names -notcontains "python.exe" -or $names -notcontains [string]$Asset.Stdlib -or $names -notcontains [string]$Asset.Pth) {
                Throw-HarnessError "FIXTURE_RUNTIME_INVALID" "official Python archive does not match its exact-version layout"
            }
        }
        finally { $archive.Dispose() }
    }
    finally { Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue }
    $script:LockedPythonAssetCache[$cacheKey] = $destination
    return $destination
}

function New-FixtureUvWheel {
    param([Parameter(Mandatory = $true)][string]$Path)
    $root = Join-Path (Split-Path -Parent $Path) ("uv-wheel-" + [guid]::NewGuid().ToString("N"))
    $entry = Join-Path $root "uv-0.11.28.data\scripts\uv.exe"
    try {
        Write-FixtureUvExe -Path $entry
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Force }
        $wheelStream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $wheel = New-Object IO.Compression.ZipArchive($wheelStream, [IO.Compression.ZipArchiveMode]::Create, $false)
            try {
                [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                    $wheel,
                    $entry,
                    "uv-0.11.28.data/scripts/uv.exe",
                    [IO.Compression.CompressionLevel]::Optimal
                ) | Out-Null
            }
            finally { $wheel.Dispose() }
        }
        finally { $wheelStream.Dispose() }
    }
    finally { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue }
}

function Install-FixtureProtocol {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Component,
        [Parameter(Mandatory = $true)][string]$AssetUrl,
        [Parameter(Mandatory = $true)][byte[]]$AssetPayload
    )
    $manifestPath = Join-Path $Root "package\tts-more-package.json"
    foreach ($rootLauncher in @("Initialize.cmd", "Start.cmd", "Stop.cmd", "Repair.cmd")) {
        if (!(Test-Path -LiteralPath (Join-Path $Root $rootLauncher) -PathType Leaf)) { Throw-HarnessError "PACKAGE_CORRUPT" "required root launcher is missing" }
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $lockedPython = Get-LockedEmbeddedPythonAsset -Root $Root
    $officialPython = Get-OrDownloadLockedAsset -Asset $lockedPython
    $port = Get-RandomLoopbackPort
    $manifest.endpoint.port = $port
    $manifest.endpoint.default_url = "http://127.0.0.1:$port"
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12), $Utf8NoBom)

    $bundle = if ($Component -eq "tts-more") { Join-Path $Root "scripts" } else { Join-Path $Root "app\tts_more" }
    $componentConfigPath = Join-Path $bundle "component.json"
    if (Test-Path -LiteralPath $componentConfigPath -PathType Leaf) {
        $componentConfig = Get-Content -LiteralPath $componentConfigPath -Raw | ConvertFrom-Json
        $componentConfig.port = $port
        [IO.File]::WriteAllText($componentConfigPath, ($componentConfig | ConvertTo-Json -Depth 12), $Utf8NoBom)
    }
    $runtimeLock = Join-Path $Root ([string]$manifest.runtime.lock)
    $modelLock = Join-Path $Root ([string]$manifest.models.lock)

    $fixture = Join-Path $Root "data\local\fixture"
    New-Item -ItemType Directory -Force -Path $fixture, (Join-Path $Root "data\cache\fixture"), (Join-Path $Root "runtime") | Out-Null
    $assetLock = Join-Path $fixture "asset.lock.json"
    $assetTarget = "data/cache/fixture/payload.dat"
    $assetPath = Join-Path $Root $assetTarget
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $assetPath) | Out-Null
    [IO.File]::WriteAllBytes("$assetPath.partial", $AssetPayload[0..7])
    $asset = [ordered]@{ id="$Component-fixture"; target=$assetTarget; size_bytes=$AssetPayload.Length; sha256=([BitConverter]::ToString(([Security.Cryptography.SHA256]::Create()).ComputeHash($AssetPayload))).Replace("-", "").ToLowerInvariant(); urls=@($AssetUrl) }
    [IO.File]::WriteAllText($assetLock, ($asset | ConvertTo-Json -Depth 6), $Utf8NoBom)
    Write-FixtureService -Path (Join-Path $fixture "fixture-service.py")
    $fixturePackageRuntime = Join-Path $fixture "uv-install-source"
    Write-FixturePythonPackages -RuntimeRoot $fixturePackageRuntime
    $fixturePackages = Join-Path $fixturePackageRuntime "Lib\site-packages"

    $runtimePython = Join-Path $Root "runtime\live\python.exe"

    $pythonFixtureName = "$Component-python-$($lockedPython.Version)-embed.zip"
    $pythonFixture = Join-Path $AssetsRoot $pythonFixtureName
    Copy-Item -LiteralPath $officialPython -Destination $pythonFixture -Force
    $uvFixture = Join-Path $AssetsRoot "$Component-uv-0.11.28.whl"
    New-FixtureUvWheel -Path $uvFixture
    $uvId = "uv-0.11.28-$Component-fixture"
    $uvTarget = "data/cache/portable/assets/$uvId.whl"
    $uvWheel = Join-Path $Root $uvTarget
    $pythonAsset = [ordered]@{
        id = "$Component-python-fixture"
        size_bytes = (Get-Item -LiteralPath $pythonFixture).Length
        sha256 = Get-PortableFileSha256 -Path $pythonFixture
        urls = @("$FixtureEndpoint/$pythonFixtureName")
        archive_entry = "python.exe"
    }
    $uvAsset = [ordered]@{
        id = $uvId
        target = $uvTarget
        size_bytes = (Get-Item -LiteralPath $uvFixture).Length
        sha256 = Get-PortableFileSha256 -Path $uvFixture
        urls = @("$FixtureEndpoint/$Component-uv-0.11.28.whl")
        archive_entry = "uv-0.11.28.data/scripts/uv.exe"
    }
    $runtimePayload = [ordered]@{
        schema_version = 1
        component = $Component
        python_version = [string]$lockedPython.Version
        import_probe = "import sys"
        required_free_bytes = 1
        dependency_mode = "requirements"
        auto_order = @("cpu")
        profiles = [ordered]@{ cpu = [ordered]@{ dependency_lock = "requirements-cpu.lock.txt" } }
        assets = [ordered]@{ python = $pythonAsset; uv = $uvAsset }
        payloads = @()
    }
    $pythonPartial = Join-Path $Root "data\cache\portable\assets\$($pythonAsset.id).zip.partial"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $pythonPartial) | Out-Null
    $pythonBytes = [IO.File]::ReadAllBytes($pythonFixture)
    [IO.File]::WriteAllBytes($pythonPartial, $pythonBytes[0..7])
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $uvWheel) | Out-Null
    $uvBytes = [IO.File]::ReadAllBytes($uvFixture)
    [IO.File]::WriteAllBytes("$uvWheel.partial", $uvBytes[0..7])
    $modelPayload = [ordered]@{
        schema_version = 1
        component = $Component
        complete = $true
        required_paths = @($asset.target)
        assets = @($asset)
        required_free_bytes = 1
    }
    [IO.File]::WriteAllText($runtimeLock, ($runtimePayload | ConvertTo-Json -Depth 12), $Utf8NoBom)
    [IO.File]::WriteAllText($modelLock, ($modelPayload | ConvertTo-Json -Depth 12), $Utf8NoBom)
    if ($Component -ne "tts-more") {
        $dependencyLock = Join-Path $bundle "locks\requirements-cpu.lock.txt"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dependencyLock) | Out-Null
        [IO.File]::WriteAllText($dependencyLock, "# fixture-only dependency lock`n", $Utf8NoBom)
    }
    Write-FixtureSha256Manifest -Root $Root
    Test-FixtureSha256Manifest -Root $Root
    return [pscustomobject]@{ Root=$Root; Component=$Component; Port=$port; AssetLock=$assetLock; AssetPath=$assetPath; AssetSha=[string]$asset.sha256; AssetUrl=$AssetUrl; PythonAssetName=$pythonFixtureName; PythonVersion=[string]$lockedPython.Version; UvAssetName="$Component-uv-0.11.28.whl"; Bundle=$bundle; Service=(Join-Path $fixture "fixture-service.py"); FixturePackages=$fixturePackages; RuntimePython=$runtimePython; RuntimeLock=$runtimeLock; ModelLock=$modelLock; UvAssetPath=$uvWheel; UvAssetSha=$uvAsset.sha256 }
}

function Invoke-RootCommand {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$Name, [string[]]$Arguments=@())
    $launcher = Join-Path $Root $Name
    if (!(Test-Path -LiteralPath $launcher -PathType Leaf)) { Throw-HarnessError "PACKAGE_CORRUPT" "root launcher is missing" }
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& $launcher @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousPreference }
    return [pscustomobject]@{ ExitCode=$exitCode; Output=($output -join "`n") }
}

function Set-FixtureAssetUrl {
    param([Parameter(Mandatory = $true)][object]$Package, [Parameter(Mandatory = $true)][string]$Url)
    Set-FixtureAssetUrls -Package $Package -Urls @($Url)
}

function Set-FixtureAssetUrls {
    param([Parameter(Mandatory = $true)][object]$Package, [Parameter(Mandatory = $true)][string[]]$Urls)
    $payload = Get-Content -LiteralPath $Package.AssetLock -Raw | ConvertFrom-Json
    $payload.urls = @($Urls)
    [IO.File]::WriteAllText($Package.AssetLock, ($payload | ConvertTo-Json -Depth 6), $Utf8NoBom)
    $lock = Get-Content -LiteralPath $Package.ModelLock -Raw | ConvertFrom-Json
    foreach ($asset in @($lock.assets)) {
        if ([string]$asset.id -eq "$($Package.Component)-fixture") { $asset.urls = @($Urls) }
    }
    [IO.File]::WriteAllText($Package.ModelLock, ($lock | ConvertTo-Json -Depth 12), $Utf8NoBom)
    if ($Package.PSObject.Properties.Name -contains "RuntimeLock" -and (Test-Path -LiteralPath ([string]$Package.RuntimeLock) -PathType Leaf)) {
        $runtime = Get-Content -LiteralPath ([string]$Package.RuntimeLock) -Raw | ConvertFrom-Json
        if ($runtime.PSObject.Properties["assets"] -and $runtime.assets.PSObject.Properties["python"]) {
            $runtime.assets.python.urls = @("$FixtureEndpoint/$($Package.PythonAssetName)")
        }
        if ($runtime.PSObject.Properties["assets"] -and $runtime.assets.PSObject.Properties["uv"]) { $runtime.assets.uv.urls = @("$FixtureEndpoint/$($Package.UvAssetName)") }
        [IO.File]::WriteAllText(([string]$Package.RuntimeLock), ($runtime | ConvertTo-Json -Depth 12), $Utf8NoBom)
    }
    Write-FixtureSha256Manifest -Root $Package.Root
    Test-FixtureSha256Manifest -Root $Package.Root
}

function Start-FixtureAssetServer {
    if ($null -ne $ServerProcess) {
        try { $ServerProcess.Refresh() } catch { }
        if (!$ServerProcess.HasExited) { return }
    }
    if ([string]::IsNullOrWhiteSpace($AssetsRoot) -or !(Test-Path -LiteralPath $AssetsRoot -PathType Container)) {
        Throw-HarnessError "FIXTURE_SERVER_FAILED" "fixture asset root is missing"
    }
    $script:ServerRestartIndex += 1
    $readyFile = Join-Path $WorkRoot ("server-ready-$ServerRestartIndex.json")
    if (Test-Path -LiteralPath $readyFile -PathType Leaf) { Remove-Item -LiteralPath $readyFile -Force }
    $serverArguments = @(
        (Split-Path -Leaf $FixtureServerScript),
        "--root", $AssetsRoot,
        "--ready-file", $readyFile,
        "--interrupt-after", "8",
        "--request-log", $ServerRequestLog
    )
    $stdout = Join-Path $WorkRoot ("server-$ServerRestartIndex.stdout.log")
    $stderr = Join-Path $WorkRoot ("server-$ServerRestartIndex.stderr.log")
    $serverArgumentLine = ConvertTo-PortableWindowsArgumentLine -Arguments $serverArguments
    $script:ServerProcess = Start-Process -FilePath $FixtureBasePython -ArgumentList $serverArgumentLine -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    $OwnedProcesses.Add([pscustomobject]@{ Process=$ServerProcess; Executable=$FixtureBasePython; StartedAt=$ServerProcess.StartTime.ToUniversalTime(); Port=0 })
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    while (!(Test-Path -LiteralPath $readyFile -PathType Leaf) -and [DateTime]::UtcNow -lt $deadline) {
        if ($ServerProcess.HasExited) { Throw-HarnessError "FIXTURE_SERVER_FAILED" "fixture asset server exited before readiness" }
        Start-Sleep -Milliseconds 100
    }
    if (!(Test-Path -LiteralPath $readyFile -PathType Leaf)) { Throw-HarnessError "FIXTURE_SERVER_FAILED" "fixture asset server readiness timed out" }
    $endpoint = [string](Get-Content -LiteralPath $readyFile -Raw | ConvertFrom-Json).endpoint
    if ($endpoint -notmatch '^http://127\.0\.0\.1:[0-9]+$') { Throw-HarnessError "FIXTURE_SERVER_FAILED" "fixture server did not bind random loopback" }
    $script:FixtureEndpoint = $endpoint
}

function Restart-FixtureAssetServer {
    if ($null -ne $ServerProcess) {
        $owned = @($OwnedProcesses | Where-Object {
            $null -ne $_.Process -and $_.Process.Id -eq $ServerProcess.Id
        } | Select-Object -Last 1)
        if ($owned.Count -gt 0) { Stop-OwnedFixtureProcess -Owned $owned[0] }
        $script:ServerProcess = $null
    }
    Start-FixtureAssetServer
}

function Assert-FixtureNetworkEvidence {
    if (!(Test-Path -LiteralPath $ServerRequestLog -PathType Leaf)) { Throw-HarnessError "NETWORK_EVIDENCE_MISSING" "fixture request log is missing" }
    $requests = @(
        foreach ($line in [IO.File]::ReadAllLines($ServerRequestLog)) {
            if (![string]::IsNullOrWhiteSpace($line)) { $line | ConvertFrom-Json }
        }
    )
    foreach ($component in $ExpectedComponents) {
        $assetPath = "$component.bin"
        $componentRequests = @($requests | Where-Object { [string]$_.path -eq $assetPath })
        $resumed = @($componentRequests | Where-Object { [string]$_.range -eq "bytes=8-" -and [int]$_.status -eq 206 })
        if ($resumed.Count -eq 0) { Throw-HarnessError "RANGE_EVIDENCE_MISSING" "real package download did not resume from byte 8" }
        $fallbackProved = $false
        for ($index = 0; $index -lt $componentRequests.Count; $index++) {
            if ([int]$componentRequests[$index].status -eq 503) {
                for ($later = $index + 1; $later -lt $componentRequests.Count; $later++) {
                    if ([int]$componentRequests[$later].status -in @(200, 206)) { $fallbackProved = $true; break }
                }
            }
            if ($fallbackProved) { break }
        }
        if (!$fallbackProved) { Throw-HarnessError "MIRROR_EVIDENCE_MISSING" "real package download did not fall back after HTTP 503" }
    }
}

function Assert-AssetHash {
    param([Parameter(Mandatory = $true)][object]$Package)
    if (!(Test-Path -LiteralPath $Package.AssetPath -PathType Leaf)) { Throw-HarnessError "ASSET_MISSING" "fixture asset is missing" }
    $actual = Get-PortableFileSha256 -Path ([string]$Package.AssetPath)
    if (![string]::Equals($actual, $Package.AssetSha, [StringComparison]::OrdinalIgnoreCase)) { Throw-HarnessError "ASSET_HASH_INVALID" "fixture asset hash is invalid" }
}

function Assert-DownloadAssetHash {
    param([Parameter(Mandatory = $true)][object]$Package)
    Assert-AssetHash -Package $Package
}

function Invoke-FixtureAssetDownload {
    param([Parameter(Mandatory = $true)][object]$Package)
    $installer = Join-Path $Package.Bundle "portable_install.py"
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& $Package.RuntimePython $installer ensure-asset --asset $Package.AssetLock --path $Package.AssetPath --package-root $Package.Root 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousPreference }
    return [pscustomobject]@{ ExitCode=$exitCode; Output=($output -join "`n") }
}

function Stop-OwnedFixtureProcess {
    param([Parameter(Mandatory = $true)][object]$Owned)
    $process = $Owned.Process
    try { $process.Refresh() } catch { return }
    if ($process.HasExited) { return }
    $actual = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
    if (!$actual) { return }
    try {
        $actualPath = Resolve-FixtureCanonicalPath -Path $actual.Path
        $ownedPath = Resolve-FixtureCanonicalPath -Path $Owned.Executable
        $actualStartedAt = $actual.StartTime.ToUniversalTime()
    }
    catch { return }
    if (![string]::Equals($actualPath, $ownedPath, [StringComparison]::OrdinalIgnoreCase) -or $actualStartedAt -ne $Owned.StartedAt) { return }
    if ($Owned.PSObject.Properties.Name -contains "Port" -and [int]$Owned.Port -gt 0) {
        $owners = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$Owned.Port) -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
        if ($owners.Count -gt 0 -and ($owners.Count -ne 1 -or [int]$owners[0] -ne [int]$process.Id)) {
            Throw-HarnessError "UNKNOWN_PROCESS_REFUSED" "fixture cleanup refused a mismatched listener owner"
        }
    }
    $process.Kill()
    if (!$process.WaitForExit(10000)) { Throw-HarnessError "OWNED_PROCESS_STUCK" "owned fixture process did not stop" }
}

function Resolve-FixtureCanonicalPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $previousPythonIoEncoding = [Environment]::GetEnvironmentVariable("PYTHONIOENCODING", "Process")
    try {
        $env:PYTHONIOENCODING = "utf-8"
        $output = @(& $FixtureBasePython -c "import base64,pathlib,sys; print(base64.b64encode(str(pathlib.Path(sys.argv[1]).resolve(strict=True)).encode('utf-8')).decode('ascii'))" $Path 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        [Environment]::SetEnvironmentVariable("PYTHONIOENCODING", $previousPythonIoEncoding, "Process")
    }
    if ($exitCode -ne 0 -or $output.Count -ne 1) {
        Throw-HarnessError "PATH_IDENTITY_INVALID" "fixture path could not be resolved to its stable filesystem identity"
    }
    try {
        $resolved = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([string]$output[0]))
    }
    catch {
        Throw-HarnessError "PATH_IDENTITY_INVALID" "fixture path identity transport is invalid"
    }
    if (![IO.Path]::IsPathRooted($resolved)) {
        Throw-HarnessError "PATH_IDENTITY_INVALID" "fixture path identity is not absolute"
    }
    return [IO.Path]::GetFullPath($resolved)
}

function Register-PackageOwnedProcess {
    param([Parameter(Mandatory = $true)][object]$Package)
    $recordPath = Join-Path $Package.Root "data\local\run\worker.pid.json"
    if (!(Test-Path -LiteralPath $recordPath -PathType Leaf)) { return }
    $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
    $processId = [int]$record.pid
    $createdAt = [DateTime]::Parse([string]$record.process_created_at, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
    $expectedExecutable = Resolve-FixtureCanonicalPath -Path (Join-Path $Package.Root "runtime\live\python.exe")
    $recordedExecutable = Resolve-FixtureCanonicalPath -Path ([string]$record.executable_path)
    if ($processId -le 0 -or ![string]::Equals($recordedExecutable, $expectedExecutable, [StringComparison]::OrdinalIgnoreCase)) {
        Throw-HarnessError "OWNED_PROCESS_INVALID" "fixture package PID record has an invalid executable identity"
    }
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if (!$process) { return }
    $actualExecutable = Resolve-FixtureCanonicalPath -Path $process.Path
    if (![string]::Equals($actualExecutable, $expectedExecutable, [StringComparison]::OrdinalIgnoreCase) -or $process.StartTime.ToUniversalTime() -ne $createdAt) {
        Throw-HarnessError "UNKNOWN_PROCESS_REFUSED" "fixture package PID record resolves to another process"
    }
    $key = "$processId|$($createdAt.ToString('o'))"
    if ($OwnedProcessKeys.Add($key)) {
        $OwnedProcesses.Add([pscustomobject]@{ Process=$process; Executable=$expectedExecutable; StartedAt=$createdAt; Port=[int]$record.port })
    }
}

function Assert-OwnedFixtureProcessesStopped {
    foreach ($owned in $OwnedProcesses) {
        try { $owned.Process.Refresh() } catch { continue }
        if ($owned.Process.HasExited) { continue }
        $actual = Get-Process -Id $owned.Process.Id -ErrorAction SilentlyContinue
        if (!$actual) { continue }
        try {
            $actualPath = Resolve-FixtureCanonicalPath -Path $actual.Path
            $ownedPath = Resolve-FixtureCanonicalPath -Path $owned.Executable
            $actualStartedAt = $actual.StartTime.ToUniversalTime()
        }
        catch { continue }
        if ([string]::Equals($actualPath, $ownedPath, [StringComparison]::OrdinalIgnoreCase) -and $actualStartedAt -eq $owned.StartedAt) {
            Throw-HarnessError "OWNED_PROCESS_REMAINED" "an owned fixture process survived cleanup"
        }
    }
}

function New-FixtureManagedOperation {
    param([Parameter(Mandatory = $true)][object]$Package)
    $manifest = Get-Content -LiteralPath (Join-Path $Package.Root "package\tts-more-package.json") -Raw | ConvertFrom-Json
    $operations = Join-Path $Package.Root ([string]$manifest.data.operations)
    $operationId = [guid]::NewGuid().ToString()
    $operation = Join-Path $operations $operationId
    New-Item -ItemType Directory -Force -Path $operation | Out-Null
    $payload = [ordered]@{ operation_id=$operationId; component=[string]$Package.Component; action="initialize"; initiator="portable-first-run"; started_at=[DateTime]::UtcNow.ToString("o"); status="installing"; exit_code=$null }
    [IO.File]::WriteAllText((Join-Path $operation "operation.json"), ($payload | ConvertTo-Json -Depth 4), $Utf8NoBom)
    return $operation
}

function Assert-ManagedOperationProgress {
    param([Parameter(Mandatory = $true)][object]$Package, [Parameter(Mandatory = $true)][string]$OperationRoot)
    $eventsPath = Join-Path $OperationRoot "events.jsonl"
    if (!(Test-Path -LiteralPath $eventsPath -PathType Leaf)) { Throw-HarnessError "OPERATION_PROGRESS_MISSING" "managed initialization did not emit operation progress" }
    $events = @([IO.File]::ReadAllLines($eventsPath) | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
    if (@($events | Where-Object { $_.phase -eq "downloading" }).Count -eq 0) { Throw-HarnessError "OPERATION_PROGRESS_MISSING" "managed initialization did not emit download progress" }
    $actual = (@(& $Package.RuntimePython -c "import platform;print(platform.python_version())" 2>&1) -join "").Trim()
    if ($LASTEXITCODE -ne 0 -or $actual -ne [string]$Package.PythonVersion) { Throw-HarnessError "FIXTURE_RUNTIME_INVALID" "managed operation progress did not run with the component's exact package Python" }
    return $actual
}

function Invoke-PackageAcceptance {
    param([Parameter(Mandatory = $true)][object]$Package)
    $component = [string]$Package.Component
    Start-FixtureAssetServer
    $Package.AssetUrl = "$FixtureEndpoint/$component.bin"
    Set-FixtureAssetUrl -Package $Package -Url $Package.AssetUrl
    Invoke-Scenario -Component $component -Scenario "path_isolation" -Action { Assert-RestrictedChildPath }
    $ServerProcess.Refresh()
    if ($ServerProcess.HasExited) { Throw-HarnessError "FIXTURE_SERVER_EXITED" "fixture asset server exited before package initialization" }
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$($Package.AssetUrl)?mode=proxy-failure" -TimeoutSec 3 | Out-Null
        Throw-HarnessError "PROXY_FAILURE_NOT_INJECTED" "fixture server proxy probe unexpectedly succeeded"
    }
    catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 503) { }
        elseif ((Get-ErrorCode -ErrorRecord $_) -eq "PROXY_FAILURE_NOT_INJECTED") { throw }
        else { Throw-HarnessError "FIXTURE_SERVER_FAILED" "fixture asset server proxy probe failed unexpectedly" }
    }

    $initializeScenario = if ($component -eq "tts-more") { "controller_real_initialize" } else { "worker_real_initialize" }
    Invoke-Scenario -Component $component -Scenario $initializeScenario -Action {
        $env:TTS_MORE_FIXTURE_PACKAGES = [string]$Package.FixturePackages
        $initializeArguments = @()
        $managedOperation = ""
        if ($component -ne "tts-more") {
            $managedOperation = New-FixtureManagedOperation -Package $Package
            $initializeArguments = @("-OperationRoot", $managedOperation, "-CancelFile", (Join-Path $managedOperation "cancel.requested"))
        }
        $initialized = Invoke-RootCommand -Root $Package.Root -Name "Initialize.cmd" -Arguments $initializeArguments
        if ($initialized.ExitCode -ne 0) {
            if ([string]$env:TTS_MORE_FIRST_RUN_DEBUG -eq "1") { [Console]::Error.WriteLine("INITIALIZE_EXIT=$($initialized.ExitCode)`n$($initialized.Output)") }
            Throw-HarnessError "INITIALIZE_FAILED" "Initialize.cmd failed"
        }
        Assert-AssetHash -Package $Package
        if ($component -ne "tts-more") {
            $actualProgressPython = Assert-ManagedOperationProgress -Package $Package -OperationRoot $managedOperation
            $Package | Add-Member -NotePropertyName OperationProgressPython -NotePropertyValue $actualProgressPython -Force
        }
    }

    $interruptedAssetPath = [string]$Package.AssetPath
    $partial = "$interruptedAssetPath.partial"
    Remove-Item -LiteralPath $interruptedAssetPath -Force
    Restart-FixtureAssetServer
    $Package.AssetUrl = "$FixtureEndpoint/$component.bin"
    Set-FixtureAssetUrl -Package $Package -Url $Package.AssetUrl
    if ($component -eq "tts-more") {
        foreach ($runtimeDirectory in @("runtime\live", "runtime\staging", "runtime\previous")) {
            Remove-Item -LiteralPath (Join-Path $Package.Root $runtimeDirectory) -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    if ($component -eq "tts-more") {
        Remove-Item -LiteralPath (Join-Path $Package.Root "data\local\install-state.json") -Force -ErrorAction SilentlyContinue
    }
    $requestLogBaseline = if (Test-Path -LiteralPath $ServerRequestLog -PathType Leaf) {
        @([IO.File]::ReadAllLines($ServerRequestLog)).Count
    } else {
        0
    }
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $first = Invoke-RootCommand -Root $Package.Root -Name "Start.cmd" -Arguments @("-ManagedBy", "portable-first-run", "-NoUi", "-PortOverride", [string]$Package.Port)
    $clock.Stop()
    $newRequestEvidence = @()
    if (Test-Path -LiteralPath $ServerRequestLog -PathType Leaf) {
        $newRequestLines = @([IO.File]::ReadAllLines($ServerRequestLog) | Select-Object -Skip $requestLogBaseline)
        $newRequestEvidence = @(
            for ($requestIndex = 0; $requestIndex -lt $newRequestLines.Count; $requestIndex += 1) {
                $line = $newRequestLines[$requestIndex]
                if ([string]::IsNullOrWhiteSpace($line)) { continue }
                $request = $line | ConvertFrom-Json
                [pscustomobject]@{ Index=$requestIndex; Request=$request }
            }
        )
    }
    $interruptionEvidence = @($newRequestEvidence | Where-Object {
        [string]$_.Request.path -eq "$component.bin" -and
        [string]::IsNullOrWhiteSpace([string]$_.Request.range) -and
        [int]$_.Request.status -eq 200 -and
        [bool]$_.Request.interrupted
    } | Select-Object -First 1)
    $assetLength = (Get-Item -LiteralPath (Join-Path $AssetsRoot "$component.bin")).Length
    $expectedContentRange = "bytes 8-$($assetLength - 1)/$assetLength"
    $resumeEvidence = if ($interruptionEvidence.Count -gt 0) {
        @($newRequestEvidence | Where-Object {
            $_.Index -gt $interruptionEvidence[0].Index -and
            [string]$_.Request.path -eq "$component.bin" -and
            [string]$_.Request.range -eq "bytes=8-" -and
            [int]$_.Request.status -eq 206 -and
            [string]$_.Request.content_range -eq $expectedContentRange
        } | Select-Object -First 1)
    } else {
        @()
    }
    $resumeEvidence = @($resumeEvidence)
    if (
        $interruptionEvidence.Count -eq 0
    ) {
        if ([string]$env:TTS_MORE_FIRST_RUN_DEBUG -eq "1") {
            $ServerProcess.Refresh()
            $partialExists = Test-Path -LiteralPath $partial -PathType Leaf
            $partialLength = if ($partialExists) { (Get-Item -LiteralPath $partial).Length } else { -1 }
            [Console]::Error.WriteLine("FIRST_EXIT=$($first.ExitCode) SERVER_EXITED=$($ServerProcess.HasExited) PARTIAL_EXISTS=$partialExists PARTIAL_LENGTH=$partialLength INTERRUPTION_COUNT=$($interruptionEvidence.Count) RESUME_COUNT=$($resumeEvidence.Count) PARTIAL=$partial`n$($first.Output)")
        }
        Add-Evidence -Component $component -Scenario "interruption" -Result fail -Duration $clock.Elapsed.TotalSeconds -ErrorCode "INTERRUPTION_NOT_PRESERVED"
        Throw-HarnessError "INTERRUPTION_NOT_PRESERVED" "first start did not record the fresh interrupted response"
    }
    Add-Evidence -Component $component -Scenario "interruption" -Result pass -Duration $clock.Elapsed.TotalSeconds

    Invoke-Scenario -Component $component -Scenario "resume" -Action {
        if (
            $first.ExitCode -ne 0 -or
            (Test-Path -LiteralPath $partial) -or
            !(Test-Path -LiteralPath $interruptedAssetPath -PathType Leaf) -or
            $resumeEvidence.Count -eq 0
        ) {
            Throw-HarnessError "RANGE_EVIDENCE_MISSING" "first start did not resume the fresh interrupted response from byte 8 with a valid Content-Range"
        }
        Assert-AssetHash -Package $Package
        $uvActual = Get-PortableFileSha256 -Path ([string]$Package.UvAssetPath)
        if (![string]::Equals($uvActual, [string]$Package.UvAssetSha, [StringComparison]::OrdinalIgnoreCase)) { Throw-HarnessError "ASSET_HASH_INVALID" "fixture uv asset hash is invalid" }
        Wait-LoopbackPort -Port $Package.Port -Listening $true
    }
    $recordPath = Join-Path $Package.Root "data\local\run\worker.pid.json"
    $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
    $initialPid = [int]$record.pid
    Register-PackageOwnedProcess -Package $Package

    Invoke-Scenario -Component $component -Scenario "duplicate_start" -Action {
        $duplicate = Invoke-RootCommand -Root $Package.Root -Name "Start.cmd" -Arguments @("-ManagedBy", "portable-first-run", "-NoUi", "-PortOverride", [string]$Package.Port)
        if ($duplicate.ExitCode -ne 0) {
            if ([string]$env:TTS_MORE_FIRST_RUN_DEBUG -eq "1") {
                [Console]::Error.WriteLine("DUPLICATE_EXIT=$($duplicate.ExitCode)`n$($duplicate.Output)")
                if ($component -eq "tts-more") {
                    $manifest = Get-Content -LiteralPath (Join-Path $Package.Root "package\tts-more-package.json") -Raw | ConvertFrom-Json
                    $launcher = Join-Path $Package.Root "scripts\portable_launcher.py"
                    $backend = Join-Path $Package.Root "app\backend"
                    $verify = @($launcher, "verify-owned-listener", "--package-root", $Package.Root, "--record-path", $recordPath, "--port", [string]$Package.Port, "--build-id", [string]$manifest.build_id, "--executable", $Package.RuntimePython, "--listener-pid", [string]$initialPid, "--", "-m", "uvicorn", "app.main:app", "--app-dir", $backend, "--host", "127.0.0.1", "--port", [string]$Package.Port)
                    $previousPreference = $ErrorActionPreference
                    try {
                        $ErrorActionPreference = "Continue"
                        $detail = @(& $Package.RuntimePython @verify 2>&1)
                        $detailExit = $LASTEXITCODE
                    }
                    finally { $ErrorActionPreference = $previousPreference }
                    [Console]::Error.WriteLine("DUPLICATE_VERIFY_EXIT=$detailExit`n$($detail -join "`n")")
                }
            }
            Throw-HarnessError "DUPLICATE_START_FAILED" "duplicate Start.cmd failed"
        }
        $unchanged = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
        if ([int]$unchanged.pid -ne $initialPid) { Throw-HarnessError "DUPLICATE_PROCESS" "duplicate Start.cmd created another listener" }
    }

    $stoppedForRepair = Invoke-RootCommand -Root $Package.Root -Name "Stop.cmd"
    if ($stoppedForRepair.ExitCode -ne 0) {
        if ([string]$env:TTS_MORE_FIRST_RUN_DEBUG -eq "1") { [Console]::Error.WriteLine("PRE_REPAIR_STOP_EXIT=$($stoppedForRepair.ExitCode)`n$($stoppedForRepair.Output)") }
        Throw-HarnessError "STOP_FAILED" "Stop.cmd failed before Repair.cmd validation"
    }
    Wait-LoopbackPort -Port $Package.Port -Listening $false
    $preRepairDeadline = [DateTime]::UtcNow.AddSeconds(15)
    while ((Get-Process -Id $initialPid -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $preRepairDeadline) {
        Start-Sleep -Milliseconds 100
    }
    if (Get-Process -Id $initialPid -ErrorAction SilentlyContinue) { Throw-HarnessError "CHILD_PROCESS_REMAINED" "an owned process survived the pre-Repair Stop.cmd" }
    Start-Sleep -Milliseconds 500

    Invoke-Scenario -Component $component -Scenario "proxy_fallback" -Action {
        $proxyUrl = "$($Package.AssetUrl)?mode=proxy-failure"
        Set-FixtureAssetUrls -Package $Package -Urls @($proxyUrl, $Package.AssetUrl)
        $repairTarget = [string]$Package.AssetPath
        Remove-Item -LiteralPath $repairTarget -Force
        Remove-Item -LiteralPath "$repairTarget.partial" -Force -ErrorAction SilentlyContinue
        if ($component -eq "tts-more") {
            foreach ($runtimeDirectory in @("runtime\live", "runtime\staging", "runtime\previous")) {
                Remove-Item -LiteralPath (Join-Path $Package.Root $runtimeDirectory) -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        if ($component -eq "tts-more") {
            Remove-Item -LiteralPath (Join-Path $Package.Root "data\local\install-state.json") -Force -ErrorAction SilentlyContinue
        }
        $repair = Invoke-RootCommand -Root $Package.Root -Name "Repair.cmd"
        if ($repair.ExitCode -ne 0) {
            if ([string]$env:TTS_MORE_FIRST_RUN_DEBUG -eq "1") { [Console]::Error.WriteLine("FALLBACK_EXIT=$($repair.ExitCode)`n$($repair.Output)") }
            Throw-HarnessError "REPAIR_FAILED" "Repair.cmd did not fall back to the second mirror"
        }
        Assert-DownloadAssetHash -Package $Package
        Set-FixtureAssetUrl -Package $Package -Url $Package.AssetUrl
    }

    Invoke-Scenario -Component $component -Scenario "corruption_repair" -Action {
        $repairTarget = [string]$Package.AssetPath
        $bytes = [IO.File]::ReadAllBytes($repairTarget)
        $originalLength = $bytes.Length
        $bytes[0] = $bytes[0] -bxor 0xff
        [IO.File]::WriteAllBytes($repairTarget, $bytes)
        if ((Get-Item -LiteralPath $repairTarget).Length -ne $originalLength) { Throw-HarnessError "CORRUPTION_INVALID" "corruption injection changed asset length" }
        if ($component -eq "tts-more") {
            foreach ($runtimeDirectory in @("runtime\live", "runtime\staging", "runtime\previous")) {
                Remove-Item -LiteralPath (Join-Path $Package.Root $runtimeDirectory) -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        if ($component -eq "tts-more") {
            Remove-Item -LiteralPath (Join-Path $Package.Root "data\local\install-state.json") -Force -ErrorAction SilentlyContinue
        }
        $repaired = Invoke-RootCommand -Root $Package.Root -Name "Repair.cmd"
        if ($repaired.ExitCode -ne 0) { Throw-HarnessError "REPAIR_FAILED" "Repair.cmd failed for same-length corruption" }
        Assert-DownloadAssetHash -Package $Package
    }

    Invoke-Scenario -Component $component -Scenario "stale_pid" -Action {
        $startedForCrash = Invoke-RootCommand -Root $Package.Root -Name "Start.cmd" -Arguments @("-ManagedBy", "portable-first-run", "-NoUi", "-PortOverride", [string]$Package.Port)
        if ($startedForCrash.ExitCode -ne 0) {
            if ([string]$env:TTS_MORE_FIRST_RUN_DEBUG -eq "1") { [Console]::Error.WriteLine("PRE_STALE_START_EXIT=$($startedForCrash.ExitCode)`n$($startedForCrash.Output)") }
            Throw-HarnessError "STALE_PID_RECOVERY_FAILED" "Start.cmd did not start after Repair.cmd validation"
        }
        Wait-LoopbackPort -Port $Package.Port -Listening $true
        $runningRecord = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
        $initialPid = [int]$runningRecord.pid
        $initialCreatedAt = [DateTime]::Parse([string]$runningRecord.process_created_at, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
        Register-PackageOwnedProcess -Package $Package
        $crashProcess = Get-Process -Id $initialPid -ErrorAction SilentlyContinue
        if (!$crashProcess) { Throw-HarnessError "STALE_PID_RECOVERY_FAILED" "owned worker process was missing before crash injection" }
        $expectedExecutable = Resolve-FixtureCanonicalPath -Path (Join-Path $Package.Root "runtime\live\python.exe")
        $crashExecutable = Resolve-FixtureCanonicalPath -Path $crashProcess.Path
        if (![string]::Equals($crashExecutable, $expectedExecutable, [StringComparison]::OrdinalIgnoreCase)) {
            Throw-HarnessError "UNKNOWN_PROCESS_REFUSED" "stale PID crash injection refused a non-package process"
        }
        Stop-Process -Id $initialPid -Force
        $crashProcess.WaitForExit(10000)
        Wait-LoopbackPort -Port $Package.Port -Listening $false
        if (!(Test-Path -LiteralPath $recordPath -PathType Leaf)) { Throw-HarnessError "STALE_PID_MISSING" "crash did not leave stale ownership evidence" }
        $restarted = Invoke-RootCommand -Root $Package.Root -Name "Start.cmd" -Arguments @("-ManagedBy", "portable-first-run", "-NoUi", "-PortOverride", [string]$Package.Port)
        if ($restarted.ExitCode -ne 0) {
            if ([string]$env:TTS_MORE_FIRST_RUN_DEBUG -eq "1") { [Console]::Error.WriteLine("STALE_RESTART_COMPONENT=$component EXIT=$($restarted.ExitCode)`n$($restarted.Output)") }
            Throw-HarnessError "STALE_PID_RECOVERY_FAILED" "Start.cmd did not recover stale PID evidence"
        }
        $replacement = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
        $replacementCreatedAt = [DateTime]::Parse([string]$replacement.process_created_at, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
        if ([int]$replacement.pid -eq $initialPid -and $replacementCreatedAt -eq $initialCreatedAt) { Throw-HarnessError "STALE_PID_RECOVERY_FAILED" "recovery did not create a fresh process identity" }
        Register-PackageOwnedProcess -Package $Package
    }
    $replacementRecord = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
    $knownPids = @($initialPid, [int]$replacementRecord.pid)

    Invoke-Scenario -Component $component -Scenario "clean_stop" -Action {
        $stopped = Invoke-RootCommand -Root $Package.Root -Name "Stop.cmd"
        if ($stopped.ExitCode -ne 0) {
            if ([string]$env:TTS_MORE_FIRST_RUN_DEBUG -eq "1") { [Console]::Error.WriteLine("STOP_EXIT=$($stopped.ExitCode)`n$($stopped.Output)") }
            Throw-HarnessError "STOP_FAILED" "Stop.cmd failed"
        }
        Wait-LoopbackPort -Port $Package.Port -Listening $false
        if (Test-Path -LiteralPath $recordPath -PathType Leaf) { Throw-HarnessError "STALE_PID_RECORD" "Stop.cmd left a PID record" }
        Start-Sleep -Milliseconds 300
        foreach ($knownPid in $knownPids) { if (Get-Process -Id $knownPid -ErrorAction SilentlyContinue) { Throw-HarnessError "CHILD_PROCESS_REMAINED" "an owned process survived Stop.cmd" } }
        $lexicalPackageRoot = [IO.Path]::GetFullPath($Package.Root)
        $packagePrefix = (Resolve-FixtureCanonicalPath -Path $Package.Root).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
        $descendants = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            if ([int]$_.ParentProcessId -notin $knownPids) { return $false }
            $executable = [string]$_.ExecutablePath
            $commandLine = [string]$_.CommandLine
            $canonicalExecutable = if (![string]::IsNullOrWhiteSpace($executable) -and (Test-Path -LiteralPath $executable -PathType Leaf)) {
                Resolve-FixtureCanonicalPath -Path $executable
            } else {
                ""
            }
            return (
                (![string]::IsNullOrWhiteSpace($canonicalExecutable) -and $canonicalExecutable.StartsWith($packagePrefix, [StringComparison]::OrdinalIgnoreCase)) -or
                (![string]::IsNullOrWhiteSpace($commandLine) -and $commandLine.IndexOf($lexicalPackageRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0)
            )
        })
        if ($descendants.Count -gt 0) { Throw-HarnessError "CHILD_PROCESS_REMAINED" "an owned child process survived Stop.cmd" }
    }

    Invoke-Scenario -Component $component -Scenario "unknown_port" -Action {
        $foreign = Start-Process -FilePath $Package.RuntimePython -ArgumentList @("fixture-service.py", "--port", [string]$Package.Port) -WorkingDirectory (Split-Path -Parent $Package.Service) -WindowStyle Hidden -PassThru
        $owned = [pscustomobject]@{ Process=$foreign; Executable=$Package.RuntimePython; StartedAt=$foreign.StartTime.ToUniversalTime(); Port=[int]$Package.Port }
        $OwnedProcesses.Add($owned)
        try {
            Wait-LoopbackPort -Port $Package.Port -Listening $true
            $refused = Invoke-RootCommand -Root $Package.Root -Name "Start.cmd" -Arguments @("-ManagedBy", "portable-first-run", "-NoUi", "-PortOverride", [string]$Package.Port)
            if ($refused.ExitCode -ne 23) { Throw-HarnessError "UNKNOWN_PORT_NOT_REFUSED" "Start.cmd did not reject an unknown listener" }
            $safeStop = Invoke-RootCommand -Root $Package.Root -Name "Stop.cmd"
            if ($safeStop.ExitCode -ne 0 -or $foreign.HasExited -or !(Test-LoopbackPort -Port $Package.Port)) { Throw-HarnessError "UNKNOWN_PROCESS_TOUCHED" "Stop.cmd changed an unknown listener" }
        }
        finally {
            Stop-OwnedFixtureProcess -Owned $owned
            Wait-LoopbackPort -Port $Package.Port -Listening $false
        }
    }
    if ($component -ne "tts-more") {
        $script:WorkerLifecycleSucceeded[$component] = [pscustomobject]@{ OperationProgressPython=[string]$Package.OperationProgressPython }
    }
}

function Remove-OwnedFixtureRoot {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$Identity)
    if ([string]::IsNullOrWhiteSpace($Root) -or !(Test-Path -LiteralPath $Root -PathType Container)) { return }
    $resolved = [IO.Path]::GetFullPath($Root)
    $runRoot = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) "TTS More 中文")).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $marker = Join-Path $resolved ".fixture-owner.json"
    $payload = if (Test-Path -LiteralPath $marker -PathType Leaf) { try { Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json } catch { $null } } else { $null }
    if (
        !$resolved.StartsWith($runRoot, [StringComparison]::OrdinalIgnoreCase) -or
        ((Get-Item -LiteralPath $resolved -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $null -eq $payload -or
        [string]$payload.run_id -ne $Identity -or
        [int]$payload.owner_pid -ne $PID -or
        [string]$payload.owner_started_at -ne $script:OwnerStartedAt -or
        ![string]::Equals([IO.Path]::GetFullPath([string]$payload.root), $resolved, [StringComparison]::OrdinalIgnoreCase)
    ) {
        Throw-HarnessError "CLEANUP_REFUSED" "fixture cleanup identity check failed closed"
    }
    $deleted = $false
    $lastDeleteError = $null
    foreach ($attempt in 1..60) {
        try {
            Remove-Item -LiteralPath $resolved -Recurse -Force
            $deleted = $true
            break
        }
        catch {
            $lastDeleteError = $_
            Start-Sleep -Milliseconds 500
        }
    }
    if (!$deleted) {
        throw $lastDeleteError
    }
    $parent = Split-Path -Parent $resolved
    if ((Test-Path -LiteralPath $parent -PathType Container) -and @(Get-ChildItem -LiteralPath $parent -Force).Count -eq 0) {
        Remove-Item -LiteralPath $parent -Force
    }
}

$caught = $null
try {
    Assert-FixturePython
    $inputPackages = Assert-InputPackages
    $runRoot = Join-Path ([IO.Path]::GetTempPath()) "TTS More 中文"
    New-Item -ItemType Directory -Force -Path $runRoot | Out-Null
    $WorkRoot = Join-Path $runRoot $WorkIdentity
    New-Item -ItemType Directory -Path $WorkRoot | Out-Null
    $ownerMarker = [ordered]@{
        schema_version = 1
        run_id = $WorkIdentity
        owner_pid = $PID
        owner_started_at = $OwnerStartedAt
        root = [IO.Path]::GetFullPath($WorkRoot)
    }
    [IO.File]::WriteAllText((Join-Path $WorkRoot ".fixture-owner.json"), ($ownerMarker | ConvertTo-Json -Depth 4), $Utf8NoBom)
    $script:AssetsRoot = Join-Path $WorkRoot "assets"
    New-Item -ItemType Directory -Path $AssetsRoot | Out-Null
    $assetPayloads = @{}
    foreach ($component in $inputPackages.Keys) {
        $payload = [Text.Encoding]::UTF8.GetBytes(("portable-fixture-$component-") * 3)
        $assetPayloads[$component] = $payload
        [IO.File]::WriteAllBytes((Join-Path $AssetsRoot "$component.bin"), $payload)
    }
    $script:ServerRequestLog = Join-Path $WorkRoot "server-requests.jsonl"
    Start-FixtureAssetServer

    foreach ($component in @($ExpectedComponents | Where-Object { $inputPackages.ContainsKey($_) })) {
        $entry = $inputPackages[$component]
        $destination = Join-Path $WorkRoot ("x-" + $component.Substring(0, [Math]::Min(4, $component.Length)))
        Expand-Archive -LiteralPath $entry.Zip -DestinationPath $destination
        $roots = @(Get-ChildItem -LiteralPath $destination -Directory)
        if ($roots.Count -ne 1) { Throw-HarnessError "PACKAGE_LAYOUT_INVALID" "expanded ZIP does not have one package root" }
        $root = Join-Path $destination "p"
        Move-Item -LiteralPath $roots[0].FullName -Destination $root
        & $FixturePython $PortablePackagesScript verify-sha256 --package-root $root *> $null
        if ($LASTEXITCODE -ne 0) { Throw-HarnessError "PACKAGE_HASH_INVALID" "expanded Bootstrap SHA-256 audit failed" }
        Add-Evidence -Component $component -Scenario "package_audit" -Result pass -Duration 0
        $package = Install-FixtureProtocol -Root $root -Component $component -AssetUrl "$FixtureEndpoint/$component.bin" -AssetPayload $assetPayloads[$component]
        $ExpandedPackages.Add($package)
    }
    Assert-RestrictedChildPath
    foreach ($package in $ExpandedPackages) { Invoke-PackageAcceptance -Package $package }
    Assert-FixtureNetworkEvidence
    Finalize-WorkerInitializationEvidence
}
catch {
    $caught = $_
}
finally {
    $cleanupComplete = $true
    $cleanupError = $null
    foreach ($package in $ExpandedPackages) {
        try { Register-PackageOwnedProcess -Package $package } catch { $cleanupComplete = $false; if ($null -eq $cleanupError) { $cleanupError = $_ } }
        try {
            $stopResult = Invoke-RootCommand -Root $package.Root -Name "Stop.cmd"
            if ($stopResult.ExitCode -ne 0) { Throw-HarnessError "CLEANUP_STOP_FAILED" "fixture package Stop.cmd failed during cleanup" }
        } catch { if ($null -eq $cleanupError) { $cleanupError = $_ } }
    }
    $cleanupProcesses = @($OwnedProcesses)
    [array]::Reverse($cleanupProcesses)
    foreach ($owned in $cleanupProcesses) {
        try { Stop-OwnedFixtureProcess -Owned $owned } catch { $cleanupComplete = $false; if ($null -eq $cleanupError) { $cleanupError = $_ } }
    }
    try { Assert-OwnedFixtureProcessesStopped } catch { $cleanupComplete = $false; if ($null -eq $cleanupError) { $cleanupError = $_ } }
    $env:PATH = $OriginalPath
    try { Write-AcceptanceEvidence -Directory ([IO.Path]::GetFullPath($Output)) } catch { if ($null -eq $caught) { $caught = $_ } }
    try {
        if ($WorkRoot -and [string]$env:TTS_MORE_FIRST_RUN_KEEP -ne "1") { Remove-OwnedFixtureRoot -Root $WorkRoot -Identity $WorkIdentity }
    } catch { if ($null -eq $caught) { $caught = $_ } }
    if ($null -eq $caught -and $null -ne $cleanupError) { $caught = $cleanupError }
}

if ($caught) {
    $code = Get-ErrorCode -ErrorRecord $caught
    if ([string]$env:TTS_MORE_FIRST_RUN_DEBUG -eq "1") {
        [Console]::Error.WriteLine([string]$caught.Exception.Message)
        [Console]::Error.WriteLine([string]$caught.ScriptStackTrace)
    }
    $safeMessage = if ($code -eq "PACKAGE_SET_INVALID") { "clean Windows acceptance requires exactly four Bootstrap ZIPs" } else { "portable first-run harness failed" }
    [Console]::Error.WriteLine("$safeMessage`: $code")
    exit 1
}
if (@($Evidence | Where-Object { $_.result -ne "pass" }).Count -gt 0) { exit 1 }
Write-Host "portable first-run fixture acceptance passed"
exit 0
