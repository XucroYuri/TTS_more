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

$runId = [Guid]::NewGuid().ToString('N')
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
                        $processCleanupProven = $false
                        continue
                    }
                    if (-not (Stop-RecordedProcessPair `
                            -LaunchRootRecord $launchRootRecord -ListenerRecord $fullRecord)) {
                        $processCleanupProven = $false
                    }
                    continue
                }
                if ($null -ne $launchRootRecord) {
                    if (-not (Stop-RecordedProcessPair `
                            -LaunchRootRecord $launchRootRecord -ListenerRecord $null)) {
                        $processCleanupProven = $false
                    }
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
    } catch {
        $cleanupFailure = $_
        Write-Warning 'Process cleanup verification failed; preserving private process, temp, and control evidence'
    }
}

Complete-LauncherFailureState -PrimaryFailure $primaryFailure -CleanupFailure $cleanupFailure
