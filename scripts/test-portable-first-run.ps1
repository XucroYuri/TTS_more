[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string[]]$Packages,
    [Parameter(Mandatory = $true)][string]$Output
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ExpectedComponents = @("tts-more", "gpt-sovits", "indextts", "cosyvoice")
$AllowedEvidenceFields = @("component", "scenario", "result", "duration", "error_code")
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
$ExpandedPackages = [Collections.Generic.List[object]]::new()
$OwnedProcesses = [Collections.Generic.List[object]]::new()
$OwnedProcessKeys = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$ServerProcess = $null
$WorkRoot = ""
$WorkIdentity = [guid]::NewGuid().ToString("N")
$Utf8NoBom = New-Object Text.UTF8Encoding($false)

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
    }
    $names = @($record.Keys | Sort-Object)
    $expected = @($AllowedEvidenceFields | Sort-Object)
    if (($names -join "`n") -ne ($expected -join "`n")) { Throw-HarnessError "EVIDENCE_SCHEMA" "acceptance record is not allowlisted" }
    foreach ($name in $ForbiddenEvidenceFields) {
        if ($record.Contains($name)) { Throw-HarnessError "EVIDENCE_SCHEMA" "acceptance record contains a forbidden field" }
    }
    $Evidence.Add([pscustomobject]$record)
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
    $script:FixtureBasePython = (@(& $FixturePython -c "import pathlib,sys;print(pathlib.Path(sys.base_prefix)/'python.exe')" 2>&1) -join "").Trim()
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
    $actual = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash
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
        if ($manifest.schema_version -isnot [int] -or [int]$manifest.schema_version -ne 2 -or [string]$manifest.package_profile -ne "bootstrap") {
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
    # fixture-only copy mutation: the delivered ZIP remains immutable and was audited before extraction.
    $sum = Join-Path $Root "SHA256SUMS.txt"
    $lines = @(Get-ChildItem -LiteralPath $Root -File -Recurse -Force | Where-Object { $_.FullName -ne $sum } | Sort-Object FullName | ForEach-Object {
        $relative = $_.FullName.Substring($Root.Length).TrimStart('\', '/').Replace('\', '/')
        "$((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant())  $relative"
    })
    [IO.File]::WriteAllLines($sum, $lines, $Utf8NoBom)
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

function Write-FixtureInitialize {
    param([Parameter(Mandatory = $true)][string]$Path)
    $source = @'
[CmdletBinding()]
param([string]$Device="Auto", [switch]$Repair, [string]$PackageRoot="", [string]$OperationRoot="", [string]$CancelFile="")
$ErrorActionPreference="Stop"
function Find-Root([string]$start) { $p=[IO.Path]::GetFullPath($start); while($true){if(Test-Path -LiteralPath (Join-Path $p "package\tts-more-package.json") -PathType Leaf){return $p}; $parent=Split-Path -Parent $p; if(!$parent -or $parent -eq $p){throw "fixture-only package root missing"}; $p=$parent} }
$Root=if([string]::IsNullOrWhiteSpace($PackageRoot)){Find-Root $PSScriptRoot}else{[IO.Path]::GetFullPath($PackageRoot)}
$ManifestPath=Join-Path $Root "package\tts-more-package.json"; $Manifest=Get-Content -LiteralPath $ManifestPath -Raw|ConvertFrom-Json
$Bundle=if([string]$Manifest.component -eq "tts-more"){Join-Path $Root "scripts"}else{Join-Path $Root "app\tts_more"}
$Python=Join-Path $Root "runtime\live\python.exe"; $Installer=Join-Path $Bundle "portable_install.py"
$AssetLock=Join-Path $Root "data\local\fixture\asset.lock.json"; $Asset=Get-Content -LiteralPath $AssetLock -Raw|ConvertFrom-Json
$Destination=Join-Path $Root ([string]$Asset.target); $arguments=@("ensure-asset","--asset",$AssetLock,"--path",$Destination,"--package-root",$Root)
if($OperationRoot){$arguments += @("--operation-root",$OperationRoot,"--cancel-file",$CancelFile)}
$previousPreference=$ErrorActionPreference; try{$ErrorActionPreference="Continue";$downloadOutput=@(& $Python $Installer @arguments 2>&1);$downloadExit=$LASTEXITCODE}finally{$ErrorActionPreference=$previousPreference}
if($downloadExit -eq 20){exit 20}; if($downloadExit -ne 0){if([string]$env:TTS_MORE_FIRST_RUN_DEBUG -eq "1"){[IO.File]::WriteAllLines((Join-Path (Split-Path -Parent $AssetLock) "download-error.txt"),@($downloadOutput|ForEach-Object{[string]$_}))};throw "fixture-only asset download failed"}
$RuntimeLock=Join-Path $Root ([string]$Manifest.runtime.lock); $ModelLock=Join-Path $Root ([string]$Manifest.models.lock)
$RuntimeSha=(Get-FileHash -LiteralPath $RuntimeLock -Algorithm SHA256).Hash.ToLowerInvariant(); $ModelSha=(Get-FileHash -LiteralPath $ModelLock -Algorithm SHA256).Hash.ToLowerInvariant()
& $Python $Installer write-state --path (Join-Path $Root ([string]$Manifest.runtime.state_path)) --component ([string]$Manifest.component) --build-id ([string]$Manifest.build_id) --profile cpu --runtime-lock-sha256 $RuntimeSha --model-lock-sha256 $ModelSha
if($LASTEXITCODE -ne 0){throw "fixture-only state write failed"}
'@
    [IO.File]::WriteAllText($Path, $source, $Utf8NoBom)
}

function Write-FixtureStart {
    param([Parameter(Mandatory = $true)][string]$Path)
    $source = @'
[CmdletBinding()]
param([string]$PackageRoot="", [string]$OperationRoot="", [Nullable[int]]$PortOverride=$null)
$ErrorActionPreference="Stop"; $Root=[IO.Path]::GetFullPath($PackageRoot); $Manifest=Get-Content -LiteralPath (Join-Path $Root "package\tts-more-package.json") -Raw|ConvertFrom-Json
$Bundle=if([string]$Manifest.component -eq "tts-more"){Join-Path $Root "scripts"}else{Join-Path $Root "app\tts_more"}
$Python=Join-Path $Root "runtime\live\python.exe"; $Launcher=Join-Path $Bundle "portable_launcher.py"; $Fixture=Join-Path $Root "data\local\fixture"
$Port=if($null -ne $PortOverride){[int]$PortOverride}else{[int]$Manifest.endpoint.port}; $Health=[string]$Manifest.endpoint.health_path
$Record=Join-Path $Root "data\local\run\worker.pid.json"; $arguments=@("fixture-service.py","--port",[string]$Port)
$listeners=@(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
if($listeners.Count -gt 0){$verify=@($Launcher,"verify-owned-listener","--package-root",$Root,"--record-path",$Record,"--port",[string]$Port,"--build-id",[string]$Manifest.build_id,"--executable",$Python); foreach($owner in @($listeners|Select-Object -ExpandProperty OwningProcess -Unique)){$verify += @("--listener-pid",[string]$owner)}; $verify += "--"; $verify += $arguments; & $Python @verify *> $null; if($LASTEXITCODE -eq 0){try{$health=Invoke-RestMethod -Uri "http://127.0.0.1:$Port$Health" -TimeoutSec 2; if($health){exit 0}}catch{}}; throw "PORT_IN_USE: fixture-only listener is not owned; no process was terminated"}
$process=Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $Fixture -WindowStyle Hidden -PassThru; $created=$process.StartTime.ToUniversalTime().ToString("o")
& $Python $Launcher write-process-record --package-root $Root --record-path $Record --pid $process.Id --parent-pid $PID --process-created-at $created --executable $Python --port $Port --build-id ([string]$Manifest.build_id) -- @arguments
if($LASTEXITCODE -ne 0){if(!$process.HasExited){$process.Kill();$process.WaitForExit()};throw "fixture-only ownership record failed"}
$deadline=[DateTime]::UtcNow.AddSeconds(15); do{if($process.HasExited){throw "fixture-only service exited"};try{$health=Invoke-RestMethod -Uri "http://127.0.0.1:$Port$Health" -TimeoutSec 2;if($health){exit 0}}catch{Start-Sleep -Milliseconds 100}}while([DateTime]::UtcNow -lt $deadline)
& $Python $Launcher stop-worker --package-root $Root *> $null; throw "fixture-only service readiness failed"
'@
    [IO.File]::WriteAllText($Path, $source, $Utf8NoBom)
}

function Write-FixtureStop {
    param([Parameter(Mandatory = $true)][string]$Path)
    $source = @'
[CmdletBinding()]
param([string]$PackageRoot="")
$ErrorActionPreference="Stop"
function Find-Root([string]$start) { $p=[IO.Path]::GetFullPath($start); while($true){if(Test-Path -LiteralPath (Join-Path $p "package\tts-more-package.json") -PathType Leaf){return $p}; $parent=Split-Path -Parent $p; if(!$parent -or $parent -eq $p){throw "fixture-only package root missing"}; $p=$parent} }
$Root=if([string]::IsNullOrWhiteSpace($PackageRoot)){Find-Root $PSScriptRoot}else{[IO.Path]::GetFullPath($PackageRoot)}; $Manifest=Get-Content -LiteralPath (Join-Path $Root "package\tts-more-package.json") -Raw|ConvertFrom-Json
$Bundle=if([string]$Manifest.component -eq "tts-more"){Join-Path $Root "scripts"}else{Join-Path $Root "app\tts_more"}; $Python=Join-Path $Root "runtime\live\python.exe"; $Launcher=Join-Path $Bundle "portable_launcher.py"
& $Python $Launcher stop-worker --package-root $Root; if($LASTEXITCODE -ne 0){throw "fixture-only safe stop failed"}
'@
    [IO.File]::WriteAllText($Path, $source, $Utf8NoBom)
}

function Write-FixtureRepair {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Initialize)
    $relative = Split-Path -Leaf $Initialize
    $source = "[CmdletBinding()]`nparam([string]`$PackageRoot=`"`", [string]`$OperationRoot=`"`", [string]`$CancelFile=`"`")`n& (Join-Path `$PSScriptRoot `"$relative`") -PackageRoot `$PackageRoot -OperationRoot `$OperationRoot -CancelFile `$CancelFile -Repair`nexit `$LASTEXITCODE`n"
    [IO.File]::WriteAllText($Path, $source, $Utf8NoBom)
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
    $port = Get-RandomLoopbackPort
    $manifest.endpoint.port = $port
    $manifest.endpoint.default_url = "http://127.0.0.1:$port"
    $manifest.runtime.python_version = "3.11"
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12), $Utf8NoBom)

    $bundle = if ($Component -eq "tts-more") { Join-Path $Root "scripts" } else { Join-Path $Root "app\tts_more" }
    $runtimeLock = Join-Path $Root ([string]$manifest.runtime.lock)
    $modelLock = Join-Path $Root ([string]$manifest.models.lock)
    $runtimePayload = [ordered]@{ schema_version=1; component=$Component; python_version="3.11"; import_probe="import sys"; auto_order=@("cpu"); profiles=[ordered]@{cpu=[ordered]@{dependency_lock="fixture-only"}} }
    $modelPayload = [ordered]@{ schema_version=1; component=$Component; complete=$true; required_paths=@(); assets=@(); required_free_bytes=0 }
    [IO.File]::WriteAllText($runtimeLock, ($runtimePayload | ConvertTo-Json -Depth 8), $Utf8NoBom)
    [IO.File]::WriteAllText($modelLock, ($modelPayload | ConvertTo-Json -Depth 8), $Utf8NoBom)

    $fixture = Join-Path $Root "data\local\fixture"
    New-Item -ItemType Directory -Force -Path $fixture, (Join-Path $Root "data\cache\fixture"), (Join-Path $Root "runtime\live") | Out-Null
    $assetLock = Join-Path $fixture "asset.lock.json"
    $asset = [ordered]@{ id="$Component-fixture"; target="data/cache/fixture/payload.dat"; size_bytes=$AssetPayload.Length; sha256=([BitConverter]::ToString(([Security.Cryptography.SHA256]::Create()).ComputeHash($AssetPayload))).Replace("-", "").ToLowerInvariant(); urls=@($AssetUrl) }
    [IO.File]::WriteAllText($assetLock, ($asset | ConvertTo-Json -Depth 6), $Utf8NoBom)
    Write-FixtureService -Path (Join-Path $fixture "fixture-service.py")

    $runtimePython = Join-Path $Root "runtime\live\python.exe"
    $basePrefix = $FixtureBasePrefix
    $baseExecutable = $FixtureBasePython
    if (!(Test-Path -LiteralPath $baseExecutable -PathType Leaf)) { Throw-HarnessError "FIXTURE_RUNTIME_INVALID" "fixture base Python executable is missing" }
    Copy-Item -LiteralPath $baseExecutable -Destination $runtimePython -Force
    $baseDll = Join-Path $basePrefix "python311.dll"
    if (!(Test-Path -LiteralPath $baseDll -PathType Leaf)) { Throw-HarnessError "FIXTURE_RUNTIME_INVALID" "fixture base Python DLL is missing" }
    Copy-Item -LiteralPath $baseDll -Destination (Join-Path $Root "runtime\live\python311.dll") -Force
    [IO.File]::WriteAllText((Join-Path $Root "runtime\live\pyvenv.cfg"), "home = $basePrefix`ninclude-system-site-packages = true`nversion = 3.11.0`n", $Utf8NoBom)

    $initialize = if ($Component -eq "tts-more") { Join-Path $bundle "initialize-portable.ps1" } else { Join-Path $bundle "Initialize.ps1" }
    $start = if ($Component -eq "tts-more") { Join-Path $bundle "start-production.ps1" } else { Join-Path $bundle "Start-Worker.ps1" }
    $stop = if ($Component -eq "tts-more") { Join-Path $bundle "stop-production.ps1" } else { Join-Path $bundle "Stop-Worker.ps1" }
    $repair = if ($Component -eq "tts-more") { Join-Path $bundle "repair-portable.ps1" } else { Join-Path $bundle "Repair.ps1" }
    Write-FixtureInitialize -Path $initialize
    Write-FixtureStart -Path $start
    Write-FixtureStop -Path $stop
    Write-FixtureRepair -Path $repair -Initialize $initialize
    Write-FixtureSha256Manifest -Root $Root
    & $FixturePython $PortablePackagesScript verify-sha256 --package-root $Root *> $null
    if ($LASTEXITCODE -ne 0) { Throw-HarnessError "FIXTURE_COPY_HASH_INVALID" "fixture-only copy SHA-256 audit failed" }
    return [pscustomobject]@{ Root=$Root; Component=$Component; Port=$port; AssetLock=$assetLock; AssetPath=(Join-Path $Root "data\cache\fixture\payload.dat"); AssetSha=[string]$asset.sha256; AssetUrl=$AssetUrl; Bundle=$bundle; Service=(Join-Path $fixture "fixture-service.py") }
}

function Invoke-RootCommand {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$Name, [string[]]$Arguments=@())
    $launcher = Join-Path $Root $Name
    if (!(Test-Path -LiteralPath $launcher -PathType Leaf)) { Throw-HarnessError "PACKAGE_CORRUPT" "root launcher is missing" }
    $cmd = Join-Path $env:SystemRoot "System32\cmd.exe"
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& $cmd /d /c $launcher @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousPreference }
    return [pscustomobject]@{ ExitCode=$exitCode; Output=($output -join "`n") }
}

function Set-FixtureAssetUrl {
    param([Parameter(Mandatory = $true)][object]$Package, [Parameter(Mandatory = $true)][string]$Url)
    $payload = Get-Content -LiteralPath $Package.AssetLock -Raw | ConvertFrom-Json
    $payload.urls = @($Url)
    [IO.File]::WriteAllText($Package.AssetLock, ($payload | ConvertTo-Json -Depth 6), $Utf8NoBom)
    Write-FixtureSha256Manifest -Root $Package.Root
}

function Assert-AssetHash {
    param([Parameter(Mandatory = $true)][object]$Package)
    if (!(Test-Path -LiteralPath $Package.AssetPath -PathType Leaf)) { Throw-HarnessError "ASSET_MISSING" "fixture asset is missing" }
    $actual = (Get-FileHash -LiteralPath $Package.AssetPath -Algorithm SHA256).Hash
    if (![string]::Equals($actual, $Package.AssetSha, [StringComparison]::OrdinalIgnoreCase)) { Throw-HarnessError "ASSET_HASH_INVALID" "fixture asset hash is invalid" }
}

function Stop-OwnedFixtureProcess {
    param([Parameter(Mandatory = $true)][object]$Owned)
    $process = $Owned.Process
    try { $process.Refresh() } catch { return }
    if ($process.HasExited) { return }
    $actual = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
    if (!$actual -or ![string]::Equals([IO.Path]::GetFullPath($actual.Path), [IO.Path]::GetFullPath($Owned.Executable), [StringComparison]::OrdinalIgnoreCase) -or $actual.StartTime.ToUniversalTime() -ne $Owned.StartedAt) {
        Throw-HarnessError "UNKNOWN_PROCESS_REFUSED" "fixture cleanup refused an unknown process"
    }
    if ($Owned.PSObject.Properties.Name -contains "Port" -and [int]$Owned.Port -gt 0) {
        $owners = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$Owned.Port) -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
        if ($owners.Count -gt 0 -and ($owners.Count -ne 1 -or [int]$owners[0] -ne [int]$process.Id)) {
            Throw-HarnessError "UNKNOWN_PROCESS_REFUSED" "fixture cleanup refused a mismatched listener owner"
        }
    }
    $process.Kill()
    if (!$process.WaitForExit(10000)) { Throw-HarnessError "OWNED_PROCESS_STUCK" "owned fixture process did not stop" }
}

function Register-PackageOwnedProcess {
    param([Parameter(Mandatory = $true)][object]$Package)
    $recordPath = Join-Path $Package.Root "data\local\run\worker.pid.json"
    if (!(Test-Path -LiteralPath $recordPath -PathType Leaf)) { return }
    $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
    $processId = [int]$record.pid
    $createdAt = [DateTime]::Parse([string]$record.process_created_at, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
    $expectedExecutable = [IO.Path]::GetFullPath((Join-Path $Package.Root "runtime\live\python.exe"))
    $recordedExecutable = [IO.Path]::GetFullPath([string]$record.executable_path)
    if ($processId -le 0 -or ![string]::Equals($recordedExecutable, $expectedExecutable, [StringComparison]::OrdinalIgnoreCase)) {
        Throw-HarnessError "OWNED_PROCESS_INVALID" "fixture package PID record has an invalid executable identity"
    }
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if (!$process) { return }
    if (![string]::Equals([IO.Path]::GetFullPath($process.Path), $expectedExecutable, [StringComparison]::OrdinalIgnoreCase) -or $process.StartTime.ToUniversalTime() -ne $createdAt) {
        Throw-HarnessError "UNKNOWN_PROCESS_REFUSED" "fixture package PID record resolves to another process"
    }
    $key = "$processId|$($createdAt.ToString('o'))"
    if ($OwnedProcessKeys.Add($key)) {
        $OwnedProcesses.Add([pscustomobject]@{ Process=$process; Executable=$expectedExecutable; StartedAt=$createdAt; Port=[int]$record.port })
    }
}

function Assert-OwnedFixtureProcessesStopped {
    foreach ($owned in $OwnedProcesses) {
        $owned.Process.Refresh()
        if (!$owned.Process.HasExited) { Throw-HarnessError "OWNED_PROCESS_REMAINED" "an owned fixture process survived cleanup" }
    }
}

function Invoke-PackageAcceptance {
    param([Parameter(Mandatory = $true)][object]$Package)
    $component = [string]$Package.Component
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

    $partial = "$($Package.AssetPath).partial"
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $first = Invoke-RootCommand -Root $Package.Root -Name "Start.cmd" -Arguments @("-ManagedBy", "portable-first-run", "-NoUi")
    $clock.Stop()
    if ($first.ExitCode -ne 26 -or !(Test-Path -LiteralPath $partial -PathType Leaf) -or (Get-Item -LiteralPath $partial).Length -ne 8) {
        if ([string]$env:TTS_MORE_FIRST_RUN_DEBUG -eq "1") { $ServerProcess.Refresh(); [Console]::Error.WriteLine("FIRST_EXIT=$($first.ExitCode) SERVER_EXITED=$($ServerProcess.HasExited)`n$($first.Output)") }
        Add-Evidence -Component $component -Scenario "interruption" -Result fail -Duration $clock.Elapsed.TotalSeconds -ErrorCode "INTERRUPTION_NOT_PRESERVED"
        Throw-HarnessError "INTERRUPTION_NOT_PRESERVED" "first start did not retain an eight-byte partial"
    }
    Add-Evidence -Component $component -Scenario "interruption" -Result pass -Duration $clock.Elapsed.TotalSeconds

    Invoke-Scenario -Component $component -Scenario "resume" -Action {
        $resumed = Invoke-RootCommand -Root $Package.Root -Name "Start.cmd" -Arguments @("-ManagedBy", "portable-first-run", "-NoUi")
        if ($resumed.ExitCode -ne 0) { Throw-HarnessError "RESUME_FAILED" "resumed Start.cmd failed" }
        Assert-AssetHash -Package $Package
        Wait-LoopbackPort -Port $Package.Port -Listening $true
    }
    $recordPath = Join-Path $Package.Root "data\local\run\worker.pid.json"
    $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
    $initialPid = [int]$record.pid
    Register-PackageOwnedProcess -Package $Package

    Invoke-Scenario -Component $component -Scenario "duplicate_start" -Action {
        $duplicate = Invoke-RootCommand -Root $Package.Root -Name "Start.cmd" -Arguments @("-ManagedBy", "portable-first-run", "-NoUi")
        if ($duplicate.ExitCode -ne 0) {
            if ([string]$env:TTS_MORE_FIRST_RUN_DEBUG -eq "1") { [Console]::Error.WriteLine("DUPLICATE_EXIT=$($duplicate.ExitCode)`n$($duplicate.Output)") }
            Throw-HarnessError "DUPLICATE_START_FAILED" "duplicate Start.cmd failed"
        }
        $unchanged = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
        if ([int]$unchanged.pid -ne $initialPid) { Throw-HarnessError "DUPLICATE_PROCESS" "duplicate Start.cmd created another listener" }
    }

    Invoke-Scenario -Component $component -Scenario "proxy_failure" -Action {
        $proxyUrl = "$($Package.AssetUrl)?mode=proxy-failure"
        Set-FixtureAssetUrl -Package $Package -Url $proxyUrl
        Remove-Item -LiteralPath $Package.AssetPath -Force
        Remove-Item -LiteralPath "$($Package.AssetPath).partial" -Force -ErrorAction SilentlyContinue
        $failed = Invoke-RootCommand -Root $Package.Root -Name "Repair.cmd"
        if ($failed.ExitCode -eq 0 -or (Test-Path -LiteralPath $Package.AssetPath -PathType Leaf)) { Throw-HarnessError "PROXY_FAILURE_NOT_INJECTED" "503 proxy failure did not fail closed" }
        Set-FixtureAssetUrl -Package $Package -Url $Package.AssetUrl
    }

    Invoke-Scenario -Component $component -Scenario "corruption_repair" -Action {
        $repair = Invoke-RootCommand -Root $Package.Root -Name "Repair.cmd"
        if ($repair.ExitCode -ne 0) { Throw-HarnessError "REPAIR_FAILED" "Repair.cmd could not restore the asset after proxy failure" }
        Assert-AssetHash -Package $Package
        $bytes = [IO.File]::ReadAllBytes($Package.AssetPath)
        $originalLength = $bytes.Length
        $bytes[0] = $bytes[0] -bxor 0xff
        [IO.File]::WriteAllBytes($Package.AssetPath, $bytes)
        if ((Get-Item -LiteralPath $Package.AssetPath).Length -ne $originalLength) { Throw-HarnessError "CORRUPTION_INVALID" "corruption injection changed asset length" }
        $repaired = Invoke-RootCommand -Root $Package.Root -Name "Repair.cmd"
        if ($repaired.ExitCode -ne 0) { Throw-HarnessError "REPAIR_FAILED" "Repair.cmd failed for same-length corruption" }
        Assert-AssetHash -Package $Package
    }

    Invoke-Scenario -Component $component -Scenario "stale_pid" -Action {
        try { Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$($Package.Port)/crash" -TimeoutSec 3 | Out-Null } catch { }
        Wait-LoopbackPort -Port $Package.Port -Listening $false
        if (!(Test-Path -LiteralPath $recordPath -PathType Leaf)) { Throw-HarnessError "STALE_PID_MISSING" "crash did not leave stale ownership evidence" }
        $restarted = Invoke-RootCommand -Root $Package.Root -Name "Start.cmd" -Arguments @("-ManagedBy", "portable-first-run", "-NoUi")
        if ($restarted.ExitCode -ne 0) { Throw-HarnessError "STALE_PID_RECOVERY_FAILED" "Start.cmd did not recover stale PID evidence" }
        $replacement = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
        if ([int]$replacement.pid -eq $initialPid) { Throw-HarnessError "STALE_PID_RECOVERY_FAILED" "recovery did not create a fresh process identity" }
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
        $descendants = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { [int]$_.ParentProcessId -in $knownPids })
        if ($descendants.Count -gt 0) { Throw-HarnessError "CHILD_PROCESS_REMAINED" "an owned child process survived Stop.cmd" }
    }

    Invoke-Scenario -Component $component -Scenario "unknown_port" -Action {
        $foreign = Start-Process -FilePath $FixtureBasePython -ArgumentList @("fixture-service.py", "--port", [string]$Package.Port) -WorkingDirectory (Split-Path -Parent $Package.Service) -WindowStyle Hidden -PassThru
        $owned = [pscustomobject]@{ Process=$foreign; Executable=$FixtureBasePython; StartedAt=$foreign.StartTime.ToUniversalTime(); Port=[int]$Package.Port }
        $OwnedProcesses.Add($owned)
        try {
            Wait-LoopbackPort -Port $Package.Port -Listening $true
            $refused = Invoke-RootCommand -Root $Package.Root -Name "Start.cmd" -Arguments @("-ManagedBy", "portable-first-run", "-NoUi")
            if ($refused.ExitCode -ne 23) { Throw-HarnessError "UNKNOWN_PORT_NOT_REFUSED" "Start.cmd did not reject an unknown listener" }
            $safeStop = Invoke-RootCommand -Root $Package.Root -Name "Stop.cmd"
            if ($safeStop.ExitCode -ne 0 -or $foreign.HasExited -or !(Test-LoopbackPort -Port $Package.Port)) { Throw-HarnessError "UNKNOWN_PROCESS_TOUCHED" "Stop.cmd changed an unknown listener" }
        }
        finally {
            Stop-OwnedFixtureProcess -Owned $owned
            Wait-LoopbackPort -Port $Package.Port -Listening $false
        }
    }
}

function Remove-OwnedFixtureRoot {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$Identity)
    if ([string]::IsNullOrWhiteSpace($Root) -or !(Test-Path -LiteralPath $Root -PathType Container)) { return }
    $resolved = [IO.Path]::GetFullPath($Root)
    $temp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $marker = Join-Path $resolved ".fixture-owner"
    if (!$resolved.StartsWith($temp, [StringComparison]::OrdinalIgnoreCase) -or ((Get-Item -LiteralPath $resolved -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or !(Test-Path -LiteralPath $marker -PathType Leaf) -or (Get-Content -LiteralPath $marker -Raw).Trim() -ne $Identity) {
        Throw-HarnessError "CLEANUP_REFUSED" "fixture cleanup identity check failed closed"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

$caught = $null
try {
    Assert-FixturePython
    $inputPackages = Assert-InputPackages
    Assert-RestrictedChildPath
    $WorkRoot = Join-Path ([IO.Path]::GetTempPath()) ("tm验收-" + $WorkIdentity.Substring(0, 12))
    New-Item -ItemType Directory -Path $WorkRoot | Out-Null
    [IO.File]::WriteAllText((Join-Path $WorkRoot ".fixture-owner"), $WorkIdentity, $Utf8NoBom)
    $assetsRoot = Join-Path $WorkRoot "assets"
    New-Item -ItemType Directory -Path $assetsRoot | Out-Null
    $assetPayloads = @{}
    foreach ($component in $inputPackages.Keys) {
        $payload = [Text.Encoding]::UTF8.GetBytes(("portable-fixture-$component-") * 3)
        $assetPayloads[$component] = $payload
        [IO.File]::WriteAllBytes((Join-Path $assetsRoot "$component.bin"), $payload)
    }
    $readyFile = Join-Path $WorkRoot "server-ready.json"
    $serverRequestLog = Join-Path $WorkRoot "server-requests.jsonl"
    $serverArguments = @((Split-Path -Leaf $FixtureServerScript), "--root", $assetsRoot, "--ready-file", $readyFile, "--interrupt-after", "8", "--request-log", $serverRequestLog)
    $ServerProcess = Start-Process -FilePath $FixtureBasePython -ArgumentList $serverArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
    $OwnedProcesses.Add([pscustomobject]@{ Process=$ServerProcess; Executable=$FixtureBasePython; StartedAt=$ServerProcess.StartTime.ToUniversalTime(); Port=0 })
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    while (!(Test-Path -LiteralPath $readyFile -PathType Leaf) -and [DateTime]::UtcNow -lt $deadline) {
        if ($ServerProcess.HasExited) { Throw-HarnessError "FIXTURE_SERVER_FAILED" "fixture asset server exited before readiness" }
        Start-Sleep -Milliseconds 100
    }
    if (!(Test-Path -LiteralPath $readyFile -PathType Leaf)) { Throw-HarnessError "FIXTURE_SERVER_FAILED" "fixture asset server readiness timed out" }
    $endpoint = [string](Get-Content -LiteralPath $readyFile -Raw | ConvertFrom-Json).endpoint
    if ($endpoint -notmatch '^http://127\.0\.0\.1:[0-9]+$') { Throw-HarnessError "FIXTURE_SERVER_FAILED" "fixture server did not bind random loopback" }

    foreach ($component in @($inputPackages.Keys | Sort-Object)) {
        $entry = $inputPackages[$component]
        $destination = Join-Path $WorkRoot ("解压 测试-" + $component + "-" + [guid]::NewGuid().ToString("N").Substring(0, 6))
        Expand-Archive -LiteralPath $entry.Zip -DestinationPath $destination
        $roots = @(Get-ChildItem -LiteralPath $destination -Directory)
        if ($roots.Count -ne 1) { Throw-HarnessError "PACKAGE_LAYOUT_INVALID" "expanded ZIP does not have one package root" }
        $root = $roots[0].FullName
        & $FixturePython $PortablePackagesScript verify-sha256 --package-root $root *> $null
        if ($LASTEXITCODE -ne 0) { Throw-HarnessError "PACKAGE_HASH_INVALID" "expanded Bootstrap SHA-256 audit failed" }
        Add-Evidence -Component $component -Scenario "package_audit" -Result pass -Duration 0
        $package = Install-FixtureProtocol -Root $root -Component $component -AssetUrl "$endpoint/$component.bin" -AssetPayload $assetPayloads[$component]
        $ExpandedPackages.Add($package)
    }
    foreach ($package in $ExpandedPackages) { Invoke-PackageAcceptance -Package $package }
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
        if ($WorkRoot -and $cleanupComplete) { Remove-OwnedFixtureRoot -Root $WorkRoot -Identity $WorkIdentity }
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
