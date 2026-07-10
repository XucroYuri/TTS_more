param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("single-clean", "single-release", "distributed")]
    [string]$Mode,
    [Parameter(Mandatory = $true)][string]$Fixture,
    [string]$Topology = "",
    [string]$Node = "",
    [string]$Services = "data\local\services.json",
    [string]$Output = "",
    [string]$SshUser = "",
    [string]$RemoteRoot = "",
    [string]$RepoPaths = "",
    [switch]$SkipDeploy,
    [switch]$SkipStart,
    [switch]$SkipFaultRecovery,
    [switch]$RequireBaseline
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (!(Test-Path -LiteralPath $Python)) { $Python = "python" }
if (-not $Output) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $Output = Join-Path $Root "data\validation\cuda-$Mode-$stamp"
} elseif (![IO.Path]::IsPathRooted($Output)) {
    $Output = Join-Path $Root $Output
}
$FixturePath = if ([IO.Path]::IsPathRooted($Fixture)) { $Fixture } else { Join-Path $Root $Fixture }
$ServicesPath = if ([IO.Path]::IsPathRooted($Services)) { $Services } else { Join-Path $Root $Services }
$TopologyPath = if ($Topology -and [IO.Path]::IsPathRooted($Topology)) { $Topology } elseif ($Topology) { Join-Path $Root $Topology } else { "" }
$RepoPathsPath = if ($RepoPaths -and [IO.Path]::IsPathRooted($RepoPaths)) {
    $RepoPaths
} elseif ($RepoPaths) {
    Join-Path $Root $RepoPaths
} else {
    ""
}
$ControllerCommit = (& git -C $Root rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $ControllerCommit -notmatch '^[0-9a-f]{40}$') { throw "Unable to resolve TTS More HEAD" }
$env:TTS_MORE_EXPECTED_APP_COMMIT = $ControllerCommit
Remove-Item Env:TTS_MORE_DISTRIBUTED_ORCHESTRATION_TOKEN -ErrorAction SilentlyContinue
$script:DistributedPreflightPath = ""
$script:DistributedDeploymentStarted = $false
$isDiagnostic = $SkipDeploy -or $SkipStart
$currentStage = "host-preflight"

New-Item -ItemType Directory -Force -Path $Output | Out-Null
$TranscriptPath = Join-Path $Output "controller.log"
Start-Transcript -Path $TranscriptPath -Force | Out-Null

function Assert-SafeSshToken {
    param([string]$Value, [string]$Label)
    if ($Value -notmatch '^[A-Za-z0-9._:-]+$') { throw "$Label contains unsupported characters" }
}

function Assert-DistributedHostIsolation {
    param($Manifest)
    $addressOwners = @{}
    foreach ($nodeProperty in @($Manifest.nodes.PSObject.Properties)) {
        $nodeName = [string]$nodeProperty.Name
        $hostName = [string]$nodeProperty.Value.host
        try {
            $addresses = @([System.Net.Dns]::GetHostAddresses($hostName))
        } catch {
            throw "Distributed topology node $nodeName host cannot be resolved"
        }
        $usable = @($addresses | Where-Object {
            -not [System.Net.IPAddress]::IsLoopback($_) -and
            -not $_.Equals([System.Net.IPAddress]::Any) -and
            -not $_.Equals([System.Net.IPAddress]::IPv6Any)
        })
        if ($usable.Count -eq 0) { throw "Distributed topology node $nodeName has no non-loopback address" }
        foreach ($address in $usable) {
            $addressKey = $address.ToString()
            if ($addressOwners.ContainsKey($addressKey)) {
                throw "Distributed topology nodes must resolve to distinct IP addresses"
            }
            $addressOwners[$addressKey] = $nodeName
        }
    }
}

function ConvertTo-EncodedPowerShell {
    param([string]$Command)
    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command))
}

function Quote-PowerShellLiteral {
    param([string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

function Invoke-LocalScript {
    param([string]$Path, [hashtable]$Parameters)
    Write-Host "[local] $Path" -ForegroundColor Cyan
    & $Path @Parameters
    if ($LASTEXITCODE -ne 0) { throw "Local command failed: $Path" }
}

function Invoke-RemotePowerShell {
    param([string]$Target, [string]$Command)
    if ($Target -notmatch '^[A-Za-z0-9._-]+@[A-Za-z0-9._:-]+$') { throw "SSH target contains unsupported characters" }
    $encoded = ConvertTo-EncodedPowerShell $Command
    Write-Host "[ssh] remote worker powershell.exe -EncodedCommand <redacted>" -ForegroundColor Cyan
    & ssh $Target "powershell.exe" "-NoLogo" "-NoProfile" "-NonInteractive" "-EncodedCommand" $encoded
    if ($LASTEXITCODE -ne 0) { throw "Remote PowerShell command failed" }
}

function Assert-DistributedMachineIsolation {
    param($Manifest, [array]$Workers)
    $controllerIdentity = [string](Get-ItemProperty -LiteralPath "HKLM:\SOFTWARE\Microsoft\Cryptography").MachineGuid
    if ([string]::IsNullOrWhiteSpace($controllerIdentity)) {
        throw "Application controller Windows machine identity is unavailable"
    }
    $identityOwners = @{}
    $identityOwners[$controllerIdentity] = [string]$Manifest.app_node
    foreach ($worker in $Workers) {
        $nodeName = [string]$worker.Name
        $hostName = [string]$worker.Value.host
        Assert-SafeSshToken $hostName "Topology host"
        $target = "$script:SshUser@$hostName"
        $identityOutput = @(Invoke-RemotePowerShell $target '(Get-ItemProperty -LiteralPath "HKLM:\SOFTWARE\Microsoft\Cryptography").MachineGuid')
        $machineIdentity = [string]($identityOutput | Select-Object -Last 1)
        $machineIdentity = $machineIdentity.Trim()
        if ([string]::IsNullOrWhiteSpace($machineIdentity)) {
            throw "Distributed worker Windows machine identity is unavailable"
        }
        if ($identityOwners.ContainsKey($machineIdentity)) {
            throw "Distributed topology nodes must have a distinct Windows machine identity"
        }
        $identityOwners[$machineIdentity] = $nodeName
    }
}

function New-DistributedOrchestrationPreflight {
    $token = [Guid]::NewGuid().ToString("N")
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $tokenBytes = [System.Text.Encoding]::UTF8.GetBytes($token)
        $tokenHash = [BitConverter]::ToString($sha256.ComputeHash($tokenBytes)).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha256.Dispose()
    }
    $topologyHash = (Get-FileHash -LiteralPath $TopologyPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $script:DistributedPreflightPath = Join-Path $Output "orchestration-preflight.json"
    [ordered]@{
        schema_version = 1
        mode = "distributed"
        topology_sha256 = $topologyHash
        controller_commit = $ControllerCommit
        token_sha256 = $tokenHash
        created_at = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $script:DistributedPreflightPath -Encoding UTF8
    $env:TTS_MORE_DISTRIBUTED_ORCHESTRATION_TOKEN = $token
}

function Start-RemoteGpuMonitor {
    param([string]$Target, [string]$RemoteEvidenceRoot)
    $monitorCommand = "New-Item -ItemType Directory -Force -Path " + (Quote-PowerShellLiteral $RemoteEvidenceRoot) +
        " | Out-Null; Remove-Item -Force -ErrorAction SilentlyContinue " +
        (Quote-PowerShellLiteral (Join-Path $RemoteEvidenceRoot "nvidia-smi.csv")) + "," +
        (Quote-PowerShellLiteral (Join-Path $RemoteEvidenceRoot "nvidia-smi.pid")) +
        "; `$args = @('--query-gpu=timestamp,index,uuid,memory.total,memory.free,memory.used,utilization.gpu','--format=csv,noheader,nounits','--loop-ms=2000'); " +
        "`$process = Start-Process -FilePath 'nvidia-smi.exe' -ArgumentList `$args -RedirectStandardOutput " +
        (Quote-PowerShellLiteral (Join-Path $RemoteEvidenceRoot "nvidia-smi.csv")) +
        " -RedirectStandardError " + (Quote-PowerShellLiteral (Join-Path $RemoteEvidenceRoot "nvidia-smi.stderr.log")) +
        " -WindowStyle Hidden -PassThru; Set-Content -LiteralPath " +
        (Quote-PowerShellLiteral (Join-Path $RemoteEvidenceRoot "nvidia-smi.pid")) + " -Value `$process.Id"
    Invoke-RemotePowerShell $Target $monitorCommand
}

function Copy-RemoteEvidenceFile {
    param([string]$Target, [string]$RemotePath, [string]$LocalPath)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LocalPath) | Out-Null
    $scpPath = $RemotePath.Replace('\', '/')
    Write-Host "[scp] collect remote evidence" -ForegroundColor Cyan
    & scp "${Target}:$scpPath" $LocalPath
    if ($LASTEXITCODE -ne 0) { throw "Failed to collect remote evidence from $Target" }
}

function Collect-DistributedEvidence {
    if (-not $script:DistributedWorkers) { return }
    $collected = @()
    foreach ($worker in $script:DistributedWorkers) {
        $nodeName = [string]$worker.Name
        $target = "$script:SshUser@$([string]$worker.Value.host)"
        $remoteEvidenceRoot = Join-Path $script:RemoteRoot "data\validation\cuda-controller"
        $stopMonitor = "`$pidPath = " + (Quote-PowerShellLiteral (Join-Path $remoteEvidenceRoot "nvidia-smi.pid")) +
            "; if (Test-Path -LiteralPath `$pidPath) { `$monitorPid = [int](Get-Content -LiteralPath `$pidPath -Raw); Stop-Process -Id `$monitorPid -Force -ErrorAction SilentlyContinue }"
        Invoke-RemotePowerShell $target $stopMonitor
        $nodeOutput = Join-Path $Output ("worker-logs\" + $nodeName)
        Copy-RemoteEvidenceFile $target (Join-Path $remoteEvidenceRoot "nvidia-smi.csv") (Join-Path $nodeOutput "nvidia-smi.csv")
        foreach ($serviceId in @($worker.Value.services)) {
            Copy-RemoteEvidenceFile $target (Join-Path $script:RemoteRoot ("data\.runtime\logs\" + $serviceId + ".log")) (Join-Path $nodeOutput ($serviceId + ".log"))
        }
        $collected += [ordered]@{ node = $nodeName; services = @($worker.Value.services); directory = ("worker-logs/" + $nodeName) }
    }
    $collected | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $Output "distributed-evidence.json") -Encoding UTF8
}

function Wait-ServiceReady {
    param([string]$Path, [int]$TimeoutSeconds = 600)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $servicesPayload = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $required = @("local-gpt-sovits-main", "local-indextts", "local-cosyvoice")
    while ((Get-Date) -lt $deadline) {
        $pending = @()
        foreach ($serviceId in $required) {
            $service = @($servicesPayload | Where-Object { $_.service_id -eq $serviceId })[0]
            if ($null -eq $service) { throw "Missing service in rendered services file: $serviceId" }
            $healthUrl = if ($service.health_url) { $service.health_url } else { $service.base_url.TrimEnd('/') + "/health" }
            try {
                $health = Invoke-RestMethod -Uri $healthUrl -Method Get -TimeoutSec 10
                if (-not $health.ready) { $pending += $serviceId }
            } catch {
                $pending += $serviceId
            }
        }
        if ($pending.Count -eq 0) { return }
        Write-Host "Waiting for workers: $($pending -join ', ')" -ForegroundColor Yellow
        Start-Sleep -Seconds 5
    }
    throw "Workers did not become ready within $TimeoutSeconds seconds"
}

function Test-ConfiguredWorkerProcessOwnership {
    param(
        [AllowEmptyString()][string]$CommandLine,
        [AllowEmptyString()][string]$ExecutablePath,
        [string]$ProjectRoot,
        [string]$WorkerModule
    )
    if ([string]::IsNullOrWhiteSpace($CommandLine) -or [string]::IsNullOrWhiteSpace($WorkerModule)) {
        return $false
    }
    try {
        $normalizedRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd([char[]]@('\', '/'))
    } catch {
        return $false
    }
    $rootWithSeparator = $normalizedRoot + [IO.Path]::DirectorySeparatorChar
    $executableOwned = $false
    if (-not [string]::IsNullOrWhiteSpace($ExecutablePath)) {
        try {
            $normalizedExecutable = [IO.Path]::GetFullPath($ExecutablePath)
            $executableOwned = $normalizedExecutable.StartsWith(
                $rootWithSeparator, [StringComparison]::OrdinalIgnoreCase
            )
        } catch {
            $executableOwned = $false
        }
    }
    $escapedModule = [regex]::Escape($WorkerModule)
    $moduleArgumentPattern = '(?:^|\s)(?:"' + $escapedModule + '"|' + $escapedModule + ')(?=\s|$)'
    $moduleOwned = [regex]::IsMatch(
        $CommandLine,
        $moduleArgumentPattern,
        [Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
    return [bool]($executableOwned -and $moduleOwned)
}

function Stop-ConfiguredWorkerListeners {
    param([string]$Path)
    if (!(Test-Path -LiteralPath $Path)) { return }
    $formal = @("local-gpt-sovits-main", "local-indextts", "local-cosyvoice")
    $workerModules = @{
        "local-gpt-sovits-main" = "app.workers.gpt_sovits_worker:app"
        "local-indextts" = "app.workers.indextts_worker:app"
        "local-cosyvoice" = "app.workers.cosyvoice_worker:app"
    }
    $servicesPayload = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $ownedListeners = @()
    foreach ($service in @($servicesPayload | Where-Object { $_.service_id -in $formal })) {
        $port = ([Uri]$service.base_url).Port
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
        foreach ($processId in @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)) {
            $process = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$processId)" -ErrorAction SilentlyContinue
            $creationDate = if ($null -ne $process) { [string]$process.CreationDate } else { "" }
            $owned = -not [string]::IsNullOrWhiteSpace($creationDate) -and (Test-ConfiguredWorkerProcessOwnership `
                -CommandLine ([string]$process.CommandLine) `
                -ExecutablePath ([string]$process.ExecutablePath) `
                -ProjectRoot $Root `
                -WorkerModule $workerModules[[string]$service.service_id]
            )
            if (-not $owned) {
                throw "阻塞：端口 $port 被非本次验证进程占用"
            }
            $ownedListeners += [pscustomobject]@{
                Port = $port
                ProcessId = [int]$processId
                CreationDate = $creationDate
                WorkerModule = $workerModules[[string]$service.service_id]
            }
        }
    }
    $revalidatedListeners = @()
    foreach ($listener in $ownedListeners) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$listener.ProcessId)" -ErrorAction SilentlyContinue
        $sameCreation =
            $null -ne $process -and
            ([string]$process.CreationDate).Equals(
                [string]$listener.CreationDate, [StringComparison]::Ordinal
            )
        $owned = $sameCreation -and (Test-ConfiguredWorkerProcessOwnership `
            -CommandLine ([string]$process.CommandLine) `
            -ExecutablePath ([string]$process.ExecutablePath) `
            -ProjectRoot $Root `
            -WorkerModule ([string]$listener.WorkerModule)
        )
        if (-not $owned) {
            throw "阻塞：端口 $($listener.Port) 被非本次验证进程占用"
        }
        $revalidatedListeners += $listener
    }
    foreach ($listener in $revalidatedListeners) {
        Write-Host "[replace] stop owned worker listener on port $($listener.Port)" -ForegroundColor Yellow
        try {
            Stop-Process -Id $listener.ProcessId -Force -ErrorAction Stop
        } catch {
            throw "阻塞：端口 $($listener.Port) 的本次验证进程停止失败"
        }
    }
}

function Get-ControlPlaneHeaders {
    $headers = @{}
    if (-not [string]::IsNullOrWhiteSpace($env:TTS_MORE_API_TOKEN)) {
        $headers.Authorization = "Bearer $env:TTS_MORE_API_TOKEN"
    }
    return $headers
}

function Start-ValidationControlPlane {
    $headers = Get-ControlPlaneHeaders
    try {
        $health = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/api/health -Headers $headers -TimeoutSec 5
        if ($health.StatusCode -eq 200) { return }
    } catch {}
    $logs = Join-Path $Output "logs"
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    $previousServicesPath = $env:TTS_MORE_SERVICES_PATH
    $previousServiceMode = $env:TTS_MORE_SERVICE_MODE
    $env:TTS_MORE_SERVICES_PATH = $ServicesPath
    $env:TTS_MORE_SERVICE_MODE = "real"
    try {
        $script:ValidationControlPlane = Start-Process `
            -FilePath $Python `
            -ArgumentList "-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", "8000" `
            -WorkingDirectory $Root `
            -RedirectStandardOutput (Join-Path $logs "fault-control-plane.stdout.log") `
            -RedirectStandardError (Join-Path $logs "fault-control-plane.stderr.log") `
            -PassThru
    } finally {
        $env:TTS_MORE_SERVICES_PATH = $previousServicesPath
        $env:TTS_MORE_SERVICE_MODE = $previousServiceMode
    }
    for ($attempt = 0; $attempt -lt 60; $attempt += 1) {
        try {
            $health = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/api/health -Headers $headers -TimeoutSec 5
            if ($health.StatusCode -eq 200) { return }
        } catch { Start-Sleep -Seconds 2 }
    }
    throw "Validation control plane did not become ready"
}

function Invoke-SingleNodeDeployment {
    if ($RepoPaths -and !(Test-Path -LiteralPath $RepoPathsPath)) {
        throw "RepoPaths file not found: $RepoPathsPath"
    }
    $deploy = @{
        Profile = "local-all"
        Device = "CU128"
        Targets = @("default")
    }
    if ($TopologyPath) { $deploy.Topology = $TopologyPath }
    if ($Node) { $deploy.Node = $Node }
    if ($RepoPaths) { $deploy.RepoPaths = $RepoPaths }
    if ($Mode -eq "single-clean") { $deploy.CleanRepos = $true }
    Invoke-LocalScript (Join-Path $Root "scripts\deploy-local-tts.ps1") $deploy
    if (-not $SkipStart) {
        Stop-ConfiguredWorkerListeners $ServicesPath
        $start = @{ Detach = $true }
        if ($TopologyPath) { $start.Topology = $TopologyPath }
        if ($Node) { $start.Node = $Node }
        if ($RepoPaths) { $start.RepoPaths = $RepoPaths }
        Invoke-LocalScript (Join-Path $Root "scripts\start-service-workers.ps1") $start
    }
}

function Invoke-DistributedDeployment {
    if (-not $TopologyPath) { throw "distributed mode requires -Topology" }
    if (!(Test-Path -LiteralPath $TopologyPath)) { throw "Topology file not found: $TopologyPath" }
    if (-not $script:SshUser) { $script:SshUser = $env:TTS_MORE_VALIDATION_SSH_USER }
    if (-not $script:SshUser) { throw "distributed mode requires -SshUser or TTS_MORE_VALIDATION_SSH_USER" }
    Assert-SafeSshToken $script:SshUser "SSH user"
    if (-not $script:RemoteRoot) { $script:RemoteRoot = $env:TTS_MORE_VALIDATION_REMOTE_ROOT }
    if (-not $script:RemoteRoot) { throw "distributed mode requires -RemoteRoot or TTS_MORE_VALIDATION_REMOTE_ROOT" }

    $manifest = Get-Content -LiteralPath $TopologyPath -Raw | ConvertFrom-Json
    Assert-DistributedHostIsolation $manifest
    $workers = @($manifest.nodes.PSObject.Properties | Where-Object { $_.Value.role -eq "worker" })
    if ($Node) { $workers = @($workers | Where-Object { $_.Name -eq $Node }) }
    if ($workers.Count -eq 0) { throw "No worker nodes selected from topology" }
    Assert-DistributedMachineIsolation $manifest $workers
    $appDeploy = @{ Profile = "app-only"; Device = "CU128"; Targets = @("default"); Topology = $TopologyPath; Node = $manifest.app_node }
    Invoke-LocalScript (Join-Path $Root "scripts\deploy-local-tts.ps1") $appDeploy

    $script:DistributedWorkers = $workers
    foreach ($worker in $workers) {
        $nodeName = [string]$worker.Name
        $hostName = [string]$worker.Value.host
        Assert-SafeSshToken $nodeName "Topology node"
        Assert-SafeSshToken $hostName "Topology host"
        $target = "$script:SshUser@$hostName"
        $quotedRemoteRoot = Quote-PowerShellLiteral $script:RemoteRoot
        $quotedControllerCommit = Quote-PowerShellLiteral $ControllerCommit
        $remoteSync = "`$dirty = & git -C $quotedRemoteRoot status --porcelain --untracked-files=all; " +
            "if (`$LASTEXITCODE -ne 0) { throw 'Remote TTS More status failed' }; " +
            "if (`$dirty) { throw 'Remote TTS More checkout is dirty' }; " +
            "& git -C $quotedRemoteRoot fetch origin $quotedControllerCommit; " +
            "if (`$LASTEXITCODE -ne 0) { throw 'Remote TTS More fetch failed' }; " +
            "& git -C $quotedRemoteRoot checkout --detach $quotedControllerCommit; " +
            "if (`$LASTEXITCODE -ne 0) { throw 'Remote TTS More checkout failed' }; " +
            "`$actualCommit = (& git -C $quotedRemoteRoot rev-parse HEAD).Trim(); " +
            "if (`$LASTEXITCODE -ne 0 -or `$actualCommit -ne $quotedControllerCommit) { throw 'Remote TTS More commit mismatch' }; " +
            "`$dirty = & git -C $quotedRemoteRoot status --porcelain --untracked-files=all; " +
            "if (`$LASTEXITCODE -ne 0 -or `$dirty) { throw 'Remote TTS More checkout is dirty after sync' }"
        Invoke-RemotePowerShell $target $remoteSync
        $remoteTopology = Join-Path $script:RemoteRoot "data\local\topology.validation.json"
        $remoteData = Split-Path -Parent $remoteTopology
        Invoke-RemotePowerShell $target ("New-Item -ItemType Directory -Force -Path " + (Quote-PowerShellLiteral $remoteData) + " | Out-Null")
        Write-Host "[scp] topology -> remote worker" -ForegroundColor Cyan
        & scp $TopologyPath "${target}:$remoteTopology"
        if ($LASTEXITCODE -ne 0) { throw "Failed to copy topology to $target" }

        $deployScript = Join-Path $script:RemoteRoot "scripts\deploy-local-tts.ps1"
        $remoteDeploy = "& " + (Quote-PowerShellLiteral $deployScript) +
            " -Profile worker-node -Device CU128 -Targets default -Topology " + (Quote-PowerShellLiteral $remoteTopology) +
            " -Node " + (Quote-PowerShellLiteral $nodeName)
        if (-not $RequireBaseline) { $remoteDeploy += " -CleanRepos" }
        Invoke-RemotePowerShell $target $remoteDeploy
        if (-not $SkipStart) {
            $appServices = Get-Content -LiteralPath $ServicesPath -Raw | ConvertFrom-Json
            foreach ($serviceId in @($worker.Value.services)) {
                $workerService = @($appServices | Where-Object { $_.service_id -eq $serviceId })[0]
                if ($null -eq $workerService) { throw "Worker service missing from controller config: $serviceId" }
                $workerPort = ([Uri]$workerService.base_url).Port
                $remoteStop = "`$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $workerPort -ErrorAction SilentlyContinue); " +
                    "`$listeners | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id `$_ -Force -ErrorAction Stop }"
                Invoke-RemotePowerShell $target $remoteStop
            }
            $startScript = Join-Path $script:RemoteRoot "scripts\start-service-workers.ps1"
            $remoteStart = "& " + (Quote-PowerShellLiteral $startScript) +
                " -Topology " + (Quote-PowerShellLiteral $remoteTopology) +
                " -Node " + (Quote-PowerShellLiteral $nodeName) + " -Detach"
            Invoke-RemotePowerShell $target $remoteStart
        }
        Start-RemoteGpuMonitor $target (Join-Path $script:RemoteRoot "data\validation\cuda-controller")
    }
}

function Invoke-DistributedFaultRecovery {
    if (-not $script:SshUser) { $script:SshUser = $env:TTS_MORE_VALIDATION_SSH_USER }
    if (-not $script:RemoteRoot) { $script:RemoteRoot = $env:TTS_MORE_VALIDATION_REMOTE_ROOT }
    if (-not $script:SshUser -or -not $script:RemoteRoot) {
        throw "Distributed fault recovery requires SSH user and remote root"
    }
    $manifest = Get-Content -LiteralPath $TopologyPath -Raw | ConvertFrom-Json
    $workers = @($manifest.nodes.PSObject.Properties | Where-Object { $_.Value.role -eq "worker" })
    $requestedFaultNode = $env:TTS_MORE_VALIDATION_FAULT_NODE
    $faultWorker = if ($requestedFaultNode) {
        @($workers | Where-Object { $_.Name -eq $requestedFaultNode })[0]
    } else {
        $workers[0]
    }
    if ($null -eq $faultWorker) { throw "Fault-injection worker was not found in topology" }
    $serviceId = [string]@($faultWorker.Value.services)[0]
    $servicesPayload = Get-Content -LiteralPath $ServicesPath -Raw | ConvertFrom-Json
    $service = @($servicesPayload | Where-Object { $_.service_id -eq $serviceId })[0]
    if ($null -eq $service) { throw "Fault-injection service is missing from services file: $serviceId" }
    $port = ([Uri]$service.base_url).Port
    $target = "$script:SshUser@$([string]$faultWorker.Value.host)"
    $remoteTopology = Join-Path $script:RemoteRoot "data\local\topology.validation.json"
    $startScript = Join-Path $script:RemoteRoot "scripts\start-service-workers.ps1"
    $remoteStart = "& " + (Quote-PowerShellLiteral $startScript) +
        " -Topology " + (Quote-PowerShellLiteral $remoteTopology) +
        " -Node " + (Quote-PowerShellLiteral ([string]$faultWorker.Name)) + " -Detach"
    $report = [ordered]@{
        node = [string]$faultWorker.Name
        service_id = $serviceId
        degraded_within_seconds = $null
        other_services_ready = $false
        application_survived = $false
        restart_ready = $false
        retry_passed = $false
    }
    Start-ValidationControlPlane
    $headers = Get-ControlPlaneHeaders
    $workerStopped = $false
    try {
        $killCommand = '$connections = @(Get-NetTCPConnection -State Listen -LocalPort {0} -ErrorAction Stop); if ($connections.Count -eq 0) {{ throw ''No listener on validation port'' }}; $connections | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {{ Stop-Process -Id $_ -Force }}' -f $port
        $stopwatch = [Diagnostics.Stopwatch]::StartNew()
        $workerStopped = $true
        Invoke-RemotePowerShell $target $killCommand
        while ($stopwatch.Elapsed.TotalSeconds -le 15) {
            $status = Invoke-RestMethod -Uri http://127.0.0.1:8000/api/services/status -Headers $headers -TimeoutSec 10
            $faultStatus = @($status.services | Where-Object { $_.service_id -eq $serviceId })[0]
            $otherStatuses = @($status.services | Where-Object { $_.service_id -ne $serviceId -and $_.service_id -in @("local-gpt-sovits-main", "local-indextts", "local-cosyvoice") })
            if ($null -ne $faultStatus -and -not $faultStatus.ready) {
                $report.degraded_within_seconds = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 3)
                $report.other_services_ready = $otherStatuses.Count -eq 2 -and @($otherStatuses | Where-Object { -not $_.ready }).Count -eq 0
                break
            }
            Start-Sleep -Seconds 1
        }
        $appHealth = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/api/health -Headers $headers -TimeoutSec 5
        $report.application_survived = $appHealth.StatusCode -eq 200
    } finally {
        if ($workerStopped) {
            Invoke-RemotePowerShell $target $remoteStart
        }
    }
    Wait-ServiceReady $ServicesPath
    $report.restart_ready = $true
    $recoveryOutput = Join-Path $Output "recovery"
    $recoveryArgs = @(
        (Join-Path $Root "scripts\run-cuda-validation.py"),
        "--mode", "distributed",
        "--services", $ServicesPath,
        "--fixture", $FixturePath,
        "--output", $recoveryOutput,
        "--topology", $TopologyPath,
        "--distributed-preflight", $script:DistributedPreflightPath
    )
    if ($RequireBaseline) { $recoveryArgs += "--require-baseline" }
    & $Python @recoveryArgs
    $report.retry_passed = $LASTEXITCODE -eq 0
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $Output "fault-recovery.json") -Encoding UTF8
    if ($null -eq $report.degraded_within_seconds) { throw "Application did not report the stopped worker degraded within 15 seconds" }
    if (-not $report.other_services_ready) { throw "Another formal service became unavailable during fault injection" }
    if (-not $report.application_survived) { throw "Application control plane failed during worker outage" }
    if (-not $report.retry_passed) { throw "CUDA synthesis retry failed after worker restart" }
}

$environmentPreflightPath = Join-Path $Output "environment-preflight.json"
try {
    $hostPreflightArgs = @(
        (Join-Path $Root "scripts\tts_more_deploy.py"),
        "--root", $Root,
        "preflight-cuda-host",
        "--mode", $Mode,
        "--output", $environmentPreflightPath
    )
    & $Python @hostPreflightArgs | Out-Null
    $hostPreflightExitCode = $LASTEXITCODE
    if ($hostPreflightExitCode -ne 0) {
        $hostPreflightNextAction = "Resolve the failed CUDA host requirements, then rerun host preflight."
        try {
            $environmentPreflight = Get-Content -LiteralPath $environmentPreflightPath -Raw | ConvertFrom-Json
            if ($environmentPreflight.next_action) {
                $hostPreflightNextAction = [string]$environmentPreflight.next_action
            }
        } catch { }
        throw $hostPreflightNextAction
    }

    $currentStage = "input-preflight"
    $validatorArgs = @(
        (Join-Path $Root "scripts\run-cuda-validation.py"),
        "--mode", $Mode,
        "--services", $ServicesPath,
        "--fixture", $FixturePath,
        "--output", $Output,
        "--preflight-only"
    )
    if ($TopologyPath) { $validatorArgs += @("--topology", $TopologyPath) }
    if ($Node) { $validatorArgs += @("--node", $Node) }
    if ($RequireBaseline) { $validatorArgs += "--require-baseline" }
    if ($isDiagnostic) { $validatorArgs += "--diagnostic" }
    & $Python @validatorArgs | Out-Null
    $preflightExitCode = $LASTEXITCODE
    if ($preflightExitCode -ne 0) {
        $blockerCount = 1
        try {
            $preflightSummary = Get-Content -LiteralPath (Join-Path $Output "summary.json") -Raw | ConvertFrom-Json
            $blockerCount = [int]$preflightSummary.blocker_count
        } catch { }
        Write-Host "阻塞：input-preflight 有 $blockerCount 个未解决项；证据：summary.json" -ForegroundColor Red
        exit $preflightExitCode
    }
    $currentStage = "argument-validation"
    if ($Mode -eq "distributed") {
        if ($SkipDeploy) { throw "distributed mode does not allow -SkipDeploy because deployment identity checks are mandatory" }
        if ($Node) { throw "distributed mode does not allow -Node because all four machines are mandatory" }
        if ($SkipStart) { throw "distributed mode does not allow -SkipStart because worker restart is mandatory" }
        if ($SkipFaultRecovery) { throw "distributed mode does not allow -SkipFaultRecovery because recovery is mandatory" }
    }
    $currentStage = "deployment"
    if (-not $SkipDeploy) {
        if ($Mode -eq "distributed") {
            $script:DistributedDeploymentStarted = $true
            Invoke-DistributedDeployment
        } else {
            Invoke-SingleNodeDeployment
        }
    }
    $currentStage = "orchestration-preflight"
    if ($Mode -eq "distributed") { New-DistributedOrchestrationPreflight }
    $currentStage = "worker-wait"
    if (!(Test-Path -LiteralPath $ServicesPath)) { throw "Services file not found: $ServicesPath" }
    Wait-ServiceReady $ServicesPath

    $currentStage = "core-validation"
    $validatorArgs = @(
        (Join-Path $Root "scripts\run-cuda-validation.py"),
        "--mode", $Mode,
        "--services", $ServicesPath,
        "--fixture", $FixturePath,
        "--output", $Output
    )
    if ($TopologyPath) { $validatorArgs += @("--topology", $TopologyPath) }
    if ($script:DistributedPreflightPath) { $validatorArgs += @("--distributed-preflight", $script:DistributedPreflightPath) }
    if ($Node) { $validatorArgs += @("--node", $Node) }
    if ($RequireBaseline) { $validatorArgs += "--require-baseline" }
    if ($isDiagnostic) { $validatorArgs += "--diagnostic" }
    & $Python @validatorArgs
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) { throw "CUDA validation gate failed; see $(Join-Path $Output 'summary.json')" }
    if ($Mode -eq "distributed" -and -not $SkipFaultRecovery) {
        $currentStage = "fault-recovery"
        Invoke-DistributedFaultRecovery
    }
    if ($Mode -eq "distributed") {
        $currentStage = "evidence-collection"
        Collect-DistributedEvidence
        $script:DistributedEvidenceCollected = $true
    }
    if ($isDiagnostic) {
        Write-Host "CUDA diagnostic core 通过（不可认证）：$Output" -ForegroundColor Yellow
    } else {
        Write-Host "CUDA core 通过，Playwright/人工待完成：$Output" -ForegroundColor Green
    }
} catch {
    $failureMessage = $_.Exception.Message
    if ($Mode -eq "distributed" -and $script:DistributedDeploymentStarted -and -not $script:DistributedEvidenceCollected) {
        try { Collect-DistributedEvidence } catch { Write-Warning "Distributed evidence collection failed: $_" }
    }
    $writeBlocker = $currentStage -eq "host-preflight"
    if (-not $writeBlocker) {
        $writeBlocker = $true
        try {
            $existingSummary = Get-Content -LiteralPath (Join-Path $Output "summary.json") -Raw | ConvertFrom-Json
            if ($existingSummary.passed -eq $false) { $writeBlocker = $false }
        } catch { }
    }
    if ($writeBlocker) {
        $blockerArgs = @(
            (Join-Path $Root "scripts\run-cuda-validation.py"),
            "--mode", $Mode,
            "--services", $ServicesPath,
            "--fixture", $FixturePath,
            "--output", $Output,
            "--write-blocker-stage", $currentStage,
            "--blocker-message", $failureMessage
        )
        if ($TopologyPath) { $blockerArgs += @("--topology", $TopologyPath) }
        if ($Node) { $blockerArgs += @("--node", $Node) }
        if ($isDiagnostic) { $blockerArgs += "--diagnostic" }
        if ($currentStage -in @("fault-recovery", "evidence-collection")) { $blockerArgs += "--preserve-existing" }
        & $Python @blockerArgs | Out-Null
        if ($LASTEXITCODE -notin @(0, 1)) { Write-Warning "Unable to write blocker evidence for $currentStage" }
    }
    if ($currentStage -eq "host-preflight") {
        Write-Host "阻塞：CUDA 主机预检未通过；证据：environment-preflight.json、summary.json" -ForegroundColor Red
    } else {
        Write-Host "阻塞：$currentStage 失败；证据：summary.json" -ForegroundColor Red
    }
    throw
} finally {
    if ($script:ValidationControlPlane -and -not $script:ValidationControlPlane.HasExited) {
        Stop-Process -Id $script:ValidationControlPlane.Id -Force
    }
    Remove-Item Env:TTS_MORE_DISTRIBUTED_ORCHESTRATION_TOKEN -ErrorAction SilentlyContinue
    Stop-Transcript | Out-Null
}
