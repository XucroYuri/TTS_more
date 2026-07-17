param(
    [Parameter(Mandatory = $true)][string]$EvidenceRoot
)

$ErrorActionPreference = "Stop"
$EvidencePath = [IO.Path]::GetFullPath($EvidenceRoot)
$IdentityPath = Join-Path $EvidencePath "nvidia-smi-process.json"
$CsvPath = Join-Path $EvidencePath "nvidia-smi.csv"
$ErrorPath = Join-Path $EvidencePath "nvidia-smi.stderr.log"
$process = $null

try {
    New-Item -ItemType Directory -Force -Path $EvidencePath | Out-Null
    if (Test-Path -LiteralPath $IdentityPath) {
        throw "GPU monitor startup blocked: an owned-process identity already exists"
    }
    $command = Get-Command nvidia-smi.exe -CommandType Application -ErrorAction Stop
    $executablePath = [IO.Path]::GetFullPath([string]$command.Source)
    if (-not [IO.Path]::GetFileName($executablePath).Equals("nvidia-smi.exe", [StringComparison]::OrdinalIgnoreCase)) {
        throw "GPU monitor startup blocked: NVIDIA SMI executable identity is invalid"
    }
    Remove-Item -LiteralPath $CsvPath, $ErrorPath -Force -ErrorAction SilentlyContinue
    $arguments = @(
        "--query-gpu=timestamp,index,uuid,memory.total,memory.free,memory.used,utilization.gpu",
        "--format=csv,noheader,nounits",
        "--loop-ms=2000"
    )
    $process = Start-Process `
        -FilePath $executablePath `
        -ArgumentList $arguments `
        -RedirectStandardOutput $CsvPath `
        -RedirectStandardError $ErrorPath `
        -WindowStyle Hidden `
        -PassThru
    $identity = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$process.Id)" -ErrorAction Stop
    if (
        $null -eq $identity -or
        [string]::IsNullOrWhiteSpace([string]$identity.CreationDate) -or
        [string]::IsNullOrWhiteSpace([string]$identity.ExecutablePath) -or
        -not ([IO.Path]::GetFullPath([string]$identity.ExecutablePath)).Equals($executablePath, [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "GPU monitor startup blocked: launched-process identity could not be verified"
    }
    $payload = [ordered]@{
        schema_version = 1
        process_id = [int]$process.Id
        creation_date = [string]$identity.CreationDate
        executable_path = $executablePath
    }
    $temporaryPath = $IdentityPath + ".tmp"
    $payload | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
    Move-Item -LiteralPath $temporaryPath -Destination $IdentityPath -Force
} catch {
    if ($null -ne $process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath ($IdentityPath + ".tmp") -Force -ErrorAction SilentlyContinue
    throw
}
