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

function Write-PrivateJsonAtomic {
    param([string] $Path, [object] $Document)
    $temporaryPath = "{0}.{1}.tmp" -f $Path, [Guid]::NewGuid().ToString('N')
    $backupPath = "{0}.{1}.previous" -f $Path, [Guid]::NewGuid().ToString('N')
    try {
        $Document | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
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
}

function Test-CommandLineArgument {
    param([string] $CommandLine, [string] $Argument)
    if (-not $CommandLine -or $Argument -notmatch '^[A-Za-z0-9_=-]+$') { return $false }
    $pattern = '(^|[\s"])' + [regex]::Escape($Argument) + '($|[\s"])'
    return [regex]::IsMatch($CommandLine, $pattern)
}

function ConvertTo-WindowsCommandLineArgument {
    param([AllowEmptyString()] [string] $Argument)
    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') {
        return $Argument
    }
    $encoded = New-Object Text.StringBuilder
    [void] $encoded.Append([char] 34)
    $backslashes = 0
    foreach ($character in $Argument.ToCharArray()) {
        if ($character -eq [char] 92) {
            $backslashes += 1
            continue
        }
        if ($character -eq [char] 34) {
            for ($index = 0; $index -lt (2 * $backslashes + 1); $index += 1) {
                [void] $encoded.Append([char] 92)
            }
            [void] $encoded.Append($character)
            $backslashes = 0
            continue
        }
        for ($index = 0; $index -lt $backslashes; $index += 1) {
            [void] $encoded.Append([char] 92)
        }
        $backslashes = 0
        [void] $encoded.Append($character)
    }
    for ($index = 0; $index -lt (2 * $backslashes); $index += 1) {
        [void] $encoded.Append([char] 92)
    }
    [void] $encoded.Append([char] 34)
    return $encoded.ToString()
}

function Test-FullRecordPromotesProvisional {
    param([object] $FullRecord, [object] $ProvisionalRecord, [object] $LaunchIntent)
    if ($null -eq $FullRecord -or $null -eq $ProvisionalRecord -or $null -eq $LaunchIntent) {
        return $false
    }
    try {
        $creationTicks = Get-UtcTicks -Value $FullRecord.creation_time
        return (
            [int] $FullRecord.pid -eq [int] $ProvisionalRecord.pid -and
            [string] $FullRecord.executable_path -eq [string] $ProvisionalRecord.executable_path -and
            [string] $FullRecord.executable_path -eq [string] $LaunchIntent.executable_path -and
            [int] $FullRecord.parent_pid -eq [int] $ProvisionalRecord.parent_pid -and
            [int] $FullRecord.parent_pid -eq [int] $LaunchIntent.parent_pid -and
            (Get-UtcTicks -Value $FullRecord.parent_creation_time) -eq
                (Get-UtcTicks -Value $ProvisionalRecord.parent_creation_time) -and
            (Get-UtcTicks -Value $FullRecord.parent_creation_time) -eq
                (Get-UtcTicks -Value $LaunchIntent.parent_creation_time) -and
            $creationTicks -ge (Get-UtcTicks -Value $ProvisionalRecord.started_after) -and
            $creationTicks -le (Get-UtcTicks -Value $ProvisionalRecord.started_before) -and
            (Test-CommandLineArgument -CommandLine ([string] $FullRecord.command_line) `
                -Argument ([string] $LaunchIntent.marker))
        )
    } catch { return $false }
}

function Write-LaunchIntentRunControlState {
    param(
        [string] $Path,
        [string] $RunId,
        [ValidateSet('tts-more', 'comfyui')] [string] $ProcessLabel,
        [object] $LaunchIntent,
        [object] $BackendRecord,
        [object] $ComfyRecord
    )
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $previous = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        if (
            $previous.version -ne 2 -or
            $previous.run_id -ne $RunId -or
            -not (Test-RecordDocumentMatches -Expected $BackendRecord -Actual $previous.owned_processes.'tts-more') -or
            -not (Test-RecordDocumentMatches -Expected $ComfyRecord -Actual $previous.owned_processes.comfyui) -or
            $null -ne $previous.provisional_processes.'tts-more' -or
            $null -ne $previous.provisional_processes.comfyui -or
            $null -ne $previous.launch_intents.'tts-more' -or
            $null -ne $previous.launch_intents.comfyui
        ) { throw 'Existing process control state is not ready for a new launch intent' }
    }
    $backendIntent = if ($ProcessLabel -eq 'tts-more') { $LaunchIntent } else { $null }
    $comfyIntent = if ($ProcessLabel -eq 'comfyui') { $LaunchIntent } else { $null }
    $document = [ordered]@{
        version = 2
        run_id = $RunId
        owned_processes = [ordered]@{
            'tts-more' = $BackendRecord
            comfyui = $ComfyRecord
        }
        provisional_processes = [ordered]@{
            'tts-more' = $null
            comfyui = $null
        }
        launch_intents = [ordered]@{
            'tts-more' = $backendIntent
            comfyui = $comfyIntent
        }
    }
    Write-PrivateJsonAtomic -Path $Path -Document $document
    $persisted = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $actualIntent = $persisted.launch_intents.PSObject.Properties[$ProcessLabel].Value
    if (
        $persisted.version -ne 2 -or
        $persisted.run_id -ne $RunId -or
        -not (Test-RecordDocumentMatches -Expected $BackendRecord -Actual $persisted.owned_processes.'tts-more') -or
        -not (Test-RecordDocumentMatches -Expected $ComfyRecord -Actual $persisted.owned_processes.comfyui) -or
        ($LaunchIntent | ConvertTo-Json -Depth 8 -Compress) -ne
            ($actualIntent | ConvertTo-Json -Depth 8 -Compress)
    ) { throw 'Pre-launch process recovery intent could not be revalidated' }
}

function Write-ProvisionalRunControlState {
    param(
        [string] $Path,
        [string] $RunId,
        [ValidateSet('tts-more', 'comfyui')] [string] $ProcessLabel,
        [object] $LaunchIntent,
        [object] $ProvisionalRecord,
        [object] $BackendRecord,
        [object] $ComfyRecord
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'Durable launch intent is missing before provisional identity write'
    }
    $previous = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $previousIntent = $previous.launch_intents.PSObject.Properties[$ProcessLabel].Value
    $otherLabel = if ($ProcessLabel -eq 'tts-more') { 'comfyui' } else { 'tts-more' }
    if (
        $previous.version -ne 2 -or
        $previous.run_id -ne $RunId -or
        -not (Test-RecordDocumentMatches -Expected $BackendRecord -Actual $previous.owned_processes.'tts-more') -or
        -not (Test-RecordDocumentMatches -Expected $ComfyRecord -Actual $previous.owned_processes.comfyui) -or
        ($LaunchIntent | ConvertTo-Json -Depth 8 -Compress) -ne
            ($previousIntent | ConvertTo-Json -Depth 8 -Compress) -or
        $null -ne $previous.launch_intents.PSObject.Properties[$otherLabel].Value -or
        $null -ne $previous.provisional_processes.'tts-more' -or
        $null -ne $previous.provisional_processes.comfyui
    ) { throw 'Existing launch intent does not accept the provisional identity' }
    $backendIntent = if ($ProcessLabel -eq 'tts-more') { $LaunchIntent } else { $null }
    $comfyIntent = if ($ProcessLabel -eq 'comfyui') { $LaunchIntent } else { $null }
    $backendProvisional = if ($ProcessLabel -eq 'tts-more') { $ProvisionalRecord } else { $null }
    $comfyProvisional = if ($ProcessLabel -eq 'comfyui') { $ProvisionalRecord } else { $null }
    $document = [ordered]@{
        version = 2
        run_id = $RunId
        owned_processes = [ordered]@{
            'tts-more' = $BackendRecord
            comfyui = $ComfyRecord
        }
        provisional_processes = [ordered]@{
            'tts-more' = $backendProvisional
            comfyui = $comfyProvisional
        }
        launch_intents = [ordered]@{
            'tts-more' = $backendIntent
            comfyui = $comfyIntent
        }
    }
    Write-PrivateJsonAtomic -Path $Path -Document $document
    $persisted = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $actualIntent = $persisted.launch_intents.PSObject.Properties[$ProcessLabel].Value
    $actualProvisional = $persisted.provisional_processes.PSObject.Properties[$ProcessLabel].Value
    if (
        $persisted.version -ne 2 -or
        $persisted.run_id -ne $RunId -or
        -not (Test-RecordDocumentMatches -Expected $BackendRecord -Actual $persisted.owned_processes.'tts-more') -or
        -not (Test-RecordDocumentMatches -Expected $ComfyRecord -Actual $persisted.owned_processes.comfyui) -or
        ($LaunchIntent | ConvertTo-Json -Depth 8 -Compress) -ne
            ($actualIntent | ConvertTo-Json -Depth 8 -Compress) -or
        ($ProvisionalRecord | ConvertTo-Json -Depth 8 -Compress) -ne
            ($actualProvisional | ConvertTo-Json -Depth 8 -Compress)
    ) { throw 'Provisional process recovery identity could not be revalidated' }
}

function Write-RunControlState {
    param(
        [string] $Path,
        [string] $RunId,
        [object] $BackendRecord,
        [object] $ComfyRecord
    )
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $previous = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        if ($previous.version -ne 2 -or $previous.run_id -ne $RunId) {
            throw 'Existing process control state is not promotable'
        }
        foreach ($role in @('tts-more', 'comfyui')) {
            $fullRecord = if ($role -eq 'tts-more') { $BackendRecord } else { $ComfyRecord }
            $previousFull = $previous.owned_processes.PSObject.Properties[$role].Value
            $provisional = $previous.provisional_processes.PSObject.Properties[$role].Value
            $intent = $previous.launch_intents.PSObject.Properties[$role].Value
            if ($null -ne $provisional -or $null -ne $intent) {
                if (-not (Test-FullRecordPromotesProvisional `
                        -FullRecord $fullRecord -ProvisionalRecord $provisional -LaunchIntent $intent)) {
                    throw 'Full process identity does not promote the provisional recovery identity'
                }
            } elseif (-not (Test-RecordDocumentMatches -Expected $previousFull -Actual $fullRecord)) {
                throw 'Existing full process identity changed during control-state update'
            }
        }
    }
    $document = [ordered]@{
        version = 2
        run_id = $RunId
        owned_processes = [ordered]@{
            'tts-more' = $BackendRecord
            comfyui = $ComfyRecord
        }
        provisional_processes = [ordered]@{
            'tts-more' = $null
            comfyui = $null
        }
        launch_intents = [ordered]@{
            'tts-more' = $null
            comfyui = $null
        }
    }
    Write-PrivateJsonAtomic -Path $Path -Document $document
    $persisted = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    if (
        $persisted.version -ne 2 -or
        $persisted.run_id -ne $RunId -or
        -not (Test-RecordDocumentMatches -Expected $BackendRecord -Actual $persisted.owned_processes.'tts-more') -or
        -not (Test-RecordDocumentMatches -Expected $ComfyRecord -Actual $persisted.owned_processes.comfyui) -or
        $null -ne $persisted.provisional_processes.'tts-more' -or
        $null -ne $persisted.provisional_processes.comfyui -or
        $null -ne $persisted.launch_intents.'tts-more' -or
        $null -ne $persisted.launch_intents.comfyui
    ) { throw 'Run-bound process control state could not be revalidated' }
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
            $createdAt.Ticks -lt (Get-UtcTicks -Value $Token.started_after) -or
            $createdAt.Ticks -gt (Get-UtcTicks -Value $Token.started_before)
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

function Resolve-LaunchIntentProcess {
    param([object] $Intent)
    $expectedPath = [IO.Path]::GetFullPath([string] $Intent.executable_path)
    $expectedName = [IO.Path]::GetFileName($expectedPath)
    $startedAfterTicks = Get-UtcTicks -Value $Intent.started_after
    $allProcesses = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $matches = @()
    foreach ($candidate in $allProcesses) {
        $candidateParentPid = [int] $candidate.ParentProcessId
        $candidateName = [string] $candidate.Name
        $candidatePathText = [string] $candidate.ExecutablePath
        $sameNamedChild = (
            $candidateParentPid -eq [int] $Intent.parent_pid -and
            $candidateName.Equals($expectedName, [StringComparison]::OrdinalIgnoreCase)
        )
        $samePathChild = $false
        if ($candidatePathText) {
            try {
                $samePathChild = (
                    $candidateParentPid -eq [int] $Intent.parent_pid -and
                    [IO.Path]::GetFullPath($candidatePathText).Equals(
                        $expectedPath,
                        [StringComparison]::OrdinalIgnoreCase
                    )
                )
            } catch {
                if ($sameNamedChild) { throw 'Launch intent candidate executable path is invalid' }
            }
        }
        if (-not $sameNamedChild -and -not $samePathChild) { continue }
        if ($null -eq $candidate.CreationDate) {
            throw 'Launch intent candidate creation time is missing'
        }
        try { $createdAt = $candidate.CreationDate.ToUniversalTime() } catch {
            throw 'Launch intent candidate creation time is invalid'
        }
        if ($createdAt.Ticks -lt $startedAfterTicks) { continue }
        if (-not $candidatePathText -or -not $candidate.CommandLine) {
            throw 'Launch intent candidate identity is incomplete'
        }
        if (-not $samePathChild) { continue }
        if (-not (Test-CommandLineArgument -CommandLine ([string] $candidate.CommandLine) `
                -Argument ([string] $Intent.marker))) { continue }
        $matches += [pscustomobject]@{
            pid = [int] $candidate.ProcessId
            creation_time = $createdAt.ToString('o')
            executable_path = $expectedPath
            command_line = [string] $candidate.CommandLine
            parent_pid = [int] $candidate.ParentProcessId
            parent_creation_time = [string] $Intent.parent_creation_time
        }
    }
    if ($matches.Count -gt 1) { throw 'Launch intent matched multiple processes' }
    if ($matches.Count -eq 0) { return $null }
    $resolved = $matches[0]
    $parents = @($allProcesses | Where-Object { [int] $_.ProcessId -eq [int] $resolved.parent_pid })
    if ($parents.Count -gt 1) { throw 'Launch intent parent identity is ambiguous' }
    if ($parents.Count -eq 1) {
        if (
            $null -eq $parents[0].CreationDate -or
            $parents[0].CreationDate.ToUniversalTime().Ticks -ne
                (Get-UtcTicks -Value $Intent.parent_creation_time)
        ) { throw 'Launch intent parent identity changed' }
    }
    return $resolved
}

function Start-ProvisionallyTrackedProcess {
    param(
        [string] $FilePath,
        [string[]] $ArgumentList,
        [string] $WorkingDirectory,
        [string] $ChildTempRoot,
        [object] $LauncherRecord,
        [System.Collections.Generic.List[object]] $StartedProcesses,
        [string] $ControlStatePath,
        [string] $RunId,
        [ValidateSet('tts-more', 'comfyui')] [string] $ProcessLabel,
        [string] $LaunchMarker,
        [object] $BackendRecord,
        [object] $ComfyRecord
    )
    if ($LaunchMarker -notmatch '^tts_more_reliability_run=[0-9a-f]{32}-(tts-more|comfyui)$') {
        throw 'Launch marker is invalid'
    }
    $markerPairCount = 0
    for ($index = 0; $index -lt ($ArgumentList.Count - 1); $index += 1) {
        if ($ArgumentList[$index] -eq '-X' -and $ArgumentList[$index + 1] -eq $LaunchMarker) {
            $markerPairCount += 1
        }
    }
    if ($markerPairCount -ne 1 -or @($ArgumentList | Where-Object { $_ -eq $LaunchMarker }).Count -ne 1) {
        throw 'Launch arguments do not contain the unique recovery marker'
    }
    $hadTemp = Test-Path Env:TEMP
    $hadTmp = Test-Path Env:TMP
    $previousTemp = $env:TEMP
    $previousTmp = $env:TMP
    $startedAfter = [DateTime]::UtcNow
    $launchIntent = [ordered]@{
        marker = $LaunchMarker
        executable_path = [IO.Path]::GetFullPath($FilePath)
        arguments = @($ArgumentList)
        working_directory = [IO.Path]::GetFullPath($WorkingDirectory)
        child_temp_root = [IO.Path]::GetFullPath($ChildTempRoot)
        parent_pid = [int] $LauncherRecord.pid
        parent_creation_time = [string] $LauncherRecord.creation_time
        started_after = $startedAfter.ToString('o')
    }
    # This canonical intent must exist before Start-Process. The random -X
    # marker makes an intent-only recovery uniquely enumerable if the first
    # post-start identity write fails.
    Write-LaunchIntentRunControlState -Path $ControlStatePath -RunId $RunId `
        -ProcessLabel $ProcessLabel -LaunchIntent $launchIntent `
        -BackendRecord $BackendRecord -ComfyRecord $ComfyRecord
    $encodedArgumentList = (@(
        foreach ($argument in $ArgumentList) {
            ConvertTo-WindowsCommandLineArgument -Argument $argument
        }
    )) -join ' '
    try {
        $env:TEMP = $ChildTempRoot
        $env:TMP = $ChildTempRoot
        $process = Start-Process -FilePath $FilePath -ArgumentList $encodedArgumentList `
            -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -PassThru
        $token = [pscustomobject]@{
            pid = [int] $process.Id
            executable_path = [IO.Path]::GetFullPath($FilePath)
            parent_pid = [int] $LauncherRecord.pid
            parent_creation_time = [string] $LauncherRecord.creation_time
            started_after = $startedAfter.ToString('o')
            started_before = [DateTime]::UtcNow.ToString('o')
        }
        $StartedProcesses.Add($token)
        try {
            Write-ProvisionalRunControlState -Path $ControlStatePath -RunId $RunId `
                -ProcessLabel $ProcessLabel -LaunchIntent $launchIntent `
                -ProvisionalRecord $token -BackendRecord $BackendRecord -ComfyRecord $ComfyRecord
        } catch {
            $persistenceFailure = $_
            if (-not (Stop-ProvisionalStartedProcess -Token $token)) {
                Write-Warning 'Provisional identity write failed; preserving the durable launch intent'
            }
            throw $persistenceFailure
        }
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

function Test-PrivateIdentityRecordsCanBeRemoved {
    param(
        [bool] $ProcessCleanupProven,
        [bool] $TempCleanupProven,
        [int] $OwnedProcessCount
    )
    if ($OwnedProcessCount -eq 0) { return $true }
    return $ProcessCleanupProven -and $TempCleanupProven
}

function Remove-PrivateIdentityRecordsIfSafe {
    param(
        [string] $HostManifestPath,
        [string] $ControlStatePath,
        [bool] $ProcessCleanupProven,
        [bool] $TempCleanupProven,
        [int] $OwnedProcessCount
    )
    if (-not (Test-PrivateIdentityRecordsCanBeRemoved `
            -ProcessCleanupProven $ProcessCleanupProven `
            -TempCleanupProven $TempCleanupProven `
            -OwnedProcessCount $OwnedProcessCount)) {
        Write-Warning 'Cleanup was not proven; preserving private process identity records'
        return $false
    }
    # Remove current state first so a partial deletion still leaves the full
    # launch manifest as the unique recovery identity record.
    foreach ($privateRecord in @($ControlStatePath, $HostManifestPath)) {
        if (-not (Test-Path -LiteralPath $privateRecord -PathType Leaf)) { continue }
        try {
            Remove-Item -LiteralPath $privateRecord -Force -ErrorAction Stop
        } catch {
            Write-Warning 'Private process identity record removal failed; preserving the remaining record'
            return $false
        }
    }
    return (
        -not (Test-Path -LiteralPath $HostManifestPath) -and
        -not (Test-Path -LiteralPath $ControlStatePath)
    )
}

function Remove-OwnedTempRoot {
    param([string] $Root, [string] $OwnerMarker, [string] $ExpectedRunId, [string] $ResolvedOutputRoot)
    $rootExists = Test-Path -LiteralPath $Root
    $resolvedRoot = if ($rootExists) {
        (Resolve-Path -LiteralPath $Root).Path
    } else {
        [IO.Path]::GetFullPath($Root)
    }
    $prefix = $ResolvedOutputRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $resolvedRoot.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        Write-Warning 'Validation temp root escaped the output root; preserving it'
        return $false
    }
    if (-not (Test-Path -LiteralPath $OwnerMarker -PathType Leaf)) {
        if (-not $rootExists) { return $true }
        Write-Warning 'Validation temp owner marker is missing; preserving the temp root'
        return $false
    }
    try {
        $owner = Get-Content -LiteralPath $OwnerMarker -Raw | ConvertFrom-Json
        if ($owner.run_id -ne $ExpectedRunId -or $owner.temp_root -ne $resolvedRoot) {
            Write-Warning 'Validation temp owner marker does not match; preserving the temp root'
            return $false
        }
        if ($rootExists) {
            Remove-Item -LiteralPath $resolvedRoot -Recurse -Force -ErrorAction Stop
        }
        Remove-Item -LiteralPath $OwnerMarker -Force -ErrorAction Stop
    } catch {
        Write-Warning 'Validation temp cleanup failed; preserving remaining owned artifacts'
        return $false
    }
    return (
        -not (Test-Path -LiteralPath $resolvedRoot) -and
        -not (Test-Path -LiteralPath $OwnerMarker)
    )
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
    $comfyLaunchMarker = "tts_more_reliability_run=$runId-comfyui"
    $comfyArguments = @(
        '-X', $comfyLaunchMarker, 'main.py', '--listen', $listenAddress, '--port', '8188',
        '--temp-directory', $comfyTempBase
    )
    $comfyStart = Start-ProvisionallyTrackedProcess -FilePath $comfyPythonPath `
        -ArgumentList $comfyArguments -WorkingDirectory $comfyRootPath `
        -ChildTempRoot $runnerTempRoot -LauncherRecord $launcherRecord `
        -StartedProcesses $startedProcesses -ControlStatePath $controlStatePath `
        -RunId $runId -ProcessLabel 'comfyui' -LaunchMarker $comfyLaunchMarker `
        -BackendRecord $backendRecord -ComfyRecord $comfyRecord
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

    $backendLaunchMarker = "tts_more_reliability_run=$runId-tts-more"
    $backendArguments = @(
        '-X', $backendLaunchMarker, '-m', 'uvicorn', 'app.main:app',
        '--app-dir', 'backend', '--host', $listenAddress, '--port', '8000'
    )
    $backendStart = Start-ProvisionallyTrackedProcess -FilePath $backendPythonPath `
        -ArgumentList $backendArguments -WorkingDirectory $ttsRootPath `
        -ChildTempRoot $runnerTempRoot -LauncherRecord $launcherRecord `
        -StartedProcesses $startedProcesses -ControlStatePath $controlStatePath `
        -RunId $runId -ProcessLabel 'tts-more' -LaunchMarker $backendLaunchMarker `
        -BackendRecord $backendRecord -ComfyRecord $comfyRecord
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
    $provisionalRecords = @{'tts-more' = $null; comfyui = $null}
    $launchIntents = @{'tts-more' = $null; comfyui = $null}
    $attemptedLabels = @{}
    $controlStateValid = $true
    $processCleanupProven = -not $provisionalCleanupFailed
    if (Test-Path -LiteralPath $controlStatePath -PathType Leaf) {
        try {
            $controlState = Get-Content -LiteralPath $controlStatePath -Raw | ConvertFrom-Json
            if ($controlState.version -eq 2 -and $controlState.run_id -eq $runId) {
                $latestBackendRecord = $controlState.owned_processes.'tts-more'
                $latestComfyRecord = $controlState.owned_processes.comfyui
                foreach ($role in @('tts-more', 'comfyui')) {
                    $provisionalRecords[$role] =
                        $controlState.provisional_processes.PSObject.Properties[$role].Value
                    $launchIntents[$role] =
                        $controlState.launch_intents.PSObject.Properties[$role].Value
                    $fullRecord = if ($role -eq 'tts-more') {
                        $latestBackendRecord
                    } else {
                        $latestComfyRecord
                    }
                    if (
                        $null -ne $fullRecord -or
                        $null -ne $provisionalRecords[$role] -or
                        $null -ne $launchIntents[$role]
                    ) { $attemptedLabels[$role] = $true }
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
    if (-not $controlStateValid -and (Test-Path -LiteralPath $controlStatePath)) {
        $attemptedLabels['unresolved-control'] = $true
    }
    if ($controlStateValid) {
        foreach ($role in @('tts-more', 'comfyui')) {
            $fullRecord = if ($role -eq 'tts-more') {
                $latestBackendRecord
            } else {
                $latestComfyRecord
            }
            $provisionalRecord = $provisionalRecords[$role]
            $launchIntent = $launchIntents[$role]
            if ($null -ne $fullRecord) {
                if ($null -ne $provisionalRecord -or $null -ne $launchIntent) {
                    Write-Warning 'Process control state contains conflicting full and provisional identities'
                    $processCleanupProven = $false
                    continue
                }
                if (-not (Stop-RecordedTree -Record $fullRecord)) {
                    $processCleanupProven = $false
                }
                continue
            }
            if ($null -ne $provisionalRecord) {
                if ($null -eq $launchIntent) {
                    Write-Warning 'Provisional process identity is missing its launch intent'
                    $processCleanupProven = $false
                } elseif (-not (Stop-ProvisionalStartedProcess -Token $provisionalRecord)) {
                    $processCleanupProven = $false
                }
                continue
            }
            if ($null -ne $launchIntent) {
                try {
                    $intentRecord = Resolve-LaunchIntentProcess -Intent $launchIntent
                    if ($null -ne $intentRecord -and -not (Stop-RecordedTree -Record $intentRecord)) {
                        $processCleanupProven = $false
                    }
                } catch {
                    Write-Warning 'Launch intent could not be resolved uniquely; preserving recovery records'
                    $processCleanupProven = $false
                }
            }
        }
    } else {
        $processCleanupProven = $false
    }
    $tempCleanupProven = $false
    if ($processCleanupProven) {
        $tempCleanupProven = Remove-OwnedTempRoot -Root $tempRoot -OwnerMarker $tempOwnerMarker `
            -ExpectedRunId $runId -ResolvedOutputRoot $outputRootPath
    } else {
        Write-Warning 'Process cleanup was not proven; preserving the validation temp root and owner marker'
    }
    $ownedProcessCount = [Math]::Max($attemptedLabels.Count, $startedProcesses.Count)
    $null = Remove-PrivateIdentityRecordsIfSafe `
        -HostManifestPath $hostManifestPath `
        -ControlStatePath $controlStatePath `
        -ProcessCleanupProven $processCleanupProven `
        -TempCleanupProven $tempCleanupProven `
        -OwnedProcessCount $ownedProcessCount
}
