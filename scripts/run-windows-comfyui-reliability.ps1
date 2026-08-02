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
        Get-NetTCPConnection -State Listen -ErrorAction Stop |
            Where-Object { [int] $_.LocalPort -eq $Port } |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
    if ($owners.Count -gt 1) { throw "Port $Port has multiple listening owners" }
    if ($owners.Count -eq 0) { return $null }
    return [int] $owners[0]
}

function Get-ProcessRecord {
    param([int] $ProcessId)
    $process = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ProcessId) -ErrorAction Stop
    if ($null -eq $process) { throw 'Process identity is absent' }
    foreach ($field in @(
        'ProcessId', 'CreationDate', 'ExecutablePath', 'CommandLine', 'ParentProcessId'
    )) {
        $property = $process.PSObject.Properties[$field]
        if ($null -eq $property -or $null -eq $property.Value) {
            throw 'Process identity is incomplete'
        }
    }
    $parent = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $process.ParentProcessId) -ErrorAction Stop
    if ($null -eq $parent) { throw 'Parent process identity is absent' }
    $parentCreation = $parent.PSObject.Properties['CreationDate']
    if ($null -eq $parentCreation -or $null -eq $parentCreation.Value) {
        throw 'Parent process identity is incomplete'
    }
    $processCreationUtc = $process.CreationDate.ToUniversalTime()
    $parentCreationUtc = $parent.CreationDate.ToUniversalTime()
    if ($processCreationUtc.Ticks -lt $parentCreationUtc.Ticks) {
        throw 'Process identity predates its current parent'
    }
    return [ordered]@{
        pid = [int] $process.ProcessId
        creation_time = $processCreationUtc.ToString('o')
        executable_path = [IO.Path]::GetFullPath([string] $process.ExecutablePath)
        command_line = [string] $process.CommandLine
        parent_pid = [int] $process.ParentProcessId
        parent_creation_time = $parentCreationUtc.ToString('o')
    }
}

function Test-RecordedIdentity {
    param([object] $Record)
    try {
        $current = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f ([int] $Record.pid)) -ErrorAction Stop
        if ($null -eq $current) { return $false }
        foreach ($field in @(
            'ProcessId', 'CreationDate', 'ExecutablePath', 'CommandLine', 'ParentProcessId'
        )) {
            $property = $current.PSObject.Properties[$field]
            if ($null -eq $property -or $null -eq $property.Value) { return $false }
        }
        if (
            [int] $current.ProcessId -ne [int] $Record.pid -or
            $current.CreationDate.ToUniversalTime().Ticks -ne (Get-UtcTicks -Value $Record.creation_time) -or
            [IO.Path]::GetFullPath([string] $current.ExecutablePath) -ne [string] $Record.executable_path -or
            [string] $current.CommandLine -ne [string] $Record.command_line -or
            [int] $current.ParentProcessId -ne [int] $Record.parent_pid
        ) { return $false }
        $parent = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f ([int] $Record.parent_pid)) -ErrorAction Stop
        if ($null -eq $parent) { return $false }
        $parentCreation = $parent.PSObject.Properties['CreationDate']
        if ($null -eq $parentCreation -or $null -eq $parentCreation.Value) { return $false }
        return $parent.CreationDate.ToUniversalTime().Ticks -eq (Get-UtcTicks -Value $Record.parent_creation_time)
    } catch { return $false }
}

function Test-ProcessAbsent {
    param([int] $ProcessId)
    $current = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ProcessId) -ErrorAction Stop
    return $null -eq $current
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
    param([int] $ProcessId, [int] $TimeoutSeconds = 10)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try { return Get-ProcessRecord -ProcessId $ProcessId } catch { Start-Sleep -Milliseconds 100 }
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
        [object] $ComfyRecord,
        [object] $BackendLaunchRootRecord,
        [object] $ComfyLaunchRootRecord
    )
    if ($null -eq $BackendLaunchRootRecord) { $BackendLaunchRootRecord = $BackendRecord }
    if ($null -eq $ComfyLaunchRootRecord) { $ComfyLaunchRootRecord = $ComfyRecord }
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $previous = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        $previousLaunchRoots = $previous.PSObject.Properties['launch_roots']
        $previousBackendLaunchRoot = if ($null -eq $previousLaunchRoots) {
            $previous.owned_processes.'tts-more'
        } else { $previousLaunchRoots.Value.'tts-more' }
        $previousComfyLaunchRoot = if ($null -eq $previousLaunchRoots) {
            $previous.owned_processes.comfyui
        } else { $previousLaunchRoots.Value.comfyui }
        if (
            $previous.version -ne 2 -or
            $previous.run_id -ne $RunId -or
            -not (Test-RecordDocumentMatches -Expected $BackendRecord -Actual $previous.owned_processes.'tts-more') -or
            -not (Test-RecordDocumentMatches -Expected $ComfyRecord -Actual $previous.owned_processes.comfyui) -or
            -not (Test-RecordDocumentMatches -Expected $BackendLaunchRootRecord -Actual $previousBackendLaunchRoot) -or
            -not (Test-RecordDocumentMatches -Expected $ComfyLaunchRootRecord -Actual $previousComfyLaunchRoot) -or
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
        launch_roots = [ordered]@{
            'tts-more' = $BackendLaunchRootRecord
            comfyui = $ComfyLaunchRootRecord
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
        -not (Test-RecordDocumentMatches -Expected $BackendLaunchRootRecord -Actual $persisted.launch_roots.'tts-more') -or
        -not (Test-RecordDocumentMatches -Expected $ComfyLaunchRootRecord -Actual $persisted.launch_roots.comfyui) -or
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
        [object] $ComfyRecord,
        [object] $BackendLaunchRootRecord,
        [object] $ComfyLaunchRootRecord
    )
    if ($null -eq $BackendLaunchRootRecord) { $BackendLaunchRootRecord = $BackendRecord }
    if ($null -eq $ComfyLaunchRootRecord) { $ComfyLaunchRootRecord = $ComfyRecord }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'Durable launch intent is missing before provisional identity write'
    }
    $previous = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $previousIntent = $previous.launch_intents.PSObject.Properties[$ProcessLabel].Value
    $otherLabel = if ($ProcessLabel -eq 'tts-more') { 'comfyui' } else { 'tts-more' }
    $previousLaunchRoots = $previous.PSObject.Properties['launch_roots']
    $previousBackendLaunchRoot = if ($null -eq $previousLaunchRoots) {
        $previous.owned_processes.'tts-more'
    } else { $previousLaunchRoots.Value.'tts-more' }
    $previousComfyLaunchRoot = if ($null -eq $previousLaunchRoots) {
        $previous.owned_processes.comfyui
    } else { $previousLaunchRoots.Value.comfyui }
    if (
        $previous.version -ne 2 -or
        $previous.run_id -ne $RunId -or
        -not (Test-RecordDocumentMatches -Expected $BackendRecord -Actual $previous.owned_processes.'tts-more') -or
        -not (Test-RecordDocumentMatches -Expected $ComfyRecord -Actual $previous.owned_processes.comfyui) -or
        -not (Test-RecordDocumentMatches -Expected $BackendLaunchRootRecord -Actual $previousBackendLaunchRoot) -or
        -not (Test-RecordDocumentMatches -Expected $ComfyLaunchRootRecord -Actual $previousComfyLaunchRoot) -or
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
        launch_roots = [ordered]@{
            'tts-more' = $BackendLaunchRootRecord
            comfyui = $ComfyLaunchRootRecord
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
        -not (Test-RecordDocumentMatches -Expected $BackendLaunchRootRecord -Actual $persisted.launch_roots.'tts-more') -or
        -not (Test-RecordDocumentMatches -Expected $ComfyLaunchRootRecord -Actual $persisted.launch_roots.comfyui) -or
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
        [object] $ComfyRecord,
        [object] $BackendLaunchRootRecord,
        [object] $ComfyLaunchRootRecord
    )
    if ($null -eq $BackendLaunchRootRecord) { $BackendLaunchRootRecord = $BackendRecord }
    if ($null -eq $ComfyLaunchRootRecord) { $ComfyLaunchRootRecord = $ComfyRecord }
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $previous = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        if ($previous.version -ne 2 -or $previous.run_id -ne $RunId) {
            throw 'Existing process control state is not promotable'
        }
        foreach ($role in @('tts-more', 'comfyui')) {
            $fullRecord = if ($role -eq 'tts-more') { $BackendRecord } else { $ComfyRecord }
            $launchRootRecord = if ($role -eq 'tts-more') {
                $BackendLaunchRootRecord
            } else { $ComfyLaunchRootRecord }
            $previousFull = $previous.owned_processes.PSObject.Properties[$role].Value
            $previousLaunchRoots = $previous.PSObject.Properties['launch_roots']
            $previousLaunchRoot = if ($null -eq $previousLaunchRoots) {
                $previousFull
            } else { $previousLaunchRoots.Value.PSObject.Properties[$role].Value }
            $provisional = $previous.provisional_processes.PSObject.Properties[$role].Value
            $intent = $previous.launch_intents.PSObject.Properties[$role].Value
            if ($null -ne $provisional -or $null -ne $intent) {
                if (-not (Test-FullRecordPromotesProvisional `
                        -FullRecord $fullRecord -ProvisionalRecord $provisional -LaunchIntent $intent)) {
                    throw 'Full process identity does not promote the provisional recovery identity'
                }
                if (-not (Test-RecordDocumentMatches -Expected $fullRecord -Actual $launchRootRecord)) {
                    throw 'Promoted process identity does not establish its launch root'
                }
            } elseif (-not (Test-RecordDocumentMatches -Expected $previousFull -Actual $fullRecord)) {
                throw 'Existing full process identity changed during control-state update'
            } elseif (-not (Test-RecordDocumentMatches -Expected $previousLaunchRoot -Actual $launchRootRecord)) {
                throw 'Existing launch-root identity changed during control-state update'
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
        launch_roots = [ordered]@{
            'tts-more' = $BackendLaunchRootRecord
            comfyui = $ComfyLaunchRootRecord
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
        -not (Test-RecordDocumentMatches -Expected $BackendLaunchRootRecord -Actual $persisted.launch_roots.'tts-more') -or
        -not (Test-RecordDocumentMatches -Expected $ComfyLaunchRootRecord -Actual $persisted.launch_roots.comfyui) -or
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

function Write-ListenerRunControlState {
    param(
        [string] $Path,
        [string] $RunId,
        [ValidateSet('tts-more', 'comfyui')] [string] $ProcessLabel,
        [object] $LaunchRootRecord,
        [object] $ListenerRecord,
        [object] $BackendRecord,
        [object] $ComfyRecord,
        [object] $BackendLaunchRootRecord,
        [object] $ComfyLaunchRootRecord
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'Launch-root recovery state is missing before listener promotion'
    }
    $previous = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $previousLaunchRoots = $previous.PSObject.Properties['launch_roots']
    if ($null -eq $previousLaunchRoots) {
        throw 'Launch-root recovery state is missing before listener promotion'
    }
    $previousRoot = $previousLaunchRoots.Value.PSObject.Properties[$ProcessLabel].Value
    $previousListener = $previous.owned_processes.PSObject.Properties[$ProcessLabel].Value
    if (
        $previous.version -ne 2 -or
        $previous.run_id -ne $RunId -or
        -not (Test-RecordDocumentMatches -Expected $LaunchRootRecord -Actual $previousRoot) -or
        -not (Test-RecordDocumentMatches -Expected $LaunchRootRecord -Actual $previousListener) -or
        $null -ne $previous.provisional_processes.PSObject.Properties[$ProcessLabel].Value -or
        $null -ne $previous.launch_intents.PSObject.Properties[$ProcessLabel].Value
    ) { throw 'Existing control state does not accept listener promotion' }
    if (
        -not (Test-RecordDocumentMatches -Expected $BackendLaunchRootRecord -Actual $previous.launch_roots.'tts-more') -or
        -not (Test-RecordDocumentMatches -Expected $ComfyLaunchRootRecord -Actual $previous.launch_roots.comfyui) -or
        -not (Test-RecordDocumentMatches -Expected $BackendRecord -Actual $previous.owned_processes.'tts-more') -or
        -not (Test-RecordDocumentMatches -Expected $ComfyRecord -Actual $previous.owned_processes.comfyui)
    ) { throw 'Other process control identity changed before listener promotion' }
    if (
        -not (Test-RecordedIdentity -Record $LaunchRootRecord) -or
        -not (Test-RecordedIdentity -Record $ListenerRecord)
    ) { throw 'Listener promotion identity no longer matches' }
    $promotedBackend = if ($ProcessLabel -eq 'tts-more') { $ListenerRecord } else { $BackendRecord }
    $promotedComfy = if ($ProcessLabel -eq 'comfyui') { $ListenerRecord } else { $ComfyRecord }
    $document = [ordered]@{
        version = 2
        run_id = $RunId
        owned_processes = [ordered]@{
            'tts-more' = $promotedBackend
            comfyui = $promotedComfy
        }
        launch_roots = [ordered]@{
            'tts-more' = $BackendLaunchRootRecord
            comfyui = $ComfyLaunchRootRecord
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
        -not (Test-RecordDocumentMatches -Expected $promotedBackend -Actual $persisted.owned_processes.'tts-more') -or
        -not (Test-RecordDocumentMatches -Expected $promotedComfy -Actual $persisted.owned_processes.comfyui) -or
        -not (Test-RecordDocumentMatches -Expected $BackendLaunchRootRecord -Actual $persisted.launch_roots.'tts-more') -or
        -not (Test-RecordDocumentMatches -Expected $ComfyLaunchRootRecord -Actual $persisted.launch_roots.comfyui)
    ) { throw 'Listener promotion control state could not be revalidated' }
    if (
        -not (Test-RecordedIdentity -Record $LaunchRootRecord) -or
        -not (Test-RecordedIdentity -Record $ListenerRecord)
    ) { throw 'Persisted listener promotion identity no longer matches' }
}

function Stop-ProvisionalStartedProcess {
    param([object] $Token)
    if (Test-ProcessAbsent -ProcessId ([int] $Token.pid)) { return $true }
    try {
        $current = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f ([int] $Token.pid)) -ErrorAction Stop
        if ($null -eq $current) { return $true }
        $parent = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f ([int] $current.ParentProcessId)) -ErrorAction Stop
        if ($null -eq $parent) {
            Write-Warning 'Provisional parent identity is absent; preserving the current PID'
            return $false
        }
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
            if (Test-ProcessAbsent -ProcessId ([int] $Token.pid)) { return $true }
            Start-Sleep -Milliseconds 100
        } while ([DateTime]::UtcNow -lt $deadline)
        Write-Warning 'Provisional process did not stop; preserving its temp root'
        return $false
    } catch {
        if (Test-ProcessAbsent -ProcessId ([int] $Token.pid)) { return $true }
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

function Complete-ProvisionalStartupFailure {
    param(
        [object] $PrimaryFailure,
        [object] $Token,
        [ref] $CleanupFailed,
        [string] $UnprovedWarning
    )
    $cleanupError = $null
    $cleanupProven = $false
    try {
        $cleanupProven = Stop-ProvisionalStartedProcess -Token $Token
    } catch {
        $cleanupError = $_
    }
    if (-not $cleanupProven) {
        $CleanupFailed.Value = $true
        if ($null -ne $cleanupError) {
            Write-Warning 'Provisional process cleanup verification failed; preserving startup evidence'
        } else {
            Write-Warning $UnprovedWarning
        }
    }
    throw $PrimaryFailure
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
        [object] $ComfyRecord,
        [object] $BackendLaunchRootRecord,
        [object] $ComfyLaunchRootRecord,
        [string] $StandardOutputPath,
        [string] $StandardErrorPath,
        [ref] $ProvisionalCleanupFailed
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
        -BackendRecord $BackendRecord -ComfyRecord $ComfyRecord `
        -BackendLaunchRootRecord $BackendLaunchRootRecord `
        -ComfyLaunchRootRecord $ComfyLaunchRootRecord
    $encodedArgumentList = (@(
        foreach ($argument in $ArgumentList) {
            ConvertTo-WindowsCommandLineArgument -Argument $argument
        }
    )) -join ' '
    if ([bool] $StandardOutputPath -ne [bool] $StandardErrorPath) {
        throw 'Child stdout and stderr sidecars must be configured together'
    }
    if (
        $StandardOutputPath -and
        [IO.Path]::GetFullPath($StandardOutputPath).Equals(
            [IO.Path]::GetFullPath($StandardErrorPath),
            [StringComparison]::OrdinalIgnoreCase
        )
    ) { throw 'Child stdout and stderr sidecars must be distinct' }
    try {
        $env:TEMP = $ChildTempRoot
        $env:TMP = $ChildTempRoot
        $startParameters = @{
            FilePath = $FilePath
            ArgumentList = $encodedArgumentList
            WorkingDirectory = $WorkingDirectory
            WindowStyle = 'Hidden'
            PassThru = $true
        }
        if ($StandardOutputPath) {
            $startParameters.RedirectStandardOutput = [IO.Path]::GetFullPath($StandardOutputPath)
            $startParameters.RedirectStandardError = [IO.Path]::GetFullPath($StandardErrorPath)
        }
        $process = Start-Process @startParameters
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
                -ProvisionalRecord $token -BackendRecord $BackendRecord -ComfyRecord $ComfyRecord `
                -BackendLaunchRootRecord $BackendLaunchRootRecord `
                -ComfyLaunchRootRecord $ComfyLaunchRootRecord
        } catch {
            $persistenceFailure = $_
            $cleanupFailedRef = $ProvisionalCleanupFailed
            if ($null -eq $cleanupFailedRef) {
                $localCleanupFailed = $false
                $cleanupFailedRef = [ref] $localCleanupFailed
            }
            Complete-ProvisionalStartupFailure `
                -PrimaryFailure $persistenceFailure -Token $token `
                -CleanupFailed $cleanupFailedRef `
                -UnprovedWarning 'Provisional identity write failed; preserving the durable launch intent'
        }
        return [pscustomobject]@{ process = $process; token = $token }
    } finally {
        if ($hadTemp) { $env:TEMP = $previousTemp } else { Remove-Item Env:TEMP -ErrorAction SilentlyContinue }
        if ($hadTmp) { $env:TMP = $previousTmp } else { Remove-Item Env:TMP -ErrorAction SilentlyContinue }
    }
}

function Get-ExactCurrentProcessRecord {
    param([object] $Record)
    $current = Get-ProcessRecord -ProcessId ([int] $Record.pid)
    if (-not (Test-RecordDocumentMatches -Expected $Record -Actual $current)) {
        throw 'Recorded process identity changed'
    }
    return $current
}

function Resolve-DelegatedListenerRecord {
    param(
        [int] $ListenerProcessId,
        [object] $LaunchRecord,
        [object] $StartedAfter,
        [object] $ObservedBefore
    )
    $launchCurrent = Get-ExactCurrentProcessRecord -Record $LaunchRecord
    $listener = Get-ProcessRecord -ProcessId $ListenerProcessId
    $startedAfterTicks = Get-UtcTicks -Value $StartedAfter
    $observedBeforeTicks = Get-UtcTicks -Value $ObservedBefore
    $listenerTicks = Get-UtcTicks -Value $listener.creation_time
    $launchTicks = Get-UtcTicks -Value $launchCurrent.creation_time
    if (
        $listenerTicks -lt $startedAfterTicks -or
        $listenerTicks -lt $launchTicks -or
        $listenerTicks -gt $observedBeforeTicks
    ) { throw 'Listening process falls outside the launch observation window' }

    $candidate = $listener
    $visited = @{}
    for ($depth = 0; $depth -lt 64; $depth += 1) {
        $candidatePid = [int] $candidate.pid
        if ($visited.ContainsKey($candidatePid)) {
            throw 'Listening process ancestry contains a cycle'
        }
        $visited[$candidatePid] = $true
        if ($candidatePid -eq [int] $launchCurrent.pid) {
            if (-not (Test-RecordDocumentMatches -Expected $launchCurrent -Actual $candidate)) {
                throw 'Listening process ancestry reached a changed launch root'
            }
            return $listener
        }
        $parentPid = [int] $candidate.parent_pid
        if ($parentPid -le 0 -or $parentPid -eq $candidatePid) {
            throw 'Listening process ancestry is broken'
        }
        $parent = Get-ProcessRecord -ProcessId $parentPid
        if (
            (Get-UtcTicks -Value $parent.creation_time) -ne
                (Get-UtcTicks -Value $candidate.parent_creation_time) -or
            (Get-UtcTicks -Value $parent.creation_time) -gt
                (Get-UtcTicks -Value $candidate.creation_time)
        ) { throw 'Listening process ancestry identity changed' }
        $candidate = $parent
    }
    throw 'Listening process ancestry exceeds the bounded depth'
}

function Wait-ExactPortOwner {
    param(
        [int] $Port,
        [int] $ProcessId,
        [int] $TimeoutSeconds = 180,
        [object] $Process,
        [object] $LaunchRecord,
        [object] $StartedAfter
    )
    if ($null -ne $Process -and $Process.HasExited) {
        $Process.WaitForExit()
        throw "Owned process exited before acquiring port $Port; inspect the private child logs"
    }
    if ($null -eq $LaunchRecord) {
        $LaunchRecord = Get-ProcessRecord -ProcessId $ProcessId
    }
    if ([int] $LaunchRecord.pid -ne $ProcessId) {
        throw 'Tracked process PID does not match its launch identity'
    }
    if ($null -eq $StartedAfter) { $StartedAfter = $LaunchRecord.creation_time }
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if ($null -ne $Process -and $Process.HasExited) {
            $Process.WaitForExit()
            throw "Owned process exited before acquiring port $Port; inspect the private child logs"
        }
        $ownerPid = Get-PortOwnerPid -Port $Port
        if ($null -ne $ownerPid) {
            $observedBefore = [DateTime]::UtcNow.ToString('o')
            $listener = Resolve-DelegatedListenerRecord `
                -ListenerProcessId ([int] $ownerPid) -LaunchRecord $LaunchRecord `
                -StartedAfter $StartedAfter -ObservedBefore $observedBefore
            if ((Get-PortOwnerPid -Port $Port) -ne [int] $listener.pid) {
                throw 'Listening port owner changed during identity promotion'
            }
            $null = Get-ExactCurrentProcessRecord -Record $listener
            $null = Get-ExactCurrentProcessRecord -Record $LaunchRecord
            return $listener
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Owned process did not acquire port $Port"
}

function Stop-RecordedTree {
    param(
        [object] $Record,
        [object[]] $AdditionalRecords = @(),
        [string] $RecordRole = 'recorded-process',
        [string[]] $AdditionalRoles = @(),
        [int] $TimeoutMilliseconds = 30000,
        [int] $PollIntervalMilliseconds = 100
    )
    if ($TimeoutMilliseconds -lt 0 -or $PollIntervalMilliseconds -lt 0) {
        throw 'Process cleanup observation bounds are invalid'
    }

    $seedRecords = @($Record) + @($AdditionalRecords | Where-Object { $null -ne $_ })
    $seedRoles = @($RecordRole) + @($AdditionalRoles)
    $forest = @{}
    $depths = @{}
    $roles = @{}
    $frontier = @()

    for ($seedIndex = 0; $seedIndex -lt $seedRecords.Count; $seedIndex += 1) {
        $seed = $seedRecords[$seedIndex]
        if ($null -eq $seed) { continue }
        $seedPid = [int] $seed.pid
        $seedRole = 'recorded-process'
        if ($seedIndex -lt $seedRoles.Count -and $seedRoles[$seedIndex]) {
            $seedRole = [string] $seedRoles[$seedIndex]
        }
        if ($forest.ContainsKey($seedPid)) {
            if (-not (Test-RecordDocumentMatches -Expected $forest[$seedPid] -Actual $seed)) {
                throw 'Recorded cleanup seeds reuse one PID with different identities'
            }
            if ([string] $roles[$seedPid] -ne $seedRole) {
                $roles[$seedPid] = ('{0}+{1}' -f [string] $roles[$seedPid], $seedRole)
            }
            continue
        }
        if (Test-ProcessAbsent -ProcessId $seedPid) { continue }
        if (-not (Test-RecordedIdentity -Record $seed)) {
            if (Test-ProcessAbsent -ProcessId $seedPid) { continue }
            Write-Warning (
                'Process cleanup decision role={0}; pid={1}; expected_creation={2}; current_creation=unavailable; decision=pre-stop-unproved' -f
                    $seedRole, $seedPid, [string] $seed.creation_time
            )
            return $false
        }
        $forest[$seedPid] = $seed
        $depths[$seedPid] = 0
        $roles[$seedPid] = $seedRole
        $frontier += $seed
    }
    if ($forest.Count -eq 0) { return $true }

    $all = @(Get-CimInstance Win32_Process)
    while ($frontier.Count -gt 0) {
        $next = @()
        foreach ($parentRecord in $frontier) {
            $parentPid = [int] $parentRecord.pid
            foreach ($child in @($all | Where-Object {
                [int] $_.ParentProcessId -eq $parentPid
            })) {
                $childPid = [int] $child.ProcessId
                if ($childPid -eq $parentPid) {
                    throw 'Recorded process forest contains a self-parent edge'
                }
                $childRecord = Get-ProcessRecord -ProcessId $childPid
                $parentCreationTicks = Get-UtcTicks -Value $parentRecord.creation_time
                if (
                    [int] $childRecord.parent_pid -ne $parentPid -or
                    (Get-UtcTicks -Value $childRecord.parent_creation_time) -ne $parentCreationTicks -or
                    (Get-UtcTicks -Value $childRecord.creation_time) -lt $parentCreationTicks
                ) {
                    throw 'Snapshot descendant no longer belongs to the exact frontier process'
                }
                $childDepth = [int] $depths[$parentPid] + 1
                if ($forest.ContainsKey($childPid)) {
                    if (-not (Test-RecordDocumentMatches -Expected $forest[$childPid] -Actual $childRecord)) {
                        throw 'Recorded process forest PID identity changed during collection'
                    }
                    if ($childDepth -gt [int] $depths[$childPid]) {
                        $depths[$childPid] = $childDepth
                    }
                    continue
                }
                $forest[$childPid] = $childRecord
                $depths[$childPid] = $childDepth
                $roles[$childPid] = 'descendant'
                $next += $childRecord
            }
        }
        $frontier = $next
    }

    foreach ($candidatePid in @($forest.Keys)) {
        $seen = @{}
        $cursorPid = [int] $candidatePid
        while ($forest.ContainsKey($cursorPid)) {
            if ($seen.ContainsKey($cursorPid)) {
                throw 'Recorded process forest contains a cyclic parent edge'
            }
            $seen[$cursorPid] = $true
            $cursorPid = [int] $forest[$cursorPid].parent_pid
        }
    }

    # Complete the exact-identity and edge gate for the entire forest before
    # the first Stop-Process call. Post-stop processing is observation-only.
    foreach ($candidate in @($forest.Values)) {
        if (-not (Test-RecordedIdentity -Record $candidate)) {
            Write-Warning (
                'Process cleanup decision role={0}; pid={1}; expected_creation={2}; current_creation=unavailable; decision=pre-stop-unproved' -f
                    [string] $roles[[int] $candidate.pid], [int] $candidate.pid,
                    [string] $candidate.creation_time
            )
            return $false
        }
    }

    # Seed traversal depth is provisional because a listener can be both a
    # depth-zero seed and a later descendant of the launch root. Recompute the
    # final topology from the now-complete, cycle-free recorded parent edges.
    $finalDepths = @{}
    foreach ($candidatePid in @($forest.Keys)) {
        $candidateDepth = 0
        $cursorPid = [int] $candidatePid
        while ($forest.ContainsKey([int] $forest[$cursorPid].parent_pid)) {
            $candidateDepth += 1
            $cursorPid = [int] $forest[$cursorPid].parent_pid
        }
        $finalDepths[[int] $candidatePid] = $candidateDepth
    }
    $depths = $finalDepths

    $stopOrder = @(
        foreach ($candidatePid in $forest.Keys) {
            [pscustomobject]@{
                pid = [int] $candidatePid
                depth = [int] $depths[[int] $candidatePid]
            }
        }
    ) | Sort-Object -Property @(
        @{ Expression = 'depth'; Descending = $true },
        @{ Expression = 'pid'; Descending = $true }
    )
    foreach ($item in $stopOrder) {
        Stop-Process -Id ([int] $item.pid) -Force -ErrorAction SilentlyContinue
    }

    $remaining = @{}
    $lastObservations = @{}
    foreach ($candidatePid in $forest.Keys) {
        $remaining[[int] $candidatePid] = $forest[[int] $candidatePid]
    }
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
    do {
        foreach ($candidatePid in @($remaining.Keys)) {
            $candidate = $remaining[[int] $candidatePid]
            $role = [string] $roles[[int] $candidatePid]
            $current = $null
            try {
                $current = Get-CimInstance Win32_Process `
                    -Filter ("ProcessId = {0}" -f ([int] $candidatePid)) `
                    -ErrorAction Stop
            } catch {
                $lastObservations[[int] $candidatePid] = [pscustomobject]@{
                    current_creation = 'unavailable'
                    state = 'query-error'
                }
                continue
            }
            if ($null -eq $current) {
                $remaining.Remove([int] $candidatePid)
                continue
            }

            $creationProperty = $current.PSObject.Properties['CreationDate']
            $currentCreation = 'unavailable'
            $currentTicks = $null
            if ($null -ne $creationProperty -and $null -ne $creationProperty.Value) {
                try {
                    $currentTicks = $current.CreationDate.ToUniversalTime().Ticks
                    $currentCreation = $current.CreationDate.ToUniversalTime().ToString('o')
                } catch { $currentTicks = $null }
            }
            if ($null -eq $currentTicks) {
                $lastObservations[[int] $candidatePid] = [pscustomobject]@{
                    current_creation = $currentCreation
                    state = 'identity-incomplete'
                }
                continue
            }
            $expectedTicks = Get-UtcTicks -Value $candidate.creation_time
            if ($currentTicks -ne $expectedTicks) {
                Write-Warning (
                    'Process cleanup decision role={0}; pid={1}; expected_creation={2}; current_creation={3}; decision=replacement-not-owned' -f
                        $role, [int] $candidatePid, [string] $candidate.creation_time,
                        $currentCreation
                )
                $remaining.Remove([int] $candidatePid)
                continue
            }

            $identityIncomplete = $false
            foreach ($field in @('ProcessId', 'ExecutablePath', 'CommandLine', 'ParentProcessId')) {
                $property = $current.PSObject.Properties[$field]
                if ($null -eq $property -or $null -eq $property.Value) {
                    $identityIncomplete = $true
                    break
                }
            }
            if ($identityIncomplete) {
                $lastObservations[[int] $candidatePid] = [pscustomobject]@{
                    current_creation = $currentCreation
                    state = 'identity-incomplete'
                }
                continue
            }
            if (
                [int] $current.ProcessId -ne [int] $candidate.pid -or
                [IO.Path]::GetFullPath([string] $current.ExecutablePath) -ne [string] $candidate.executable_path -or
                [string] $current.CommandLine -ne [string] $candidate.command_line -or
                [int] $current.ParentProcessId -ne [int] $candidate.parent_pid
            ) {
                Write-Warning (
                    'Process cleanup decision role={0}; pid={1}; expected_creation={2}; current_creation={3}; decision=changed' -f
                        $role, [int] $candidatePid, [string] $candidate.creation_time,
                        $currentCreation
                )
                return $false
            }

            try {
                $parent = Get-CimInstance Win32_Process `
                    -Filter ("ProcessId = {0}" -f ([int] $candidate.parent_pid)) `
                    -ErrorAction Stop
            } catch {
                $lastObservations[[int] $candidatePid] = [pscustomobject]@{
                    current_creation = $currentCreation
                    state = 'query-error'
                }
                continue
            }
            if ($null -eq $parent) {
                $lastObservations[[int] $candidatePid] = [pscustomobject]@{
                    current_creation = $currentCreation
                    state = 'parent-exited'
                }
                continue
            }
            $parentCreation = $parent.PSObject.Properties['CreationDate']
            $parentTicks = $null
            if ($null -ne $parentCreation -and $null -ne $parentCreation.Value) {
                try { $parentTicks = $parent.CreationDate.ToUniversalTime().Ticks } catch { $parentTicks = $null }
            }
            if ($null -eq $parentTicks) {
                $lastObservations[[int] $candidatePid] = [pscustomobject]@{
                    current_creation = $currentCreation
                    state = 'identity-incomplete'
                }
                continue
            }
            if ($parentTicks -ne (Get-UtcTicks -Value $candidate.parent_creation_time)) {
                Write-Warning (
                    'Process cleanup decision role={0}; pid={1}; expected_creation={2}; current_creation={3}; decision=changed' -f
                        $role, [int] $candidatePid, [string] $candidate.creation_time,
                        $currentCreation
                )
                return $false
            }
            $lastObservations[[int] $candidatePid] = [pscustomobject]@{
                current_creation = $currentCreation
                state = 'same-identity'
            }
        }
        if ($remaining.Count -eq 0) { return $true }
        if ([DateTime]::UtcNow -ge $deadline) { break }
        Start-Sleep -Milliseconds $PollIntervalMilliseconds
    } while ($true)

    foreach ($candidatePid in @($remaining.Keys | Sort-Object)) {
        $candidate = $remaining[[int] $candidatePid]
        $observation = $lastObservations[[int] $candidatePid]
        $currentCreation = 'unavailable'
        $state = 'unobserved'
        if ($null -ne $observation) {
            $currentCreation = [string] $observation.current_creation
            $state = [string] $observation.state
        }
        Write-Warning (
            'Process cleanup decision role={0}; pid={1}; expected_creation={2}; current_creation={3}; decision=timeout; state={4}' -f
                [string] $roles[[int] $candidatePid], [int] $candidatePid,
                [string] $candidate.creation_time, $currentCreation, $state
        )
    }
    return $false
}

function Stop-RecordedProcessPair {
    param(
        [object] $LaunchRootRecord,
        [object] $ListenerRecord,
        [int] $TimeoutMilliseconds = 30000,
        [int] $PollIntervalMilliseconds = 100
    )
    try {
        if ($null -ne $LaunchRootRecord) {
            $additional = @()
            $additionalRoles = @()
            if ($null -ne $ListenerRecord) {
                $additional = @($ListenerRecord)
                $additionalRoles = @('listener')
            }
            return Stop-RecordedTree -Record $LaunchRootRecord `
                -AdditionalRecords $additional `
                -RecordRole 'launch-root' -AdditionalRoles $additionalRoles `
                -TimeoutMilliseconds $TimeoutMilliseconds `
                -PollIntervalMilliseconds $PollIntervalMilliseconds
        }
        if ($null -ne $ListenerRecord) {
            return Stop-RecordedTree -Record $ListenerRecord `
                -RecordRole 'listener' `
                -TimeoutMilliseconds $TimeoutMilliseconds `
                -PollIntervalMilliseconds $PollIntervalMilliseconds
        }
        return $true
    } catch {
        Write-Warning 'Process cleanup verification failed; preserving private process, temp, and control evidence'
        return $false
    }
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

function Test-OwnedTempRootCanBeRemoved {
    param(
        [string] $Root,
        [string] $OwnerMarker,
        [string] $ExpectedRunId,
        [string] $ResolvedOutputRoot
    )
    try {
        $rootExists = Test-Path -LiteralPath $Root
        $resolvedRoot = if ($rootExists) {
            (Resolve-Path -LiteralPath $Root).Path
        } else {
            [IO.Path]::GetFullPath($Root)
        }
        $prefix = $ResolvedOutputRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) +
            [IO.Path]::DirectorySeparatorChar
        if (-not $resolvedRoot.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
        if (-not (Test-Path -LiteralPath $OwnerMarker -PathType Leaf)) {
            return -not $rootExists
        }
        $owner = Get-Content -LiteralPath $OwnerMarker -Raw | ConvertFrom-Json
        $ownerNames = @($owner.PSObject.Properties.Name)
        if (
            $ownerNames.Count -ne 4 -or
            @($ownerNames | Where-Object {
                $_ -notin @('run_id', 'temp_root', 'runner_temp_root', 'comfy_temp_root')
            }).Count -ne 0
        ) { return $false }
        return (
            [string] $owner.run_id -ceq $ExpectedRunId -and
            [string] $owner.temp_root -ceq $resolvedRoot
        )
    } catch {
        return $false
    }
}

function Complete-LauncherFailureState {
    param([object] $PrimaryFailure, [object] $CleanupFailure)
    if ($null -ne $PrimaryFailure) { throw $PrimaryFailure }
    if ($null -ne $CleanupFailure) {
        throw 'Windows reliability cleanup verification failed'
    }
}

function Invoke-ReliabilityValidator {
    param(
        [string] $PythonPath,
        [string[]] $ValidatorArguments,
        [string] $WorkingDirectory
    )
    Push-Location $WorkingDirectory
    try {
        & $PythonPath @ValidatorArguments
        $validatorExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($validatorExitCode -ne 0) {
        throw 'Windows ComfyUI reliability gate failed'
    }
}

function Get-Sha256Hex {
    param([AllowEmptyString()] [string] $Value)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
        return -join @($algorithm.ComputeHash($bytes) | ForEach-Object { $_.ToString('X2') })
    } finally {
        $algorithm.Dispose()
    }
}

function ConvertTo-PublicUtcTimestamp {
    param([AllowNull()] [object] $Value)
    if ($null -eq $Value) { return $null }
    try {
        $timestamp = [DateTimeOffset]::Parse(
            [string] $Value,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        ).ToUniversalTime()
    } catch {
        throw 'Public launcher lifecycle timestamp is invalid'
    }
    return $timestamp.ToString(
        'yyyy-MM-ddTHH:mm:ss.ffffffZ',
        [Globalization.CultureInfo]::InvariantCulture
    )
}

function Assert-ExactPublicProperties {
    param([object] $Value, [string[]] $Expected)
    if ($null -eq $Value) { throw 'Public launcher lifecycle object is missing' }
    $actual = @($Value.PSObject.Properties.Name)
    if (
        $actual.Count -ne $Expected.Count -or
        @($actual | Where-Object { $_ -notin $Expected }).Count -ne 0
    ) { throw 'Public launcher lifecycle property set is invalid' }
}

function Test-PublicSha256Value {
    param([object] $Value)
    return (
        $Value -is [string] -and
        ([string] $Value).Length -eq 64 -and
        [string] $Value -cmatch '^[0-9A-F]{64}$'
    )
}

function Test-PublicUtcTimestampValue {
    param([AllowNull()] [object] $Value, [bool] $AllowNull = $false)
    if ($null -eq $Value) { return $AllowNull }
    if ($Value -isnot [string] -or ([string] $Value).Length -ne 27) { return $false }
    if ([string] $Value -cnotmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$') {
        return $false
    }
    try {
        $parsed = [DateTimeOffset]::ParseExact(
            [string] $Value,
            'yyyy-MM-ddTHH:mm:ss.ffffffZ',
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::AssumeUniversal
        )
        return $parsed.Offset -eq [TimeSpan]::Zero
    } catch {
        return $false
    }
}

function New-PublicProcessCommitment {
    param(
        [ValidateSet('tts-more', 'comfyui')] [string] $Role,
        [ValidateSet('launch-root', 'listener')] [string] $Kind,
        [object] $Record
    )
    if ($null -eq $Record) { throw 'Public launcher lifecycle process identity is missing' }
    $pid = [int64] $Record.pid
    $parentPid = [int64] $Record.parent_pid
    if ($pid -lt 1 -or $pid -gt [int]::MaxValue -or
        $parentPid -lt 0 -or $parentPid -gt [int]::MaxValue) {
        throw 'Public launcher lifecycle process identifier is invalid'
    }
    $created = ConvertTo-PublicUtcTimestamp -Value $Record.creation_time
    $parentCreated = ConvertTo-PublicUtcTimestamp -Value $Record.parent_creation_time
    $canonicalPrivateIdentity = @(
        [string] $pid,
        $created,
        [string] $Record.executable_path,
        [string] $Record.command_line,
        [string] $parentPid,
        $parentCreated
    ) | ForEach-Object { '{0}:{1}' -f ([string] $_).Length, [string] $_ }
    return [ordered]@{
        role = $Role
        kind = $Kind
        pid = [int] $pid
        creation_time_utc = $created
        parent_pid = [int] $parentPid
        parent_creation_time_utc = $parentCreated
        identity_sha256 = Get-Sha256Hex -Value ($canonicalPrivateIdentity -join '|')
    }
}

function New-LauncherFailureLifecycleDocument {
    param(
        [string] $RunId,
        [string] $PrimaryCode,
        [string] $PrimaryStage,
        [object] $RunStartedAt,
        [AllowNull()] [string] $CaseId,
        [AllowNull()] [object] $CaseStartedAt,
        [AllowNull()] [object] $CaseFinishedAt,
        [hashtable] $LaunchRoots,
        [hashtable] $Listeners
    )
    $processes = [System.Collections.Generic.List[object]]::new()
    foreach ($role in @('tts-more', 'comfyui')) {
        if ($null -ne $LaunchRoots[$role]) {
            $processes.Add((New-PublicProcessCommitment `
                -Role $role -Kind 'launch-root' -Record $LaunchRoots[$role]))
        }
        if ($null -ne $Listeners[$role]) {
            $processes.Add((New-PublicProcessCommitment `
                -Role $role -Kind 'listener' -Record $Listeners[$role]))
        }
    }
    if ($processes.Count -lt 1 -or $processes.Count -gt 8) {
        throw 'Public launcher lifecycle process count is invalid'
    }
    $ownershipRows = @($processes | ForEach-Object {
        '{0}|{1}|{2}|{3}|{4}|{5}' -f $_.role, $_.kind, $_.pid,
            $_.creation_time_utc, $_.parent_pid, $_.identity_sha256
    })
    $caseHash = if ([string]::IsNullOrEmpty($CaseId)) {
        $null
    } else {
        Get-Sha256Hex -Value $CaseId
    }
    return [ordered]@{
        schema_version = [int] 1
        kind = 'launcher-failure-lifecycle'
        status = 'failed'
        run_id_sha256 = Get-Sha256Hex -Value $RunId
        primary = [ordered]@{
            code = $PrimaryCode
            stage = $PrimaryStage
        }
        case = [ordered]@{
            case_id_sha256 = $caseHash
            started_at = ConvertTo-PublicUtcTimestamp -Value $CaseStartedAt
            finished_at = ConvertTo-PublicUtcTimestamp -Value $CaseFinishedAt
        }
        timestamps = [ordered]@{
            run_started_at = ConvertTo-PublicUtcTimestamp -Value $RunStartedAt
            snapshot_written_at = ConvertTo-PublicUtcTimestamp -Value ([DateTime]::UtcNow)
            cleanup_finished_at = $null
        }
        processes = @($processes)
        promotion_ownership_sha256 = Get-Sha256Hex -Value ($ownershipRows -join "`n")
        cleanup = [ordered]@{
            state = 'snapshot-written'
            snapshot_written = $true
            stop_attempted = $false
            stop_proven = $false
            temp_removal_eligible = $false
            raw_disposition = [ordered]@{
                temp_root = 'preserved-pending-cleanup'
                owner_marker = 'preserved-pending-cleanup'
                control_record = 'preserved-pending-cleanup'
                host_manifest = 'preserved-pending-cleanup'
            }
            warning_sha256 = @()
            secondary_sha256 = @()
        }
    }
}

function Assert-LauncherFailureLifecycleDocument {
    param([object] $Document)
    Assert-ExactPublicProperties -Value $Document -Expected @(
        'schema_version', 'kind', 'status', 'run_id_sha256', 'primary', 'case',
        'timestamps', 'processes', 'promotion_ownership_sha256', 'cleanup'
    )
    if ($Document.schema_version.GetType() -ne [int] -or $Document.schema_version -ne 1) {
        throw 'Public launcher lifecycle version is invalid'
    }
    if ([string] $Document.kind -cne 'launcher-failure-lifecycle' -or
        [string] $Document.status -cne 'failed') {
        throw 'Public launcher lifecycle kind or status is invalid'
    }
    if (-not (Test-PublicSha256Value $Document.run_id_sha256) -or
        -not (Test-PublicSha256Value $Document.promotion_ownership_sha256)) {
        throw 'Public launcher lifecycle commitment is invalid'
    }
    Assert-ExactPublicProperties -Value $Document.primary -Expected @('code', 'stage')
    foreach ($field in @('code', 'stage')) {
        $value = $Document.primary.$field
        if ($value -isnot [string] -or ([string] $value).Length -lt 1 -or
            ([string] $value).Length -gt 64 -or
            [string] $value -cnotmatch '^[a-z][a-z0-9-]*$') {
            throw 'Public launcher lifecycle primary classification is invalid'
        }
    }
    Assert-ExactPublicProperties -Value $Document.case -Expected @(
        'case_id_sha256', 'started_at', 'finished_at'
    )
    if ($null -ne $Document.case.case_id_sha256 -and
        -not (Test-PublicSha256Value $Document.case.case_id_sha256)) {
        throw 'Public launcher lifecycle case commitment is invalid'
    }
    foreach ($field in @('started_at', 'finished_at')) {
        if (-not (Test-PublicUtcTimestampValue -Value $Document.case.$field -AllowNull $true)) {
            throw 'Public launcher lifecycle case timestamp is invalid'
        }
    }
    if ($null -ne $Document.case.finished_at -and $null -eq $Document.case.started_at) {
        throw 'Public launcher lifecycle case timestamp order is invalid'
    }
    if ($null -ne $Document.case.started_at -and $null -ne $Document.case.finished_at -and
        (Get-UtcTicks $Document.case.finished_at) -lt (Get-UtcTicks $Document.case.started_at)) {
        throw 'Public launcher lifecycle case timestamp order is invalid'
    }
    Assert-ExactPublicProperties -Value $Document.timestamps -Expected @(
        'run_started_at', 'snapshot_written_at', 'cleanup_finished_at'
    )
    foreach ($field in @('run_started_at', 'snapshot_written_at')) {
        if (-not (Test-PublicUtcTimestampValue -Value $Document.timestamps.$field)) {
            throw 'Public launcher lifecycle run timestamp is invalid'
        }
    }
    if (-not (Test-PublicUtcTimestampValue `
            -Value $Document.timestamps.cleanup_finished_at -AllowNull $true)) {
        throw 'Public launcher lifecycle cleanup timestamp is invalid'
    }
    $processes = @($Document.processes)
    if ($processes.Count -lt 1 -or $processes.Count -gt 8) {
        throw 'Public launcher lifecycle process list is invalid'
    }
    $processKeys = @{}
    foreach ($process in $processes) {
        Assert-ExactPublicProperties -Value $process -Expected @(
            'role', 'kind', 'pid', 'creation_time_utc', 'parent_pid',
            'parent_creation_time_utc', 'identity_sha256'
        )
        if ([string] $process.role -cnotin @('tts-more', 'comfyui') -or
            [string] $process.kind -cnotin @('launch-root', 'listener')) {
            throw 'Public launcher lifecycle process role is invalid'
        }
        if ($process.pid.GetType() -ne [int] -or $process.pid -lt 1 -or
            $process.parent_pid.GetType() -ne [int] -or $process.parent_pid -lt 0) {
            throw 'Public launcher lifecycle process identifier is invalid'
        }
        if (-not (Test-PublicUtcTimestampValue $process.creation_time_utc) -or
            -not (Test-PublicUtcTimestampValue $process.parent_creation_time_utc) -or
            -not (Test-PublicSha256Value $process.identity_sha256)) {
            throw 'Public launcher lifecycle process commitment is invalid'
        }
        $processKey = '{0}|{1}' -f $process.role, $process.kind
        if ($processKeys.ContainsKey($processKey)) {
            throw 'Public launcher lifecycle process role is duplicated'
        }
        $processKeys[$processKey] = $true
    }
    Assert-ExactPublicProperties -Value $Document.cleanup -Expected @(
        'state', 'snapshot_written', 'stop_attempted', 'stop_proven',
        'temp_removal_eligible', 'raw_disposition', 'warning_sha256',
        'secondary_sha256'
    )
    if ([string] $Document.cleanup.state -cnotin @(
        'snapshot-written', 'cleanup-unproven', 'cleanup-proven'
    )) { throw 'Public launcher lifecycle cleanup state is invalid' }
    foreach ($field in @(
        'snapshot_written', 'stop_attempted', 'stop_proven', 'temp_removal_eligible'
    )) {
        if ($Document.cleanup.$field.GetType() -ne [bool]) {
            throw 'Public launcher lifecycle cleanup flag is invalid'
        }
    }
    if (-not $Document.cleanup.snapshot_written) {
        throw 'Public launcher lifecycle snapshot flag is invalid'
    }
    Assert-ExactPublicProperties -Value $Document.cleanup.raw_disposition -Expected @(
        'temp_root', 'owner_marker', 'control_record', 'host_manifest'
    )
    foreach ($value in @($Document.cleanup.raw_disposition.PSObject.Properties.Value)) {
        if ([string] $value -cnotin @(
            'preserved-pending-cleanup', 'preserved', 'removal-committed'
        )) { throw 'Public launcher lifecycle raw disposition is invalid' }
    }
    foreach ($field in @('warning_sha256', 'secondary_sha256')) {
        $hashes = @($Document.cleanup.$field)
        if ($hashes.Count -gt 32) {
            throw 'Public launcher lifecycle diagnostic list is invalid'
        }
        foreach ($hash in $hashes) {
            if (-not (Test-PublicSha256Value $hash)) {
                throw 'Public launcher lifecycle diagnostic commitment is invalid'
            }
        }
    }
}

function Write-LauncherFailureLifecycleAtomic {
    param([string] $Path, [object] $Document)
    $normalized = $Document | ConvertTo-Json -Depth 12 | ConvertFrom-Json
    Assert-LauncherFailureLifecycleDocument -Document $normalized
    $temporaryPath = '{0}.{1}.tmp' -f $Path, [Guid]::NewGuid().ToString('N')
    $backupPath = '{0}.{1}.bak' -f $Path, [Guid]::NewGuid().ToString('N')
    try {
        $normalized | ConvertTo-Json -Depth 12 |
            Set-Content -LiteralPath $temporaryPath -Encoding UTF8 -ErrorAction Stop
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [IO.File]::Replace($temporaryPath, $Path, $backupPath)
            if (Test-Path -LiteralPath $backupPath) {
                Remove-Item -LiteralPath $backupPath -Force -ErrorAction Stop
            }
        } else {
            [IO.File]::Move($temporaryPath, $Path)
        }
        $roundTrip = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json
        Assert-LauncherFailureLifecycleDocument -Document $roundTrip
    } finally {
        foreach ($artifact in @($temporaryPath, $backupPath)) {
            if (Test-Path -LiteralPath $artifact -PathType Leaf) {
                Remove-Item -LiteralPath $artifact -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

function Assert-LauncherLifecycleSecondaryMarkerDocument {
    param([object] $Document)
    Assert-ExactPublicProperties -Value $Document -Expected @(
        'schema_version', 'kind', 'status', 'run_id_sha256',
        'lifecycle_write_failed', 'raw_removal_failed', 'secondary_sha256'
    )
    if ($Document.schema_version.GetType() -ne [int] -or $Document.schema_version -ne 1 -or
        [string] $Document.kind -cne 'launcher-failure-lifecycle-secondary' -or
        [string] $Document.status -cne 'failed') {
        throw 'Public launcher lifecycle secondary classification is invalid'
    }
    if (-not (Test-PublicSha256Value $Document.run_id_sha256) -or
        $Document.lifecycle_write_failed.GetType() -ne [bool] -or
        $Document.raw_removal_failed.GetType() -ne [bool] -or
        (-not $Document.lifecycle_write_failed -and -not $Document.raw_removal_failed)) {
        throw 'Public launcher lifecycle secondary flag is invalid'
    }
    $hashes = @($Document.secondary_sha256)
    if ($hashes.Count -lt 1 -or $hashes.Count -gt 32) {
        throw 'Public launcher lifecycle secondary list is invalid'
    }
    foreach ($hash in $hashes) {
        if (-not (Test-PublicSha256Value $hash)) {
            throw 'Public launcher lifecycle secondary commitment is invalid'
        }
    }
}

function Write-LauncherLifecycleSecondaryMarkerAtomic {
    param(
        [string] $Path,
        [string] $RunIdSha256,
        [string[]] $SecondarySha256,
        [bool] $LifecycleWriteFailed,
        [bool] $RawRemovalFailed
    )
    if (-not (Test-PublicSha256Value $RunIdSha256)) {
        throw 'Public launcher lifecycle secondary run commitment is invalid'
    }
    $hashes = @($SecondarySha256 | Select-Object -Unique | Select-Object -First 32)
    foreach ($hash in $hashes) {
        if (-not (Test-PublicSha256Value $hash)) {
            throw 'Public launcher lifecycle secondary commitment is invalid'
        }
    }
    $document = [ordered]@{
        schema_version = [int] 1
        kind = 'launcher-failure-lifecycle-secondary'
        status = 'failed'
        run_id_sha256 = $RunIdSha256
        lifecycle_write_failed = [bool] $LifecycleWriteFailed
        raw_removal_failed = [bool] $RawRemovalFailed
        secondary_sha256 = $hashes
    }
    $normalized = $document | ConvertTo-Json -Depth 5 | ConvertFrom-Json
    Assert-LauncherLifecycleSecondaryMarkerDocument -Document $normalized
    $temporaryPath = '{0}.{1}.tmp' -f $Path, [Guid]::NewGuid().ToString('N')
    try {
        $normalized | ConvertTo-Json -Depth 5 |
            Set-Content -LiteralPath $temporaryPath -Encoding UTF8 -ErrorAction Stop
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [IO.File]::Replace($temporaryPath, $Path, $null)
        } else {
            [IO.File]::Move($temporaryPath, $Path)
        }
        $roundTrip = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop |
            ConvertFrom-Json
        Assert-LauncherLifecycleSecondaryMarkerDocument -Document $roundTrip
    } finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Invoke-LauncherCleanupTransaction {
    param(
        [bool] $PublishFailureLifecycle,
        [string] $LifecyclePath,
        [string] $SecondaryPath,
        [object] $LifecycleDocument,
        [scriptblock] $CleanupProofOperation,
        [scriptblock] $RawRemovalOperation
    )
    $initialSnapshotWritten = $false
    $cleanupProven = $false
    $rawRemovalAttempted = $false
    $cleanupFailure = $null
    $secondaryHashes = [System.Collections.Generic.List[string]]::new()
    if ($PublishFailureLifecycle) {
        try {
            Write-LauncherFailureLifecycleAtomic `
                -Path $LifecyclePath -Document $LifecycleDocument
            $initialSnapshotWritten = $true
        } catch {
            $cleanupFailure = $_
            $secondaryHashes.Add((Get-Sha256Hex -Value ([string] $_.Exception.Message)))
            try {
                Write-LauncherLifecycleSecondaryMarkerAtomic `
                    -Path $SecondaryPath `
                    -RunIdSha256 $LifecycleDocument.run_id_sha256 `
                    -SecondarySha256 @($secondaryHashes) `
                    -LifecycleWriteFailed $true -RawRemovalFailed $false
            } catch {
                Write-Warning 'Launcher lifecycle secondary evidence could not be published'
            }
        }
    }
    $proof = $null
    try {
        $proof = & $CleanupProofOperation $PublishFailureLifecycle
        if ($null -eq $proof) { throw 'Launcher cleanup proof result is missing' }
    } catch {
        if ($null -eq $cleanupFailure) { $cleanupFailure = $_ }
        $secondaryHashes.Add((Get-Sha256Hex -Value ([string] $_.Exception.Message)))
        $proof = [pscustomobject]@{
            stop_attempted = $false
            stop_proven = $false
            temp_removal_eligible = $false
            warning_sha256 = @()
            secondary_sha256 = @()
        }
    }
    $cleanupProven = (
        $proof.stop_attempted -is [bool] -and $proof.stop_attempted -and
        $proof.stop_proven -is [bool] -and $proof.stop_proven -and
        $proof.temp_removal_eligible -is [bool] -and $proof.temp_removal_eligible
    )
    foreach ($hash in @($proof.secondary_sha256)) {
        if (Test-PublicSha256Value $hash) { $secondaryHashes.Add([string] $hash) }
    }
    if ($PublishFailureLifecycle -and $initialSnapshotWritten) {
        $LifecycleDocument.timestamps.cleanup_finished_at =
            ConvertTo-PublicUtcTimestamp -Value ([DateTime]::UtcNow)
        $LifecycleDocument.cleanup.stop_attempted = [bool] $proof.stop_attempted
        $LifecycleDocument.cleanup.stop_proven = [bool] $proof.stop_proven
        $LifecycleDocument.cleanup.temp_removal_eligible =
            [bool] $proof.temp_removal_eligible
        $LifecycleDocument.cleanup.warning_sha256 = @(
            @($proof.warning_sha256) |
                Where-Object { Test-PublicSha256Value $_ } |
                Select-Object -Unique |
                Select-Object -First 32
        )
        $LifecycleDocument.cleanup.secondary_sha256 = @(
            @($secondaryHashes) | Select-Object -Unique | Select-Object -First 32
        )
        $disposition = if ($cleanupProven) { 'removal-committed' } else { 'preserved' }
        $LifecycleDocument.cleanup.state = if ($cleanupProven) {
            'cleanup-proven'
        } else {
            'cleanup-unproven'
        }
        foreach ($field in @('temp_root', 'owner_marker', 'control_record', 'host_manifest')) {
            $LifecycleDocument.cleanup.raw_disposition[$field] = $disposition
        }
        try {
            Write-LauncherFailureLifecycleAtomic `
                -Path $LifecyclePath -Document $LifecycleDocument
        } catch {
            $cleanupProven = $false
            if ($null -eq $cleanupFailure) { $cleanupFailure = $_ }
            $secondaryHashes.Add((Get-Sha256Hex -Value ([string] $_.Exception.Message)))
            try {
                Write-LauncherLifecycleSecondaryMarkerAtomic `
                    -Path $SecondaryPath `
                    -RunIdSha256 $LifecycleDocument.run_id_sha256 `
                    -SecondarySha256 @($secondaryHashes) `
                    -LifecycleWriteFailed $true -RawRemovalFailed $false
            } catch {
                Write-Warning 'Launcher lifecycle secondary evidence could not be published'
            }
        }
    }
    $removalAuthorized = if ($PublishFailureLifecycle) {
        $initialSnapshotWritten -and $cleanupProven
    } else {
        $cleanupProven
    }
    if ($removalAuthorized) {
        $rawRemovalAttempted = $true
        try {
            & $RawRemovalOperation $proof
        } catch {
            if ($null -eq $cleanupFailure) { $cleanupFailure = $_ }
            $secondaryHashes.Add((Get-Sha256Hex -Value ([string] $_.Exception.Message)))
            if ($PublishFailureLifecycle) {
                try {
                    Write-LauncherLifecycleSecondaryMarkerAtomic `
                        -Path $SecondaryPath `
                        -RunIdSha256 $LifecycleDocument.run_id_sha256 `
                        -SecondarySha256 @($secondaryHashes) `
                        -LifecycleWriteFailed $false -RawRemovalFailed $true
                } catch {
                    Write-Warning 'Launcher lifecycle secondary evidence could not be published'
                }
            }
        }
    } elseif (-not $cleanupProven -and $null -eq $cleanupFailure) {
        try { throw 'Launcher cleanup proof did not converge' } catch { $cleanupFailure = $_ }
    }
    return [pscustomobject]@{
        cleanup_proven = [bool] $cleanupProven
        raw_removal_attempted = [bool] $rawRemovalAttempted
        cleanup_failure = $cleanupFailure
    }
}

function Get-LauncherPublicFailureContext {
    param([string] $OutputRoot)
    $code = 'launcher-validation-failed'
    $stage = 'validator'
    $failurePath = Join-Path $OutputRoot 'failure.json'
    if (Test-Path -LiteralPath $failurePath -PathType Leaf) {
        try {
            $failure = Get-Content -LiteralPath $failurePath -Raw | ConvertFrom-Json
            foreach ($field in @('code', 'stage')) {
                $candidate = [string] $failure.$field
                if (
                    $candidate.Length -lt 1 -or $candidate.Length -gt 64 -or
                    $candidate -cnotmatch '^[a-z][a-z0-9-]*$'
                ) { throw 'Public failure classification is invalid' }
            }
            $code = [string] $failure.code
            $stage = [string] $failure.stage
        } catch {
            $code = 'launcher-validation-failed'
            $stage = 'validator'
        }
    }
    $caseId = $null
    $caseStartedAt = $null
    $caseFinishedAt = $null
    $caseRoot = Join-Path $OutputRoot 'cases'
    if (Test-Path -LiteralPath $caseRoot -PathType Container) {
        try {
            $caseFile = Get-ChildItem -LiteralPath $caseRoot -Filter '*.json' -File |
                Sort-Object -Property LastWriteTimeUtc, Name |
                Select-Object -Last 1
            if ($null -ne $caseFile) {
                $caseDocument = Get-Content -LiteralPath $caseFile.FullName -Raw |
                    ConvertFrom-Json
                $candidateId = [string] $caseDocument.case_id
                if ($candidateId.Length -ge 1 -and $candidateId.Length -le 128) {
                    $caseId = $candidateId
                }
                $host = $caseDocument.PSObject.Properties['host']
                if ($null -ne $host -and $null -ne $host.Value) {
                    try {
                        $caseStartedAt = ConvertTo-PublicUtcTimestamp `
                            -Value $host.Value.started_at
                        $caseFinishedAt = ConvertTo-PublicUtcTimestamp `
                            -Value $host.Value.finished_at
                    } catch {
                        $caseStartedAt = $null
                        $caseFinishedAt = $null
                    }
                }
            }
        } catch {
            $caseId = $null
            $caseStartedAt = $null
            $caseFinishedAt = $null
        }
    }
    return [pscustomobject]@{
        code = $code
        stage = $stage
        case_id = $caseId
        case_started_at = $caseStartedAt
        case_finished_at = $caseFinishedAt
    }
}

$fixturePath = Resolve-ExistingPath -LiteralPath $Fixture -Kind File
$comfyRootPath = Resolve-ExistingPath -LiteralPath $ComfyUiRoot -Kind Directory
$comfyPythonPath = Resolve-ExistingPath -LiteralPath $ComfyPython -Kind File
$ttsRootPath = Resolve-ExistingPath -LiteralPath $TtsMoreRoot -Kind Directory
$backendRootPath = Resolve-ExistingPath -LiteralPath (Join-Path $ttsRootPath 'backend') -Kind Directory
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

$runStartedAt = [DateTime]::UtcNow
$runId = [Guid]::NewGuid().ToString('N')
$runIdSha256 = Get-Sha256Hex -Value $runId
$launcherLifecyclePath = Join-Path $outputRootPath (
    'launcher-failure-lifecycle-{0}.json' -f $runIdSha256
)
$launcherLifecycleSecondaryPath = Join-Path $outputRootPath (
    'launcher-failure-lifecycle-secondary-{0}.json' -f $runIdSha256
)
$comfyStdoutPath = Join-Path $outputRootPath (".comfyui-{0}.stdout.log" -f $runId)
$comfyStderrPath = Join-Path $outputRootPath (".comfyui-{0}.stderr.log" -f $runId)
$backendStdoutPath = Join-Path $outputRootPath (".tts-more-{0}.stdout.log" -f $runId)
$backendStderrPath = Join-Path $outputRootPath (".tts-more-{0}.stderr.log" -f $runId)
foreach ($sidecarPath in @(
    $comfyStdoutPath, $comfyStderrPath, $backendStdoutPath, $backendStderrPath
)) {
    New-Item -ItemType File -Path $sidecarPath -ErrorAction Stop | Out-Null
}
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
$comfyLaunchRootRecord = $null
$backendLaunchRootRecord = $null
$startedProcesses = [System.Collections.Generic.List[object]]::new()
$provisionalCleanupFailed = $false
$primaryFailure = $null
$cleanupFailure = $null
$formalValidatorInvoked = $false
try {
    $launcherRecord = Get-ProcessRecord -ProcessId $PID
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
        -BackendRecord $backendRecord -ComfyRecord $comfyRecord `
        -BackendLaunchRootRecord $backendLaunchRootRecord `
        -ComfyLaunchRootRecord $comfyLaunchRootRecord `
        -StandardOutputPath $comfyStdoutPath -StandardErrorPath $comfyStderrPath `
        -ProvisionalCleanupFailed ([ref] $provisionalCleanupFailed)
    $comfyProcess = $comfyStart.process
    try {
        $comfyRecord = Wait-ProcessRecord -ProcessId $comfyProcess.Id
        $comfyLaunchRootRecord = $comfyRecord
        Write-RunControlState -Path $controlStatePath -RunId $runId `
            -BackendRecord $backendRecord -ComfyRecord $comfyRecord `
            -BackendLaunchRootRecord $backendLaunchRootRecord `
            -ComfyLaunchRootRecord $comfyLaunchRootRecord
    } catch {
        $startupFailure = $_
        Complete-ProvisionalStartupFailure `
            -PrimaryFailure $startupFailure -Token $comfyStart.token `
            -CleanupFailed ([ref] $provisionalCleanupFailed) `
            -UnprovedWarning 'ComfyUI provisional process cleanup was not proven; preserving startup evidence'
    }
    $comfyListenerRecord = Wait-ExactPortOwner -Port 8188 -ProcessId $comfyProcess.Id `
        -Process $comfyProcess -LaunchRecord $comfyLaunchRootRecord `
        -StartedAfter $comfyStart.token.started_after
    Write-ListenerRunControlState -Path $controlStatePath -RunId $runId `
        -ProcessLabel 'comfyui' -LaunchRootRecord $comfyLaunchRootRecord `
        -ListenerRecord $comfyListenerRecord -BackendRecord $backendRecord `
        -ComfyRecord $comfyRecord -BackendLaunchRootRecord $backendLaunchRootRecord `
        -ComfyLaunchRootRecord $comfyLaunchRootRecord
    $comfyRecord = $comfyListenerRecord

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
        -BackendRecord $backendRecord -ComfyRecord $comfyRecord `
        -BackendLaunchRootRecord $backendLaunchRootRecord `
        -ComfyLaunchRootRecord $comfyLaunchRootRecord `
        -StandardOutputPath $backendStdoutPath -StandardErrorPath $backendStderrPath `
        -ProvisionalCleanupFailed ([ref] $provisionalCleanupFailed)
    $backendProcess = $backendStart.process
    try {
        $backendRecord = Wait-ProcessRecord -ProcessId $backendProcess.Id
        $backendLaunchRootRecord = $backendRecord
        Write-RunControlState -Path $controlStatePath -RunId $runId `
            -BackendRecord $backendRecord -ComfyRecord $comfyRecord `
            -BackendLaunchRootRecord $backendLaunchRootRecord `
            -ComfyLaunchRootRecord $comfyLaunchRootRecord
    } catch {
        $startupFailure = $_
        Complete-ProvisionalStartupFailure `
            -PrimaryFailure $startupFailure -Token $backendStart.token `
            -CleanupFailed ([ref] $provisionalCleanupFailed) `
            -UnprovedWarning 'TTS More provisional process cleanup was not proven; preserving startup evidence'
    }
    $backendListenerRecord = Wait-ExactPortOwner -Port 8000 -ProcessId $backendProcess.Id `
        -Process $backendProcess -LaunchRecord $backendLaunchRootRecord `
        -StartedAfter $backendStart.token.started_after
    Write-ListenerRunControlState -Path $controlStatePath -RunId $runId `
        -ProcessLabel 'tts-more' -LaunchRootRecord $backendLaunchRootRecord `
        -ListenerRecord $backendListenerRecord -BackendRecord $backendRecord `
        -ComfyRecord $comfyRecord -BackendLaunchRootRecord $backendLaunchRootRecord `
        -ComfyLaunchRootRecord $comfyLaunchRootRecord
    $backendRecord = $backendListenerRecord

    $hostManifest = [ordered]@{
        version = 1
        run_id = $runId
        owned_processes = [ordered]@{
            'tts-more' = $backendRecord
            comfyui = $comfyRecord
        }
        launch_roots = [ordered]@{
            'tts-more' = $backendLaunchRootRecord
            comfyui = $comfyLaunchRootRecord
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
        '--comfyui-pid', [string] $comfyRecord.pid,
        '--tts-more-pid', [string] $backendRecord.pid
    )
    if ($AllowLan) { $pythonArguments += '--allow-lan' }
    if ($PreflightOnly) { $pythonArguments += '--preflight-only' }
    $formalValidatorInvoked = $true
    Invoke-ReliabilityValidator -PythonPath $backendPythonPath `
        -ValidatorArguments $pythonArguments -WorkingDirectory $backendRootPath
} catch {
    $primaryFailure = $_
} finally {
    try {
        # Python publishes public evidence before returning. Cleanup then touches
        # only identities and temp paths created and revalidated by this run.
        $latestBackendRecord = $backendRecord
        $latestComfyRecord = $comfyRecord
        $latestBackendLaunchRootRecord = $backendLaunchRootRecord
        $latestComfyLaunchRootRecord = $comfyLaunchRootRecord
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
                    $controlLaunchRoots = $controlState.PSObject.Properties['launch_roots']
                    if ($null -eq $controlLaunchRoots) {
                        $latestBackendLaunchRootRecord = $latestBackendRecord
                        $latestComfyLaunchRootRecord = $latestComfyRecord
                    } else {
                        $latestBackendLaunchRootRecord = $controlLaunchRoots.Value.'tts-more'
                        $latestComfyLaunchRootRecord = $controlLaunchRoots.Value.comfyui
                    }
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
                        $launchRootRecord = if ($role -eq 'tts-more') {
                            $latestBackendLaunchRootRecord
                        } else {
                            $latestComfyLaunchRootRecord
                        }
                        if (
                            $null -ne $fullRecord -or
                            $null -ne $launchRootRecord -or
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
        $publishFailureLifecycle = [bool] (
            $formalValidatorInvoked -and $null -ne $primaryFailure
        )
        $lifecycleDocument = $null
        if ($publishFailureLifecycle) {
            try {
                $publicFailureContext = Get-LauncherPublicFailureContext `
                    -OutputRoot $outputRootPath
                $lifecycleDocument = New-LauncherFailureLifecycleDocument `
                    -RunId $runId `
                    -PrimaryCode $publicFailureContext.code `
                    -PrimaryStage $publicFailureContext.stage `
                    -RunStartedAt $runStartedAt `
                    -CaseId $publicFailureContext.case_id `
                    -CaseStartedAt $publicFailureContext.case_started_at `
                    -CaseFinishedAt $publicFailureContext.case_finished_at `
                    -LaunchRoots @{
                        'tts-more' = $latestBackendLaunchRootRecord
                        comfyui = $latestComfyLaunchRootRecord
                    } `
                    -Listeners @{
                        'tts-more' = $latestBackendRecord
                        comfyui = $latestComfyRecord
                    }
            } catch {
                # A deliberately invalid minimal value forces the first public
                # gate to fail while retaining the run commitment needed by
                # the separate secondary marker. Cleanup proof still runs.
                $lifecycleDocument = [ordered]@{
                    run_id_sha256 = $runIdSha256
                }
            }
        }
        $cleanupProofOperation = {
            param([bool] $PreserveRaw)
            $operationProcessCleanupProven = [bool] $processCleanupProven
            if ($controlStateValid) {
                foreach ($role in @('tts-more', 'comfyui')) {
                    $fullRecord = if ($role -eq 'tts-more') {
                        $latestBackendRecord
                    } else {
                        $latestComfyRecord
                    }
                    $launchRootRecord = if ($role -eq 'tts-more') {
                        $latestBackendLaunchRootRecord
                    } else {
                        $latestComfyLaunchRootRecord
                    }
                    $provisionalRecord = $provisionalRecords[$role]
                    $launchIntent = $launchIntents[$role]
                    if ($null -ne $fullRecord) {
                        if ($null -ne $provisionalRecord -or $null -ne $launchIntent) {
                            Write-Warning 'Process control state contains conflicting full and provisional identities'
                            $operationProcessCleanupProven = $false
                            continue
                        }
                        if (-not (Stop-RecordedProcessPair `
                                -LaunchRootRecord $launchRootRecord `
                                -ListenerRecord $fullRecord)) {
                            $operationProcessCleanupProven = $false
                        }
                        continue
                    }
                    if ($null -ne $launchRootRecord) {
                        if (-not (Stop-RecordedProcessPair `
                                -LaunchRootRecord $launchRootRecord `
                                -ListenerRecord $null)) {
                            $operationProcessCleanupProven = $false
                        }
                    }
                    if ($null -ne $provisionalRecord) {
                        if ($null -eq $launchIntent) {
                            Write-Warning 'Provisional process identity is missing its launch intent'
                            $operationProcessCleanupProven = $false
                        } elseif (-not (Stop-ProvisionalStartedProcess -Token $provisionalRecord)) {
                            $operationProcessCleanupProven = $false
                        }
                        continue
                    }
                    if ($null -ne $launchIntent) {
                        try {
                            $intentRecord = Resolve-LaunchIntentProcess -Intent $launchIntent
                            if (
                                $null -ne $intentRecord -and
                                -not (Stop-RecordedTree -Record $intentRecord)
                            ) { $operationProcessCleanupProven = $false }
                        } catch {
                            Write-Warning 'Launch intent could not be resolved uniquely; preserving recovery records'
                            $operationProcessCleanupProven = $false
                        }
                    }
                }
            } else {
                $operationProcessCleanupProven = $false
            }
            $tempRemovalEligible = $false
            if ($operationProcessCleanupProven) {
                $tempRemovalEligible = Test-OwnedTempRootCanBeRemoved `
                    -Root $tempRoot -OwnerMarker $tempOwnerMarker `
                    -ExpectedRunId $runId -ResolvedOutputRoot $outputRootPath
            }
            $warningHashes = @()
            if (-not $operationProcessCleanupProven) {
                $warningHashes += Get-Sha256Hex `
                    -Value 'process-cleanup-unproven'
            }
            if (-not $tempRemovalEligible) {
                $warningHashes += Get-Sha256Hex `
                    -Value 'temp-removal-eligibility-unproven'
            }
            return [pscustomobject]@{
                stop_attempted = $true
                stop_proven = [bool] $operationProcessCleanupProven
                temp_removal_eligible = [bool] $tempRemovalEligible
                warning_sha256 = @($warningHashes)
                secondary_sha256 = @()
                owned_process_count = [Math]::Max(
                    $attemptedLabels.Count,
                    $startedProcesses.Count
                )
            }
        }
        $rawRemovalOperation = {
            param([object] $Proof)
            $tempRemoved = Remove-OwnedTempRoot `
                -Root $tempRoot -OwnerMarker $tempOwnerMarker `
                -ExpectedRunId $runId -ResolvedOutputRoot $outputRootPath
            if (-not $tempRemoved) {
                throw 'Owned temp removal did not converge after lifecycle commit'
            }
            $recordsRemoved = Remove-PrivateIdentityRecordsIfSafe `
                -HostManifestPath $hostManifestPath `
                -ControlStatePath $controlStatePath `
                -ProcessCleanupProven $true `
                -TempCleanupProven $true `
                -OwnedProcessCount ([int] $Proof.owned_process_count)
            if (-not $recordsRemoved) {
                throw 'Private identity removal did not converge after lifecycle commit'
            }
        }
        $cleanupTransaction = Invoke-LauncherCleanupTransaction `
            -PublishFailureLifecycle $publishFailureLifecycle `
            -LifecyclePath $launcherLifecyclePath `
            -SecondaryPath $launcherLifecycleSecondaryPath `
            -LifecycleDocument $lifecycleDocument `
            -CleanupProofOperation $cleanupProofOperation `
            -RawRemovalOperation $rawRemovalOperation
        $cleanupFailure = $cleanupTransaction.cleanup_failure
        if ($null -ne $cleanupFailure) {
            Write-Warning 'Process cleanup verification failed; preserving remaining private recovery evidence'
        }
    } catch {
        $cleanupFailure = $_
        Write-Warning 'Process cleanup verification failed; preserving private process, temp, and control evidence'
    }
}

Complete-LauncherFailureState -PrimaryFailure $primaryFailure -CleanupFailure $cleanupFailure
