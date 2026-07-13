param(
    [Parameter(Mandatory = $true)][string]$EvidenceRoot,
    [switch]$Required
)

$ErrorActionPreference = "Stop"
$EvidencePath = [IO.Path]::GetFullPath($EvidenceRoot)
$IdentityPath = Join-Path $EvidencePath "nvidia-smi-process.json"
$ExpectedCommandTokens = @(
    "--query-gpu=timestamp,index,uuid,memory.total,memory.free,memory.used,utilization.gpu",
    "--format=csv,noheader,nounits",
    "--loop-ms=2000"
)

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

function Test-GpuMonitorCommand {
    param([string]$CommandLine)
    foreach ($token in $ExpectedCommandTokens) {
        if (-not (Test-ExactCommandToken $CommandLine $token)) { return $false }
    }
    return $true
}

if (-not (Test-Path -LiteralPath $IdentityPath)) {
    if ($Required) { throw "GPU monitor cleanup blocked: owned-process identity is missing" }
    return
}

try {
    $identity = Get-Content -LiteralPath $IdentityPath -Raw | ConvertFrom-Json
    $processId = [int]$identity.process_id
    $creationDate = [string]$identity.creation_date
    $executablePath = [IO.Path]::GetFullPath([string]$identity.executable_path)
    $canonicalExecutable = [IO.Path]::GetFullPath(
        [string](Get-Command nvidia-smi.exe -CommandType Application -ErrorAction Stop).Source
    )
    if (
        [int]$identity.schema_version -ne 1 -or
        $processId -le 0 -or
        [string]::IsNullOrWhiteSpace($creationDate) -or
        -not [IO.Path]::GetFileName($executablePath).Equals("nvidia-smi.exe", [StringComparison]::OrdinalIgnoreCase) -or
        -not $executablePath.Equals($canonicalExecutable, [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "invalid identity"
    }

    $first = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if ($null -eq $first) {
        Remove-Item -LiteralPath $IdentityPath -Force
        return
    }
    $firstExecutable = [IO.Path]::GetFullPath([string]$first.ExecutablePath)
    if (
        -not ([string]$first.CreationDate).Equals($creationDate, [StringComparison]::Ordinal) -or
        -not $firstExecutable.Equals($executablePath, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-GpuMonitorCommand ([string]$first.CommandLine))
    ) {
        throw "identity mismatch"
    }

    $second = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if ($null -eq $second) { throw "identity changed" }
    $secondExecutable = [IO.Path]::GetFullPath([string]$second.ExecutablePath)
    if (
        -not ([string]$second.CreationDate).Equals($creationDate, [StringComparison]::Ordinal) -or
        -not $secondExecutable.Equals($executablePath, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-GpuMonitorCommand ([string]$second.CommandLine))
    ) {
        throw "identity changed"
    }
    Stop-Process -Id $processId -Force -ErrorAction Stop
    Remove-Item -LiteralPath $IdentityPath -Force
} catch {
    throw "GPU monitor cleanup blocked: owned-process identity could not be verified"
}
