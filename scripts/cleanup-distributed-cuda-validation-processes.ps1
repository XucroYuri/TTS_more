param(
    [Parameter(Mandatory = $true)][string]$Topology,
    [Parameter(Mandatory = $true)][string]$SshUser,
    [Parameter(Mandatory = $true)][string]$RemoteRoot,
    [switch]$Required
)

$ErrorActionPreference = "Stop"
$Root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd([char[]]@('\', '/'))

function Quote-PowerShellLiteral {
    param([string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

function Invoke-EncodedRemoteCleanup {
    param([string]$Target, [string]$Command)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command))
    $captured = & ssh -o BatchMode=yes $Target "powershell.exe" "-NoLogo" "-NoProfile" "-NonInteractive" "-EncodedCommand" $encoded 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "distributed cleanup blocked: remote owned-process cleanup failed"
    }
}

if ($SshUser -notmatch '^[A-Za-z0-9._-]+$') {
    throw "distributed cleanup blocked: SSH identity is invalid"
}
$TopologyPath = if ([IO.Path]::IsPathRooted($Topology)) {
    [IO.Path]::GetFullPath($Topology)
} else {
    [IO.Path]::GetFullPath((Join-Path $Root $Topology))
}
if (-not $TopologyPath.StartsWith($Root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "distributed cleanup blocked: topology must stay inside the current checkout"
}
try {
    $manifest = Get-Content -LiteralPath $TopologyPath -Raw | ConvertFrom-Json
} catch {
    throw "distributed cleanup blocked: topology is unavailable"
}
$workers = @($manifest.nodes.PSObject.Properties | Where-Object { $_.Value.role -eq "worker" })
if ($workers.Count -eq 0) { throw "distributed cleanup blocked: topology has no workers" }

$failed = $false
foreach ($worker in $workers) {
    $hostName = [string]$worker.Value.host
    if ($hostName -notmatch '^[A-Za-z0-9._:-]+$') {
        $failed = $true
        continue
    }
    $target = "$SshUser@$hostName"
    $cleanupScript = Join-Path $RemoteRoot "scripts\cleanup-cuda-validation-processes.ps1"
    $processManifest = Join-Path $RemoteRoot "data\.runtime\cuda-validation-processes.json"
    $processCommand = "& " + (Quote-PowerShellLiteral $cleanupScript) +
        " -Manifest " + (Quote-PowerShellLiteral $processManifest)
    if ($Required) { $processCommand += " -Required" }
    try {
        Invoke-EncodedRemoteCleanup $target $processCommand
    } catch {
        $failed = $true
    }
    $monitorCleanupScript = Join-Path $RemoteRoot "scripts\stop-cuda-gpu-monitor.ps1"
    $monitorEvidenceRoot = Join-Path $RemoteRoot "data\validation\cuda-controller"
    $monitorCommand = "& " + (Quote-PowerShellLiteral $monitorCleanupScript) +
        " -EvidenceRoot " + (Quote-PowerShellLiteral $monitorEvidenceRoot)
    try {
        Invoke-EncodedRemoteCleanup $target $monitorCommand
    } catch {
        $failed = $true
    }
}
if ($failed) { throw "distributed cleanup blocked: one or more remote cleanups failed" }
