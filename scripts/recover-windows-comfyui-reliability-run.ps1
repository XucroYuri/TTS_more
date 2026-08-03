[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $OutputRoot,
    [Parameter(Mandatory = $true)] [ValidatePattern('^[0-9a-f]{64}$')] [string] $RunKey
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$MaximumObservationBytes = 4194304
$MaximumObservedProcesses = 4096

function Get-Sha256Hex {
    param([AllowEmptyString()] [string] $Value)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return -join @(
            $algorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value)) |
                ForEach-Object { $_.ToString('x2') }
        )
    } finally { $algorithm.Dispose() }
}

function Invoke-RecoveryBridge {
    param([string[]] $Arguments, [AllowNull()] [string] $InputJson)
    Push-Location -LiteralPath $script:BackendRoot
    try {
        if ($null -eq $InputJson) {
            $rendered = @(& $script:PythonPath @Arguments 2>$null)
        } else {
            $rendered = @($InputJson | & $script:PythonPath @Arguments 2>$null)
        }
        $exitCode = $LASTEXITCODE
    } finally { Pop-Location }
    if ($rendered.Count -ne 1) { throw 'Recovery helper returned invalid output' }
    try { $document = ([string] $rendered[0]) | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'Recovery helper returned invalid output' }
    return [pscustomobject]@{ exit_code = [int] $exitCode; document = $document }
}

try {
    $script:BackendRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\backend'))
    $script:PythonPath = Join-Path $script:BackendRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $script:PythonPath -PathType Leaf)) {
        throw 'Formal backend Python is unavailable'
    }
    $processRows = [System.Collections.Generic.List[object]]::new()
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    if ($processes.Count -gt $MaximumObservedProcesses) {
        throw 'Process observation exceeds its bound'
    }
    $byPid = @{}
    foreach ($process in $processes) { $byPid[[int] $process.ProcessId] = $process }
    foreach ($process in $processes) {
        if ([int] $process.ProcessId -le 0) { continue }
        $parent = $byPid[[int] $process.ParentProcessId]
        $complete = (
            $null -ne $process.CreationDate -and
            $null -ne $process.ExecutablePath -and
            $null -ne $process.CommandLine -and
            $null -ne $parent -and
            $null -ne $parent.CreationDate
        )
        if (-not $complete) {
            # Preserve the PID in a deliberately nonmatching sanitized row so
            # an owned-but-incompletely-observed process can never look absent.
            $processRows.Add([ordered]@{
                pid = [int] $process.ProcessId
                creation_time = '1970-01-01T00:00:00.0000000Z'
                executable_sha256 = '0' * 64
                command_line_sha256 = '0' * 64
                parent_pid = [Math]::Max(0, [int] $process.ParentProcessId)
                parent_creation_time = '1970-01-01T00:00:00.0000000Z'
            })
            continue
        }
        $processRows.Add([ordered]@{
            pid = [int] $process.ProcessId
            creation_time = $process.CreationDate.ToUniversalTime().ToString('o')
            executable_sha256 = Get-Sha256Hex -Value ([string] $process.ExecutablePath)
            command_line_sha256 = Get-Sha256Hex -Value ([string] $process.CommandLine)
            parent_pid = [int] $process.ParentProcessId
            parent_creation_time = $parent.CreationDate.ToUniversalTime().ToString('o')
        })
    }
    $ports = [ordered]@{}
    foreach ($port in @(8000, 8188)) {
        $owners = @(
            Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique
        )
        if ($owners.Count -gt 1) { throw 'Port observation is ambiguous' }
        $ports[[string] $port] = if ($owners.Count -eq 0) { $null } else { [int] $owners[0] }
    }
    $observation = [ordered]@{ processes = @($processRows); ports = $ports } |
        ConvertTo-Json -Depth 6 -Compress
    if ([Text.Encoding]::UTF8.GetByteCount($observation) -gt $MaximumObservationBytes) {
        throw 'Recovery observation exceeds its bound'
    }
    $plan = Invoke-RecoveryBridge -InputJson $observation -Arguments @(
        '-m', 'app.comfyui.reliability_recovery_cli', 'plan',
        '--output-root', ([IO.Path]::GetFullPath($OutputRoot)),
        '--run-key', $RunKey
    )
    if ($plan.exit_code -ne 0 -or $plan.document.ok -ne $true -or
        [string]::IsNullOrEmpty([string] $plan.document.plan_token)) {
        throw 'Recovery ownership proof was rejected'
    }
    $execute = Invoke-RecoveryBridge -InputJson $null -Arguments @(
        '-m', 'app.comfyui.reliability_recovery_cli', 'execute',
        '--output-root', ([IO.Path]::GetFullPath($OutputRoot)),
        '--run-key', $RunKey,
        '--plan-token', ([string] $plan.document.plan_token)
    )
    if ($execute.exit_code -ne 0 -or $execute.document.ok -ne $true -or
        $execute.document.result.status -cne 'removed') {
        throw 'Recovery deletion did not complete'
    }
    [Console]::Out.WriteLine('Recovery private namespace removed')
    exit 0
} catch {
    [Console]::Error.WriteLine('Recovery was rejected or only partially completed; inspect private evidence before retrying')
    exit 1
}
