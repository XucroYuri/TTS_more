[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Fixture,
    [Parameter(Mandatory = $true)] [string] $OutputRoot,
    [Parameter(Mandatory = $true)] [string] $ComfyUiRoot,
    [Parameter(Mandatory = $true)] [string] $ComfyPython,
    [Parameter(Mandatory = $true)] [string] $TtsMoreRoot,
    [switch] $AllowLan,
    [switch] $PreflightOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Resolve-ExistingPath {
    param([string] $LiteralPath, [ValidateSet('File', 'Directory')] [string] $Kind)
    $resolved = (Resolve-Path -LiteralPath $LiteralPath -ErrorAction Stop).Path
    $item = Get-Item -LiteralPath $resolved -Force
    if ($Kind -eq 'File' -and -not $item.PSIsContainer) { return $item.FullName }
    if ($Kind -eq 'Directory' -and $item.PSIsContainer) { return $item.FullName }
    throw "Expected an existing $Kind"
}

function Get-PortOwnerPid {
    param([int] $Port)
    $owners = @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
    if ($owners.Count -gt 1) { throw "Port $Port has multiple listening owners" }
    if ($owners.Count -eq 0) { return $null }
    return [int] $owners[0]
}

function Get-ProcessRecord {
    param([int] $Pid)
    $process = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $Pid) -ErrorAction Stop
    $parent = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $process.ParentProcessId) -ErrorAction Stop
    if (-not $process.ExecutablePath -or -not $process.CommandLine) {
        throw 'Process identity is incomplete'
    }
    return [ordered]@{
        pid = [int] $process.ProcessId
        creation_time = $process.CreationDate.ToUniversalTime().ToString('o')
        executable_path = [IO.Path]::GetFullPath([string] $process.ExecutablePath)
        command_line = [string] $process.CommandLine
        parent_pid = [int] $process.ParentProcessId
        parent_creation_time = $parent.CreationDate.ToUniversalTime().ToString('o')
    }
}

function Test-RecordedIdentity {
    param([object] $Record)
    try {
        $current = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f ([int] $Record.pid)) -ErrorAction Stop
    } catch { return $false }
    if (
        [int] $current.ProcessId -ne [int] $Record.pid -or
        $current.CreationDate.ToUniversalTime().Ticks -ne (Get-UtcTicks -Value $Record.creation_time) -or
        [IO.Path]::GetFullPath([string] $current.ExecutablePath) -ne [string] $Record.executable_path -or
        [string] $current.CommandLine -ne [string] $Record.command_line -or
        [int] $current.ParentProcessId -ne [int] $Record.parent_pid
    ) { return $false }
    $parent = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f ([int] $Record.parent_pid)) -ErrorAction SilentlyContinue
    if ($null -ne $parent) {
        return $parent.CreationDate.ToUniversalTime().Ticks -eq (Get-UtcTicks -Value $Record.parent_creation_time)
    }
    return $true
}

function Test-ProcessAbsent {
    param([int] $Pid)
    try {
        $null = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $Pid) -ErrorAction Stop
        return $false
    } catch {
        return $true
    }
}

function Get-UtcTicks {
    param([object] $Value)
    return [DateTimeOffset]::Parse(
        [string] $Value,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind
    ).UtcDateTime.Ticks
}

function Wait-ProcessRecord {
    param([int] $Pid, [int] $TimeoutSeconds = 10)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try { return Get-ProcessRecord -Pid $Pid } catch { Start-Sleep -Milliseconds 100 }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Started process identity could not be recorded'
}

function Test-RecordDocumentMatches {
    param([object] $Expected, [object] $Actual)
    if ($null -eq $Expected) { return $null -eq $Actual }
    if ($null -eq $Actual) { return $false }
    foreach ($field in @('pid', 'executable_path', 'command_line', 'parent_pid')) {
        if ([string] $Expected.$field -ne [string] $Actual.$field) { return $false }
    }
    if (
        (Get-UtcTicks -Value $Expected.creation_time) -ne (Get-UtcTicks -Value $Actual.creation_time) -or
        (Get-UtcTicks -Value $Expected.parent_creation_time) -ne (Get-UtcTicks -Value $Actual.parent_creation_time)
    ) { return $false }
    return $true
}

function Write-RunControlState {
    param(
        [string] $Path,
        [string] $RunId,
        [object] $BackendRecord,
        [object] $ComfyRecord
    )
    $document = [ordered]@{
        version = 1
        run_id = $RunId
        owned_processes = [ordered]@{
            'tts-more' = $BackendRecord
            comfyui = $ComfyRecord
        }
    }
    $temporaryPath = "{0}.{1}.tmp" -f $Path, [Guid]::NewGuid().ToString('N')
    $backupPath = "{0}.{1}.previous" -f $Path, [Guid]::NewGuid().ToString('N')
    try {
        $document | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [IO.File]::Replace($temporaryPath, $Path, $backupPath)
        } else {
            [IO.File]::Move($temporaryPath, $Path)
        }
    } finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
        if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
            Remove-Item -LiteralPath $backupPath -Force
        }
    }
    $persisted = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    if (
        $persisted.version -ne 1 -or
        $persisted.run_id -ne $RunId -or
        -not (Test-RecordDocumentMatches -Expected $BackendRecord -Actual $persisted.owned_processes.'tts-more') -or
        -not (Test-RecordDocumentMatches -Expected $ComfyRecord -Actual $persisted.owned_processes.comfyui)
    ) {
        throw 'Run-bound process control state could not be revalidated'
    }
    foreach ($record in @($BackendRecord, $ComfyRecord)) {
        if ($null -ne $record -and -not (Test-RecordedIdentity -Record $record)) {
            throw 'Persisted process identity no longer matches'
        }
    }
}

function Stop-ProvisionalStartedProcess {
    param([object] $Token)
    if (Test-ProcessAbsent -Pid ([int] $Token.pid)) { return $true }
    try {
        $current = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f ([int] $Token.pid)) -ErrorAction Stop
        $parent = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f ([int] $current.ParentProcessId)) -ErrorAction Stop
        $createdAt = $current.CreationDate.ToUniversalTime()
        if (
            [int] $current.ProcessId -ne [int] $Token.pid -or
            -not $current.ExecutablePath -or
            -not [IO.Path]::GetFullPath([string] $current.ExecutablePath).Equals(
                [string] $Token.executable_path,
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            [int] $current.ParentProcessId -ne [int] $Token.parent_pid -or
            $parent.CreationDate.ToUniversalTime().Ticks -ne (Get-UtcTicks -Value $Token.parent_creation_time) -or
            $createdAt -lt $Token.started_after -or
            $createdAt -gt $Token.started_before
        ) {
            Write-Warning 'Provisional process identity does not match; preserving the current PID'
            return $false
        }
        Stop-Process -Id ([int] $Token.pid) -Force -ErrorAction Stop
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        do {
            if (Test-ProcessAbsent -Pid ([int] $Token.pid)) { return $true }
            Start-Sleep -Milliseconds 100
        } while ([DateTime]::UtcNow -lt $deadline)
        Write-Warning 'Provisional process did not stop; preserving its temp root'
        return $false
    } catch {
        if (Test-ProcessAbsent -Pid ([int] $Token.pid)) { return $true }
        Write-Warning 'Provisional process could not be proven owned; preserving the current PID'
        return $false
    }
}

function Start-ProvisionallyTrackedProcess {
    param(
        [string] $FilePath,
        [string[]] $ArgumentList,
        [string] $WorkingDirectory,
        [string] $ChildTempRoot,
        [object] $LauncherRecord,
        [System.Collections.Generic.List[object]] $StartedProcesses
    )
    $hadTemp = Test-Path Env:TEMP
    $hadTmp = Test-Path Env:TMP
    $previousTemp = $env:TEMP
    $previousTmp = $env:TMP
    $startedAfter = [DateTime]::UtcNow
    try {
        $env:TEMP = $ChildTempRoot
        $env:TMP = $ChildTempRoot
        $process = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList `
            -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -PassThru
        $token = [pscustomobject]@{
            pid = [int] $process.Id
            executable_path = [IO.Path]::GetFullPath($FilePath)
            parent_pid = [int] $LauncherRecord.pid
            parent_creation_time = [string] $LauncherRecord.creation_time
            started_after = $startedAfter
            started_before = [DateTime]::UtcNow
        }
        $StartedProcesses.Add($token)
        return [pscustomobject]@{ process = $process; token = $token }
    } finally {
        if ($hadTemp) { $env:TEMP = $previousTemp } else { Remove-Item Env:TEMP -ErrorAction SilentlyContinue }
        if ($hadTmp) { $env:TMP = $previousTmp } else { Remove-Item Env:TMP -ErrorAction SilentlyContinue }
    }
}

function Wait-ExactPortOwner {
    param([int] $Port, [int] $Pid, [int] $TimeoutSeconds = 180)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if ((Get-PortOwnerPid -Port $Port) -eq $Pid) { return }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Owned process did not acquire port $Port"
}

function Stop-RecordedTree {
    param([object] $Record)
    if (Test-ProcessAbsent -Pid ([int] $Record.pid)) { return $true }
    if (-not (Test-RecordedIdentity -Record $Record)) {
        Write-Warning 'Recorded process identity no longer matches; preserving the current PID'
        return $false
    }
    $all = @(Get-CimInstance Win32_Process)
    $descendants = @{}
    $frontier = @([int] $Record.pid)
    while ($frontier.Count -gt 0) {
        $next = @()
        foreach ($parentPid in $frontier) {
            foreach ($child in @($all | Where-Object { [int] $_.ParentProcessId -eq $parentPid })) {
                if (-not $descendants.ContainsKey([int] $child.ProcessId)) {
                    $childRecord = Get-ProcessRecord -Pid ([int] $child.ProcessId)
                    $descendants[[int] $child.ProcessId] = $childRecord
                    $next += [int] $child.ProcessId
                }
            }
        }
        $frontier = $next
    }
    foreach ($childPid in @($descendants.Keys | Sort-Object -Descending)) {
        $childRecord = $descendants[$childPid]
        if (Test-RecordedIdentity -Record $childRecord) {
            Stop-Process -Id ([int] $childPid) -Force -ErrorAction SilentlyContinue
        }
    }
    if (Test-RecordedIdentity -Record $Record) {
        Stop-Process -Id ([int] $Record.pid) -Force -ErrorAction SilentlyContinue
    }
    $records = @($descendants.Values) + @($Record)
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        $allAbsent = $true
        foreach ($candidate in $records) {
            if (Test-ProcessAbsent -Pid ([int] $candidate.pid)) { continue }
            $allAbsent = $false
            if (-not (Test-RecordedIdentity -Record $candidate)) {
                Write-Warning 'A stopped PID was reused or changed; preserving the temp root'
                return $false
            }
        }
        if ($allAbsent) { return $true }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    Write-Warning 'Recorded process tree did not stop; preserving the temp root'
    return $false
}

function Remove-OwnedTempRoot {
    param([string] $Root, [string] $OwnerMarker, [string] $ExpectedRunId, [string] $ResolvedOutputRoot)
    if (-not (Test-Path -LiteralPath $Root)) { return }
    $resolvedRoot = (Resolve-Path -LiteralPath $Root).Path
    $prefix = $ResolvedOutputRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $resolvedRoot.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        Write-Warning 'Validation temp root escaped the output root; preserving it'
        return
    }
    if (-not (Test-Path -LiteralPath $OwnerMarker -PathType Leaf)) {
        Write-Warning 'Validation temp owner marker is missing; preserving the temp root'
        return
    }
    $owner = Get-Content -LiteralPath $OwnerMarker -Raw | ConvertFrom-Json
    if ($owner.run_id -ne $ExpectedRunId -or $owner.temp_root -ne $resolvedRoot) {
        Write-Warning 'Validation temp owner marker does not match; preserving the temp root'
        return
    }
    Remove-Item -LiteralPath $resolvedRoot -Recurse -Force
    Remove-Item -LiteralPath $OwnerMarker -Force
}

$fixturePath = Resolve-ExistingPath -LiteralPath $Fixture -Kind File
$comfyRootPath = Resolve-ExistingPath -LiteralPath $ComfyUiRoot -Kind Directory
$comfyPythonPath = Resolve-ExistingPath -LiteralPath $ComfyPython -Kind File
$ttsRootPath = Resolve-ExistingPath -LiteralPath $TtsMoreRoot -Kind Directory
$backendPythonPath = Resolve-ExistingPath -LiteralPath (Join-Path $ttsRootPath 'backend\.venv\Scripts\python.exe') -Kind File
$fixtureDocument = Get-Content -LiteralPath $fixturePath -Raw | ConvertFrom-Json

if (-not (Test-Path -LiteralPath $OutputRoot)) {
    New-Item -ItemType Directory -Path $OutputRoot | Out-Null
}
$outputRootPath = (Resolve-Path -LiteralPath $OutputRoot).Path
if ($outputRootPath -eq [IO.Path]::GetPathRoot($outputRootPath)) {
    throw 'OutputRoot must not be a drive root'
}

$suiteCandidate = Join-Path $comfyRootPath 'custom_nodes\TTS-Audio-Suite'
$suiteRoot = Resolve-ExistingPath -LiteralPath $suiteCandidate -Kind Directory
$gptRoot = Resolve-ExistingPath -LiteralPath $env:TTS_MORE_RELIABILITY_GPT_SOVITS_ROOT -Kind Directory
$indexRoot = Resolve-ExistingPath -LiteralPath $env:TTS_MORE_RELIABILITY_INDEXTTS_ROOT -Kind Directory
$cosyRoot = Resolve-ExistingPath -LiteralPath $env:TTS_MORE_RELIABILITY_COSYVOICE_ROOT -Kind Directory
$registryPath = Resolve-ExistingPath -LiteralPath $env:TTS_AUDIO_SUITE_RESOURCES -Kind File

$fixtureDirectory = Split-Path -Parent $fixturePath
$references = [ordered]@{}
foreach ($engine in @('gpt-sovits', 'indextts', 'cosyvoice')) {
    $relativeReference = [string] $fixtureDocument.resources.$engine.reference_audio
    if ([IO.Path]::IsPathRooted($relativeReference)) { throw 'Fixture reference_audio must remain relative' }
    $referencePath = Resolve-ExistingPath -LiteralPath (Join-Path $fixtureDirectory $relativeReference) -Kind File
    $references[$engine] = $referencePath
}

$runId = [Guid]::NewGuid().ToString('N')
$tempRoot = Join-Path $outputRootPath ("reliability-temp-{0}" -f $runId)
$runnerTempRoot = Join-Path $tempRoot 'runner'
$comfyTempBase = Join-Path $tempRoot 'comfyui'
$comfyTempRoot = Join-Path $comfyTempBase 'temp'
foreach ($directory in @($runnerTempRoot, $comfyTempRoot)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
$tempRoot = (Resolve-Path -LiteralPath $tempRoot).Path
$runnerTempRoot = (Resolve-Path -LiteralPath $runnerTempRoot).Path
$comfyTempBase = (Resolve-Path -LiteralPath $comfyTempBase).Path
$comfyTempRoot = (Resolve-Path -LiteralPath $comfyTempRoot).Path
$tempOwnerMarker = Join-Path $outputRootPath (".request-temp-{0}.owner.json" -f $runId)
@{
    run_id = $runId
    temp_root = $tempRoot
    runner_temp_root = $runnerTempRoot
    comfy_temp_root = $comfyTempRoot
} | ConvertTo-Json -Compress | Set-Content -LiteralPath $tempOwnerMarker -Encoding UTF8
$hostManifestPath = Join-Path $outputRootPath (".host-manifest-{0}.private.json" -f $runId)
$controlStatePath = "{0}.current.json" -f $hostManifestPath

$comfyRecord = $null
$backendRecord = $null
$startedProcesses = [System.Collections.Generic.List[object]]::new()
$provisionalCleanupFailed = $false
$launcherRecord = Get-ProcessRecord -Pid $PID
try {
    foreach ($port in @(8000, 8188)) {
        if ($null -ne (Get-PortOwnerPid -Port $port)) {
            throw "Port $port is already occupied by a process not owned by this validation run"
        }
    }

    $listenAddress = if ($AllowLan) { '0.0.0.0' } else { '127.0.0.1' }
    $quotedComfyTempBase = '"{0}"' -f $comfyTempBase
    $comfyArguments = @(
        'main.py', '--listen', $listenAddress, '--port', '8188',
        '--temp-directory', $quotedComfyTempBase
    )
    $comfyStart = Start-ProvisionallyTrackedProcess -FilePath $comfyPythonPath `
        -ArgumentList $comfyArguments -WorkingDirectory $comfyRootPath `
        -ChildTempRoot $runnerTempRoot -LauncherRecord $launcherRecord `
        -StartedProcesses $startedProcesses
    $comfyProcess = $comfyStart.process
    try {
        $comfyRecord = Wait-ProcessRecord -Pid $comfyProcess.Id
        Write-RunControlState -Path $controlStatePath -RunId $runId `
            -BackendRecord $backendRecord -ComfyRecord $comfyRecord
    } catch {
        if (-not (Stop-ProvisionalStartedProcess -Token $comfyStart.token)) {
            $provisionalCleanupFailed = $true
        }
        throw
    }
    Wait-ExactPortOwner -Port 8188 -Pid $comfyProcess.Id

    $backendArguments = @('-m', 'uvicorn', 'app.main:app', '--app-dir', 'backend', '--host', $listenAddress, '--port', '8000')
    $backendStart = Start-ProvisionallyTrackedProcess -FilePath $backendPythonPath `
        -ArgumentList $backendArguments -WorkingDirectory $ttsRootPath `
        -ChildTempRoot $runnerTempRoot -LauncherRecord $launcherRecord `
        -StartedProcesses $startedProcesses
    $backendProcess = $backendStart.process
    try {
        $backendRecord = Wait-ProcessRecord -Pid $backendProcess.Id
        Write-RunControlState -Path $controlStatePath -RunId $runId `
            -BackendRecord $backendRecord -ComfyRecord $comfyRecord
    } catch {
        if (-not (Stop-ProvisionalStartedProcess -Token $backendStart.token)) {
            $provisionalCleanupFailed = $true
        }
        throw
    }
    Wait-ExactPortOwner -Port 8000 -Pid $backendProcess.Id

    $hostManifest = [ordered]@{
        version = 1
        run_id = $runId
        owned_processes = [ordered]@{
            'tts-more' = $backendRecord
            comfyui = $comfyRecord
        }
        launch = [ordered]@{
            comfyui = [ordered]@{
                executable_path = $comfyPythonPath
                arguments = $comfyArguments
                working_directory = $comfyRootPath
                port = 8188
                temp_root = $runnerTempRoot
            }
        }
        boundary = [ordered]@{
            repositories = [ordered]@{
                'tts-more' = $ttsRootPath
                'tts-audio-suite' = $suiteRoot
                comfyui = $comfyRootPath
                'gpt-sovits' = $gptRoot
                indextts = $indexRoot
                cosyvoice = $cosyRoot
            }
            private_registry = $registryPath
            references = $references
        }
        temp_roots = @($runnerTempRoot, $comfyTempRoot)
    }
    $hostManifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $hostManifestPath -Encoding UTF8

    $pythonArguments = @(
        '-m', 'app.comfyui.reliability_validation',
        '--fixture', $fixturePath,
        '--output-root', $outputRootPath,
        '--host-manifest', $hostManifestPath,
        '--comfyui-pid', [string] $comfyProcess.Id,
        '--tts-more-pid', [string] $backendProcess.Id
    )
    if ($AllowLan) { $pythonArguments += '--allow-lan' }
    if ($PreflightOnly) { $pythonArguments += '--preflight-only' }
    Push-Location $ttsRootPath
    try {
        & $backendPythonPath @pythonArguments
        $validatorExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($validatorExitCode -ne 0) { throw 'Windows ComfyUI reliability gate failed' }
} finally {
    # Python publishes public evidence before returning. Cleanup then touches
    # only identities and temp paths created and revalidated by this run.
    $latestBackendRecord = $backendRecord
    $latestComfyRecord = $comfyRecord
    $controlStateValid = $true
    $processCleanupProven = -not $provisionalCleanupFailed
    if (Test-Path -LiteralPath $controlStatePath -PathType Leaf) {
        try {
            $controlState = Get-Content -LiteralPath $controlStatePath -Raw | ConvertFrom-Json
            if ($controlState.version -eq 1 -and $controlState.run_id -eq $runId) {
                if ($null -ne $controlState.owned_processes.'tts-more') {
                    $latestBackendRecord = $controlState.owned_processes.'tts-more'
                }
                if ($null -ne $controlState.owned_processes.comfyui) {
                    $latestComfyRecord = $controlState.owned_processes.comfyui
                } else {
                    $latestComfyRecord = $null
                }
            } else {
                Write-Warning 'Current process control state does not match this run; preserving replacement processes'
                $controlStateValid = $false
            }
        } catch {
            Write-Warning 'Current process control state is invalid; preserving replacement processes'
            $controlStateValid = $false
        }
    } elseif ($startedProcesses.Count -gt 0) {
        Write-Warning 'Current process control state is missing; preserving the validation temp root'
        $controlStateValid = $false
    }
    if ($controlStateValid) {
        if ($null -ne $latestBackendRecord -and -not (Stop-RecordedTree -Record $latestBackendRecord)) {
            $processCleanupProven = $false
        }
        if ($null -ne $latestComfyRecord -and -not (Stop-RecordedTree -Record $latestComfyRecord)) {
            $processCleanupProven = $false
        }
    } else {
        $processCleanupProven = $false
    }
    if ($processCleanupProven) {
        Remove-OwnedTempRoot -Root $tempRoot -OwnerMarker $tempOwnerMarker `
            -ExpectedRunId $runId -ResolvedOutputRoot $outputRootPath
    } else {
        Write-Warning 'Process cleanup was not proven; preserving the validation temp root and owner marker'
    }
    if (Test-Path -LiteralPath $hostManifestPath -PathType Leaf) {
        Remove-Item -LiteralPath $hostManifestPath -Force
    }
    if (Test-Path -LiteralPath $controlStatePath -PathType Leaf) {
        Remove-Item -LiteralPath $controlStatePath -Force
    }
}
