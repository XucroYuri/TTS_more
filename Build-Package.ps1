[CmdletBinding()]
param(
    [ValidateSet("Bootstrap", "Full")][string]$Profile = "Bootstrap",
    [ValidateSet("Auto", "CU128", "CU126", "CPU")][string]$Device = "Auto",
    [string]$Version = "0.2.0",
    [string]$OutputRoot = "",
    [string]$WorkRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($Profile -eq "Full" -and $env:GITHUB_ACTIONS -eq "true") { throw "profile=full is local-only and cannot be built by a GitHub upload workflow" }
if ($Version -notmatch "^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$") { throw "package Version must contain only ASCII letters, digits, dot, underscore, or hyphen (maximum 128 characters)" }

if (-not ("TtsMorePortableDirectoryHandle" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public static class TtsMorePortableDirectoryHandle
{
    [StructLayout(LayoutKind.Sequential)]
    private struct ByHandleFileInformation
    {
        public uint FileAttributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct UnicodeString
    {
        public ushort Length;
        public ushort MaximumLength;
        public IntPtr Buffer;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ObjectAttributes
    {
        public int Length;
        public IntPtr RootDirectory;
        public IntPtr ObjectName;
        public uint Attributes;
        public IntPtr SecurityDescriptor;
        public IntPtr SecurityQualityOfService;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoStatusBlock
    {
        public IntPtr Status;
        public IntPtr Information;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct FileDispositionInformation
    {
        [MarshalAs(UnmanagedType.Bool)]
        public bool DeleteFile;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(
        string fileName,
        uint desiredAccess,
        uint shareMode,
        IntPtr securityAttributes,
        uint creationDisposition,
        uint flagsAndAttributes,
        IntPtr templateFile);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(
        SafeFileHandle file,
        out ByHandleFileInformation information);

    [DllImport("ntdll.dll")]
    private static extern int NtCreateFile(
        out IntPtr fileHandle,
        uint desiredAccess,
        ref ObjectAttributes objectAttributes,
        out IoStatusBlock ioStatusBlock,
        IntPtr allocationSize,
        uint fileAttributes,
        uint shareAccess,
        uint createDisposition,
        uint createOptions,
        IntPtr eaBuffer,
        uint eaLength);

    [DllImport("ntdll.dll")]
    private static extern uint RtlNtStatusToDosError(int status);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetFileInformationByHandle(
        SafeFileHandle file,
        int fileInformationClass,
        ref FileDispositionInformation information,
        int bufferSize);

    public static SafeFileHandle Open(string path, bool shareDelete)
    {
        return Open(path, shareDelete, false);
    }

    public static SafeFileHandle Open(string path, bool shareDelete, bool childAccess)
    {
        const uint FileReadAttributes = 0x00000080;
        const uint FileListDirectory = 0x00000001;
        const uint FileAddSubdirectory = 0x00000004;
        const uint FileShareRead = 0x00000001;
        const uint FileShareWrite = 0x00000002;
        const uint FileShareDelete = 0x00000004;
        const uint OpenExisting = 3;
        const uint FileFlagBackupSemantics = 0x02000000;
        const uint FileFlagOpenReparsePoint = 0x00200000;
        uint share = FileShareRead | FileShareWrite | (shareDelete ? FileShareDelete : 0);
        uint access = FileReadAttributes | FileListDirectory | (childAccess ? FileAddSubdirectory : 0);
        SafeFileHandle handle = CreateFile(
            path,
            access,
            share,
            IntPtr.Zero,
            OpenExisting,
            FileFlagBackupSemantics | FileFlagOpenReparsePoint,
            IntPtr.Zero);
        if (handle.IsInvalid)
        {
            int error = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw new Win32Exception(error, "Cannot open controller staging directory by handle: " + path);
        }
        return handle;
    }

    public static SafeFileHandle CreateDirectoryRelative(
        SafeFileHandle parent,
        string name,
        bool shareDelete)
    {
        if (String.IsNullOrEmpty(name) || name == "." || name == ".." || name.Contains("\\") || name.Contains("/"))
        {
            throw new ArgumentException("Unsafe controller staging directory name", "name");
        }
        IntPtr nameBuffer = IntPtr.Zero;
        IntPtr unicodePointer = IntPtr.Zero;
        try
        {
            nameBuffer = Marshal.StringToHGlobalUni(name);
            UnicodeString unicode = new UnicodeString();
            unicode.Length = checked((ushort)(name.Length * 2));
            unicode.MaximumLength = checked((ushort)((name.Length + 1) * 2));
            unicode.Buffer = nameBuffer;
            unicodePointer = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(UnicodeString)));
            Marshal.StructureToPtr(unicode, unicodePointer, false);
            ObjectAttributes attributes = new ObjectAttributes();
            attributes.Length = Marshal.SizeOf(typeof(ObjectAttributes));
            attributes.RootDirectory = parent.DangerousGetHandle();
            attributes.ObjectName = unicodePointer;
            attributes.Attributes = 0x00000040;
            IoStatusBlock ioStatus;
            IntPtr rawHandle;
            const uint DesiredAccess = 0x00100000 | 0x00010000 | 0x00000080 | 0x00000007;
            const uint ShareRead = 0x00000001;
            const uint ShareWrite = 0x00000002;
            const uint ShareDelete = 0x00000004;
            const uint FileCreate = 2;
            const uint FileDirectoryFile = 0x00000001;
            const uint FileSynchronousIoNonalert = 0x00000020;
            const uint FileOpenReparsePoint = 0x00200000;
            uint share = ShareRead | ShareWrite | (shareDelete ? ShareDelete : 0);
            int status = NtCreateFile(
                out rawHandle,
                DesiredAccess,
                ref attributes,
                out ioStatus,
                IntPtr.Zero,
                0x00000010,
                share,
                FileCreate,
                FileDirectoryFile | FileSynchronousIoNonalert | FileOpenReparsePoint,
                IntPtr.Zero,
                0);
            if (status < 0)
            {
                throw new Win32Exception(
                    unchecked((int)RtlNtStatusToDosError(status)),
                    "Cannot atomically create controller staging directory: " + name);
            }
            return new SafeFileHandle(rawHandle, true);
        }
        finally
        {
            if (unicodePointer != IntPtr.Zero) { Marshal.FreeHGlobal(unicodePointer); }
            if (nameBuffer != IntPtr.Zero) { Marshal.FreeHGlobal(nameBuffer); }
        }
    }

    public static void MarkDirectoryForDeletion(SafeFileHandle handle)
    {
        FileDispositionInformation information = new FileDispositionInformation();
        information.DeleteFile = true;
        if (!SetFileInformationByHandle(
            handle,
            4,
            ref information,
            Marshal.SizeOf(typeof(FileDispositionInformation))))
        {
            throw new Win32Exception(
                Marshal.GetLastWin32Error(),
                "Cannot mark verified controller staging directory for deletion");
        }
    }

    public static string Identity(SafeFileHandle handle)
    {
        ByHandleFileInformation information;
        if (!GetFileInformationByHandle(handle, out information))
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot inspect controller staging directory handle");
        }
        const uint FileAttributeReparsePoint = 0x00000400;
        if ((information.FileAttributes & FileAttributeReparsePoint) != 0)
        {
            throw new InvalidOperationException("Controller staging directory handle resolves to a reparse point");
        }
        ulong index = ((ulong)information.FileIndexHigh << 32) | information.FileIndexLow;
        return information.VolumeSerialNumber.ToString("X8") + ":" + index.ToString("X16");
    }

    public static uint NumberOfLinks(string path)
    {
        const uint FileReadAttributes = 0x00000080;
        const uint FileShareRead = 0x00000001;
        const uint FileShareWrite = 0x00000002;
        const uint FileShareDelete = 0x00000004;
        const uint OpenExisting = 3;
        const uint FileFlagOpenReparsePoint = 0x00200000;
        using (SafeFileHandle handle = CreateFile(path, FileReadAttributes,
            FileShareRead | FileShareWrite | FileShareDelete, IntPtr.Zero,
            OpenExisting, FileFlagOpenReparsePoint, IntPtr.Zero))
        {
            if (handle.IsInvalid)
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot inspect staged file link count: " + path);
            }
            ByHandleFileInformation information;
            if (!GetFileInformationByHandle(handle, out information))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot inspect staged file link count: " + path);
            }
            return information.NumberOfLinks;
        }
    }

    public static bool ContainsAnyPrefix(string path, string[] prefixes)
    {
        var patterns = new System.Collections.Generic.List<byte[]>();
        foreach (string prefix in prefixes)
        {
            if (String.IsNullOrEmpty(prefix)) { continue; }
            patterns.Add(System.Text.Encoding.UTF8.GetBytes(prefix));
            patterns.Add(System.Text.Encoding.Unicode.GetBytes(prefix));
        }
        if (patterns.Count == 0) { return false; }
        int maximum = 1;
        foreach (byte[] pattern in patterns) { maximum = Math.Max(maximum, pattern.Length); }
        byte[] buffer = new byte[1048576 + maximum - 1];
        using (var stream = new System.IO.FileStream(path, System.IO.FileMode.Open,
            System.IO.FileAccess.Read, System.IO.FileShare.ReadWrite | System.IO.FileShare.Delete))
        {
            int carry = 0;
            int read;
            while ((read = stream.Read(buffer, carry, 1048576)) > 0)
            {
                int length = carry + read;
                foreach (byte[] pattern in patterns)
                {
                    for (int offset = 0; offset <= length - pattern.Length; offset++)
                    {
                        int index = 0;
                        while (index < pattern.Length && buffer[offset + index] == pattern[index]) { index++; }
                        if (index == pattern.Length) { return true; }
                    }
                }
                carry = Math.Min(maximum - 1, length);
                if (carry > 0) { Buffer.BlockCopy(buffer, length - carry, buffer, 0, carry); }
            }
        }
        return false;
    }
}
'@
}

function Get-PortableFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "").ToLowerInvariant() }
    finally {
        $stream.Dispose()
        $sha256.Dispose()
    }
}

function Assert-PortableWorkPath {
    param([Parameter(Mandatory = $true)][string]$CandidatePath)
    try {
        $fullPath = [IO.Path]::GetFullPath($CandidatePath)
        $volumeRoot = [IO.Path]::GetPathRoot($fullPath)
    }
    catch {
        throw "WorkRoot path validation failed closed. Choose a different -WorkRoot path. Error: $($_.Exception.Message)"
    }
    if ([string]::IsNullOrWhiteSpace($volumeRoot)) { throw "WorkRoot path validation failed closed. Choose an absolute -WorkRoot path." }
    $pathsToCheck = @($volumeRoot)
    $currentPath = $volumeRoot
    foreach ($segment in @($fullPath.Substring($volumeRoot.Length) -split '[\\/]' | Where-Object { $_ })) {
        $currentPath = Join-Path $currentPath $segment
        $pathsToCheck += $currentPath
    }
    foreach ($currentPath in $pathsToCheck) {
        try { $item = Get-Item -LiteralPath $currentPath -Force -ErrorAction Stop }
        catch [System.Management.Automation.ItemNotFoundException] { return $false }
        catch { throw "WorkRoot path validation failed closed. Choose a different -WorkRoot path. Path: $currentPath. Error: $($_.Exception.Message)" }
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "WorkRoot path must not traverse a reparse point. Choose -WorkRoot outside junctions and symbolic links. Path: $currentPath"
        }
        if (!$item.PSIsContainer) { throw "WorkRoot path contains an existing non-directory segment. Choose a different -WorkRoot path. Path: $currentPath" }
    }
    return $true
}

function Update-PortablePathBudget {
    param([string]$ProjectedPath, [ref]$MaximumLength, [ref]$MaximumPath)
    if ($ProjectedPath.Length -gt $MaximumLength.Value) {
        $MaximumLength.Value = $ProjectedPath.Length
        $MaximumPath.Value = $ProjectedPath
    }
}

function Measure-PortableCopyTree {
    param([string]$Source, [string]$Destination, [ref]$MaximumLength, [ref]$MaximumPath)
    Update-PortablePathBudget -ProjectedPath $Destination -MaximumLength $MaximumLength -MaximumPath $MaximumPath
    foreach ($entry in Get-ChildItem -LiteralPath $Source -Force) {
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "controller package source cannot contain a reparse point: $($entry.FullName)" }
        $target = Join-Path $Destination $entry.Name
        Update-PortablePathBudget -ProjectedPath $target -MaximumLength $MaximumLength -MaximumPath $MaximumPath
        if ($entry.PSIsContainer) { Measure-PortableCopyTree -Source $entry.FullName -Destination $target -MaximumLength $MaximumLength -MaximumPath $MaximumPath }
    }
}

function Assert-TtsMoreFullPayloadBoundary {
    param([Parameter(Mandatory = $true)][string]$PackageRoot)
    $manifestPath = Join-Path $PackageRoot "package\tts-more-package.json"
    $payload = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ([string]$payload.component -ne "tts-more" -or $payload.models.required -ne $false) {
        throw "TTS More Full manifest must remain controller-only and device-neutral"
    }
    foreach ($relative in @("models", "app\repo", "app\tts_more")) {
        if (Test-Path -LiteralPath (Join-Path $PackageRoot $relative)) {
            throw "TTS More Full package contains worker runtime or model payload"
        }
    }
}

function Assert-TtsMoreFullRuntimeBoundary {
    param([Parameter(Mandatory = $true)][string]$PackageRoot)
    $forbiddenNames = @("pyvenv.cfg", "conda-meta", "condabin", "Miniforge")
    foreach ($entry in @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -Force)) {
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Full package staging contains a reparse point: $($entry.FullName)"
        }
        foreach ($segment in @($entry.FullName.Substring($PackageRoot.Length).TrimStart("\", "/") -split '[\\/]')) {
            if ($segment -eq "__pycache__" -or $segment.EndsWith(".pyc", [StringComparison]::OrdinalIgnoreCase)) {
                throw "Full package staging contains Python bytecode: $($entry.FullName)"
            }
            if ($forbiddenNames -contains $segment -or $segment -like "Miniforge*") {
                throw "Full package staging contains forbidden portable-runtime content: $($entry.FullName)"
            }
        }
    }
}

function Remove-TtsMoreFullRuntimeBytecode {
    param([Parameter(Mandatory = $true)][string]$PackageRoot)
    $runtimeRoot = [IO.Path]::GetFullPath((Join-Path $PackageRoot "runtime\live"))
    foreach ($directory in @(Get-ChildItem -LiteralPath $runtimeRoot -Directory -Recurse -Force | Where-Object { $_.Name -eq "__pycache__" } | Sort-Object FullName -Descending)) {
        if (($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Full runtime bytecode cleanup refused a reparse point" }
        Remove-Item -LiteralPath $directory.FullName -Recurse -Force
    }
    foreach ($file in @(Get-ChildItem -LiteralPath $runtimeRoot -File -Recurse -Force -Filter "*.pyc")) {
        if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Full runtime bytecode cleanup refused a reparse point" }
        Remove-Item -LiteralPath $file.FullName -Force
    }
    if (@(Get-ChildItem -LiteralPath $runtimeRoot -Recurse -Force | Where-Object { $_.Name -eq "__pycache__" -or $_.Name -like "*.pyc" }).Count -gt 0) {
        throw "Full package staging contains Python bytecode after cleanup"
    }
}

function Assert-TtsMoreFullStagingReparseBoundary {
    param([Parameter(Mandatory = $true)][string]$PackageRoot)
    foreach ($entry in @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -Force)) {
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Full package staging contains a reparse point: $($entry.FullName)"
        }
    }
}

function Assert-TtsMoreFullRuntimeLinkBoundary {
    param([Parameter(Mandatory = $true)][string]$PackageRoot)
    foreach ($file in @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -File -Force)) {
        if ([TtsMorePortableDirectoryHandle]::NumberOfLinks($file.FullName) -gt 1) {
            throw "Full package staging contains a multiply-linked file: $($file.FullName)"
        }
    }
}

function Test-TtsMoreFullRuntimeOnOtherVolume {
    param(
        [Parameter(Mandatory = $true)][string]$PackageRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedPython,
        [Parameter(Mandatory = $true)][string]$ImportProbe
    )
    $sourceRoot = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($PackageRoot))
    $candidates = @(Get-PSDrive -PSProvider FileSystem | Where-Object {
        $_.Root -and ![string]::Equals([IO.Path]::GetPathRoot($_.Root), $sourceRoot, [StringComparison]::OrdinalIgnoreCase)
    })
    $probeRoot = $null
    foreach ($candidate in $candidates) {
        $candidateRoot = Join-Path $candidate.Root ("tts-more-runtime-probe-" + [Guid]::NewGuid().ToString("N"))
        try {
            New-Item -ItemType Directory -Path $candidateRoot -ErrorAction Stop | Out-Null
            $probeRoot = $candidateRoot
            break
        }
        catch { continue }
    }
    if ([string]::IsNullOrWhiteSpace($probeRoot)) { return "not_available" }
    try {
        $runtimeCopy = Join-Path $probeRoot "runtime-live"
        Copy-Item -LiteralPath (Join-Path $PackageRoot "runtime\live") -Destination $runtimeCopy -Recurse
        $python = Join-Path $runtimeCopy "python.exe"
        & $python -c "import platform,sys; raise SystemExit(0 if platform.python_version()==sys.argv[1] else 1)" $ExpectedPython
        if ($LASTEXITCODE -ne 0) { throw "cross-volume embedded Python version probe failed" }
        & $python -c $ImportProbe
        if ($LASTEXITCODE -ne 0) { throw "cross-volume embedded Python import probe failed" }
        return "passed"
    }
    finally {
        if (Test-Path -LiteralPath $probeRoot) { Remove-Item -LiteralPath $probeRoot -Recurse -Force }
    }
}

function Test-PortableBinaryContainsMachinePrefix {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string[]]$Prefixes)
    return [TtsMorePortableDirectoryHandle]::ContainsAnyPrefix($Path, $Prefixes)
}

function Assert-TtsMoreFullArchiveBoundary {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)
    Add-Type -AssemblyName System.IO.Compression
    $stream = [IO.File]::OpenRead($ArchivePath)
    try {
        $archive = New-Object IO.Compression.ZipArchive($stream, [IO.Compression.ZipArchiveMode]::Read, $false)
        try {
            foreach ($entry in $archive.Entries) {
                foreach ($segment in @($entry.FullName -split '[/\\]')) {
                    if ($segment -eq "__pycache__" -or $segment.EndsWith(".pyc", [StringComparison]::OrdinalIgnoreCase)) {
                        throw "Full package archive contains Python bytecode: $($entry.FullName)"
                    }
                    if ($segment -in @("pyvenv.cfg", "conda-meta", "condabin", "Miniforge") -or $segment -like "Miniforge*") {
                        throw "Full package archive contains forbidden portable-runtime content: $($entry.FullName)"
                    }
                }
            }
        }
        finally { $archive.Dispose() }
    }
    finally { $stream.Dispose() }
}

$Root = [System.IO.Path]::GetFullPath($PSScriptRoot)
$profileName = $Profile.ToLowerInvariant()
if ([string]::IsNullOrWhiteSpace($OutputRoot)) { $OutputRoot = Join-Path $Root "artifacts\portable\$profileName" }
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
$packageName = if ($Profile -eq "Full") { "TTS-More-$Version-windows-x64-full-staging" } else { "TTS-More-$Version-windows-x64-$profileName" }
$workBase = if ($WorkRoot) { [IO.Path]::GetFullPath($WorkRoot) } else { [IO.Path]::GetFullPath([IO.Path]::GetTempPath()) }
$normalizedSourceRoot = $Root.TrimEnd("\", "/")
$normalizedWorkBase = $workBase.TrimEnd("\", "/")
if (
    [string]::Equals($normalizedWorkBase, $normalizedSourceRoot, [StringComparison]::OrdinalIgnoreCase) -or
    $normalizedWorkBase.StartsWith($normalizedSourceRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
) { throw "WorkRoot must be outside source checkout. Set -WorkRoot to a directory outside '$Root' (for example C:\tm)." }
[void](Assert-PortableWorkPath -CandidatePath $workBase)
$workIdentity = "tts-more-controller-$PID-$([Guid]::NewGuid().ToString('N').Substring(0, 12))"
$work = [IO.Path]::GetFullPath((Join-Path $workBase $workIdentity))
$stage = Join-Path $work $packageName

& pnpm --dir (Join-Path $Root "frontend") build
if ($LASTEXITCODE -ne 0) { throw "frontend production build failed" }
$buildPythonOutput = @(& (Join-Path $Root "scripts\Resolve-PortableBuildPython.ps1") `
    -PackageRoot $Root `
    -BuildToolsRoot (Join-Path $Root "integrations\build_tools") `
    -BootstrapCondaPath (Join-Path $Root "scripts\bootstrap-conda.ps1") `
    -ToolchainLockPath (Join-Path $Root "packaging\portable\toolchain.lock.json") `
    -PortableInstallPath (Join-Path $Root "scripts\portable_install.py"))
if ($LASTEXITCODE -ne 0 -or $buildPythonOutput.Count -eq 0) { throw "portable build-tools bootstrap failed" }
$buildPython = [IO.Path]::GetFullPath([string]$buildPythonOutput[-1])
& $buildPython (Join-Path $Root "scripts\portable_packages.py") audit-builder-source --root $Root --component tts-more --profile $profileName
if ($LASTEXITCODE -ne 0) { throw "TTS More source dirty: copied source audit failed" }
$maximumPathLength = 0
$maximumProjectedPath = ""
Measure-PortableCopyTree -Source (Join-Path $Root "backend\app") -Destination (Join-Path $stage "app\backend\app") -MaximumLength ([ref]$maximumPathLength) -MaximumPath ([ref]$maximumProjectedPath)
Measure-PortableCopyTree -Source (Join-Path $Root "frontend\dist") -Destination (Join-Path $stage "app\frontend") -MaximumLength ([ref]$maximumPathLength) -MaximumPath ([ref]$maximumProjectedPath)
foreach ($projected in @(
    (Join-Path $stage "package\tts-more-package.json"),
    (Join-Path $stage "licenses\THIRD_PARTY_NOTICES.json"),
    (Join-Path $stage "packaging\portable\tts-more-package.schema.json"),
    (Join-Path $stage "scripts\import_portable_data.py"),
    (Join-Path $stage "SHA256SUMS.txt")
)) { Update-PortablePathBudget -ProjectedPath $projected -MaximumLength ([ref]$maximumPathLength) -MaximumPath ([ref]$maximumProjectedPath) }
if ($maximumPathLength -gt 240) {
    throw "controller package staging path budget exceeded before copy: projected path length $maximumPathLength exceeds the safe Windows limit 240. Use -WorkRoot with a shorter external directory (for example C:\tm). Projected path: $maximumProjectedPath"
}
$createdWorkHandle = $null
$createdWorkIdentity = $null
$workCreated = $false
$workBaseHandle = $null
$isolatedUvCache = ""
try {
New-Item -ItemType Directory -Force -Path $workBase | Out-Null
[void](Assert-PortableWorkPath -CandidatePath $workBase)
$workBaseHandle = [TtsMorePortableDirectoryHandle]::Open($workBase, $false, $true)
$createdWorkHandle = [TtsMorePortableDirectoryHandle]::CreateDirectoryRelative($workBaseHandle, $workIdentity, $false)
$workCreated = $true
$createdWorkIdentity = [TtsMorePortableDirectoryHandle]::Identity($createdWorkHandle)
New-Item -ItemType Directory -Force -Path $stage, (Join-Path $stage "app\backend"), (Join-Path $stage "package"), (Join-Path $stage "scripts"), (Join-Path $stage "packaging\portable"), (Join-Path $stage "licenses") | Out-Null
[void](Assert-PortableWorkPath -CandidatePath $stage)

Copy-Item -LiteralPath (Join-Path $Root "backend\app") -Destination (Join-Path $stage "app\backend\app") -Recurse
foreach ($file in @("pyproject.toml", "uv.lock", ".python-version")) { Copy-Item -LiteralPath (Join-Path $Root "backend\$file") -Destination (Join-Path $stage "app\backend\$file") }
Copy-Item -LiteralPath (Join-Path $Root "frontend\dist") -Destination (Join-Path $stage "app\frontend") -Recurse
foreach ($file in @("bootstrap-conda.ps1", "portable-python.ps1", "initialize-portable.ps1", "repair-portable.ps1", "start-production.ps1", "stop-production.ps1", "Invoke-PortableStart.ps1", "Show-PortableProgress.ps1", "Portable-Validation.ps1", "select-portable-folder.ps1", "export-portable-diagnostics.py", "import-portable-data.py", "import_portable_data.py", "portable_install.py", "portable_launcher.py", "portable_operations.py", "portable_packages.py", "portable_package_runner.py")) { Copy-Item -LiteralPath (Join-Path $Root "scripts\$file") -Destination (Join-Path $stage "scripts\$file") }
foreach ($file in @("toolchain.lock.json", "runtime.lock.json", "models.lock.json", "tts-more-package.schema.json", "error-catalog.zh-CN.json")) { Copy-Item -LiteralPath (Join-Path $Root "packaging\portable\$file") -Destination (Join-Path $stage "packaging\portable\$file") }
@(Get-ChildItem -LiteralPath $stage -Directory -Recurse -Force | Where-Object { $_.Name -in @("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache") } | Sort-Object FullName -Descending) | ForEach-Object {
    $resolved = [System.IO.Path]::GetFullPath($_.FullName)
    if (!$resolved.StartsWith([System.IO.Path]::GetFullPath($stage), [System.StringComparison]::OrdinalIgnoreCase)) { throw "refusing to clean outside package stage: $resolved" }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
foreach ($file in @("Initialize.cmd", "Start.cmd", "Stop.cmd", "Repair.cmd")) {
    Copy-Item -LiteralPath (Join-Path $Root $file) -Destination (Join-Path $stage $file)
}
$GuideFileName = [string]::Concat([char]0x4F7F, [char]0x7528, [char]0x8BF4, [char]0x660E, "-", [char]0x5148, [char]0x770B, [char]0x8FD9, [char]0x91CC, ".txt")
Copy-Item -LiteralPath (Join-Path $Root "LICENSE") -Destination (Join-Path $stage "licenses\LICENSE")
Copy-Item -LiteralPath (Join-Path $Root "NOTICE") -Destination (Join-Path $stage "licenses\NOTICE")
Copy-Item -LiteralPath (Join-Path $Root "repo.lock.json") -Destination (Join-Path $stage "package\repo.lock.json")
Copy-Item -LiteralPath (Join-Path (Join-Path $Root "packaging\portable") $GuideFileName) -Destination (Join-Path $stage $GuideFileName)
@'
throw "This delivered portable package cannot rebuild itself. Use the corresponding source checkout and its Build-Package.ps1."
'@ | Set-Content -LiteralPath (Join-Path $stage "Build-Package.ps1") -Encoding ASCII

$revision = (& git -C $Root rev-parse HEAD).Trim()
$integrationFiles = @(Get-ChildItem -LiteralPath (Join-Path $stage "scripts") -File | Sort-Object FullName)
$integrationDigestText = ($integrationFiles | ForEach-Object { Get-PortableFileSha256 -Path $_.FullName }) -join "`n"
$sha256 = [System.Security.Cryptography.SHA256]::Create()
$bundleSha = ([BitConverter]::ToString($sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($integrationDigestText)))).Replace("-", "").ToLowerInvariant()
$manifest = [ordered]@{
    schema_version = 2; component = "tts-more"; package_id = "tts-more"; release_version = $Version
    version = $Version; build_id = "tts-more-$Version-$($revision.Substring(0, 12))"
    package_profile = $profileName; platform = "windows-x64"; api_contract = "tts-more-v1"
    protocol = @{ name = "tts-more-v1"; version = "1.0"; controller_range = ">=0.2.0,<0.3.0" }
    source = @{ repository = "https://github.com/XucroYuri/TTS_more.git"; revision = $revision }
    integration = @{ version = "2.0.0"; source_revision = $revision; bundle_sha256 = $bundleSha }
    runtime = @{ python_version = "3.11.9"; device_profiles = @("cpu"); lock = "packaging/portable/runtime.lock.json"; state_path = "data/local/install-state.json" }
    models = @{ lock = "packaging/portable/models.lock.json"; required = $false }
    data_root = "data/local"
    data = @{ user = "data/user"; local = "data/local"; cache = "data/cache"; operations = "data/local/operations" }
    launchers = @{ initialize = "Initialize.cmd"; start = "Start.cmd"; stop = "Stop.cmd"; repair = "Repair.cmd"; build = "Build-Package.ps1" }
    endpoint = @{ default_url = "http://127.0.0.1:8000"; port = 8000; health_path = "/api/health"; capabilities_path = "/api/open-source-tts/catalog"; bind_policy = "loopback" }
    capabilities = @("orchestrator", "package-discovery", "artifact-transfer", "trusted-lan-registration")
    sha256_manifest = "SHA256SUMS.txt"; licenses = "licenses/THIRD_PARTY_NOTICES.json"
}
$manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $stage "package\tts-more-package.json") -Encoding UTF8
@{ schema_version = 1; component = "tts-more"; packages = @(); upstream_repositories = @("GPT-SoVITS", "IndexTTS", "CosyVoice") } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $stage "licenses\THIRD_PARTY_NOTICES.json") -Encoding UTF8

if ($Profile -eq "Full") {
    $previousUvCache = $env:UV_CACHE_DIR
    $isolatedUvCache = Join-Path $work "uv-cache"
    try {
        $env:UV_CACHE_DIR = $isolatedUvCache
        & (Join-Path $stage "scripts\initialize-portable.ps1") -Device CPU
        if ($LASTEXITCODE -ne 0) { throw "full package initialization failed" }
    }
    finally {
        $env:UV_CACHE_DIR = $previousUvCache
        if (Test-Path -LiteralPath $isolatedUvCache) { Remove-Item -LiteralPath $isolatedUvCache -Recurse -Force }
    }
    Assert-TtsMoreFullStagingReparseBoundary -PackageRoot $stage
    Remove-TtsMoreFullRuntimeBytecode -PackageRoot $stage
    Assert-TtsMoreFullPayloadBoundary -PackageRoot $stage
    Assert-TtsMoreFullRuntimeBoundary -PackageRoot $stage
    Assert-TtsMoreFullRuntimeLinkBoundary -PackageRoot $stage
    $runtimeLockForProbe = Get-Content -LiteralPath (Join-Path $stage "packaging\portable\runtime.lock.json") -Raw | ConvertFrom-Json
    $runtimeCrossVolumeProbe = Test-TtsMoreFullRuntimeOnOtherVolume -PackageRoot $stage -ExpectedPython ([string]$runtimeLockForProbe.python_version) -ImportProbe ([string]$runtimeLockForProbe.import_probe)
}

$sumPath = Join-Path $stage "SHA256SUMS.txt"
@(Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object { $_.FullName -ne $sumPath } | Sort-Object FullName | ForEach-Object {
    $relative = $_.FullName.Substring($stage.Length).TrimStart("\", "/").Replace("\", "/")
    "$(Get-PortableFileSha256 -Path $_.FullName)  $relative"
}) | Set-Content -LiteralPath $sumPath -Encoding UTF8

if ($Profile -eq "Bootstrap") {
    $forbidden = @(Get-ChildItem -LiteralPath $stage -Recurse -Force | Where-Object {
        $_.FullName -match "[\\/](\.venv|runtime[\\/]live|models?|cache|projects?)([\\/]|$)" -or $_.Name -match "\.(safetensors|ckpt|pth|pt)$"
    })
    if ($forbidden.Count -gt 0) { throw "bootstrap package contains forbidden local/full assets: $($forbidden.FullName -join ', ')" }
}
$machinePaths = @(
    $Root,
    $env:USERPROFILE,
    "$($env:HOMEDRIVE)$($env:HOMEPATH)"
) | Where-Object { ![string]::IsNullOrWhiteSpace([string]$_) -and ([string]$_).Length -ge 4 } | Select-Object -Unique
$fullRuntimeMachinePrefixes = @($machinePaths) + @(
    $work,
    $stage,
    $workBase,
    $isolatedUvCache,
    [IO.Path]::GetTempPath(),
    $env:TEMP,
    $env:TMP
) | Where-Object { ![string]::IsNullOrWhiteSpace([string]$_) -and ([string]$_).Length -ge 3 } | Select-Object -Unique
$machineNames = @($env:USERNAME, $env:COMPUTERNAME) | Where-Object { ![string]::IsNullOrWhiteSpace([string]$_) -and ([string]$_).Length -ge 4 } | Select-Object -Unique
$portableTextExtensions = @(".json", ".toml", ".txt", ".md", ".py", ".ps1", ".cmd", ".cfg", ".ini", ".pth", "._pth", ".lock", ".yaml", ".yml")
$runtimeLiveRoot = [IO.Path]::GetFullPath((Join-Path $stage "runtime\live"))
$machinePathLeak = @(Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object {
    !$_.FullName.StartsWith($runtimeLiveRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -and
    $_.Length -lt 5MB -and ($_.Extension -in $portableTextExtensions -or $_.Name -like "*._pth")
} | Select-String -SimpleMatch -Pattern $fullRuntimeMachinePrefixes -ErrorAction SilentlyContinue)
$runtimeBinaryLeak = @()
if ($Profile -eq "Full") {
    $runtimeMetadata = @(Get-ChildItem -LiteralPath $runtimeLiveRoot -Recurse -File -Force)
    $runtimeBinaryLeak = @($runtimeMetadata | Where-Object { Test-PortableBinaryContainsMachinePrefix -Path $_.FullName -Prefixes $fullRuntimeMachinePrefixes })
}
$generatedMetadata = @((Join-Path $stage "package\tts-more-package.json"), (Join-Path $stage "licenses\THIRD_PARTY_NOTICES.json"))
$generatedMetadata = @($generatedMetadata | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
$machineNameLeak = @()
if (@($machineNames).Count -gt 0 -and $generatedMetadata.Count -gt 0) { $machineNameLeak = @(Select-String -LiteralPath $generatedMetadata -SimpleMatch -Pattern $machineNames -ErrorAction SilentlyContinue) }
if ($machinePathLeak.Count -gt 0) { throw "package contains build-machine identity or path data: $($machinePathLeak[0].Path)" }
if ($runtimeBinaryLeak.Count -gt 0) { throw "package runtime metadata contains build-machine path data: $($runtimeBinaryLeak[0].FullName)" }
if ($machineNameLeak.Count -gt 0) { throw "package metadata contains build-machine identity data: $($machineNameLeak[0].Path)" }

if ($Profile -eq "Full") {
    $packageNameOutput = @(& $buildPython (Join-Path $Root "scripts\portable_packages.py") full-package-name --component tts-more --version $Version --resolved-profile cpu 2>&1)
    $packageNameExit = $LASTEXITCODE
    if ($packageNameExit -ne 0 -or $packageNameOutput.Count -ne 1 -or [string]::IsNullOrWhiteSpace([string]$packageNameOutput[0])) {
        throw "shared Full package naming rule failed for TTS More"
    }
    $packageName = ([string]$packageNameOutput[0]).Trim()
    if ($packageName.EndsWith(".zip", [StringComparison]::OrdinalIgnoreCase)) { $packageName = $packageName.Substring(0, $packageName.Length - 4) }
}
& $buildPython (Join-Path $stage "scripts\portable_packages.py") validate-manifest --manifest (Join-Path $stage "package\tts-more-package.json") --package-root $stage
if ($LASTEXITCODE -ne 0) { throw "staged package manifest failed schema v2 validation" }
& $buildPython (Join-Path $stage "scripts\portable_packages.py") verify-sha256 --package-root $stage
if ($LASTEXITCODE -ne 0) { throw "staged package SHA256SUMS exact coverage validation failed" }

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$zip = Join-Path $OutputRoot "$packageName.zip"
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
& $buildPython (Join-Path $stage "scripts\portable_packages.py") create-zip --package-root $stage --output $zip --archive-root $packageName
if ($LASTEXITCODE -ne 0) { throw "ZIP64 package creation failed" }
if ($Profile -eq "Full") { Assert-TtsMoreFullArchiveBoundary -ArchivePath $zip }
$auditPassed = $false
if ($Profile -eq "Bootstrap") {
    & $buildPython (Join-Path $stage "scripts\portable_packages.py") audit-release --zip $zip
    if ($LASTEXITCODE -ne 0) { throw "GitHub bootstrap release audit failed" }
    $auditPassed = $true
}
$zipSha = Get-PortableFileSha256 -Path $zip
"$zipSha  $([System.IO.Path]::GetFileName($zip))" | Set-Content -LiteralPath "$zip.sha256" -Encoding ASCII
$provenance = [ordered]@{ schema_version = 1; component = "tts-more"; version = $Version; profile = $profileName; source_revision = $revision; sha256 = $zipSha }
if ($Profile -eq "Full") { $provenance.resolved_profile = "cpu" }
$provenance | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath "$zip.provenance.json" -Encoding UTF8
$packages = @()
$lockText = Get-Content -LiteralPath (Join-Path $Root "backend\uv.lock") -Raw
foreach ($match in [regex]::Matches($lockText, '(?ms)\[\[package\]\]\s+name\s*=\s*"([^"]+)"\s+version\s*=\s*"([^"]+)"')) {
    $spdxId = ($match.Groups[1].Value -replace '[^A-Za-z0-9.-]', '-')
    $packages += @{ SPDXID="SPDXRef-Package-$spdxId"; name=$match.Groups[1].Value; versionInfo=$match.Groups[2].Value; downloadLocation="NOASSERTION"; filesAnalyzed=$false }
}
$deliveryResolvedProfile = if ($Profile -eq "Full") { "cpu" } else { "none" }
$deliveryComment = "TTS-More delivery binding: component=tts-more;version=$Version;profile=$profileName;resolved_profile=$deliveryResolvedProfile;source_revision=$revision;sha256=$zipSha"
@{ spdxVersion="SPDX-2.3"; dataLicense="CC0-1.0"; SPDXID="SPDXRef-DOCUMENT"; name=$packageName; documentNamespace="https://tts-more.local/spdx/tts-more/$Version/$zipSha"; comment=$deliveryComment; creationInfo=@{created=[DateTime]::UtcNow.ToString("o");creators=@("Tool: TTS-More-Build-Package-2.0.0")}; packages=$packages } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath "$zip.spdx.json" -Encoding UTF8
$licenseSidecar = Get-Content -LiteralPath (Join-Path $stage "licenses\THIRD_PARTY_NOTICES.json") -Raw | ConvertFrom-Json
$licenseDelivery = [ordered]@{ component="tts-more"; version=$Version; profile=$profileName; source_revision=$revision; sha256=$zipSha }
if ($Profile -eq "Full") { $licenseDelivery.resolved_profile = "cpu" }
$licenseSidecar | Add-Member -NotePropertyName delivery -NotePropertyValue $licenseDelivery -Force
$licenseSidecar | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath "$zip.licenses.json" -Encoding UTF8
$acceptance = [ordered]@{ schema_version=1; component="tts-more"; version=$Version; profile=$profileName; source_revision=$revision; sha256=$zipSha; manifest_valid=$true; schema_audit=$true; path_audit=$true; sha256_manifest_audit=$true; bootstrap_audit=$auditPassed; machine_path_scan=$true; generated_at=[DateTime]::UtcNow.ToString("o") }
if ($Profile -eq "Full") {
    $acceptance.resolved_profile = "cpu"
    $acceptance.runtime_reparse_scan = $true
    $acceptance.runtime_hardlink_scan = $true
    $acceptance.runtime_cross_volume_probe = $runtimeCrossVolumeProbe
}
$acceptance | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath "$zip.acceptance.json" -Encoding UTF8
Write-Host "Created $Profile package: $zip"
}
finally {
    try {
        if ($workCreated) {
            $workPathExists = Assert-PortableWorkPath -CandidatePath $work
            if (!$workPathExists) {
                throw "controller package cleanup path disappeared after creation; refusing path-based cleanup: $work"
            }
            $resolvedWork = [IO.Path]::GetFullPath($work)
            $resolvedParent = [IO.Path]::GetFullPath((Split-Path -Parent $resolvedWork))
            $resolvedLeaf = Split-Path -Leaf $resolvedWork
            if (![string]::Equals($resolvedParent.TrimEnd("\", "/"), $workBase.TrimEnd("\", "/"), [StringComparison]::OrdinalIgnoreCase) -or $resolvedLeaf -ne $workIdentity) {
                throw "refusing to clean a controller package staging directory that is not the unique directory created by this build: $resolvedWork"
            }
            $cleanupIdentity = [TtsMorePortableDirectoryHandle]::Identity($createdWorkHandle)
            if (![string]::Equals($cleanupIdentity, $createdWorkIdentity, [StringComparison]::Ordinal)) {
                throw "controller package staging handle identity changed unexpectedly: $resolvedWork"
            }
            foreach ($child in @(Get-ChildItem -LiteralPath $resolvedWork -Force)) {
                Remove-Item -LiteralPath $child.FullName -Recurse -Force
            }
            if (@(Get-ChildItem -LiteralPath $resolvedWork -Force).Count -ne 0) {
                throw "controller package staging directory is not empty after child cleanup; refusing recursive root deletion: $resolvedWork"
            }
            [TtsMorePortableDirectoryHandle]::MarkDirectoryForDeletion($createdWorkHandle)
            $createdWorkHandle.Dispose()
            $createdWorkHandle = $null
            $workCreated = $false
        }
    }
    finally {
        if ($createdWorkHandle -ne $null) { $createdWorkHandle.Dispose() }
        if ($workBaseHandle -ne $null) { $workBaseHandle.Dispose() }
    }
}
