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
$MaxChildOutputBytes = 1048576

function ConvertTo-ProcessArgument {
    param([AllowEmptyString()] [string] $Value)
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object Text.StringBuilder
    [void] $builder.Append([char] 34)
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char] 92) {
            $slashes += 1
            continue
        }
        if ($character -eq [char] 34) {
            for ($index = 0; $index -lt (2 * $slashes + 1); $index += 1) {
                [void] $builder.Append([char] 92)
            }
            [void] $builder.Append($character)
            $slashes = 0
            continue
        }
        for ($index = 0; $index -lt $slashes; $index += 1) {
            [void] $builder.Append([char] 92)
        }
        $slashes = 0
        [void] $builder.Append($character)
    }
    for ($index = 0; $index -lt (2 * $slashes); $index += 1) {
        [void] $builder.Append([char] 92)
    }
    [void] $builder.Append([char] 34)
    return $builder.ToString()
}

function Read-StrictBoundedProcessStreams {
    param(
        [IO.Stream] $StandardOutput,
        [IO.Stream] $StandardError,
        [int] $MaximumBytes
    )
    if ($MaximumBytes -lt 1) { throw 'Inner launcher output is invalid' }
    if ($null -eq ('TtsMore.ReliabilityBoundedCaptureStream' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;

namespace TtsMore {
    public sealed class ReliabilityBoundedCaptureStream : Stream {
        private readonly MemoryStream stored = new MemoryStream();
        private readonly long maximumBytes;

        public ReliabilityBoundedCaptureStream(long maximumBytes) {
            if (maximumBytes < 1) throw new ArgumentOutOfRangeException("maximumBytes");
            this.maximumBytes = maximumBytes;
        }

        public bool Overflowed { get; private set; }
        public byte[] ToArray() { return stored.ToArray(); }
        public override bool CanRead { get { return false; } }
        public override bool CanSeek { get { return false; } }
        public override bool CanWrite { get { return true; } }
        public override long Length { get { return stored.Length; } }
        public override long Position {
            get { return stored.Position; }
            set { throw new NotSupportedException(); }
        }
        public override void Flush() { }
        public override int Read(byte[] buffer, int offset, int count) {
            throw new NotSupportedException();
        }
        public override long Seek(long offset, SeekOrigin origin) {
            throw new NotSupportedException();
        }
        public override void SetLength(long value) { throw new NotSupportedException(); }
        public override void Write(byte[] buffer, int offset, int count) {
            if (buffer == null) throw new ArgumentNullException("buffer");
            if (offset < 0 || count < 0 || offset + count > buffer.Length) {
                throw new ArgumentOutOfRangeException();
            }
            long remaining = maximumBytes - stored.Length;
            int retained = remaining > 0 ? (int)Math.Min((long)count, remaining) : 0;
            if (retained > 0) stored.Write(buffer, offset, retained);
            if (retained != count) Overflowed = true;
        }
        public override Task WriteAsync(
            byte[] buffer,
            int offset,
            int count,
            CancellationToken cancellationToken
        ) {
            if (cancellationToken.IsCancellationRequested) {
                return Task.FromCanceled(cancellationToken);
            }
            Write(buffer, offset, count);
            return Task.FromResult(0);
        }
        protected override void Dispose(bool disposing) {
            if (disposing) stored.Dispose();
            base.Dispose(disposing);
        }
    }
}
'@ -ErrorAction Stop
    }
    $stdoutCapture = New-Object TtsMore.ReliabilityBoundedCaptureStream `
        -ArgumentList ([long] $MaximumBytes)
    $stderrCapture = New-Object TtsMore.ReliabilityBoundedCaptureStream `
        -ArgumentList ([long] $MaximumBytes)
    $stdoutBytes = $null
    $stderrBytes = $null
    $captureFailed = $false
    try {
        $stdoutTask = $StandardOutput.CopyToAsync($stdoutCapture)
        $stderrTask = $StandardError.CopyToAsync($stderrCapture)
        [Threading.Tasks.Task]::WaitAll(
            [Threading.Tasks.Task[]] @($stdoutTask, $stderrTask)
        )
        $stdoutBytes = [byte[]] $stdoutCapture.ToArray()
        $stderrBytes = [byte[]] $stderrCapture.ToArray()
        if ($stdoutCapture.Overflowed -or $stderrCapture.Overflowed) {
            $captureFailed = $true
        } else {
            $strictUtf8 = New-Object Text.UTF8Encoding($false, $true)
            $null = $strictUtf8.GetString($stdoutBytes)
            $null = $strictUtf8.GetString($stderrBytes)
        }
    } catch {
        $captureFailed = $true
    } finally {
        $stdoutCapture.Dispose()
        $stderrCapture.Dispose()
    }
    if ($captureFailed -or $null -eq $stdoutBytes -or $null -eq $stderrBytes) {
        throw 'Inner launcher output is invalid'
    }
    return [pscustomobject]@{
        stdout_bytes = $stdoutBytes
        stderr_bytes = $stderrBytes
    }
}

function Get-StrictProcessExitCode {
    param([object] $Process)
    $exitProperty = $Process.PSObject.Properties['ExitCode']
    if (
        $null -eq $exitProperty -or
        $null -eq $exitProperty.Value -or
        $exitProperty.Value.GetType() -ne [int]
    ) {
        throw 'Inner launcher exit is unavailable'
    }
    return [int] $exitProperty.Value
}

function Invoke-PythonJson {
    param([string] $PythonPath, [string] $BackendRoot, [string[]] $Arguments)
    Push-Location -LiteralPath $BackendRoot
    try {
        $rendered = @(& $PythonPath @Arguments 2>$null)
        $helperExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($helperExit -ne 0 -or $rendered.Count -ne 1) {
        throw 'Formal supervision helper failed'
    }
    try {
        return ([string] $rendered[0]) | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw 'Formal supervision helper returned invalid output'
    }
}

try {
    $outputRootRequest = [IO.Path]::GetFullPath($OutputRoot)
    $ttsMoreRootPath = (Resolve-Path -LiteralPath $TtsMoreRoot -ErrorAction Stop).Path
    $backendRoot = Join-Path $ttsMoreRootPath 'backend'
    $backendPython = Join-Path $backendRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $backendPython -PathType Leaf)) {
        throw 'Formal backend Python is unavailable'
    }
    $innerScript = Join-Path $PSScriptRoot 'run-windows-comfyui-reliability.ps1'
    if (-not (Test-Path -LiteralPath $innerScript -PathType Leaf)) {
        throw 'Formal inner launcher is unavailable'
    }
    $preparedRoot = Invoke-PythonJson -PythonPath $backendPython -BackendRoot $backendRoot `
        -Arguments @(
            '-m', 'app.comfyui.reliability_supervision_cli', 'prepare-output-root',
            '--output-root', $outputRootRequest
        )
    if (
        $preparedRoot.ok -ne $true -or
        $preparedRoot.result.root_identity -cnotmatch '^[0-9a-f]{64}$' -or
        [string]::IsNullOrEmpty([string] $preparedRoot.result.output_root)
    ) { throw 'Formal output root preparation failed' }
    $outputRootPath = [string] $preparedRoot.result.output_root
    $rootIdentity = [string] $preparedRoot.result.root_identity

    $snapshot = Invoke-PythonJson -PythonPath $backendPython -BackendRoot $backendRoot `
        -Arguments @(
            '-m', 'app.comfyui.reliability_evidence_cli', 'snapshot-current',
            '--output-root', $outputRootPath
        )
    if ($snapshot.ok -ne $true -or $null -eq $snapshot.snapshot.token) {
        throw 'Current pointer snapshot is invalid'
    }
    $expectedToken = [string] $snapshot.snapshot.token

    $runId = [Guid]::NewGuid().ToString('N')
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $runKey = ([BitConverter]::ToString(
            $sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($runId))
        )).Replace('-', '').ToLowerInvariant()
    } finally { $sha256.Dispose() }
    $mode = if ($PreflightOnly) { 'preflight' } else { 'matrix' }
    $prepared = Invoke-PythonJson -PythonPath $backendPython -BackendRoot $backendRoot `
        -Arguments @(
            '-m', 'app.comfyui.reliability_supervision_cli', 'prepare-run',
            '--output-root', $outputRootPath,
            '--run-key', $runKey,
            '--expected-root-identity', $rootIdentity
        )
    if (
        $prepared.ok -ne $true -or
        $prepared.result.run_key -cne $runKey -or
        $prepared.result.root_identity -cne $rootIdentity -or
        $prepared.result.run_root_identity -cnotmatch '^[0-9a-f]{64}$'
    ) {
        throw 'Formal run preparation failed'
    }
    $runRootIdentity = [string] $prepared.result.run_root_identity

    $innerArguments = @(
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $innerScript,
        '-Fixture', $Fixture,
        '-OutputRoot', $outputRootPath,
        '-ComfyUiRoot', $ComfyUiRoot,
        '-ComfyPython', $ComfyPython,
        '-TtsMoreRoot', $ttsMoreRootPath,
        '-RunId', $runId,
        '-OutputRootIdentity', $rootIdentity,
        '-RunRootIdentity', $runRootIdentity
    )
    if ($AllowLan) { $innerArguments += '-AllowLan' }
    if ($PreflightOnly) { $innerArguments += '-PreflightOnly' }
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = Join-Path $PSHOME 'powershell.exe'
    $startInfo.Arguments = (
        @($innerArguments | ForEach-Object { ConvertTo-ProcessArgument -Value ([string] $_) }) `
            -join ' '
    )
    $startInfo.WorkingDirectory = $ttsMoreRootPath
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $childStartCount = 0
    try {
        if (-not $process.Start()) { throw 'Inner launcher did not start' }
        $childStartCount += 1
        $capturedStreams = Read-StrictBoundedProcessStreams `
            -StandardOutput $process.StandardOutput.BaseStream `
            -StandardError $process.StandardError.BaseStream `
            -MaximumBytes $MaxChildOutputBytes
        $process.WaitForExit()
        $stdoutBytes = [byte[]] $capturedStreams.stdout_bytes
        $stderrBytes = [byte[]] $capturedStreams.stderr_bytes
        $childExit = Get-StrictProcessExitCode -Process $process
    } finally {
        $process.Dispose()
    }
    if ($childStartCount -ne 1) { throw 'Inner launcher start count is invalid' }

    $runDirectory = Join-Path (Join-Path $outputRootPath 'runs') $runKey
    foreach ($privateLog in @(
        @('comfyui-stdout', (Join-Path $runDirectory '.co')),
        @('comfyui-stderr', (Join-Path $runDirectory '.ce')),
        @('tts-more-stdout', (Join-Path $runDirectory '.bo')),
        @('tts-more-stderr', (Join-Path $runDirectory '.be')),
        @('launcher-lifecycle', (Join-Path $runDirectory '.l')),
        @('launcher-lifecycle-secondary', (Join-Path $runDirectory '.s'))
    )) {
        $privateSource = [string] $privateLog[1]
        if (Test-Path -LiteralPath $privateSource -PathType Leaf) {
            $null = Invoke-PythonJson -PythonPath $backendPython -BackendRoot $backendRoot `
                -Arguments @(
                    '-m', 'app.comfyui.reliability_supervision_cli', 'commit-log',
                    '--output-root', $outputRootPath,
                    '--run-key', $runKey,
                    '--name', [string] $privateLog[0],
                    '--source-file', $privateSource
                )
            Remove-Item -LiteralPath $privateSource -Force -ErrorAction Stop
        }
    }

    $stdoutTemp = [IO.Path]::GetTempFileName()
    $stderrTemp = [IO.Path]::GetTempFileName()
    try {
        [IO.File]::WriteAllBytes($stdoutTemp, $stdoutBytes)
        [IO.File]::WriteAllBytes($stderrTemp, $stderrBytes)
        foreach ($log in @(
            @('inner-stdout', $stdoutTemp),
            @('inner-stderr', $stderrTemp)
        )) {
            $null = Invoke-PythonJson -PythonPath $backendPython -BackendRoot $backendRoot `
                -Arguments @(
                    '-m', 'app.comfyui.reliability_supervision_cli', 'commit-log',
                    '--output-root', $outputRootPath,
                    '--run-key', $runKey,
                    '--name', [string] $log[0],
                    '--source-file', [string] $log[1]
                )
        }
    } finally {
        foreach ($temporary in @($stdoutTemp, $stderrTemp)) {
            if (Test-Path -LiteralPath $temporary -PathType Leaf) {
                Remove-Item -LiteralPath $temporary -Force
            }
        }
    }

    $finalized = Invoke-PythonJson -PythonPath $backendPython -BackendRoot $backendRoot `
        -Arguments @(
            '-m', 'app.comfyui.reliability_supervision_cli', 'finalize',
            '--output-root', $outputRootPath,
            '--run-key', $runKey,
            '--mode', $mode,
            '--expected-token', $expectedToken,
            '--launcher-exit-code', [string] $childExit,
            '--child-start-count', [string] $childStartCount
        )
    if ($finalized.ok -ne $true) { throw 'Formal supervision commit failed' }
    exit $childExit
} catch {
    [Console]::Error.WriteLine('Formal reliability supervision failed')
    exit 1
}
