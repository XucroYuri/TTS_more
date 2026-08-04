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
    $stderrPath = $null
    $helperStderr = ''
    Push-Location -LiteralPath $BackendRoot
    try {
        $stderrPath = [IO.Path]::GetTempFileName()
        $rendered = @(& $PythonPath @Arguments 2>$stderrPath)
        $helperExit = $LASTEXITCODE
        if (Test-Path -LiteralPath $stderrPath -PathType Leaf) {
            $helperStderr = [IO.File]::ReadAllText($stderrPath)
        }
    } finally {
        Pop-Location
        if ($null -ne $stderrPath -and (Test-Path -LiteralPath $stderrPath -PathType Leaf)) {
            Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
        }
    }
    if ($helperExit -ne 0 -or $rendered.Count -ne 1) {
        $detail = ([string] $helperStderr).Trim()
        $stdoutDetail = ([string]::Join("`n", @($rendered))).Trim()
        if ($stdoutDetail.Length -gt 1024) { $stdoutDetail = $stdoutDetail.Substring(0, 1024) }
        if ([string]::IsNullOrEmpty($detail)) { $detail = $stdoutDetail }
        if ([string]::IsNullOrEmpty($detail)) {
            $detail = ('stdout-lines={0}' -f $rendered.Count)
        } elseif ($rendered.Count -ne 1) {
            $detail = ('stdout-lines={0}: {1}' -f $rendered.Count, $detail)
        }
        if ($detail.Length -gt 1024) { $detail = $detail.Substring(0, 1024) }
        if ([string]::IsNullOrEmpty($detail)) {
            throw ('Formal supervision helper failed (exit {0})' -f $helperExit)
        }
        throw ('Formal supervision helper failed (exit {0}): {1}' -f $helperExit, $detail)
    }
    try {
        return ([string] $rendered[0]) | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw 'Formal supervision helper returned invalid output'
    }
}

function Resolve-BackendPython {
    param([string] $TtsMoreRootPath)
    $candidates = [System.Collections.Generic.List[string]]::new()
    if (-not [string]::IsNullOrWhiteSpace([string] $env:TTS_MORE_BACKEND_PYTHON)) {
        $candidates.Add([string] $env:TTS_MORE_BACKEND_PYTHON)
    }
    # The repository root .venv is the documented layout.  Keep the older
    # backend/.venv location as a compatibility fallback for existing local
    # worktrees, then use the interpreter installed by CI/setup-python.
    $candidates.Add((Join-Path $TtsMoreRootPath '.venv\Scripts\python.exe'))
    $candidates.Add((Join-Path $TtsMoreRootPath 'backend\.venv\Scripts\python.exe'))
    if ([string]::Equals([string] $env:GITHUB_ACTIONS, 'true', [StringComparison]::OrdinalIgnoreCase)) {
        $pythonCommand = Get-Command 'python.exe' -ErrorAction SilentlyContinue
        if ($null -ne $pythonCommand) { $candidates.Add([string] $pythonCommand.Source) }
    }
    foreach ($candidate in $candidates) {
        if (-not [string]::IsNullOrWhiteSpace($candidate)) {
            $resolved = Resolve-Path -LiteralPath $candidate -ErrorAction SilentlyContinue
            if ($null -ne $resolved -and (Test-Path -LiteralPath $resolved.Path -PathType Leaf)) {
                return $resolved.Path
            }
        }
    }
    throw 'Formal backend Python is unavailable'
}

function New-ReliabilityDirectoryLease {
    param(
        [string] $OutputRoot,
        [string] $RunRoot,
        [string] $PrivateRecoveryRoot,
        [string] $ExpectedRootIdentity,
        [string] $ExpectedRunIdentity,
        [string] $ExpectedPrivateRootIdentity,
        [string] $ExpectedPrivateNamespaceIdentity
    )
    if ($null -eq ('TtsMore.ReliabilityDirectoryLease' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

namespace TtsMore {
    public sealed class ReliabilityDirectoryLease : IDisposable {
        [StructLayout(LayoutKind.Sequential)]
        private struct FileTime {
            public uint LowDateTime;
            public uint HighDateTime;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct FileInformation {
            public uint FileAttributes;
            public FileTime CreationTime;
            public FileTime LastAccessTime;
            public FileTime LastWriteTime;
            public uint VolumeSerialNumber;
            public uint FileSizeHigh;
            public uint FileSizeLow;
            public uint NumberOfLinks;
            public uint FileIndexHigh;
            public uint FileIndexLow;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateFile(
            string fileName,
            uint desiredAccess,
            uint shareMode,
            IntPtr securityAttributes,
            uint creationDisposition,
            uint flagsAndAttributes,
            IntPtr templateFile
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(
            IntPtr handle,
            out FileInformation information
        );

        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);

        [StructLayout(LayoutKind.Sequential)]
        private struct FileDispositionInformation {
            [MarshalAs(UnmanagedType.Bool)]
            public bool DeleteFile;
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetFileInformationByHandle(
            IntPtr handle,
            int informationClass,
            ref FileDispositionInformation information,
            uint bufferSize
        );

        private readonly List<IntPtr> handles = new List<IntPtr>();
        private IntPtr privateRunHandle = IntPtr.Zero;
        private string privateRunPath;
        private bool disposed;

        private ReliabilityDirectoryLease() { }

        private static string NormalizeDirectory(string path) {
            string full = Path.GetFullPath(path);
            string root = Path.GetPathRoot(full);
            if (String.IsNullOrEmpty(root)) throw new IOException("missing path root");
            return full.Length == root.Length ? root : full.TrimEnd('\\');
        }

        private static string Identity(FileInformation information) {
            ulong index = ((ulong)information.FileIndexHigh << 32) |
                information.FileIndexLow;
            string material = information.VolumeSerialNumber.ToString(
                "x8", CultureInfo.InvariantCulture
            ) + ":" + index.ToString("x16", CultureInfo.InvariantCulture);
            using (SHA256 sha256 = SHA256.Create()) {
                byte[] digest = sha256.ComputeHash(Encoding.ASCII.GetBytes(material));
                return BitConverter.ToString(digest).Replace("-", "").ToLowerInvariant();
            }
        }

        private FileInformation OpenComponent(string path, bool holdDeleteAccess) {
            IntPtr handle = CreateFile(
                path,
                0x00100080 | (holdDeleteAccess ? 0x00010000u : 0u),
                0x00000001 | 0x00000002,
                IntPtr.Zero,
                3,
                0x02000000 | 0x00200000,
                IntPtr.Zero
            );
            if (handle == new IntPtr(-1)) throw new IOException("directory open failed");
            handles.Add(handle);
            FileInformation information;
            if (!GetFileInformationByHandle(handle, out information)) {
                throw new IOException("directory identity unavailable");
            }
            if (
                (information.FileAttributes & 0x00000400) != 0 ||
                (information.FileAttributes & 0x00000010) == 0
            ) {
                throw new IOException("directory lease rejected reparse or non-directory");
            }
            return information;
        }

        public static ReliabilityDirectoryLease Acquire(
            string outputRoot,
            string runRoot,
            string privateRecoveryRoot,
            string expectedRootIdentity,
            string expectedRunIdentity,
            string expectedPrivateRootIdentity,
            string expectedPrivateNamespaceIdentity
        ) {
            if (
                String.IsNullOrEmpty(expectedRootIdentity) ||
                String.IsNullOrEmpty(expectedRunIdentity) ||
                String.IsNullOrEmpty(expectedPrivateRootIdentity) ||
                String.IsNullOrEmpty(expectedPrivateNamespaceIdentity) ||
                expectedRootIdentity.Length != 64 ||
                expectedRunIdentity.Length != 64 ||
                expectedPrivateRootIdentity.Length != 64 ||
                expectedPrivateNamespaceIdentity.Length != 64
            ) throw new IOException("directory identity is invalid");
            string output = NormalizeDirectory(outputRoot);
            string run = NormalizeDirectory(runRoot);
            string privateRun = NormalizeDirectory(privateRecoveryRoot);
            string privateDirectory = Path.GetDirectoryName(privateRun);
            string prefix = output.EndsWith("\\", StringComparison.Ordinal) ?
                output : output + "\\";
            string expectedPrivateRun = Path.Combine(
                output, ".private-recovery", Path.GetFileName(run)
            );
            if (
                !run.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(
                    privateRun, expectedPrivateRun, StringComparison.OrdinalIgnoreCase
                )
            ) {
                throw new IOException("run root is outside output root");
            }
            ReliabilityDirectoryLease lease = new ReliabilityDirectoryLease();
            try {
                string root = Path.GetPathRoot(run);
                string current = root;
                bool outputBound = false;
                FileInformation information = lease.OpenComponent(current, false);
                if (String.Equals(current, output, StringComparison.OrdinalIgnoreCase)) {
                    outputBound = Identity(information) == expectedRootIdentity;
                }
                string relative = run.Substring(root.Length);
                foreach (string component in relative.Split(
                    new char[] { '\\' }, StringSplitOptions.RemoveEmptyEntries
                )) {
                    current = Path.Combine(current, component);
                    information = lease.OpenComponent(
                        current,
                        String.Equals(current, run, StringComparison.OrdinalIgnoreCase)
                    );
                    if (String.Equals(current, output, StringComparison.OrdinalIgnoreCase)) {
                        outputBound = Identity(information) == expectedRootIdentity;
                    }
                }
                if (!outputBound || Identity(information) != expectedRunIdentity) {
                    throw new IOException("directory identity changed");
                }
                root = Path.GetPathRoot(privateDirectory);
                current = root;
                information = lease.OpenComponent(current, false);
                relative = privateDirectory.Substring(root.Length);
                foreach (string component in relative.Split(
                    new char[] { '\\' }, StringSplitOptions.RemoveEmptyEntries
                )) {
                    current = Path.Combine(current, component);
                    information = lease.OpenComponent(
                        current,
                        false
                    );
                }
                if (Identity(information) != expectedPrivateNamespaceIdentity) {
                    throw new IOException("private directory identity changed");
                }
                information = lease.OpenComponent(privateRun, true);
                lease.privateRunHandle = lease.handles[lease.handles.Count - 1];
                lease.privateRunPath = privateRun;
                if (Identity(information) != expectedPrivateRootIdentity) {
                    throw new IOException("private run identity changed");
                }
                return lease;
            } catch {
                lease.Dispose();
                throw;
            }
        }

        public void RemovePrivateRunDirectory() {
            if (disposed || privateRunHandle == IntPtr.Zero) {
                throw new IOException("private run lease is unavailable");
            }
            FileDispositionInformation information =
                new FileDispositionInformation { DeleteFile = true };
            if (!SetFileInformationByHandle(
                    privateRunHandle, 4, ref information,
                    (uint)Marshal.SizeOf(typeof(FileDispositionInformation)))) {
                throw new IOException("private run directory could not be removed");
            }
        }

        public void Dispose() {
            if (disposed) return;
            disposed = true;
            for (int index = handles.Count - 1; index >= 0; index--) {
                CloseHandle(handles[index]);
            }
            handles.Clear();
        }
    }
}
'@ -ErrorAction Stop
    }
    return [TtsMore.ReliabilityDirectoryLease]::Acquire(
        $OutputRoot,
        $RunRoot,
        $PrivateRecoveryRoot,
        $ExpectedRootIdentity,
        $ExpectedRunIdentity,
        $ExpectedPrivateRootIdentity,
        $ExpectedPrivateNamespaceIdentity
    )
}

$directoryLease = $null
$formalExitCode = 1
try {
    $outputRootRequest = [IO.Path]::GetFullPath($OutputRoot)
    $ttsMoreRootPath = (Resolve-Path -LiteralPath $TtsMoreRoot -ErrorAction Stop).Path
    $backendRoot = Join-Path $ttsMoreRootPath 'backend'
    $backendPython = Resolve-BackendPython -TtsMoreRootPath $ttsMoreRootPath
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
        $prepared.result.run_root_identity -cnotmatch '^[0-9a-f]{64}$' -or
        $prepared.result.private_root_identity -cnotmatch '^[0-9a-f]{64}$' -or
        $prepared.result.private_namespace_identity -cnotmatch '^[0-9a-f]{64}$'
    ) {
        throw 'Formal run preparation failed'
    }
    $runRootIdentity = [string] $prepared.result.run_root_identity
    $runRootPath = [string] $prepared.result.run_root
    $privateRecoveryRootPath = [string] $prepared.result.private_root
    $privateRootIdentity = [string] $prepared.result.private_root_identity
    $privateNamespaceIdentity = [string] $prepared.result.private_namespace_identity
    if (
        [string]::IsNullOrEmpty($runRootPath) -or
        [string]::IsNullOrEmpty($privateRecoveryRootPath)
    ) {
        throw 'Formal run roots are unavailable'
    }
    $directoryLease = New-ReliabilityDirectoryLease `
        -OutputRoot $outputRootPath -RunRoot $runRootPath `
        -PrivateRecoveryRoot $privateRecoveryRootPath `
        -ExpectedRootIdentity $rootIdentity `
        -ExpectedRunIdentity $runRootIdentity `
        -ExpectedPrivateRootIdentity $privateRootIdentity `
        -ExpectedPrivateNamespaceIdentity $privateNamespaceIdentity
    $validatedBoundary = Invoke-PythonJson `
        -PythonPath $backendPython -BackendRoot $backendRoot `
        -Arguments @(
            '-m', 'app.comfyui.reliability_supervision_cli', 'validate-run-root',
            '--output-root', $outputRootPath,
            '--run-key', $runKey,
            '--expected-root-identity', $rootIdentity,
            '--expected-run-root-identity', $runRootIdentity,
            '--expected-private-root-identity', $privateRootIdentity,
            '--expected-private-namespace-identity', $privateNamespaceIdentity
        )
    if (
        $validatedBoundary.ok -ne $true -or
        $validatedBoundary.result.run_key -cne $runKey -or
        $validatedBoundary.result.root_identity -cne $rootIdentity -or
        $validatedBoundary.result.run_root_identity -cne $runRootIdentity -or
        $validatedBoundary.result.private_root_identity -cne $privateRootIdentity -or
        $validatedBoundary.result.private_namespace_identity -cne $privateNamespaceIdentity -or
        $validatedBoundary.result.private_root -cne $privateRecoveryRootPath
    ) { throw 'Formal run boundary validation failed' }

    $innerArguments = @(
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $innerScript,
        '-Fixture', $Fixture,
        '-OutputRoot', $outputRootPath,
        '-ComfyUiRoot', $ComfyUiRoot,
        '-ComfyPython', $ComfyPython,
        '-TtsMoreRoot', $ttsMoreRootPath,
        '-RunId', $runId,
        '-OutputRootIdentity', $rootIdentity,
        '-RunRootIdentity', $runRootIdentity,
        '-PrivateRecoveryRoot', $privateRecoveryRootPath,
        '-PrivateRecoveryRootIdentity', $privateRootIdentity,
        '-PrivateRecoveryNamespaceIdentity', $privateNamespaceIdentity
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

    $preparedFinalization = Invoke-PythonJson `
        -PythonPath $backendPython -BackendRoot $backendRoot `
        -Arguments @(
            '-m', 'app.comfyui.reliability_supervision_cli', 'prepare-finalize',
            '--output-root', $outputRootPath,
            '--run-key', $runKey,
            '--mode', $mode,
            '--expected-root-identity', $rootIdentity,
            '--expected-run-root-identity', $runRootIdentity,
            '--expected-private-root-identity', $privateRootIdentity,
            '--expected-private-namespace-identity', $privateNamespaceIdentity,
            '--launcher-exit-code', [string] $childExit,
            '--child-start-count', [string] $childStartCount
        )
    if (
        $preparedFinalization.ok -ne $true -or
        $preparedFinalization.result.run_key -cne $runKey -or
        $preparedFinalization.result.cleanup_status -cnotin @(
            'completed', 'failed', 'not-started'
        )
    ) { throw 'Private finalization preparation failed' }
    if ($preparedFinalization.result.cleanup_status -ceq 'completed') {
        $directoryLease.RemovePrivateRunDirectory()
    }

    $finalized = Invoke-PythonJson -PythonPath $backendPython -BackendRoot $backendRoot `
        -Arguments @(
            '-m', 'app.comfyui.reliability_supervision_cli', 'finalize',
            '--output-root', $outputRootPath,
            '--run-key', $runKey,
            '--mode', $mode,
            '--expected-token', $expectedToken,
            '--expected-root-identity', $rootIdentity,
            '--expected-run-root-identity', $runRootIdentity,
            '--expected-private-root-identity', $privateRootIdentity,
            '--expected-private-namespace-identity', $privateNamespaceIdentity,
            '--launcher-exit-code', [string] $childExit,
            '--child-start-count', [string] $childStartCount
        )
    if ($finalized.ok -ne $true) { throw 'Formal supervision commit failed' }
    $formalExitCode = $childExit
} catch {
    [Console]::Error.WriteLine(
        ('Formal reliability supervision failed: {0}' -f $_.Exception.Message)
    )
    $formalExitCode = 1
} finally {
    if ($null -ne $directoryLease) {
        $directoryLease.Dispose()
    }
}
exit $formalExitCode
