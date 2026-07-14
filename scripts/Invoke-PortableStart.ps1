[CmdletBinding()]
param(
    [string]$OperationId = "",
    [string]$ManagedBy = "direct",
    [switch]$NoUi,
    [ValidateRange(1, 65535)][Nullable[int]]$PortOverride = $null
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:Context = $null

class PortableStartException : System.Exception {
    [string]$Code

    PortableStartException([string]$code, [string]$message) : base($message) {
        $this.Code = $code
    }
}

function Throw-PortableStartError {
    param(
        [Parameter(Mandatory = $true)][string]$Code,
        [Parameter(Mandatory = $true)][string]$Message
    )
    throw [PortableStartException]::new($Code, $Message)
}

function Test-PathWithinRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    if ([string]::Equals($resolvedRoot, $resolvedPath, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    $prefix = $resolvedRoot + [IO.Path]::DirectorySeparatorChar
    return $resolvedPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Resolve-PackageRelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ([string]::IsNullOrWhiteSpace($Value) -or [IO.Path]::IsPathRooted($Value) -or $Value.Contains(":")) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "$Label must be a package-relative path"
    }
    $segments = @($Value -split '[\\/]')
    if ($segments -contains "..") {
        Throw-PortableStartError "PACKAGE_CORRUPT" "$Label cannot escape the package"
    }
    $resolved = [IO.Path]::GetFullPath((Join-Path $Root $Value))
    if (!(Test-PathWithinRoot -Root $Root -Path $resolved)) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "$Label resolves outside the package"
    }
    $current = [IO.Path]::GetFullPath($Root)
    foreach ($segment in $segments) {
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -eq ".") { continue }
        $current = Join-Path $current $segment
        if (Test-Path -LiteralPath $current) {
            $attributes = [IO.File]::GetAttributes($current)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Throw-PortableStartError "PACKAGE_CORRUPT" "$Label traverses a reparse point outside the portable package boundary"
            }
        }
    }
    return $resolved
}

function Get-PackageContext {
    param([Parameter(Mandatory = $true)][string]$Root)

    $resolvedRoot = [IO.Path]::GetFullPath($Root)
    $manifestPath = Join-Path $resolvedRoot "package\tts-more-package.json"
    $manifest = $null
    $isStaged = Test-Path -LiteralPath $manifestPath -PathType Leaf
    if ($isStaged) {
        try {
            $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        } catch {
            Throw-PortableStartError "PACKAGE_CORRUPT" "The staged package manifest is not valid JSON: $($_.Exception.Message)"
        }
        if ([int]$manifest.schema_version -notin @(1, 2)) {
            Throw-PortableStartError "PACKAGE_CORRUPT" "The staged package manifest schema is unsupported"
        }
    }

    $bundle = if (Test-Path -LiteralPath (Join-Path $resolvedRoot "tts_more\component.json") -PathType Leaf) {
        Join-Path $resolvedRoot "tts_more"
    } else {
        Join-Path $resolvedRoot "scripts"
    }
    $componentConfig = $null
    $componentConfigPath = Join-Path $bundle "component.json"
    if (Test-Path -LiteralPath $componentConfigPath -PathType Leaf) {
        try { $componentConfig = Get-Content -LiteralPath $componentConfigPath -Raw | ConvertFrom-Json } catch {
            Throw-PortableStartError "PACKAGE_CORRUPT" "The component configuration is not valid JSON"
        }
    }

    if ($isStaged) {
        $component = [string]$manifest.component
        if ([string]::IsNullOrWhiteSpace($component)) { Throw-PortableStartError "PACKAGE_CORRUPT" "The package component is missing" }
        $profile = if ([int]$manifest.schema_version -eq 2) { [string]$manifest.package_profile } else { "bootstrap" }
        if ($profile -notin @("bootstrap", "full")) { Throw-PortableStartError "PACKAGE_CORRUPT" "The package profile is invalid" }
        if ([int]$manifest.schema_version -eq 2) {
            if ($null -eq $manifest.data -or [string]::IsNullOrWhiteSpace([string]$manifest.data.operations)) {
                Throw-PortableStartError "PACKAGE_CORRUPT" "The package data.operations path is missing"
            }
            $operationsRoot = Resolve-PackageRelativePath -Root $resolvedRoot -Value ([string]$manifest.data.operations) -Label "data.operations"
            $statePath = Resolve-PackageRelativePath -Root $resolvedRoot -Value ([string]$manifest.runtime.state_path) -Label "runtime.state_path"
            $runtimeLock = Resolve-PackageRelativePath -Root $resolvedRoot -Value ([string]$manifest.runtime.lock) -Label "runtime.lock"
            $modelLock = Resolve-PackageRelativePath -Root $resolvedRoot -Value ([string]$manifest.models.lock) -Label "models.lock"
        } else {
            $operationsRoot = Join-Path $resolvedRoot "data\local\operations"
            $statePath = Join-Path $resolvedRoot "data\local\install-state.json"
            $runtimeLock = ""
            $modelLock = ""
        }
        $buildId = [string]$manifest.build_id
        $port = if ($null -ne $manifest.endpoint -and $null -ne $manifest.endpoint.port) { [int]$manifest.endpoint.port } elseif ($componentConfig) { [int]$componentConfig.port } else { 8000 }
        $healthPath = if ($null -ne $manifest.endpoint) { [string]$manifest.endpoint.health_path } else { "" }
    } else {
        $component = if ($componentConfig) { [string]$componentConfig.component } else { "tts-more" }
        if ([string]::IsNullOrWhiteSpace($component)) { $component = "tts-more" }
        $profile = "source-checkout"
        $operationsRoot = Join-Path $resolvedRoot "data\local\operations"
        $statePath = Join-Path $resolvedRoot "data\local\install-state.json"
        $runtimeLock = if ($component -eq "tts-more") { Join-Path $resolvedRoot "packaging\portable\runtime.lock.json" } else { Join-Path $bundle "locks\runtime.lock.json" }
        $modelLock = if ($component -eq "tts-more") { Join-Path $resolvedRoot "packaging\portable\models.lock.json" } else { Join-Path $bundle "locks\models.lock.json" }
        $buildId = "source-checkout"
        $port = if ($componentConfig) { [int]$componentConfig.port } else { 8000 }
        $healthPath = if ($component -eq "tts-more") { "/api/health" } else { "/health" }
    }

    $initializeScript = if ($component -eq "tts-more") { Join-Path $resolvedRoot "scripts\initialize-portable.ps1" } else { Join-Path $bundle "Initialize.ps1" }
    $serviceScript = if ($component -eq "tts-more") { Join-Path $resolvedRoot "scripts\start-production.ps1" } else { Join-Path $bundle "Start-Worker.ps1" }
    if (!(Test-Path -LiteralPath $initializeScript -PathType Leaf) -or !(Test-Path -LiteralPath $serviceScript -PathType Leaf)) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "Portable initialize or start controller is missing"
    }

    return [pscustomobject]@{
        Root = $resolvedRoot
        Bundle = $bundle
        IsStaged = [bool]$isStaged
        Profile = $profile
        Component = $component
        BuildId = $buildId
        OperationsRoot = [IO.Path]::GetFullPath($operationsRoot)
        StatePath = [IO.Path]::GetFullPath($statePath)
        RuntimeLock = if ($runtimeLock) { [IO.Path]::GetFullPath($runtimeLock) } else { "" }
        ModelLock = if ($modelLock) { [IO.Path]::GetFullPath($modelLock) } else { "" }
        InitializeScript = $initializeScript
        ServiceScript = $serviceScript
        Port = $port
        HealthPath = $healthPath
        EndpointUrl = "http://127.0.0.1:$port"
    }
}

function Assert-PackageWritable {
    param([Parameter(Mandatory = $true)][string]$Root)

    $resolvedRoot = [IO.Path]::GetFullPath($Root)
    if ($resolvedRoot -match '(?i)\.zip([\\/]|$)') {
        Throw-PortableStartError "PACKAGE_NOT_WRITABLE" "Extract the ZIP before starting the package"
    }
    foreach ($programFilesRoot in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($programFilesRoot -and (Test-PathWithinRoot -Root $programFilesRoot -Path $resolvedRoot)) {
            Throw-PortableStartError "PACKAGE_NOT_WRITABLE" "Move the package outside Program Files; elevation is not requested"
        }
    }
    try {
        $attributes = [IO.File]::GetAttributes($resolvedRoot)
        if (($attributes -band [IO.FileAttributes]::ReadOnly) -ne 0) { throw "root is read-only" }
        $probe = Join-Path $resolvedRoot (".tts-more-write-probe-{0}.tmp" -f [guid]::NewGuid().ToString("N"))
        [IO.File]::WriteAllText($probe, "write probe", $script:Utf8NoBom)
        [IO.File]::Delete($probe)
    } catch {
        Throw-PortableStartError "PACKAGE_NOT_WRITABLE" "The package root is not writable: $($_.Exception.Message)"
    }
}

function Open-PackageOperationLock {
    param([Parameter(Mandatory = $true)][string]$Root)

    $context = if ($script:Context -and [string]::Equals($script:Context.Root, [IO.Path]::GetFullPath($Root), [StringComparison]::OrdinalIgnoreCase)) { $script:Context } else { Get-PackageContext -Root $Root }
    New-Item -ItemType Directory -Force -Path $context.OperationsRoot | Out-Null
    $lockPath = Join-Path $context.OperationsRoot ".start.lock"
    try {
        return [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch [IO.IOException] {
        Throw-PortableStartError "OPERATION_ACTIVE" "Another package start operation owns the controller lock"
    }
}

function Write-JsonAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Payload
    )
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = Join-Path $parent (".{0}.{1}.tmp" -f (Split-Path -Leaf $Path), [guid]::NewGuid().ToString("N"))
    $json = ($Payload | ConvertTo-Json -Depth 12 -Compress) + "`n"
    [IO.File]::WriteAllText($temporary, $json, $script:Utf8NoBom)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        try { [IO.File]::Replace($temporary, $Path, $null) } catch {
            [IO.File]::Delete($Path)
            [IO.File]::Move($temporary, $Path)
        }
    } else {
        [IO.File]::Move($temporary, $Path)
    }
}

function Initialize-Operation {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$OperationId,
        [Parameter(Mandatory = $true)][string]$Initiator
    )
    $context = if ($script:Context) { $script:Context } else { Get-PackageContext -Root $Root }
    $parsed = [guid]::Empty
    if (![guid]::TryParse($OperationId, [ref]$parsed)) { Throw-PortableStartError "PACKAGE_CORRUPT" "OperationId must be a valid UUID" }
    $canonicalId = $parsed.ToString()
    $operationRoot = [IO.Path]::GetFullPath((Join-Path $context.OperationsRoot $canonicalId))
    if (!(Test-PathWithinRoot -Root $context.OperationsRoot -Path $operationRoot) -or ![string]::Equals((Split-Path -Parent $operationRoot), [IO.Path]::GetFullPath($context.OperationsRoot), [StringComparison]::OrdinalIgnoreCase)) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "OperationRoot must be a UUID direct child of data.operations"
    }
    New-Item -ItemType Directory -Force -Path $operationRoot | Out-Null
    $operationPath = Join-Path $operationRoot "operation.json"
    if (Test-Path -LiteralPath $operationPath -PathType Leaf) { Throw-PortableStartError "OPERATION_ACTIVE" "Operation already exists: $canonicalId" }
    $operation = [ordered]@{
        operation_id = $canonicalId
        component = $context.Component
        action = "start"
        initiator = $Initiator
        started_at = [DateTime]::UtcNow.ToString("o")
        status = "not_initialized"
        exit_code = $null
    }
    Write-JsonAtomic -Path $operationPath -Payload $operation
    return $operationRoot
}

function Add-OperationEvent {
    param(
        [Parameter(Mandatory = $true)][string]$Operation,
        [Parameter(Mandatory = $true)][ValidateSet("not_initialized", "checking", "downloading", "installing", "validating", "starting", "ready", "stopped", "repairable", "blocked")][string]$Phase,
        [Parameter(Mandatory = $true)][string]$Message,
        [string]$ErrorCode = "",
        [Nullable[double]]$Percent = $null
    )
    $eventsPath = Join-Path $Operation "events.jsonl"
    $sequence = 1
    if (Test-Path -LiteralPath $eventsPath -PathType Leaf) {
        $sequence = @([IO.File]::ReadAllLines($eventsPath) | Where-Object { ![string]::IsNullOrWhiteSpace($_) }).Count + 1
    }
    $event = [ordered]@{
        seq = $sequence
        timestamp = [DateTime]::UtcNow.ToString("o")
        phase = $Phase
        message = $Message
    }
    if ($null -ne $Percent) { $event.percent = [Math]::Max(0.0, [Math]::Min(100.0, [double]$Percent)) }
    if (![string]::IsNullOrWhiteSpace($ErrorCode)) { $event.error_code = $ErrorCode }
    $line = ($event | ConvertTo-Json -Depth 6 -Compress) + "`n"
    $bytes = $script:Utf8NoBom.GetBytes($line)
    $stream = New-Object IO.FileStream($eventsPath, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
}

function Complete-Operation {
    param(
        [Parameter(Mandatory = $true)][string]$Operation,
        [Parameter(Mandatory = $true)][string]$Status,
        [Parameter(Mandatory = $true)][int]$ExitCode
    )
    $operationPath = Join-Path $Operation "operation.json"
    $payload = Get-Content -LiteralPath $operationPath -Raw | ConvertFrom-Json
    $payload.status = $Status
    $payload.exit_code = $ExitCode
    $payload | Add-Member -NotePropertyName finished_at -NotePropertyValue ([DateTime]::UtcNow.ToString("o")) -Force
    Write-JsonAtomic -Path $operationPath -Payload $payload
}

function Get-PortableErrorCode {
    param([Parameter(Mandatory = $true)][System.Management.Automation.ErrorRecord]$ErrorRecord)
    if ($ErrorRecord.Exception -is [PortableStartException]) { return [string]$ErrorRecord.Exception.Code }
    $message = [string]$ErrorRecord.Exception.Message
    if ($message -match '(?i)port .*in use|port .*occupied|PORT_IN_USE') { return "PORT_IN_USE" }
    if ($message -match '(?i)space|disk') { return "DISK_SPACE_INSUFFICIENT" }
    if ($message -match '(?i)CUDA') { return "CUDA_PROBE_FAILED" }
    if ($message -match '(?i)download|network|HTTP') { return "DOWNLOAD_NETWORK_INTERRUPTED" }
    return "PACKAGE_CORRUPT"
}

function Fail-Operation {
    param(
        [Parameter(Mandatory = $true)][string]$Operation,
        [Parameter(Mandatory = $true)][System.Management.Automation.ErrorRecord]$ErrorRecord
    )
    if (!(Test-Path -LiteralPath (Join-Path $Operation "operation.json") -PathType Leaf)) { return }
    $code = Get-PortableErrorCode -ErrorRecord $ErrorRecord
    $exitCode = Resolve-PortableExitCode -ErrorRecord $ErrorRecord
    $status = if ($exitCode -eq 20) { "stopped" } else { "blocked" }
    Add-OperationEvent -Operation $Operation -Phase $status -Message ([string]$ErrorRecord.Exception.Message) -ErrorCode $code
    Complete-Operation -Operation $Operation -Status $status -ExitCode $exitCode
}

function Test-InstallState {
    param([Parameter(Mandatory = $true)][string]$Root)

    $context = if ($script:Context) { $script:Context } else { Get-PackageContext -Root $Root }
    $python = Join-Path $context.Root "runtime\live\python.exe"
    if (!(Test-Path -LiteralPath $context.StatePath -PathType Leaf) -or !(Test-Path -LiteralPath $python -PathType Leaf)) { return $false }
    try { $state = Get-Content -LiteralPath $context.StatePath -Raw | ConvertFrom-Json } catch { return $false }
    if ([string]$state.component -ne $context.Component -or [string]$state.build_id -ne $context.BuildId) { return $false }
    if ($context.IsStaged) {
        if (!(Test-Path -LiteralPath $context.RuntimeLock -PathType Leaf) -or !(Test-Path -LiteralPath $context.ModelLock -PathType Leaf)) { return $false }
        $runtimeHash = (Get-FileHash -LiteralPath $context.RuntimeLock -Algorithm SHA256).Hash.ToLowerInvariant()
        $modelHash = (Get-FileHash -LiteralPath $context.ModelLock -Algorithm SHA256).Hash.ToLowerInvariant()
        if ([string]$state.runtime_lock_sha256 -ne $runtimeHash -or [string]$state.model_lock_sha256 -ne $modelHash) { return $false }
    }
    return $true
}

function Invoke-ChildPowerShell {
    param(
        [Parameter(Mandatory = $true)][string]$Script,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $powerShell = Join-Path $PSHOME "powershell.exe"
    $command = @("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", $Script) + $Arguments
    $output = @(& $powerShell @command 2>&1)
    $exitCode = $LASTEXITCODE
    foreach ($line in $output) { Write-Host ([string]$line) }
    return [pscustomobject]@{ ExitCode = $exitCode; Output = ($output -join "`n") }
}

function Invoke-Initialize {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Operation
    )
    $context = if ($script:Context) { $script:Context } else { Get-PackageContext -Root $Root }
    $cancelFile = Join-Path $Operation "cancel.requested"
    $result = Invoke-ChildPowerShell -Script $context.InitializeScript -Arguments @("-PackageRoot", $context.Root, "-OperationRoot", $Operation, "-CancelFile", $cancelFile)
    if ($result.ExitCode -eq 20) { Throw-PortableStartError "CANCELLED" "Portable initialization was cancelled" }
    if ($result.ExitCode -ne 0) {
        if ($result.Output -match '(?i)space|disk') { Throw-PortableStartError "DISK_SPACE_INSUFFICIENT" $result.Output }
        if ($result.Output -match '(?i)CUDA') { Throw-PortableStartError "CUDA_PROBE_FAILED" $result.Output }
        if ($result.Output -match '(?i)download|network|HTTP') { Throw-PortableStartError "DOWNLOAD_NETWORK_INTERRUPTED" $result.Output }
        Throw-PortableStartError "PACKAGE_CORRUPT" "Initialization failed: $($result.Output)"
    }
}

function Invoke-ServiceStart {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Operation,
        [Nullable[int]]$PortOverride = $null
    )
    $context = if ($script:Context) { $script:Context } else { Get-PackageContext -Root $Root }
    $arguments = @("-PackageRoot", $context.Root, "-OperationRoot", $Operation)
    if ($null -ne $PortOverride) { $arguments += @("-PortOverride", [string]$PortOverride) }
    $result = Invoke-ChildPowerShell -Script $context.ServiceScript -Arguments $arguments
    if ($result.ExitCode -ne 0) {
        if ($result.Output -match '(?i)PORT_IN_USE|port .*in use|port .*occupied') { Throw-PortableStartError "PORT_IN_USE" $result.Output }
        Throw-PortableStartError "PACKAGE_CORRUPT" "Service start failed: $($result.Output)"
    }
}

function Resolve-PortableExitCode {
    param([Parameter(Mandatory = $true)][System.Management.Automation.ErrorRecord]$ErrorRecord)
    $code = Get-PortableErrorCode -ErrorRecord $ErrorRecord
    switch ($code) {
        "CANCELLED" { return 20 }
        "PACKAGE_NOT_WRITABLE" { return 21 }
        "PACKAGE_CORRUPT" { return 22 }
        "PORT_IN_USE" { return 23 }
        "DISK_SPACE_INSUFFICIENT" { return 24 }
        "CUDA_PROBE_FAILED" { return 25 }
        "DOWNLOAD_NETWORK_INTERRUPTED" { return 26 }
        default { return 1 }
    }
}

function Start-ProgressWindow {
    param(
        [Parameter(Mandatory = $true)][string]$Operation,
        [Parameter(Mandatory = $true)][string]$Url
    )
    $progressScript = Join-Path $PSScriptRoot "Show-PortableProgress.ps1"
    if (!(Test-Path -LiteralPath $progressScript -PathType Leaf)) { return }
    $quotedScript = '"{0}"' -f $progressScript.Replace('"', '\"')
    $quotedOperation = '"{0}"' -f $Operation.Replace('"', '\"')
    $quotedUrl = '"{0}"' -f $Url.Replace('"', '\"')
    Start-Process -FilePath (Join-Path $PSHOME "powershell.exe") -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $quotedScript, "-OperationRoot", $quotedOperation, "-Url", $quotedUrl) -WindowStyle Normal | Out-Null
}

function Wait-ForActiveOperation {
    param(
        [Parameter(Mandatory = $true)][object]$Context,
        [switch]$NoUi
    )
    $activePath = Join-Path $Context.OperationsRoot "active-start.json"
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while (!(Test-Path -LiteralPath $activePath -PathType Leaf) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 50 }
    if (!(Test-Path -LiteralPath $activePath -PathType Leaf)) { Throw-PortableStartError "OPERATION_ACTIVE" "The active operation pointer was not published" }
    try { $active = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json } catch { Throw-PortableStartError "OPERATION_ACTIVE" "The active operation pointer is unreadable" }
    $parsed = [guid]::Empty
    if (![guid]::TryParse([string]$active.operation_id, [ref]$parsed)) { Throw-PortableStartError "OPERATION_ACTIVE" "The active operation id is invalid" }
    $operation = [IO.Path]::GetFullPath((Join-Path $Context.OperationsRoot $parsed.ToString()))
    if (![string]::Equals((Split-Path -Parent $operation), [IO.Path]::GetFullPath($Context.OperationsRoot), [StringComparison]::OrdinalIgnoreCase)) {
        Throw-PortableStartError "OPERATION_ACTIVE" "The active operation is outside data.operations"
    }
    Write-Host "Attaching to active operation $($parsed.ToString())"
    $operationPath = Join-Path $operation "operation.json"
    do {
        if (Test-Path -LiteralPath $operationPath -PathType Leaf) {
            try { $payload = Get-Content -LiteralPath $operationPath -Raw | ConvertFrom-Json } catch { $payload = $null }
            if ($payload -and $null -ne $payload.exit_code) { return [int]$payload.exit_code }
        }
        Start-Sleep -Milliseconds 200
    } while ($true)
}

$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$operation = ""
$lock = $null
$activePath = ""
$ownsActivePointer = $false
$exitCode = 0
try {
    if ([string]::IsNullOrWhiteSpace($ManagedBy) -or $ManagedBy.Length -gt 128) { Throw-PortableStartError "PACKAGE_CORRUPT" "ManagedBy must be non-empty and at most 128 characters" }
    $script:Context = Get-PackageContext -Root $root
    Assert-PackageWritable -Root $root
    try {
        $lock = Open-PackageOperationLock -Root $root
    } catch {
        if ((Get-PortableErrorCode -ErrorRecord $_) -eq "OPERATION_ACTIVE") {
            exit (Wait-ForActiveOperation -Context $script:Context -NoUi:$NoUi)
        }
        throw
    }

    if ([string]::IsNullOrWhiteSpace($OperationId)) { $OperationId = [guid]::NewGuid().ToString() }
    $operation = Initialize-Operation -Root $root -OperationId $OperationId -Initiator $ManagedBy
    $OperationId = Split-Path -Leaf $operation
    $activePath = Join-Path $script:Context.OperationsRoot "active-start.json"
    Write-JsonAtomic -Path $activePath -Payload ([ordered]@{ operation_id = $OperationId; owner_pid = $PID; published_at = [DateTime]::UtcNow.ToString("o") })
    $ownsActivePointer = $true
    Add-OperationEvent -Operation $operation -Phase "checking" -Message "正在检查便携包安装状态" -Percent 0
    if (!$NoUi) { Start-ProgressWindow -Operation $operation -Url $script:Context.EndpointUrl }

    $installed = Test-InstallState -Root $root
    if (!$installed) {
        if ($script:Context.Profile -eq "full") {
            Throw-PortableStartError "PACKAGE_CORRUPT" "Full package assets are missing or invalid; Start will not download replacements"
        }
        Add-OperationEvent -Operation $operation -Phase "installing" -Message "正在初始化包内私有运行时" -Percent 5
        Invoke-Initialize -Root $root -Operation $operation
        if (!(Test-InstallState -Root $root)) { Throw-PortableStartError "PACKAGE_CORRUPT" "Initialization did not produce a valid package-private runtime state" }
    }
    if (Test-Path -LiteralPath (Join-Path $operation "cancel.requested") -PathType Leaf) { Throw-PortableStartError "CANCELLED" "Portable start was cancelled" }
    Add-OperationEvent -Operation $operation -Phase "starting" -Message "正在启动本地服务" -Percent 95
    Invoke-ServiceStart -Root $root -Operation $operation -PortOverride $PortOverride
    $urlPort = if ($null -ne $PortOverride) { [int]$PortOverride } else { [int]$script:Context.Port }
    $url = "http://127.0.0.1:$urlPort"
    Add-OperationEvent -Operation $operation -Phase "ready" -Message "服务已就绪：$url" -Percent 100
    Complete-Operation -Operation $operation -Status "ready" -ExitCode 0
    Write-Host "$($script:Context.Component) ready: $url"
    if (!$NoUi) {
        if ($script:Context.Component -eq "tts-more") {
            Start-Process $url | Out-Null
        } else {
            try { Set-Clipboard -Value $url -ErrorAction Stop; Write-Host "Worker URL copied to clipboard." } catch { Write-Host "Copy the worker URL shown above." }
        }
    }
} catch {
    $exitCode = Resolve-PortableExitCode -ErrorRecord $_
    $code = Get-PortableErrorCode -ErrorRecord $_
    if ($operation) { Fail-Operation -Operation $operation -ErrorRecord $_ }
    Write-Error "[$code] $($_.Exception.Message)" -ErrorAction Continue
} finally {
    if ($ownsActivePointer -and $activePath -and (Test-Path -LiteralPath $activePath -PathType Leaf)) {
        try {
            $active = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json
            if ([string]$active.operation_id -eq $OperationId) { Remove-Item -LiteralPath $activePath -Force }
        } catch { }
    }
    if ($lock) { $lock.Dispose() }
}
exit $exitCode
