param(
    [Parameter(Mandatory=$true)][string]$InputPath,
    [Parameter(Mandatory=$true)][string]$OutputDir,
    [string]$Report = "",
    [string]$TracePath = ""
)

$ErrorActionPreference = 'Stop'
$inputResolved = (Resolve-Path -LiteralPath $InputPath).Path
[System.IO.Directory]::CreateDirectory($OutputDir) | Out-Null
$outputResolved = (Resolve-Path -LiteralPath $OutputDir).Path
$OfficeComPathBudget = 255

function Write-ExportTrace {
    param(
        [string]$Engine,
        [string]$Stage,
        [hashtable]$Data = @{}
    )

    if ([string]::IsNullOrWhiteSpace($TracePath)) { return }
    $traceParent = Split-Path -Parent $TracePath
    if ($traceParent) { [System.IO.Directory]::CreateDirectory($traceParent) | Out-Null }
    $record = [ordered]@{
        ts = [DateTime]::UtcNow.ToString('o')
        engine = $Engine
        stage = $Stage
    }
    foreach ($key in @($Data.Keys)) {
        $record[$key] = $Data[$key]
    }
    $line = ($record | ConvertTo-Json -Compress -Depth 5) + [Environment]::NewLine
    [System.IO.File]::AppendAllText($TracePath, $line, [System.Text.UTF8Encoding]::new($false))
}

function Get-Sha256 {
    param([string]$Path)

    $stream = $null
    $sha = $null
    try {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $bytes = $sha.ComputeHash($stream)
        return ([System.BitConverter]::ToString($bytes)).Replace('-', '')
    } finally {
        if ($null -ne $sha) { $sha.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

function New-CleanupState {
    return [ordered]@{
        temp_pdf_removed = $false
        working_copy_removed = $false
        old_target_preserved = $true
        document_closed = $false
        quit_called = $false
        com_released = $false
        exit_observed = $false
        exit_wait_ms = 0
        remaining_owned_pids = @()
    }
}

function New-EngineReport {
    param(
        [string]$Engine,
        [string]$Role,
        [string]$RequiredIdentity,
        [string]$RequestedProgId,
        [string]$ResolvedClsid,
        [object[]]$RegistryCandidates,
        [string]$ResolvedExecutable,
        [string]$TargetPdf
    )

    return [ordered]@{
        engine = $Engine
        role = $Role
        required_identity = $RequiredIdentity
        requested_progid = $RequestedProgId
        resolved_clsid = $ResolvedClsid
        registry_candidates = @($RegistryCandidates)
        resolved_executable = $ResolvedExecutable
        input_sha256_before = ""
        input_sha256_after = ""
        input_unchanged = $false
        working_copy = ""
        working_copy_path_length = 0
        office_com_path_budget = $OfficeComPathBudget
        application_path = ""
        application_version = ""
        hwnd = $null
        pid = $null
        process_image = ""
        baseline_pids = @()
        ignored_non_writer_processes = @()
        new_pids = @()
        activation_call_count = 0
        rot_poll_count = 0
        activation_attempts = @()
        open_attempts = @()
        worker_host = $null
        identity_verified = $false
        ownership_verified = $false
        initial_documents_count = $null
        startup_documents_closed = 0
        startup_document_evidence = @()
        pdf = $TargetPdf
        temp_pdf = ""
        temp_pdf_path_length = 0
        bytes = $null
        sha256 = ""
        cleanup = New-CleanupState
        status = 'BLOCKED'
        message = ""
    }
}

function Get-WpsNonWriterHelperEvidence {
    param([object]$Process)

    try {
        $record = Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId = {0}' -f [int]$Process.Id) -ErrorAction Stop
        if ($null -eq $record) { return $null }
        $commandLine = [string]$record.CommandLine
        $isJsapiHelper = ($commandLine -match '(?i)\bRun\s+-Entry=EntryPoint\b') -and
            ($commandLine -match '(?i)[\\/]addons[\\/].*jsapibrowser\.dll')
        $isAspCenterHelper = ($commandLine -match '(?i)\bRun\s+-Entry=EntryPoint\b') -and
            ($commandLine -match '(?i)[\\/]aspcenter_[^\\/\s]+[\\/]aspcenter\.dll') -and
            ($commandLine -match '(?i)(?:^|\s)--host\b')
        $isCefRenderer = ($commandLine -match '(?i)(?:^|\s)-Entry=CefRenderEntryPoint\b') -and
            ($commandLine -match '(?i)promecefpluginhost\.exe') -and
            ($commandLine -match '(?i)--type=renderer\b')
        if (-not $isJsapiHelper -and -not $isAspCenterHelper -and -not $isCefRenderer) {
            return $null
        }
        $parent = $null
        $parentUnavailable = $false
        try {
            $parent = Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId = {0}' -f [int]$record.ParentProcessId) -ErrorAction Stop
            if ($null -eq $parent) { $parentUnavailable = $true }
        } catch {
            if (-not $isAspCenterHelper) { return $null }
            $parentUnavailable = $true
        }
        if ($parentUnavailable -and -not $isAspCenterHelper) { return $null }
        $parentClassification = ''
        if ($parentUnavailable) {
            $parentClassification = 'parent-unavailable-aspcenter-helper'
        } elseif ($isJsapiHelper -or $isAspCenterHelper) {
            if ([string]$parent.Name -ine 'wpscloudsvr.exe') { return $null }
        } else {
            if ([string]$parent.Name -ine 'wps.exe') { return $null }
            $parentProcess = Get-Process -Id ([int]$parent.ProcessId) -ErrorAction Stop
            $parentEvidence = Get-WpsNonWriterHelperEvidence -Process $parentProcess
            if ($null -eq $parentEvidence -or [string]$parentEvidence.classification -notin @('wps-cloud-jsapi-helper', 'wps-cloud-aspcenter-helper')) {
                return $null
            }
            $parentClassification = [string]$parentEvidence.classification
        }
        $classification = if ($isCefRenderer) {
            'wps-cloud-cef-renderer'
        } elseif ($isAspCenterHelper) {
            'wps-cloud-aspcenter-helper'
        } else {
            'wps-cloud-jsapi-helper'
        }
        return [ordered]@{
            pid = [int]$Process.Id
            parent_pid = [int]$record.ParentProcessId
            parent_name = if ($null -eq $parent) { '' } else { [string]$parent.Name }
            classification = $classification
            parent_classification = $parentClassification
            parent_unavailable = $parentUnavailable
        }
    } catch {
        # If helper identity cannot be proven, keep treating the process as Writer.
        return $null
    }
}

function Get-EngineProcessInventory {
    param([string]$Engine)

    $names = if ($Engine -eq 'word') { @('WINWORD') } else { @('wps') }
    $active = @()
    $ignored = @()
    foreach ($name in $names) {
        $processes = @(Get-Process -Name $name -ErrorAction SilentlyContinue)
        foreach ($process in $processes) {
            if ($Engine -eq 'wps') {
                $helperEvidence = Get-WpsNonWriterHelperEvidence -Process $process
                if ($null -ne $helperEvidence) {
                    $ignored += ,$helperEvidence
                    continue
                }
            }
            $active += ,$process
        }
    }
    return [pscustomobject]@{
        active = @($active | Sort-Object Id -Unique)
        ignored = @($ignored | Sort-Object { $_.pid } -Unique)
    }
}

function Get-BaselinePids {
    param([string]$Engine)

    $inventory = Get-EngineProcessInventory -Engine $Engine
    return @($inventory.active | ForEach-Object { [int]$_.Id })
}

function Get-ExistingWinwordPath {
    param([string]$CommandLine)

    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $null
    }

    $patterns = @(
        '(?i)"(?<path>[^"]*\\WINWORD\.EXE)"',
        '(?i)(?<path>[A-Za-z]:\\[^"\r\n]*?\\WINWORD\.EXE)(?=\s|$)'
    )
    foreach ($pattern in $patterns) {
        $match = [regex]::Match($CommandLine, $pattern)
        if ($match.Success) {
            $candidate = $match.Groups['path'].Value.Trim()
            if ((Test-Path -LiteralPath $candidate -PathType Leaf) -and
                ([System.IO.Path]::GetFileName($candidate) -ieq 'WINWORD.EXE')) {
                return (Resolve-Path -LiteralPath $candidate).Path
            }
        }
    }
    return $null
}

function Get-RegisteredClsid {
    param([Microsoft.Win32.RegistryKey]$BaseKey, [string]$ProgId)

    $progidKey = $null
    $clsidKey = $null
    $curVerKey = $null
    $curVerProgidKey = $null
    $curVerClsidKey = $null
    try {
        $progidKey = $BaseKey.OpenSubKey(('Software\Classes\' + $ProgId))
        if ($null -eq $progidKey) { return '' }

        $clsid = ''
        $clsidKey = $progidKey.OpenSubKey('CLSID')
        if ($null -ne $clsidKey) {
            $clsid = [string]$clsidKey.GetValue('', $null)
        }
        if ([string]::IsNullOrWhiteSpace($clsid)) {
            $clsid = [string]$progidKey.GetValue('CLSID', $null)
        }
        if ([string]::IsNullOrWhiteSpace($clsid)) {
            $directValue = [string]$progidKey.GetValue('', $null)
            if ($directValue -match '^\{[0-9A-Fa-f-]{36}\}$') {
                $clsid = $directValue
            }
        }
        if ([string]::IsNullOrWhiteSpace($clsid)) {
            $curVerKey = $progidKey.OpenSubKey('CurVer')
            $curVer = if ($null -ne $curVerKey) {
                [string]$curVerKey.GetValue('', $null)
            } else {
                [string]$progidKey.GetValue('CurVer', $null)
            }
            if (-not [string]::IsNullOrWhiteSpace($curVer)) {
                $curVerProgidKey = $BaseKey.OpenSubKey(('Software\Classes\' + $curVer))
                if ($null -ne $curVerProgidKey) {
                    $curVerClsidKey = $curVerProgidKey.OpenSubKey('CLSID')
                    if ($null -ne $curVerClsidKey) {
                        $clsid = [string]$curVerClsidKey.GetValue('', $null)
                    }
                    if ([string]::IsNullOrWhiteSpace($clsid)) {
                        $directValue = [string]$curVerProgidKey.GetValue('', $null)
                        if ($directValue -match '^\{[0-9A-Fa-f-]{36}\}$') {
                            $clsid = $directValue
                        }
                    }
                }
            }
        }
        return ([string]$clsid).Trim()
    } finally {
        if ($null -ne $curVerClsidKey) { $curVerClsidKey.Close() }
        if ($null -ne $curVerProgidKey) { $curVerProgidKey.Close() }
        if ($null -ne $curVerKey) { $curVerKey.Close() }
        if ($null -ne $clsidKey) { $clsidKey.Close() }
        if ($null -ne $progidKey) { $progidKey.Close() }
    }
}

function Resolve-WordRegistry {
    $candidates = @()
    foreach ($viewName in @('Registry64', 'Registry32')) {
        $candidate = [ordered]@{
            view = $viewName
            progid = 'Word.Application'
            clsid = ""
            local_server32 = ""
            executable = ""
            valid = $false
            error = ""
        }
        $base = $null
        $serverKey = $null
        try {
            $view = if ($viewName -eq 'Registry64') {
                [Microsoft.Win32.RegistryView]::Registry64
            } else {
                [Microsoft.Win32.RegistryView]::Registry32
            }
            $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, $view)
            $clsid = Get-RegisteredClsid -BaseKey $base -ProgId 'Word.Application'
            if ([string]::IsNullOrWhiteSpace($clsid)) {
                throw "Word.Application is missing from HKLM Software\Classes ($viewName)"
            }
            $clsid = $clsid.Trim()
            if ($clsid -notmatch '^\{[0-9A-Fa-f-]{36}\}$') {
                throw "Word.Application CLSID is invalid in $viewName"
            }
            $candidate.clsid = $clsid
            $serverKey = $base.OpenSubKey(('Software\Classes\CLSID\' + $clsid + '\LocalServer32'))
            if ($null -eq $serverKey) {
                throw "LocalServer32 is missing for Word.Application in $viewName"
            }
            $rawServer = [string]$serverKey.GetValue('', $null)
            $candidate.local_server32 = $rawServer
            $executable = Get-ExistingWinwordPath -CommandLine $rawServer
            if ([string]::IsNullOrWhiteSpace($executable)) {
                throw "LocalServer32 does not contain an existing WINWORD.EXE in $viewName"
            }
            $candidate.executable = $executable
            $candidate.valid = $true
        } catch {
            $candidate.error = $_.Exception.Message
        } finally {
            if ($null -ne $serverKey) { $serverKey.Close() }
            if ($null -ne $base) { $base.Close() }
        }
        $candidates += ,$candidate
    }

    $valid = @($candidates | Where-Object { $_.valid })
    if ($valid.Count -eq 0) {
        return [ordered]@{
            status = 'BLOCKED'
            clsid = ""
            executable = ""
            candidates = @($candidates)
            message = 'No valid Word.Application registration in HKLM Software\Classes Registry64 or Registry32.'
        }
    }

    $signatures = @($valid | ForEach-Object { ($_.clsid + '|' + $_.executable).ToLowerInvariant() } | Sort-Object -Unique)
    if ($signatures.Count -gt 1) {
        return [ordered]@{
            status = 'BLOCKED'
            clsid = ""
            executable = ""
            candidates = @($candidates)
            message = 'Valid Word registrations in Registry64 and Registry32 conflict.'
        }
    }

    return [ordered]@{
        status = 'PASS'
        clsid = [string]$valid[0].clsid
        executable = [string]$valid[0].executable
        candidates = @($candidates)
        message = ''
    }
}

if (-not ('WindowProcessResolver' -as [type])) {
    Add-Type @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
public static class WindowProcessResolver {
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
}
public static class RunningObjectResolver {
    [DllImport("ole32.dll", PreserveSig = true)]
    private static extern int GetRunningObjectTable(int reserved, out IRunningObjectTable runningObjectTable);

    public static object[] Snapshot() {
        IRunningObjectTable table;
        int result = GetRunningObjectTable(0, out table);
        if (result != 0) {
            Marshal.ThrowExceptionForHR(result);
        }
        IEnumMoniker enumerator;
        table.EnumRunning(out enumerator);
        var objects = new List<object>();
        var monikers = new IMoniker[1];
        try {
            while (enumerator.Next(1, monikers, IntPtr.Zero) == 0) {
                try {
                    object runningObject;
                    table.GetObject(monikers[0], out runningObject);
                    if (runningObject != null) {
                        objects.Add(runningObject);
                    }
                } catch {
                } finally {
                    if (monikers[0] != null) {
                        Marshal.ReleaseComObject(monikers[0]);
                        monikers[0] = null;
                    }
                }
            }
        } finally {
            if (enumerator != null) { Marshal.ReleaseComObject(enumerator); }
            if (table != null) { Marshal.ReleaseComObject(table); }
        }
        return objects.ToArray();
    }
}
'@
}

function Get-WindowPid {
    param([object]$Hwnd)

    $windowHandle = [IntPtr]::Zero
    try { $windowHandle = [IntPtr]$Hwnd } catch { return 0 }
    if ($windowHandle -eq [IntPtr]::Zero) { return 0 }
    [uint32]$processId = 0
    [void][WindowProcessResolver]::GetWindowThreadProcessId($windowHandle, [ref]$processId)
    return [int]$processId
}

function Get-ProcessImage {
    param([int]$ProcessId)

    if ($ProcessId -le 0) { return "" }
    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
        try { $path = [string]$process.Path } catch { $path = "" }
        if ([string]::IsNullOrWhiteSpace($path)) {
            try { $path = [string]$process.MainModule.FileName } catch { $path = "" }
        }
        if (-not [string]::IsNullOrWhiteSpace($path) -and (Test-Path -LiteralPath $path -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $path).Path
        }
    } catch {}
    return ""
}

function Get-NewEngineProcesses {
    param(
        [string]$Engine,
        [int[]]$BaselinePids
    )

    $inventory = Get-EngineProcessInventory -Engine $Engine
    $records = @()
    foreach ($process in @($inventory.active)) {
        if (@($BaselinePids) -contains [int]$process.Id) { continue }
        $records += ,[ordered]@{
            pid = [int]$process.Id
            image = Get-ProcessImage -ProcessId ([int]$process.Id)
            start_time_utc = $(try { $process.StartTime.ToUniversalTime().ToString('o') } catch { '' })
            session_id = $(try { [int]$process.SessionId } catch { $null })
        }
    }
    return @($records | Sort-Object { $_.pid })
}

function Get-MatchingCandidateProcesses {
    param(
        [string]$Engine,
        [object[]]$Processes,
        [string]$ApplicationPath,
        [string]$ResolvedExecutable
    )

    $matching = @($Processes | Where-Object {
        $image = [string]$_.image
        if ([string]::IsNullOrWhiteSpace($image)) { return $false }
        if ($Engine -eq 'word') {
            return (-not [string]::IsNullOrWhiteSpace($ResolvedExecutable)) -and
                (Test-ApplicationPathMatches -ApplicationPath $ApplicationPath -ResolvedExecutable $ResolvedExecutable) -and
                ($image -ieq $ResolvedExecutable) -and
                ([System.IO.Path]::GetFileName($image) -ieq 'WINWORD.EXE')
        }
        return ([System.IO.Path]::GetFileName($image) -ieq 'wps.exe') -and
            ($image -match '(?i)(WPS|Kingsoft)') -and
            (Test-ApplicationPathMatches -ApplicationPath $ApplicationPath -ResolvedExecutable $image)
    })
    return @($matching | Sort-Object { $_.pid })
}

function Select-CandidateProcess {
    param(
        [string]$Engine,
        [object[]]$Processes,
        [string]$ApplicationPath,
        [string]$ResolvedExecutable
    )

    $matching = @(Get-MatchingCandidateProcesses -Engine $Engine -Processes $Processes -ApplicationPath $ApplicationPath -ResolvedExecutable $ResolvedExecutable)
    if ($matching.Count -eq 1) { return $matching[0] }
    return $null
}

function Test-ApplicationPathMatches {
    param([string]$ApplicationPath, [string]$ResolvedExecutable)

    if ([string]::IsNullOrWhiteSpace($ApplicationPath) -or
        [string]::IsNullOrWhiteSpace($ResolvedExecutable)) {
        return $false
    }
    try {
        $expected = (Resolve-Path -LiteralPath $ResolvedExecutable).Path
        if ((Test-Path -LiteralPath $ApplicationPath -PathType Leaf) -and
            ((Resolve-Path -LiteralPath $ApplicationPath).Path -ieq $expected)) {
            return $true
        }
        if (Test-Path -LiteralPath $ApplicationPath -PathType Container) {
            return ((Resolve-Path -LiteralPath $ApplicationPath).Path -ieq (Split-Path -Parent $expected))
        }
    } catch {}
    return $false
}

function Test-WordIdentity {
    param(
        [string]$ApplicationPath,
        [string]$ApplicationVersion,
        [string]$ProcessImage,
        [string]$ResolvedExecutable
    )

    $fileInfo = $null
    try { $fileInfo = [Diagnostics.FileVersionInfo]::GetVersionInfo($ProcessImage) } catch {}
    $vendorVerified = $false
    if ($null -ne $fileInfo) {
        $vendorVerified = ([string]$fileInfo.CompanyName -match '(?i)Microsoft') -or
            ([string]$fileInfo.ProductName -match '(?i)Microsoft (Office|Word)')
    }
    $processVerified = (-not [string]::IsNullOrWhiteSpace($ProcessImage)) -and
        ($ProcessImage -ieq $ResolvedExecutable) -and
        ([System.IO.Path]::GetFileName($ProcessImage) -ieq 'WINWORD.EXE')
    $versionVerified = -not [string]::IsNullOrWhiteSpace($ApplicationVersion)
    $pathVerified = Test-ApplicationPathMatches -ApplicationPath $ApplicationPath -ResolvedExecutable $ResolvedExecutable
    return [ordered]@{
        verified = ($pathVerified -and $versionVerified -and $processVerified -and $vendorVerified)
        evidence = ('app.Path=' + $ApplicationPath + '; app.Version=' + $ApplicationVersion + '; process=' + $ProcessImage)
    }
}

function Test-WpsIdentity {
    param(
        [string]$ApplicationPath,
        [string]$ApplicationVersion,
        [string]$ProcessImage
    )

    $pathText = [string]$ApplicationPath
    $processText = [string]$ProcessImage
    $pathVendor = $pathText -match '(?i)(WPS|Kingsoft)'
    $processVendor = (-not [string]::IsNullOrWhiteSpace($processText)) -and
        ([System.IO.Path]::GetFileName($processText) -ieq 'wps.exe') -and
        ($processText -match '(?i)(WPS|Kingsoft)')
    $versionVerified = -not [string]::IsNullOrWhiteSpace($ApplicationVersion)
    $pathVerified = Test-ApplicationPathMatches -ApplicationPath $pathText -ResolvedExecutable $processText
    $processVerified = -not [string]::IsNullOrWhiteSpace($processText)
    return [ordered]@{
        verified = ($pathVendor -and $processVendor -and $versionVerified -and $pathVerified -and $processVerified)
        evidence = ('app.Path=' + $ApplicationPath + '; app.Version=' + $ApplicationVersion + '; process=' + $ProcessImage)
    }
}

function Get-WordApplicationFromRot {
    param([string]$ResolvedExecutable)

    try {
        Add-Type -AssemblyName Microsoft.VisualBasic -ErrorAction Stop
        $activeApplication = [Microsoft.VisualBasic.Interaction]::GetObject($null, 'Word.Application')
        if ($null -ne $activeApplication) {
            $applicationPath = [string]$activeApplication.Path
            $applicationVersion = [string]$activeApplication.Version
            if ((-not [string]::IsNullOrWhiteSpace($applicationVersion)) -and
                (Test-ApplicationPathMatches -ApplicationPath $applicationPath -ResolvedExecutable $ResolvedExecutable)) {
                return ,$activeApplication
            }
            Release-ComReference -Reference $activeApplication
        }
    } catch {}
    foreach ($runningObject in @([RunningObjectResolver]::Snapshot())) {
        $candidates = @($runningObject)
        try {
            $nestedApplication = $runningObject.Application
            if ($null -ne $nestedApplication) {
                $candidates += ,$nestedApplication
            }
        } catch {}
        foreach ($candidate in $candidates) {
            try {
                $applicationPath = [string]$candidate.Path
                $applicationVersion = [string]$candidate.Version
                if ((-not [string]::IsNullOrWhiteSpace($applicationVersion)) -and
                    (Test-ApplicationPathMatches -ApplicationPath $applicationPath -ResolvedExecutable $ResolvedExecutable)) {
                    return ,$candidate
                }
            } catch {}
        }
    }
    return $null
}

function Get-ValidatedPdf {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'PDF was not created.'
    }
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($item.Length -le 0) {
        throw 'PDF is empty.'
    }
    $stream = $null
    try {
        $stream = New-Object System.IO.FileStream($Path, ([System.IO.FileMode]::Open), ([System.IO.FileAccess]::Read), ([System.IO.FileShare]::ReadWrite))
        $header = New-Object byte[] 4
        $read = $stream.Read($header, 0, 4)
        if ($read -ne 4 -or [System.Text.Encoding]::ASCII.GetString($header) -ne '%PDF') {
            throw 'PDF does not start with %PDF.'
        }
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
    $hash = Get-Sha256 -Path $Path
    return [ordered]@{ bytes = [int64]$item.Length; sha256 = $hash }
}

function Release-ComReference {
    param([object]$Reference)
    if ($null -ne $Reference) {
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($Reference) } catch {}
    }
}

function Close-WordOwnedStartupDocument {
    param([object]$Application, [string]$OwnedWorkingInput = "")

    $count = [int]$Application.Documents.Count
    if ($count -eq 0) {
        return [ordered]@{
            closed = 0
            evidence = @()
            document = $null
            owned_working_copy_reused = $false
            blocked_reason = ''
        }
    }
    $closed = 0
    $evidence = @()
    $maxCloseAttempts = $count + 5
    while ([int]$Application.Documents.Count -gt 0 -and $closed -lt $maxCloseAttempts) {
        $startupDocument = $null
        try {
            $startupDocument = $Application.Documents.Item(1)
            $startupPath = [string]$startupDocument.Path
            $startupFullName = [string]$startupDocument.FullName
            $startupSaved = [bool]$startupDocument.Saved
            $startupName = [string]$startupDocument.Name
            $evidence += ,[ordered]@{ name = $startupName; path = $startupPath; full_name = $startupFullName; saved = $startupSaved }
            $ownedWorkingCopyStartup = $false
            if (-not [string]::IsNullOrWhiteSpace($OwnedWorkingInput) -and -not [string]::IsNullOrWhiteSpace($startupFullName)) {
                try {
                    $ownedWorkingCopyStartup = ((Resolve-Path -LiteralPath $startupFullName).Path -ieq (Resolve-Path -LiteralPath $OwnedWorkingInput).Path)
                } catch {
                    $ownedWorkingCopyStartup = ($startupFullName -ieq $OwnedWorkingInput)
                }
            }
            if (-not [string]::IsNullOrWhiteSpace($startupPath) -and -not $ownedWorkingCopyStartup) {
                return [ordered]@{
                    closed = $closed
                    evidence = $evidence
                    document = $null
                    owned_working_copy_reused = $false
                    blocked_reason = 'Explicitly started Word contains a startup document with a real file path.'
                }
            }
            if ($ownedWorkingCopyStartup) {
                if ([int]$Application.Documents.Count -ne 1) {
                    return [ordered]@{
                        closed = $closed
                        evidence = $evidence
                        document = $null
                        owned_working_copy_reused = $false
                        blocked_reason = 'Explicitly started Word contains an owned working copy plus extra startup documents.'
                    }
                }
                $startupDocument.Close($false)
                $closed++
                continue
            }
            $startupDocument.Close($false)
            $closed++
        } finally {
            if ($null -ne $startupDocument) { Release-ComReference -Reference $startupDocument }
        }
    }
    if ([int]$Application.Documents.Count -gt 0) {
        return [ordered]@{
            closed = $closed
            evidence = $evidence
            document = $null
            owned_working_copy_reused = $false
            blocked_reason = 'Explicitly started Word startup documents could not be closed safely.'
        }
    }
    return [ordered]@{
        closed = $closed
        evidence = $evidence
        document = $null
        owned_working_copy_reused = $false
        blocked_reason = ''
    }
}

function Remove-StaleOfficeWorkingCopies {
    param([string]$OutputDirectory, [string]$Engine)

    $prefix = '.' + $Engine.ToLowerInvariant()
    $pattern = ('^{0}\.[0-9a-f]{{32}}{1}$' -f [regex]::Escape($prefix), [regex]::Escape('.docx'))
    $candidates = @(Get-ChildItem -LiteralPath $OutputDirectory -Force -File -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match $pattern
    })
    foreach ($candidate in $candidates) {
        Remove-Item -LiteralPath $candidate.FullName -Force
    }
}

function New-OfficeWorkingCopyPath {
    param(
        [string]$OutputDirectory,
        [string]$Engine,
        [string]$Extension
    )

    if ($Engine -eq 'word') {
        $shortTempDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) '_tmp\word-working-copy'
        [System.IO.Directory]::CreateDirectory($shortTempDirectory) | Out-Null
        return Join-Path $shortTempDirectory ('.{0}.{1}{2}' -f $Engine, ([guid]::NewGuid().ToString('N')), $Extension)
    }
    return Join-Path $OutputDirectory ('.{0}.{1}{2}' -f $Engine, ([guid]::NewGuid().ToString('N')), $Extension)
}

function New-OfficeTempPdfPath {
    param(
        [string]$OutputDirectory,
        [string]$Engine
    )

    if ($Engine -eq 'word') {
        $shortTempDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) '_tmp\word-pdf-export'
        [System.IO.Directory]::CreateDirectory($shortTempDirectory) | Out-Null
        return Join-Path $shortTempDirectory ('.{0}.{1}.pdf' -f $Engine, ([guid]::NewGuid().ToString('N')))
    }
    return Join-Path $OutputDirectory ('.{0}.{1}.pdf' -f $Engine, ([guid]::NewGuid().ToString('N')))
}

function Open-OfficeDocumentWithRetry {
    param(
        [object]$Application,
        [string]$Path,
        [string]$Engine,
        [ref]$AttemptSink
    )

    $lastError = ''
    $openAttempts = @()
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $attemptRecord = [ordered]@{
            attempt = $attempt
            documents_available = $false
            documents_count = $null
            opened = $false
            error = ""
        }
        $documents = $null
        $opened = $null
        try {
            Write-ExportTrace -Engine $Engine -Stage 'documents_ready.before' -Data @{ attempt = $attempt }
            $documentsReady = Wait-OfficeDocumentsCollection -Application $Application
            Write-ExportTrace -Engine $Engine -Stage 'documents_ready.after' -Data @{ attempt = $attempt; count = [int]$documentsReady.count }
            $openAttempts += @($documentsReady.attempts)
            if ($null -ne $AttemptSink) {
                $AttemptSink.Value = @($AttemptSink.Value) + @($documentsReady.attempts)
            }
            $documents = $documentsReady.documents
            $attemptRecord.documents_available = $true
            $attemptRecord.documents_count = $documentsReady.count
            Write-ExportTrace -Engine $Engine -Stage 'documents_open.before' -Data @{ attempt = $attempt; path_length = [int]$Path.Length }
            if ($Engine -eq 'word') {
                $opened = $documents.Open($Path, $false, $true, $false)
            } else {
                $opened = $documents.Open($Path, $false, $true)
            }
            Write-ExportTrace -Engine $Engine -Stage 'documents_open.after' -Data @{ attempt = $attempt; opened = ($null -ne $opened) }
            if ($null -ne $opened) {
                $attemptRecord.opened = $true
                $openAttempts += ,$attemptRecord
                if ($null -ne $AttemptSink) {
                    $AttemptSink.Value = @($AttemptSink.Value) + @($attemptRecord)
                }
                return [ordered]@{ document = $opened; attempts = @($openAttempts) }
            }
            $lastError = 'Office document open returned null.'
            $attemptRecord.error = $lastError
        } catch {
            $lastError = $_.Exception.Message
            $attemptRecord.error = $lastError
        } finally {
            if ($null -ne $documents -and $null -eq $opened) {
                Write-ExportTrace -Engine $Engine -Stage 'documents_release.before' -Data @{ attempt = $attempt }
                Release-ComReference -Reference $documents
                Write-ExportTrace -Engine $Engine -Stage 'documents_release.after' -Data @{ attempt = $attempt }
            }
        }
        $openAttempts += ,$attemptRecord
        if ($null -ne $AttemptSink) {
            $AttemptSink.Value = @($AttemptSink.Value) + @($attemptRecord)
        }
        if ($attempt -lt 3) {
            Start-Sleep -Milliseconds (500 * $attempt)
        }
    }
    throw ('Office document open failed after retries: ' + $lastError)
}

function Wait-OfficeDocumentsCollection {
    param([object]$Application)

    $attempts = @()
    $lastError = ''
    for ($attempt = 1; $attempt -le 10; $attempt++) {
        $attemptRecord = [ordered]@{
            attempt = $attempt
            documents_available = $false
            documents_count = $null
            error = ""
        }
        try {
            $documents = $Application.Documents
            if ($null -eq $documents) {
                throw 'Office Documents collection returned null.'
            }
            $count = [int]$documents.Count
            $attemptRecord.documents_available = $true
            $attemptRecord.documents_count = $count
            $attempts += ,$attemptRecord
            return [ordered]@{ documents = $documents; count = $count; attempts = @($attempts) }
        } catch {
            $lastError = $_.Exception.Message
            $attemptRecord.error = $lastError
            $attempts += ,$attemptRecord
        }
        if ($attempt -lt 10) {
            Start-Sleep -Milliseconds 300
        }
    }
    throw ('Office Documents collection was not ready after retries: ' + $lastError)
}

function Export-WithOffice {
    param(
        [string]$Engine,
        [string]$Role,
        [string]$RequiredIdentity,
        [string]$RequestedProgId,
        [string]$ResolvedClsid,
        [object[]]$RegistryCandidates,
        [string]$ResolvedExecutable,
        [string]$TargetPdf
    )

    $result = New-EngineReport -Engine $Engine -Role $Role -RequiredIdentity $RequiredIdentity -RequestedProgId $RequestedProgId -ResolvedClsid $ResolvedClsid -RegistryCandidates $RegistryCandidates -ResolvedExecutable $ResolvedExecutable -TargetPdf $TargetPdf
    $baselineInventory = Get-EngineProcessInventory -Engine $Engine
    $result.baseline_pids = @($baselineInventory.active | ForEach-Object { [int]$_.Id })
    $result.ignored_non_writer_processes = @($baselineInventory.ignored)
    $targetExistedBefore = Test-Path -LiteralPath $TargetPdf -PathType Leaf
    $result.cleanup.old_target_preserved = $targetExistedBefore
    $app = $null
    $doc = $null
    $tempPdf = $null
    $workingInput = $null
    $initialDocumentsCount = $null
    $ownershipProbeComplete = $false
    $blockReason = $false

    try {
        Write-ExportTrace -Engine $Engine -Stage 'export.begin'
        $result.input_sha256_before = Get-Sha256 -Path $inputResolved
        if (@($result.baseline_pids).Count -gt 0) {
            $blockReason = $true
            throw ('Preexisting ' + $Engine + ' process detected; close it before formal export.')
        }
        Remove-StaleOfficeWorkingCopies -OutputDirectory $outputResolved -Engine $Engine
        $workingInput = New-OfficeWorkingCopyPath -OutputDirectory $outputResolved -Engine $Engine -Extension ([System.IO.Path]::GetExtension($inputResolved))
        Remove-StaleOfficeWorkingCopies -OutputDirectory (Split-Path -Parent $workingInput) -Engine $Engine
        $result.working_copy = $workingInput
        $result.working_copy_path_length = [int]$workingInput.Length
        if ($result.working_copy_path_length -gt $OfficeComPathBudget) {
            $blockReason = $true
            throw 'Office working copy path exceeds COM budget.'
        }
        Copy-Item -LiteralPath $inputResolved -Destination $workingInput -Force
        if ($Engine -eq 'word') {
            $started = Start-Process -FilePath $ResolvedExecutable -ArgumentList @('/n', '/Automation', '/a') -WindowStyle Hidden -PassThru
            $startedPid = [int]$started.Id
            $result.started_pid = $startedPid
            Write-ExportTrace -Engine $Engine -Stage 'word_start.after' -Data @{ started_pid = $startedPid }
        }

        $lastActivationError = $null
        if ($Engine -eq 'wps') {
            $candidateApp = $null
            $candidateDocumentsCount = $null
            try {
                $result.activation_call_count = [int]$result.activation_call_count + 1
                $candidateApp = New-Object -ComObject $RequestedProgId
                $stableGroupSignature = ''
                $stableCount = 0
                for ($probe = 1; $probe -le 10; $probe++) {
                    $attemptRecord = [ordered]@{
                        attempt = $probe
                        progid = $RequestedProgId
                        application_path = ""
                        application_version = ""
                        hwnd = 0
                        pid = 0
                        process_image = ""
                        new_processes = @()
                        owned_process_group = @()
                        documents_count = $null
                        identity_verified = $false
                        identity_evidence = ""
                        stable_process_observations = 0
                        candidate_quit_called = $false
                        error = ""
                    }
                    try {
                        $candidatePath = [string]$candidateApp.Path
                        $candidateVersion = [string]$candidateApp.Version
                        $candidateHwnd = [int64]$candidateApp.Hwnd
                        $candidatePid = Get-WindowPid -Hwnd $candidateHwnd
                        $candidateImage = Get-ProcessImage -ProcessId $candidatePid
                        $candidateNewProcesses = @(Get-NewEngineProcesses -Engine $Engine -BaselinePids @($result.baseline_pids))
                        $matchingProcessGroup = @(Get-MatchingCandidateProcesses -Engine $Engine -Processes $candidateNewProcesses -ApplicationPath $candidatePath -ResolvedExecutable $ResolvedExecutable)
                        if ($candidatePid -le 0 -or [string]::IsNullOrWhiteSpace($candidateImage)) {
                            if ($matchingProcessGroup.Count -gt 0) {
                                $candidatePid = [int]$matchingProcessGroup[0].pid
                                $candidateImage = [string]$matchingProcessGroup[0].image
                            }
                        }
                        $candidateDocumentsCount = [int]$candidateApp.Documents.Count
                        $candidateIdentity = Test-WpsIdentity -ApplicationPath $candidatePath -ApplicationVersion $candidateVersion -ProcessImage $candidateImage
                        $groupSignature = [string]::Join('|', @($matchingProcessGroup | ForEach-Object { ([string]$_.pid + ':' + [string]$_.start_time_utc + ':' + [string]$_.session_id + ':' + [string]$_.image) }))
                        if ($candidateIdentity.verified -and $matchingProcessGroup.Count -gt 0) {
                            if ($stableGroupSignature -eq $groupSignature) { $stableCount++ } else { $stableGroupSignature = $groupSignature; $stableCount = 1 }
                        } else {
                            $stableGroupSignature = ''
                            $stableCount = 0
                        }
                        $attemptRecord.application_path = $candidatePath
                        $attemptRecord.application_version = $candidateVersion
                        $attemptRecord.hwnd = $candidateHwnd
                        $attemptRecord.pid = $candidatePid
                        $attemptRecord.process_image = $candidateImage
                        $attemptRecord.new_processes = @($candidateNewProcesses)
                        $attemptRecord.owned_process_group = @($matchingProcessGroup)
                        $attemptRecord.documents_count = $candidateDocumentsCount
                        $attemptRecord.identity_verified = [bool]$candidateIdentity.verified
                        $attemptRecord.identity_evidence = [string]$candidateIdentity.evidence
                        $attemptRecord.stable_process_observations = $stableCount
                        if ($candidateIdentity.verified -and $stableCount -ge 2) {
                            $result.application_path = $candidatePath
                            $result.application_version = $candidateVersion
                            $result.hwnd = $candidateHwnd
                            $result.pid = $candidatePid
                            $result.process_image = $candidateImage
                            $result.new_pids = @($matchingProcessGroup | ForEach-Object { [int]$_.pid })
                            $result.identity_verified = $true
                            $result.identity_evidence = [string]$candidateIdentity.evidence
                            $app = $candidateApp
                            $candidateApp = $null
                        } else {
                            $lastActivationError = 'WPS identity or stable process ownership was not yet verified.'
                            $attemptRecord.error = $lastActivationError
                        }
                    } catch {
                        $lastActivationError = $_.Exception.Message
                        $attemptRecord.error = $lastActivationError
                    } finally {
                        $result.activation_attempts += ,$attemptRecord
                    }
                    if ($null -ne $app) { break }
                    if ($probe -lt 10) { Start-Sleep -Milliseconds 500 }
                }
            } finally {
                if ($null -ne $candidateApp) {
                    if ($candidateDocumentsCount -eq 0) {
                        try { $candidateApp.Quit() } catch {}
                    }
                    Release-ComReference -Reference $candidateApp
                }
            }
        } else {
            for ($attempt = 1; $attempt -le 10; $attempt++) {
                $candidateApp = $null
                $candidateCanQuit = $false
                $attemptRecord = [ordered]@{
                    attempt = $attempt
                    progid = 'ROT:' + $ResolvedClsid
                    application_path = ""
                    application_version = ""
                    hwnd = 0
                    pid = 0
                    process_image = ""
                    new_processes = @()
                    documents_count = $null
                    identity_verified = $false
                    identity_evidence = ""
                    candidate_quit_called = $false
                    error = ""
                }
                try {
                    $result.rot_poll_count = [int]$result.rot_poll_count + 1
                    $candidateApp = Get-WordApplicationFromRot -ResolvedExecutable $ResolvedExecutable
                    if ($null -eq $candidateApp) { throw 'Real Microsoft Word application was not present in the ROT.' }
                    $candidatePath = [string]$candidateApp.Path
                    $candidateVersion = [string]$candidateApp.Version
                    $candidateHwnd = [int64]$candidateApp.Hwnd
                    $candidatePid = Get-WindowPid -Hwnd $candidateHwnd
                    $candidateImage = Get-ProcessImage -ProcessId $candidatePid
                    $candidateNewProcesses = @(Get-NewEngineProcesses -Engine $Engine -BaselinePids @($result.baseline_pids))
                    if ($candidatePid -le 0 -or [string]::IsNullOrWhiteSpace($candidateImage)) {
                        $selectedProcess = Select-CandidateProcess -Engine $Engine -Processes $candidateNewProcesses -ApplicationPath $candidatePath -ResolvedExecutable $ResolvedExecutable
                        if ($null -ne $selectedProcess) {
                            $candidatePid = [int]$selectedProcess.pid
                            $candidateImage = [string]$selectedProcess.image
                        }
                    }
                    $candidateDocumentsCount = [int]$candidateApp.Documents.Count
                    $candidateIdentity = Test-WordIdentity -ApplicationPath $candidatePath -ApplicationVersion $candidateVersion -ProcessImage $candidateImage -ResolvedExecutable $ResolvedExecutable
                    $attemptRecord.application_path = $candidatePath
                    $attemptRecord.application_version = $candidateVersion
                    $attemptRecord.hwnd = $candidateHwnd
                    $attemptRecord.pid = $candidatePid
                    $attemptRecord.process_image = $candidateImage
                    $attemptRecord.new_processes = @($candidateNewProcesses)
                    $attemptRecord.documents_count = $candidateDocumentsCount
                    $attemptRecord.identity_verified = [bool]$candidateIdentity.verified
                    $attemptRecord.identity_evidence = [string]$candidateIdentity.evidence
                    if ($candidateIdentity.verified -and $candidatePid -eq $startedPid) {
                        $result.application_path = $candidatePath
                        $result.application_version = $candidateVersion
                        $result.hwnd = $candidateHwnd
                        $result.pid = $candidatePid
                        $result.process_image = $candidateImage
                        $result.identity_verified = $true
                        $result.identity_evidence = [string]$candidateIdentity.evidence
                        $app = $candidateApp
                        $candidateApp = $null
                    } else {
                        $lastActivationError = 'ROT object identity or PID did not match the explicitly started Microsoft Word instance.'
                        $attemptRecord.error = $lastActivationError
                        $candidateCanQuit = ($candidateDocumentsCount -eq 0) -and ($candidatePid -eq $startedPid)
                    }
                } catch {
                    $lastActivationError = $_.Exception.Message
                    $attemptRecord.error = $lastActivationError
                } finally {
                    if ($null -ne $candidateApp -and $candidateCanQuit) {
                        try {
                            $candidateApp.Quit()
                            $attemptRecord.candidate_quit_called = $true
                        } catch {}
                    }
                    $result.activation_attempts += ,$attemptRecord
                    if ($null -ne $candidateApp) { Release-ComReference -Reference $candidateApp }
                }
                if ($null -ne $app) { break }
                if ($attempt -lt 10) { Start-Sleep -Milliseconds 500 }
            }
        }
        if ($null -eq $app) {
            $blockReason = $true
            throw ('COM application ownership could not be established: ' + $lastActivationError)
        }

        $result.application_path = [string]$app.Path
        $result.application_version = [string]$app.Version
        if ($Engine -eq 'word') {
            try { $app.DisplayAlerts = 0 } catch {}
            try { $app.Visible = $false } catch {}
        }
        $result.hwnd = [int64]$app.Hwnd
        $windowPid = Get-WindowPid -Hwnd $result.hwnd
        if ($Engine -eq 'word' -and $windowPid -gt 0) { $result.pid = $windowPid }
        $windowProcessImage = Get-ProcessImage -ProcessId ([int]$result.pid)
        if (-not [string]::IsNullOrWhiteSpace($windowProcessImage)) {
            $result.process_image = $windowProcessImage
        }
        if ($Engine -eq 'word') {
            $identity = Test-WordIdentity -ApplicationPath $result.application_path -ApplicationVersion $result.application_version -ProcessImage $result.process_image -ResolvedExecutable $ResolvedExecutable
        } else {
            $identity = Test-WpsIdentity -ApplicationPath $result.application_path -ApplicationVersion $result.application_version -ProcessImage $result.process_image
        }
        $result.identity_verified = [bool]$identity.verified
        $result.identity_evidence = $identity.evidence
        if (-not $result.identity_verified) {
            $blockReason = $true
            throw ('Real engine identity could not be verified for ' + $RequiredIdentity + '.')
        }

        if ($Engine -eq 'word') {
            Write-ExportTrace -Engine $Engine -Stage 'startup_cleanup.before'
            $startupDocumentResult = Close-WordOwnedStartupDocument -Application $app -OwnedWorkingInput $workingInput
            Write-ExportTrace -Engine $Engine -Stage 'startup_cleanup.after' -Data @{ closed = [int]$startupDocumentResult.closed }
            $result.startup_documents_closed = [int]$startupDocumentResult.closed
            $result.startup_document_evidence = @($startupDocumentResult.evidence)
            if ($null -ne $startupDocumentResult.document) {
                $doc = $startupDocumentResult.document
            }
            if (-not [string]::IsNullOrWhiteSpace([string]$startupDocumentResult.blocked_reason)) {
                $blockReason = $true
                throw [string]$startupDocumentResult.blocked_reason
            }
        }
        $initialDocumentsCount = [int]$app.Documents.Count
        $result.initial_documents_count = $initialDocumentsCount
        $expectedInitialDocumentsCount = if ($null -ne $doc) { 1 } else { 0 }
        $pidIsBaseline = @($result.baseline_pids) -contains ([int]$result.pid)
        $result.new_pids = @(Get-BaselinePids -Engine $Engine | Where-Object { @($result.baseline_pids) -notcontains [int]$_ })
        $pidIsNew = @($result.new_pids) -contains ([int]$result.pid)
        $wordPidMismatch = ($Engine -eq 'word') -and ([int]$result.pid -ne [int]$startedPid)
        if ($pidIsBaseline -or (-not $pidIsNew) -or ([int]$result.pid -le 0) -or ($initialDocumentsCount -ne $expectedInitialDocumentsCount) -or $wordPidMismatch) {
            $blockReason = $true
            throw 'COM resolved to an instance that is not an owned, empty, newly started application.'
        }
        $result.ownership_verified = $true
        $ownershipProbeComplete = $true
        Write-ExportTrace -Engine $Engine -Stage 'ownership.after' -Data @{ pid = [int]$result.pid; initial_documents_count = [int]$initialDocumentsCount }

        $tempPdf = New-OfficeTempPdfPath -OutputDirectory $outputResolved -Engine $Engine
        $result.temp_pdf = $tempPdf
        $result.temp_pdf_path_length = [int]$tempPdf.Length
        if ($tempPdf.Length -gt $OfficeComPathBudget) {
            $blockReason = $true
            throw 'Office temporary PDF path exceeds COM budget.'
        }
        $openAttempts = @()
        if ($null -eq $doc) {
            Write-ExportTrace -Engine $Engine -Stage 'open_helper.before' -Data @{ working_copy_path_length = [int]$workingInput.Length }
            $openResult = Open-OfficeDocumentWithRetry -Application $app -Path $workingInput -Engine $Engine -AttemptSink ([ref]$openAttempts)
            Write-ExportTrace -Engine $Engine -Stage 'open_helper.after'
            $result.open_attempts = @($openAttempts)
            $result.open_attempts = @($openResult.attempts)
            $doc = $openResult.document
        } else {
            $result.open_attempts = @([ordered]@{
                attempt = 0
                documents_available = $true
                documents_count = $initialDocumentsCount
                opened = $true
                reused_startup_document = $true
                error = ""
            })
        }
        if ($Engine -eq 'word') {
            Write-ExportTrace -Engine $Engine -Stage 'saveas2.before' -Data @{ temp_pdf_path_length = [int]$tempPdf.Length }
            $doc.SaveAs2($tempPdf, 17)
            Write-ExportTrace -Engine $Engine -Stage 'saveas2.after'
        } else {
            try { $doc.ExportAsFixedFormat($tempPdf, 17) }
            catch { $doc.SaveAs($tempPdf, 17) }
        }
        $artifact = Get-ValidatedPdf -Path $tempPdf
        Write-ExportTrace -Engine $Engine -Stage 'pdf_validate.after' -Data @{ bytes = [int64]$artifact.bytes }
        $result.bytes = $artifact.bytes
        $result.sha256 = $artifact.sha256
        Move-Item -LiteralPath $tempPdf -Destination $TargetPdf -Force
        if (-not (Test-Path -LiteralPath $TargetPdf -PathType Leaf)) {
            throw 'Validated PDF could not be atomically moved to the target.'
        }
        $result.pdf = (Resolve-Path -LiteralPath $TargetPdf).Path
        $result.status = 'PASS'
    } catch {
        if ($null -ne $openAttempts) {
            $result.open_attempts = @($openAttempts)
        }
        $result.status = if ($blockReason) { 'BLOCKED' } else { 'FAIL' }
        $result.message = $_.Exception.Message
    } finally {
        if ($null -ne $doc) {
            if ($ownershipProbeComplete) {
                try { $doc.Close($false); $result.cleanup.document_closed = $true } catch {}
            }
            Release-ComReference -Reference $doc
        }
        if ($null -ne $app) {
            if ($ownershipProbeComplete -and $initialDocumentsCount -eq 0 -and [int]$result.pid -gt 0) {
                try { $app.Quit(); $result.cleanup.quit_called = $true } catch {}
            }
            Release-ComReference -Reference $app
            $result.cleanup.com_released = $true
        }
        if (-not [string]::IsNullOrWhiteSpace($tempPdf) -and (Test-Path -LiteralPath $tempPdf -PathType Leaf)) {
            try {
                Remove-Item -LiteralPath $tempPdf -Force
                $result.cleanup.temp_pdf_removed = -not (Test-Path -LiteralPath $tempPdf -PathType Leaf)
            } catch {}
        } else {
            $result.cleanup.temp_pdf_removed = $true
        }
        if ($ownershipProbeComplete) {
            $maxExitProbes = if ($Engine -eq 'word') { 30 } else { 10 }
            for ($exitProbe = 1; $exitProbe -le $maxExitProbes; $exitProbe++) {
                $remainingOwned = @(Get-BaselinePids -Engine $Engine | Where-Object { @($result.new_pids) -contains [int]$_ })
                if ($remainingOwned.Count -eq 0) { break }
                Start-Sleep -Milliseconds 500
                $result.cleanup.exit_wait_ms = [int]$result.cleanup.exit_wait_ms + 500
            }
            $result.cleanup.remaining_owned_pids = @($remainingOwned)
            $result.cleanup.exit_observed = @($remainingOwned).Count -eq 0
            if ($result.status -eq 'PASS' -and (-not $result.cleanup.exit_observed)) {
                $result.status = 'FAIL'
                $result.message = 'Owned Office processes did not exit after COM cleanup.'
            }
        }
        if (-not [string]::IsNullOrWhiteSpace($workingInput) -and (Test-Path -LiteralPath $workingInput -PathType Leaf)) {
            try {
                Remove-Item -LiteralPath $workingInput -Force
                $result.cleanup.working_copy_removed = -not (Test-Path -LiteralPath $workingInput -PathType Leaf)
            } catch {}
        } else {
            $result.cleanup.working_copy_removed = $true
        }
        $result.input_sha256_after = Get-Sha256 -Path $inputResolved
        $result.input_unchanged = $result.input_sha256_before -eq $result.input_sha256_after
        if (-not $result.input_unchanged) {
            $result.status = 'FAIL'
            $result.message = 'Original input changed during Office export.'
        } elseif ($result.status -eq 'PASS' -and (-not $result.cleanup.working_copy_removed)) {
            $result.status = 'FAIL'
            $result.message = 'Office working copy could not be removed.'
        }
    }
    return $result
}

function Export-WithWordWorker {
    param(
        [string]$Role,
        [string]$RequiredIdentity,
        [string]$RequestedProgId,
        [string]$ResolvedClsid,
        [object[]]$RegistryCandidates,
        [string]$ResolvedExecutable,
        [string]$TargetPdf
    )

    $result = New-EngineReport -Engine 'word' -Role $Role -RequiredIdentity $RequiredIdentity -RequestedProgId $RequestedProgId -ResolvedClsid $ResolvedClsid -RegistryCandidates $RegistryCandidates -ResolvedExecutable $ResolvedExecutable -TargetPdf $TargetPdf
    $baselineInventory = Get-EngineProcessInventory -Engine 'word'
    $result.baseline_pids = @($baselineInventory.active | ForEach-Object { [int]$_.Id })
    $result.ignored_non_writer_processes = @($baselineInventory.ignored)
    $targetExistedBefore = Test-Path -LiteralPath $TargetPdf -PathType Leaf
    $result.cleanup.old_target_preserved = $targetExistedBefore
    $workingInput = $null
    $tempPdf = $null
    $workerResult = $null

    try {
        $result.input_sha256_before = Get-Sha256 -Path $inputResolved
        if (@($result.baseline_pids).Count -gt 0) {
            throw 'Preexisting word process detected; close it before formal export.'
        }
        $workingInput = New-OfficeWorkingCopyPath -OutputDirectory $outputResolved -Engine 'word' -Extension ([System.IO.Path]::GetExtension($inputResolved))
        Remove-StaleOfficeWorkingCopies -OutputDirectory (Split-Path -Parent $workingInput) -Engine 'word'
        $result.working_copy = $workingInput
        $result.working_copy_path_length = [int]$workingInput.Length
        if ($result.working_copy_path_length -gt $OfficeComPathBudget) {
            throw 'Office working copy path exceeds COM budget.'
        }
        Copy-Item -LiteralPath $inputResolved -Destination $workingInput -Force

        $tempPdf = New-OfficeTempPdfPath -OutputDirectory $outputResolved -Engine 'word'
        $result.temp_pdf = $tempPdf
        $result.temp_pdf_path_length = [int]$tempPdf.Length
        if ($tempPdf.Length -gt $OfficeComPathBudget) {
            throw 'Office temporary PDF path exceeds COM budget.'
        }

        $workerScript = Join-Path $PSScriptRoot 'export_word_pdf_worker.ps1'
        if (-not (Test-Path -LiteralPath $workerScript -PathType Leaf)) {
            throw 'Word PDF worker script is missing.'
        }
        $workerResult = Join-Path (Split-Path -Parent $tempPdf) ('.word.' + ([guid]::NewGuid().ToString('N')) + '.worker.json')
        $workerArgs = @(
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-File', $workerScript,
            '-InputPath', $workingInput,
            '-PdfPath', $tempPdf,
            '-WinwordPath', $ResolvedExecutable,
            '-ResultPath', $workerResult
        )
        if (-not [string]::IsNullOrWhiteSpace($TracePath)) {
            $workerArgs += @('-TracePath', $TracePath)
        }
        & powershell.exe @workerArgs
        $workerExit = $LASTEXITCODE
        if (-not (Test-Path -LiteralPath $workerResult -PathType Leaf)) {
            throw 'Word PDF worker did not write a result report.'
        }
        $worker = Get-Content -LiteralPath $workerResult -Raw -Encoding UTF8 | ConvertFrom-Json
        $result.worker_host = $worker.worker_host
        $result.started_pid = [int]$worker.started_pid
        $result.application_path = [string]$worker.application_path
        $result.application_version = [string]$worker.application_version
        $result.hwnd = [int64]$worker.hwnd
        $result.pid = [int]$worker.pid
        $result.process_image = [string]$worker.process_image
        $result.identity_verified = [bool]$worker.identity_verified
        $result.ownership_verified = [bool]$worker.ownership_verified
        $result.startup_documents_closed = [int]$worker.startup_documents_closed
        $result.startup_document_evidence = @($worker.startup_document_evidence)
        $result.open_attempts = @($worker.open_attempts)
        $result.cleanup.document_closed = [bool]$worker.cleanup.document_closed
        $result.cleanup.quit_called = [bool]$worker.cleanup.quit_called
        $result.cleanup.com_released = [bool]$worker.cleanup.com_released
        $result.cleanup.exit_observed = [bool]$worker.cleanup.exit_observed
        $result.cleanup.exit_wait_ms = [int]$worker.cleanup.exit_wait_ms
        $result.cleanup.remaining_owned_pids = @($worker.cleanup.remaining_owned_pids)
        if ($workerExit -ne 0 -or [string]$worker.status -ne 'PASS') {
            throw ('Word PDF worker failed: ' + [string]$worker.message)
        }
        if (-not $result.identity_verified -or -not $result.ownership_verified -or -not $result.cleanup.exit_observed -or -not $result.cleanup.com_released) {
            throw 'Word PDF worker evidence did not pass identity, ownership, or cleanup checks.'
        }
        $artifact = Get-ValidatedPdf -Path $tempPdf
        $result.bytes = $artifact.bytes
        $result.sha256 = $artifact.sha256
        Move-Item -LiteralPath $tempPdf -Destination $TargetPdf -Force
        if (-not (Test-Path -LiteralPath $TargetPdf -PathType Leaf)) {
            throw 'Validated PDF could not be moved to the target.'
        }
        $result.pdf = (Resolve-Path -LiteralPath $TargetPdf).Path
        $result.status = 'PASS'
    } catch {
        $result.status = 'FAIL'
        $result.message = $_.Exception.Message
    } finally {
        if (-not [string]::IsNullOrWhiteSpace($tempPdf) -and (Test-Path -LiteralPath $tempPdf -PathType Leaf)) {
            try {
                Remove-Item -LiteralPath $tempPdf -Force
                $result.cleanup.temp_pdf_removed = -not (Test-Path -LiteralPath $tempPdf -PathType Leaf)
            } catch {}
        } else {
            $result.cleanup.temp_pdf_removed = $true
        }
        if (-not [string]::IsNullOrWhiteSpace($workingInput) -and (Test-Path -LiteralPath $workingInput -PathType Leaf)) {
            try {
                Remove-Item -LiteralPath $workingInput -Force
                $result.cleanup.working_copy_removed = -not (Test-Path -LiteralPath $workingInput -PathType Leaf)
            } catch {}
        } else {
            $result.cleanup.working_copy_removed = $true
        }
        if (-not [string]::IsNullOrWhiteSpace($workerResult) -and (Test-Path -LiteralPath $workerResult -PathType Leaf) -and $result.status -eq 'PASS') {
            try { Remove-Item -LiteralPath $workerResult -Force } catch {}
        }
        $result.input_sha256_after = Get-Sha256 -Path $inputResolved
        $result.input_unchanged = $result.input_sha256_before -eq $result.input_sha256_after
        if (-not $result.input_unchanged) {
            $result.status = 'FAIL'
            $result.message = 'Original input changed during Office export.'
        } elseif ($result.status -eq 'PASS' -and (-not $result.cleanup.working_copy_removed)) {
            $result.status = 'FAIL'
            $result.message = 'Office working copy could not be removed.'
        }
    }
    return $result
}

$stem = [System.IO.Path]::GetFileNameWithoutExtension($inputResolved)
$wpsTarget = Join-Path $outputResolved ($stem + '_wps.pdf')
$wordTarget = Join-Path $outputResolved ($stem + '_word.pdf')

$results = @()
$results += ,(Export-WithOffice -Engine 'wps' -Role 'primary' -RequiredIdentity 'wps-writer' -RequestedProgId 'kwps.Application' -ResolvedClsid '' -RegistryCandidates @() -ResolvedExecutable '' -TargetPdf $wpsTarget)

$wordRegistry = Resolve-WordRegistry
if ($wordRegistry.status -eq 'PASS') {
    $results += ,(Export-WithWordWorker -Role 'backup_compatibility' -RequiredIdentity 'microsoft-word' -RequestedProgId 'Word.Application' -ResolvedClsid $wordRegistry.clsid -RegistryCandidates $wordRegistry.candidates -ResolvedExecutable $wordRegistry.executable -TargetPdf $wordTarget)
} else {
    $blockedWord = New-EngineReport -Engine 'word' -Role 'backup_compatibility' -RequiredIdentity 'microsoft-word' -RequestedProgId 'Word.Application' -ResolvedClsid $wordRegistry.clsid -RegistryCandidates $wordRegistry.candidates -ResolvedExecutable $wordRegistry.executable -TargetPdf $wordTarget
    $blockedWord.status = 'BLOCKED'
    $blockedWord.message = $wordRegistry.message
    $blockedWord.cleanup.old_target_preserved = Test-Path -LiteralPath $wordTarget -PathType Leaf
    $results += ,$blockedWord
}

$hasBlocked = @($results | Where-Object { $_.status -eq 'BLOCKED' }).Count -gt 0
$hasFailure = @($results | Where-Object { $_.status -eq 'FAIL' }).Count -gt 0
$status = if ($hasBlocked) { 'BLOCKED' } elseif ($hasFailure) { 'FAIL' } elseif (@($results).Count -eq 2 -and @($results | Where-Object { $_.status -eq 'PASS' }).Count -eq 2) { 'PASS' } else { 'BLOCKED' }
$payload = [ordered]@{
    status = $status
    stage = 'export_word_wps'
    generated_at = [DateTime]::UtcNow.ToString('o')
    input = $inputResolved
    powershell_host = [ordered]@{
        version = [string]$PSVersionTable.PSVersion
        edition = [string]$PSVersionTable.PSEdition
        bitness = if ([Environment]::Is64BitProcess) { 64 } else { 32 }
        apartment_state = [Threading.Thread]::CurrentThread.ApartmentState.ToString()
        process_path = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    }
    engines = @($results)
}
$json = $payload | ConvertTo-Json -Depth 10
if ($Report) {
    $reportParent = Split-Path -Parent $Report
    if ($reportParent) { [System.IO.Directory]::CreateDirectory($reportParent) | Out-Null }
    [System.IO.File]::WriteAllText($Report, $json, [System.Text.UTF8Encoding]::new($false))
}
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$json
if ($status -eq 'PASS') { exit 0 } else { exit 2 }
