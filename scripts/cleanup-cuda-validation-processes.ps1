param(
    [Parameter(Mandatory = $true)][string]$Manifest,
    [switch]$Required,
    [string]$ServiceId = ""
)

$ErrorActionPreference = "Stop"
$Root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd([char[]]@('\', '/'))
$AllowedModules = @(
    "app.main:app",
    "app.workers.gpt_sovits_worker:app",
    "app.workers.indextts_worker:app",
    "app.workers.cosyvoice_worker:app",
    "frontend-vite"
)

function Test-PathInsideRoot {
    param([string]$Path, [string]$ProjectRoot)
    try {
        $resolvedRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd([char[]]@('\', '/'))
        $resolvedPath = [IO.Path]::GetFullPath($Path)
        $prefix = $resolvedRoot + [IO.Path]::DirectorySeparatorChar
        return $resolvedPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Test-ExactCommandToken {
    param([string]$CommandLine, [string]$Token)
    if ([string]::IsNullOrWhiteSpace($CommandLine) -or [string]::IsNullOrWhiteSpace($Token)) {
        return $false
    }
    $escaped = [regex]::Escape($Token)
    $pattern = '(?:^|\s)(?:"' + $escaped + '"|' + $escaped + ')(?=\s|$)'
    return [regex]::IsMatch(
        $CommandLine,
        $pattern,
        [Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
}

function Get-OwnedProcessSnapshot {
    param($Entry)
    $processId = 0
    if (-not [int]::TryParse([string]$Entry.pid, [ref]$processId) -or $processId -le 0) {
        throw "cleanup blocked: process ownership changed"
    }
    $entryRoot = ""
    try { $entryRoot = [IO.Path]::GetFullPath([string]$Entry.project_root).TrimEnd([char[]]@('\', '/')) } catch { }
    if (-not $entryRoot.Equals($Root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "cleanup blocked: process ownership changed"
    }
    if ([string]$Entry.worker_module -notin $AllowedModules) {
        throw "cleanup blocked: process ownership changed"
    }
    $isFrontend = [string]$Entry.worker_module -eq "frontend-vite"
    $commandToken = if ($isFrontend) { [string]$Entry.command_token } else { [string]$Entry.worker_module }
    if ($isFrontend) {
        $expectedVite = [IO.Path]::GetFullPath((Join-Path $Root "frontend\node_modules\vite\bin\vite.js"))
        $entryVite = ""
        try { $entryVite = [IO.Path]::GetFullPath($commandToken) } catch { }
        if (
            -not [IO.Path]::GetFileName([string]$Entry.executable_path).Equals("node.exe", [StringComparison]::OrdinalIgnoreCase) -or
            -not (Test-PathInsideRoot $entryVite $Root) -or
            -not $entryVite.Equals($expectedVite, [StringComparison]::OrdinalIgnoreCase)
        ) {
            throw "cleanup blocked: process ownership changed"
        }
    } elseif (-not (Test-PathInsideRoot ([string]$Entry.executable_path) $Root)) {
        throw "cleanup blocked: process ownership changed"
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    $sameCreation = ([string]$process.CreationDate).Equals(
        [string]$Entry.creation_date,
        [StringComparison]::Ordinal
    )
    $sameExecutable = $false
    try {
        $sameExecutable = ([IO.Path]::GetFullPath([string]$process.ExecutablePath)).Equals(
            [IO.Path]::GetFullPath([string]$Entry.executable_path),
            [StringComparison]::OrdinalIgnoreCase
        )
    } catch { }
    $sameModule = Test-ExactCommandToken ([string]$process.CommandLine) $commandToken
    if (-not ($sameCreation -and $sameExecutable -and $sameModule)) {
        throw "cleanup blocked: process ownership changed"
    }
    return [pscustomobject]@{
        ProcessId = $processId
        CreationDate = [string]$process.CreationDate
        ExecutablePath = [string]$process.ExecutablePath
        WorkerModule = [string]$Entry.worker_module
        CommandToken = $commandToken
    }
}

$ManifestPath = if ([IO.Path]::IsPathRooted($Manifest)) {
    [IO.Path]::GetFullPath($Manifest)
} else {
    [IO.Path]::GetFullPath((Join-Path $Root $Manifest))
}
if (-not (Test-PathInsideRoot $ManifestPath $Root)) {
    throw "cleanup blocked: manifest must stay inside the current checkout"
}
if (-not (Test-Path -LiteralPath $ManifestPath)) {
    if ($Required) { throw "cleanup blocked: required process manifest is missing" }
    return
}

try {
    $payload = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
} catch {
    throw "cleanup blocked: process manifest is invalid"
}
if ([int]$payload.schema_version -ne 1 -or $null -eq $payload.processes) {
    throw "cleanup blocked: process manifest is invalid"
}
$allEntries = @($payload.processes)
$selectedEntries = if ($ServiceId) {
    if ($ServiceId -notin @("local-gpt-sovits-main", "local-indextts", "local-cosyvoice")) {
        throw "cleanup blocked: process manifest is invalid"
    }
    @($allEntries | Where-Object { [string]$_.service_id -eq $ServiceId })
} else {
    $allEntries
}
if ($Required -and $selectedEntries.Count -eq 0) {
    throw "cleanup blocked: required process manifest is missing"
}

$validated = @()
foreach ($entry in $selectedEntries) {
    $snapshot = Get-OwnedProcessSnapshot $entry
    if ($null -ne $snapshot) { $validated += $snapshot }
}

$revalidated = @()
foreach ($entry in $validated) {
    $snapshot = Get-OwnedProcessSnapshot ([pscustomobject]@{
        pid = $entry.ProcessId
        creation_date = $entry.CreationDate
        executable_path = $entry.ExecutablePath
        project_root = $Root
        worker_module = $entry.WorkerModule
        command_token = $entry.CommandToken
    })
    if ($null -ne $snapshot) { $revalidated += $snapshot }
}

foreach ($entry in $revalidated) {
    try {
        Stop-Process -Id $entry.ProcessId -Force -ErrorAction Stop
    } catch {
        throw "cleanup blocked: unable to stop an owned validation process"
    }
}

$remaining = if ($ServiceId) {
    @($allEntries | Where-Object { [string]$_.service_id -ne $ServiceId })
} else {
    @()
}
if ($remaining.Count -eq 0) {
    Remove-Item -LiteralPath $ManifestPath -Force
} else {
    $updated = [ordered]@{ schema_version = 1; processes = $remaining }
    $temporary = Join-Path (Split-Path -Parent $ManifestPath) ("." + [IO.Path]::GetFileName($ManifestPath) + ".tmp")
    try {
        $updated | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $temporary -Encoding UTF8
        Move-Item -LiteralPath $temporary -Destination $ManifestPath -Force
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}
