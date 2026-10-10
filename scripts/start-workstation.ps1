[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [string]$ComfyRoot = $env:TTS_MORE_COMFYUI_ROOT,
    [int]$BackendPort = 0,
    [int]$FrontendPort = 0,
    [int]$ComfyPort = 0,
    [ValidateRange(5, 900)][int]$TimeoutSeconds = 180,
    [ValidateSet('start', 'check', 'plan')][string]$Mode = 'start',
    [switch]$NoBrowser,
    [string[]]$Options = @()
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $OutputEncoding

function Get-LaunchText([string]$Chinese, [string]$English) {
    if ($env:TTS_MORE_LAUNCH_LANGUAGE -eq 'en') { return $English }
    return $Chinese
}

function Get-LaunchSnapshot {
    $map = @{}
    foreach ($item in @(Get-CimInstance Win32_Process)) { $map[[int]$item.ProcessId] = $item }
    return @{ Processes = $map; Listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop) }
}

function Test-LaunchOwner($Candidate, $Service) {
    if (!$Candidate) { return $false }
    $command = ([string]$Candidate.CommandLine).Replace('/', '\')
    $exe = [string]$Candidate.ExecutablePath
    switch ($Service.Name) {
        'backend' {
            return ([IO.Path]::GetFileName($exe) -ieq 'python.exe' -and $command -match '\buvicorn\b' -and $command.Contains('app.main:app') -and (
                $exe -ieq $Service.Exe -or $command.IndexOf($Service.Script, [StringComparison]::OrdinalIgnoreCase) -ge 0))
        }
        'comfyui' {
            return ([IO.Path]::GetFileName($exe) -ieq 'python.exe' -and $command -match '\bmain\.py\b' -and (
                $exe -ieq $Service.Exe -or $command.IndexOf($Service.Script, [StringComparison]::OrdinalIgnoreCase) -ge 0))
        }
        'frontend' {
            return ($exe -ieq $Service.Exe -and $command -match '\bvite(?:\.js)?\b' -and
                $command.IndexOf(($Service.Directory + '\'), [StringComparison]::OrdinalIgnoreCase) -ge 0)
        }
    }
    return $false
}

function Get-LaunchOwner($Listener, $Service, $Snapshot) {
    $candidate = $Snapshot.Processes[[int]$Listener.OwningProcess]
    $owner = $null
    $visited = @{}
    while ($candidate -and !$visited.ContainsKey([int]$candidate.ProcessId)) {
        $visited[[int]$candidate.ProcessId] = $true
        if (Test-LaunchOwner $candidate $Service) { $owner = $candidate }
        $parent = $Snapshot.Processes[[int]$candidate.ParentProcessId]
        if ($parent -and $parent.CreationDate -gt $candidate.CreationDate) { break }
        $candidate = $parent
    }
    return $owner
}

function Test-LaunchPort([int]$Port) {
    $socket = $null
    try {
        $socket = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, $Port)
        $socket.Server.ExclusiveAddressUse = $true
        $socket.Start()
        return $true
    } catch { return $false } finally { if ($socket) { $socket.Stop() } }
}

function Find-LaunchPort([int]$Preferred, [int[]]$Reserved = @()) {
    if ($Preferred -lt 1024 -or $Preferred -gt 65535) { throw "Invalid port: $Preferred (1024..65535 required)" }
    for ($port = $Preferred; $port -le [Math]::Min(65535, $Preferred + 300); $port++) {
        if ($port -notin $Reserved -and (Test-LaunchPort $port)) { return $port }
    }
    throw (Get-LaunchText "端口 $Preferred 附近没有可用端口，请检查 Windows 保留端口和防火墙。" "No available port near $Preferred. Check Windows excluded port ranges and firewall settings.")
}

function Get-LaunchJson([string]$Url, [string]$Method = 'Get') {
    $headers = @{}
    if ($env:TTS_MORE_API_TOKEN) { $headers.Authorization = 'Bearer ' + $env:TTS_MORE_API_TOKEN }
    return Invoke-RestMethod -Uri $Url -Method $Method -Headers $headers -TimeoutSec 3
}

function Test-LaunchReady($Service, [int]$Port, $BackendIdentity = $null) {
    try {
        $base = "http://127.0.0.1:$Port"
        switch ($Service.Name) {
            'comfyui' {
                $stats = Get-LaunchJson "$base/system_stats"
                $capabilities = Get-LaunchJson "$base/api/tts-audio-suite/v1/capabilities"
                foreach ($expected in $expectedResources) {
                    if (!@($capabilities.resources | Where-Object {
                        $_.resource_id -eq $expected.resource_id -and $_.engine -eq $expected.engine -and $_.ready
                    }).Count) { return $false }
                }
                return ($null -ne $stats.system -and $null -ne $capabilities.resources)
            }
            'backend' {
                $ready = Get-LaunchJson "$base/api/ready"
                return ($ready.status -eq 'ok' -and $ready.project_root -ieq $Service.Directory)
            }
            'frontend' {
                $page = Invoke-WebRequest -UseBasicParsing -Uri "$base/" -TimeoutSec 3
                $ready = Get-LaunchJson "$base/api/ready"
                return ($page.StatusCode -eq 200 -and $ready.project_root -ieq $BackendIdentity.project_root -and
                    $ready.pid -eq $BackendIdentity.pid)
            }
        }
    } catch { return $false }
    return $false
}

function Write-LaunchRecord($Record, [string]$Path) {
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllText($temporary, ($Record | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
        if (Test-Path -LiteralPath $Path) {
            # Windows PowerShell 5.1 marshals a null backup path as an empty string.
            $backup = "$temporary.bak"
            [IO.File]::Replace($temporary, $Path, $backup)
            Remove-Item -LiteralPath $backup -Force
        }
        else { [IO.File]::Move($temporary, $Path) }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Stop-FailedLaunch($Process, $Service) {
    # Only this invocation's newly created, verified process tree is eligible.
    $snapshot = Get-LaunchSnapshot
    $root = $snapshot.Processes[[int]$Process.Id]
    if (!$root -or [Math]::Abs(($root.CreationDate.ToUniversalTime() - $Process.StartTime.ToUniversalTime()).TotalMilliseconds) -gt 10 -or
        !(Test-LaunchOwner $root $Service)) { return }
    $targets = @($root)
    for ($index = 0; $index -lt $targets.Count; $index++) {
        $parent = $targets[$index]
        $targets += @($snapshot.Processes.Values | Where-Object {
            $_.ParentProcessId -eq $parent.ProcessId -and $_.CreationDate -ge $parent.CreationDate
        })
    }
    foreach ($target in $targets) {
        $fresh = Get-CimInstance Win32_Process -Filter "ProcessId=$($target.ProcessId)"
        if ($fresh -and $fresh.CreationDate -eq $target.CreationDate) {
            Stop-Process -Id $fresh.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

function Start-LaunchService($Service, [int[]]$Reserved, $BackendIdentity) {
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $Service.Port = Find-LaunchPort $Service.Port $Reserved
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
        $stdout = Join-Path $logs "$($Service.Name)-$stamp.stdout.log"
        $stderr = Join-Path $logs "$($Service.Name)-$stamp.stderr.log"
        switch ($Service.Name) {
            'comfyui' {
                $database = (Join-Path (Split-Path $logs) "comfyui-$($Service.Port).db").Replace('\', '/')
                $arguments = '"' + $Service.Script + '" --listen 127.0.0.1 --port ' + $Service.Port +
                    ' --disable-auto-launch --preview-method auto --database-url "sqlite:///' + $database + '"'
            }
            'backend' { $arguments = '-m uvicorn app.main:app --app-dir "' + $Service.Script + '" --host 127.0.0.1 --port ' + $Service.Port }
            'frontend' { $arguments = '"' + $Service.Script + '" --host 127.0.0.1 --port ' + $Service.Port + ' --strictPort' }
        }
        Write-Host (Get-LaunchText "正在启动 $($Service.Name)：http://127.0.0.1:$($Service.Port)（第 $attempt/3 次）" "Starting $($Service.Name): http://127.0.0.1:$($Service.Port) (attempt $attempt/3)")
        $process = Start-Process -FilePath $Service.Exe -ArgumentList $arguments -WorkingDirectory $Service.Directory `
            -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
        $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
        while ([DateTime]::UtcNow -lt $deadline) {
            $process.Refresh()
            if ($process.HasExited) { break }
            if (Test-LaunchReady $Service $Service.Port $BackendIdentity) {
                $snapshot = Get-LaunchSnapshot
                $listener = $snapshot.Listeners | Where-Object {
                    $_.LocalPort -eq $Service.Port -and $_.LocalAddress -eq '127.0.0.1'
                } | Select-Object -First 1
                $owner = Get-LaunchOwner $listener $Service $snapshot
                if ($owner -and $owner.ProcessId -eq $process.Id) {
                    return @{ pid = $process.Id; created_at = $owner.CreationDate.ToUniversalTime().ToString('o');
                        port = $Service.Port; reused = $false; stdout = $stdout; stderr = $stderr }
                }
            }
            Start-Sleep -Milliseconds 500
        }
        $tail = (Get-Content -LiteralPath $stderr -Tail 15 -ErrorAction SilentlyContinue) -join "`n"
        Stop-FailedLaunch $process $Service
        # Retry bind races only. Import/configuration errors need an actionable failure.
        if ($tail -match '(?i)address already in use|only one usage|10048|port .*already in use|10013') {
            $Service.Port++
            continue
        }
        throw (Get-LaunchText "$($Service.Name) 未能就绪，日志：$stderr`n$tail" "$($Service.Name) failed to become ready. Log: $stderr`n$tail")
    }
    throw (Get-LaunchText "$($Service.Name) 尝试 3 次后仍无法绑定端口。日志：$logs" "$($Service.Name) could not bind after 3 attempts. Logs: $logs")
}

# Dot-sourcing exposes lifecycle functions for isolated integration tests.
if ($MyInvocation.InvocationName -eq '.') { return }

$mutex = $null
$locked = $false
$record = $null
$recordPath = $null
try {
    foreach ($option in $Options) {
        switch ($option) {
            '--check' { $Mode = 'check' }
            '--plan' { $Mode = 'plan' }
            '--no-browser' { $NoBrowser = $true }
            default { throw "Unknown launcher option: $option" }
        }
    }
    if (!$ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
    $ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
    if (!$ComfyRoot) { $ComfyRoot = 'D:\ComfyUI-master' }
    $ComfyRoot = [IO.Path]::GetFullPath($ComfyRoot).TrimEnd('\')
    $backendPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (!(Test-Path -LiteralPath $backendPython)) { $backendPython = Join-Path $ProjectRoot 'backend\.venv\Scripts\python.exe' }
    $comfyPython = Join-Path $ComfyRoot '.venv\Scripts\python.exe'
    $vite = Join-Path $ProjectRoot 'frontend\node_modules\vite\bin\vite.js'
    $node = (Get-Command node.exe -ErrorAction Stop).Source
    $resources = if ($env:TTS_AUDIO_SUITE_RESOURCES) { $env:TTS_AUDIO_SUITE_RESOURCES } else {
        Join-Path $ProjectRoot 'data\local\tts-audio-suite-resources.yaml'
    }
    foreach ($path in @($backendPython, $comfyPython, $vite, $resources, (Join-Path $ComfyRoot 'main.py'))) {
        if (!(Test-Path -LiteralPath $path -PathType Leaf)) { throw (Get-LaunchText "未找到必需文件：$path" "Required file missing: $path") }
    }
    & $backendPython -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,12), "Backend requires Python 3.11"; import uvicorn, fastapi'
    if ($LASTEXITCODE -ne 0) { throw 'Backend Python/dependency check failed.' }
    $nodeVersion = & $node --version
    if ($LASTEXITCODE -ne 0 -or $nodeVersion -notmatch '^v(\d+)\.' -or [int]$Matches[1] -lt 20) {
        throw 'Node.js >=20 is required.'
    }
    if ($Mode -eq 'check') { Write-Host (Get-LaunchText '启动器依赖检查：通过' 'Launcher prerequisites: OK'); exit 0 }
    $resourceJson = & $backendPython (Join-Path $PSScriptRoot 'configure-launch-services.py') --inspect-resources $resources
    if ($LASTEXITCODE -ne 0) { throw 'Resource registry check failed.' }
    $expectedResources = $resourceJson | ConvertFrom-Json
    if (!$BackendPort) { $BackendPort = if ($env:TTS_MORE_BACKEND_PORT) { [int]$env:TTS_MORE_BACKEND_PORT } else { 8000 } }
    if (!$FrontendPort) { $FrontendPort = if ($env:TTS_MORE_FRONTEND_PORT) { [int]$env:TTS_MORE_FRONTEND_PORT } else { 5173 } }
    if (!$ComfyPort) { $ComfyPort = if ($env:TTS_MORE_COMFYUI_PORT) { [int]$env:TTS_MORE_COMFYUI_PORT } else { 8188 } }
    foreach ($port in @($BackendPort, $FrontendPort, $ComfyPort)) {
        if ($port -lt 1024 -or $port -gt 65535) { throw "Invalid port: $port" }
    }
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $key = ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($ProjectRoot.ToLowerInvariant())))).Replace('-', '').Substring(0, 20) }
    finally { $hash.Dispose() }
    $mutex = [Threading.Mutex]::new($false, "Local\TTSMoreLauncher-$key")
    $lockDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while (!$locked -and [DateTime]::UtcNow -lt $lockDeadline) {
        try { $locked = $mutex.WaitOne(500) } catch [Threading.AbandonedMutexException] { $locked = $true }
    }
    if (!$locked) { throw (Get-LaunchText '另一次启动仍在进行，请等待其完成后重试。' 'Another launch is still running. Wait for it to finish and try again.') }
    $runRoot = Join-Path $ProjectRoot 'data\local\run'
    $logs = Join-Path $runRoot 'one-click'
    $recordPath = Join-Path $runRoot 'tts-more.pid.json'
    $previous = $null
    if (Test-Path -LiteralPath $recordPath) {
        try { $previous = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json } catch { Write-Warning 'Ignoring an unreadable stale PID record.' }
    }
    $services = @(
        @{ Name = 'comfyui'; Port = $ComfyPort; Exe = $comfyPython; Directory = $ComfyRoot; Script = (Join-Path $ComfyRoot 'main.py') },
        @{ Name = 'backend'; Port = $BackendPort; Exe = $backendPython; Directory = $ProjectRoot; Script = (Join-Path $ProjectRoot 'backend') },
        @{ Name = 'frontend'; Port = $FrontendPort; Exe = $node; Directory = (Join-Path $ProjectRoot 'frontend'); Script = $vite }
    )
    $snapshot = Get-LaunchSnapshot
    $reserved = @()
    $backendIdentity = @{ project_root = $ProjectRoot; pid = -1 }
    $configChanged = $false
    $record = @{ project_root = $ProjectRoot; status = 'starting'; started_at = [DateTime]::UtcNow.ToString('o'); services = @{} }
    if ($Mode -ne 'plan') {
        New-Item -ItemType Directory -Path $logs -Force | Out-Null
        $env:PYTHONUTF8 = '1'; $env:PYTHONIOENCODING = 'utf-8'
        $env:TTS_AUDIO_SUITE_RESOURCES = $resources
        $env:TTS_AUDIO_SUITE_INSTALL_PROFILE = 'tts_more_targets'; $env:TTS_AUDIO_SUITE_AUTO_INSTALL = '0'
    }
    foreach ($service in $services) {
        $entry = $null
        $ownedPorts = @{}
        foreach ($listener in $snapshot.Listeners) {
            if ($listener.LocalAddress -ne '127.0.0.1') { continue }
            $owner = Get-LaunchOwner $listener $service $snapshot
            if ($owner) { $ownedPorts[[int]$listener.LocalPort] = $owner }
        }
        foreach ($port in @($ownedPorts.Keys | Sort-Object { if ($_ -eq $service.Port) { 0 } else { $_ } })) {
            if (Test-LaunchReady $service $port $backendIdentity) {
                $owner = $ownedPorts[$port]
                $entry = @{ pid = $owner.ProcessId; created_at = $owner.CreationDate.ToUniversalTime().ToString('o'); port = $port; reused = $true }
                $service.Port = $port
                break
            }
        }
        if (!$entry -and $ownedPorts.Count -gt 0 -and $service.Name -eq 'backend') {
            throw (Get-LaunchText "本项目的 $($service.Name) 实例未就绪，请检查日志后再启动。" "Owned $($service.Name) instance is unhealthy. Check its logs before restarting.")
        }
        if (!$entry) { $service.Port = Find-LaunchPort $service.Port $reserved }
        if ($Mode -eq 'plan') {
            $action = if ($entry) { 'reuse' } else { 'start' }
            Write-Host "$($service.Name): $action http://127.0.0.1:$($service.Port)"
            if ($service.Name -eq 'backend' -and $entry) { $backendIdentity = Get-LaunchJson "http://127.0.0.1:$($service.Port)/api/ready" }
            $reserved += $service.Port
            continue
        }
        if (!$entry) { $entry = Start-LaunchService $service $reserved $backendIdentity }
        $record.services[$service.Name] = $entry
        $record["$($service.Name)_pid"] = $entry.pid
        $record["$($service.Name)_port"] = $entry.port
        $reserved += $entry.port
        Write-LaunchRecord $record $recordPath
        if ($service.Name -eq 'comfyui') {
            $oldPort = if ($previous -and $previous.comfyui_port) { [int]$previous.comfyui_port } else { 8188 }
            $configResult = & $backendPython (Join-Path $PSScriptRoot 'configure-launch-services.py') --project-root $ProjectRoot --port $entry.port --previous-port $oldPort
            if ($LASTEXITCODE -ne 0) { throw 'Could not synchronize the ComfyUI service endpoint.' }
            $configuration = $configResult | ConvertFrom-Json
            $configChanged = [bool]$configuration.changed
            if ($configChanged) { Write-Host (Get-LaunchText "已同步 ComfyUI 地址：http://127.0.0.1:$($entry.port)" "ComfyUI endpoints synchronized: http://127.0.0.1:$($entry.port)") }
        }
        if ($service.Name -eq 'backend') {
            $live = Get-LaunchJson "http://127.0.0.1:$($entry.port)/api/ready"
            foreach ($endpoint in $configuration.expected_endpoints.PSObject.Properties) {
                if (!$live.comfyui_urls -or $live.comfyui_urls.($endpoint.Name) -ne $endpoint.Value) {
                    $configChanged = $true
                }
            }
            if ($entry.reused -and $configChanged) {
                $idleDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
                do {
                    $queue = Get-LaunchJson "http://127.0.0.1:$($entry.port)/api/queue/status"
                    if (!$queue.queued -and !$queue.running) { break }
                    if ([DateTime]::UtcNow -ge $idleDeadline) { throw (Get-LaunchText '服务地址已变化，但生成队列仍忙碌；请等待完成后重试。' 'Endpoint changed but the generation queue is busy. Retry after it finishes.') }
                    Start-Sleep -Milliseconds 500
                } while ($true)
                Get-LaunchJson "http://127.0.0.1:$($entry.port)/api/settings/services/reload" 'Post' | Out-Null
            }
            $backendIdentity = Get-LaunchJson "http://127.0.0.1:$($entry.port)/api/ready"
            $env:TTS_MORE_API_TARGET = "http://127.0.0.1:$($entry.port)"
        }
        Write-Host (Get-LaunchText "$($service.Name)：已就绪，PID $($entry.pid)，复用=$($entry.reused)" "$($service.Name): ready, PID $($entry.pid), reused=$($entry.reused)")
    }
    if ($Mode -eq 'plan') { exit 0 }
    $record.status = 'ready'
    $record.frontend_url = "http://127.0.0.1:$($record.frontend_port)"
    Write-LaunchRecord $record $recordPath
    Write-Host (Get-LaunchText "工作台：$($record.frontend_url)" "Workstation: $($record.frontend_url)")
    Write-Host (Get-LaunchText "日志：$logs" "Logs: $logs")
    if (!$NoBrowser) { Start-Process $record.frontend_url | Out-Null }
    exit 0
} catch {
    if ($record -and $recordPath -and $Mode -ne 'plan' -and (Test-Path -LiteralPath (Split-Path $recordPath))) {
        $record.status = 'failed'; $record.error = $_.Exception.Message
        Write-LaunchRecord $record $recordPath
    }
    Write-Host (Get-LaunchText "启动失败：$($_.Exception.Message)" "Startup failed: $($_.Exception.Message)") -ForegroundColor Red
    exit 1
} finally {
    if ($locked -and $mutex) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
