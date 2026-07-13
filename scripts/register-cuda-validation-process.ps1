param(
    [Parameter(Mandatory = $true)][string]$Manifest,
    [Parameter(Mandatory = $true)][int]$ProcessId,
    [Parameter(Mandatory = $true)][string]$ExecutablePath,
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        "app.main:app",
        "app.workers.gpt_sovits_worker:app",
        "app.workers.indextts_worker:app",
        "app.workers.cosyvoice_worker:app",
        "frontend-vite"
    )]
    [string]$WorkerModule,
    [string]$CommandToken = ""
)

$ErrorActionPreference = "Stop"
$Root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd([char[]]@('\', '/'))

function Test-PathInsideRoot {
    param([string]$Path, [string]$ProjectRoot)
    try {
        $resolvedRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd([char[]]@('\', '/'))
        $resolvedPath = [IO.Path]::GetFullPath($Path)
        return $resolvedPath.StartsWith(
            $resolvedRoot + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase
        )
    } catch {
        return $false
    }
}

function Test-ExactCommandToken {
    param([string]$CommandLine, [string]$Token)
    if ([string]::IsNullOrWhiteSpace($CommandLine)) { return $false }
    $escaped = [regex]::Escape($Token)
    $pattern = '(?:^|\s)(?:"' + $escaped + '"|' + $escaped + ')(?=\s|$)'
    return [regex]::IsMatch(
        $CommandLine,
        $pattern,
        [Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
}

$ManifestPath = if ([IO.Path]::IsPathRooted($Manifest)) {
    [IO.Path]::GetFullPath($Manifest)
} else {
    [IO.Path]::GetFullPath((Join-Path $Root $Manifest))
}
$ExpectedExecutable = [IO.Path]::GetFullPath($ExecutablePath)
$isFrontend = $WorkerModule -eq "frontend-vite"
$ExpectedCommandToken = if ($isFrontend -and $CommandToken) {
    [IO.Path]::GetFullPath($CommandToken)
} else {
    $WorkerModule
}
if (-not (Test-PathInsideRoot $ManifestPath $Root)) {
    throw "validation process registration blocked"
}
if ($isFrontend) {
    $expectedVite = [IO.Path]::GetFullPath((Join-Path $Root "frontend\node_modules\vite\bin\vite.js"))
    if (
        -not [IO.Path]::GetFileName($ExpectedExecutable).Equals("node.exe", [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-PathInsideRoot $ExpectedCommandToken $Root) -or
        -not $ExpectedCommandToken.Equals($expectedVite, [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "validation process registration blocked"
    }
} elseif (-not (Test-PathInsideRoot $ExpectedExecutable $Root)) {
    throw "validation process registration blocked"
}

$process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
if ($null -eq $process) { throw "validation process registration blocked" }
$sameExecutable = $false
try {
    $sameExecutable = ([IO.Path]::GetFullPath([string]$process.ExecutablePath)).Equals(
        $ExpectedExecutable,
        [StringComparison]::OrdinalIgnoreCase
    )
} catch { }
if (-not $sameExecutable -or -not (Test-ExactCommandToken ([string]$process.CommandLine) $ExpectedCommandToken)) {
    throw "validation process registration blocked"
}

$payload = [ordered]@{ schema_version = 1; processes = @() }
if (Test-Path -LiteralPath $ManifestPath) {
    try {
        $existing = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    } catch {
        throw "validation process registration blocked"
    }
    if ([int]$existing.schema_version -ne 1 -or $null -eq $existing.processes) {
        throw "validation process registration blocked"
    }
    $payload.processes = @($existing.processes)
}

$creationDate = [string]$process.CreationDate
foreach ($entry in @($payload.processes)) {
    if ([int]$entry.pid -eq $ProcessId) {
        if (-not ([string]$entry.creation_date).Equals($creationDate, [StringComparison]::Ordinal)) {
            throw "validation process registration blocked"
        }
        return
    }
}

$payload.processes += [ordered]@{
    pid = $ProcessId
    creation_date = $creationDate
    executable_path = $ExpectedExecutable
    project_root = $Root
    worker_module = $WorkerModule
    command_token = $ExpectedCommandToken
    service_id = if ($isFrontend) { "application-frontend" } else { "application-control-plane" }
}

$parent = Split-Path -Parent $ManifestPath
New-Item -ItemType Directory -Force -Path $parent | Out-Null
$temporary = Join-Path $parent ("." + [IO.Path]::GetFileName($ManifestPath) + ".tmp")
try {
    $payload | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $ManifestPath -Force
} finally {
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
}
