param(
    [Parameter(Mandatory=$true)][string]$InputPath,
    [Parameter(Mandatory=$true)][string]$PdfPath,
    [Parameter(Mandatory=$true)][string]$WinwordPath,
    [Parameter(Mandatory=$true)][string]$ResultPath,
    [string]$TracePath = ""
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Write-WorkerTrace {
    param([string]$Stage)

    if ([string]::IsNullOrWhiteSpace($TracePath)) { return }
    $traceParent = Split-Path -Parent $TracePath
    if ($traceParent) { [System.IO.Directory]::CreateDirectory($traceParent) | Out-Null }
    $line = (@{
        ts = [DateTime]::UtcNow.ToString('o')
        engine = 'word'
        stage = ('worker.' + $Stage)
    } | ConvertTo-Json -Compress) + [Environment]::NewLine
    [System.IO.File]::AppendAllText($TracePath, $line, [System.Text.UTF8Encoding]::new($false))
}

function Release-ComReference {
    param([object]$Reference)
    if ($null -ne $Reference) {
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($Reference) } catch {}
    }
}

if (-not ('WordWorkerWindowProcessResolver' -as [type])) {
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class WordWorkerWindowProcessResolver {
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
}
'@
}

function Get-WindowPid {
    param([object]$Hwnd)
    $windowHandle = [IntPtr]::Zero
    try { $windowHandle = [IntPtr]$Hwnd } catch { return 0 }
    if ($windowHandle -eq [IntPtr]::Zero) { return 0 }
    [uint32]$processId = 0
    [void][WordWorkerWindowProcessResolver]::GetWindowThreadProcessId($windowHandle, [ref]$processId)
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

function Get-PdfInfo {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'PDF was not created.' }
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($item.Length -le 0) { throw 'PDF is empty.' }
    return [ordered]@{ bytes = [int64]$item.Length }
}

$result = [ordered]@{
    status = 'FAIL'
    message = ''
    worker_host = [ordered]@{
        version = [string]$PSVersionTable.PSVersion
        edition = [string]$PSVersionTable.PSEdition
        bitness = if ([Environment]::Is64BitProcess) { 64 } else { 32 }
        apartment_state = [Threading.Thread]::CurrentThread.ApartmentState.ToString()
        process_path = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    }
    started_pid = 0
    application_path = ''
    application_version = ''
    hwnd = 0
    pid = 0
    process_image = ''
    identity_verified = $false
    ownership_verified = $false
    startup_documents_closed = 0
    startup_document_evidence = @()
    open_attempts = @()
    cleanup = [ordered]@{
        document_closed = $false
        quit_called = $false
        com_released = $false
        exit_observed = $false
        exit_wait_ms = 0
        remaining_owned_pids = @()
    }
    pdf = $PdfPath
    bytes = $null
}

$app = $null
$doc = $null
$started = $null

try {
    $inputResolved = (Resolve-Path -LiteralPath $InputPath).Path
    $winwordResolved = (Resolve-Path -LiteralPath $WinwordPath).Path
    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $PdfPath)) | Out-Null
    if (Test-Path -LiteralPath $PdfPath -PathType Leaf) { Remove-Item -LiteralPath $PdfPath -Force }

    Write-WorkerTrace 'start.before'
    $started = Start-Process -FilePath $winwordResolved -ArgumentList @('/n','/Automation','/a') -WindowStyle Hidden -PassThru
    $result.started_pid = [int]$started.Id
    Write-WorkerTrace 'start.after'

    Add-Type -AssemblyName Microsoft.VisualBasic
    for ($i = 1; $i -le 20; $i++) {
        try {
            # PowerShell coerces $null to "" for string arguments; that creates a new COM instance.
            # A true null attaches only to the running Word instance started above.
            $candidate = [Microsoft.VisualBasic.Interaction]::GetObject([NullString]::Value, 'Word.Application')
            if ($candidate.Path -ieq (Split-Path -Parent $winwordResolved)) {
                $app = $candidate
                break
            }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    if ($null -eq $app) { throw 'Real Microsoft Word application was not present.' }

    $result.application_path = [string]$app.Path
    $result.application_version = [string]$app.Version
    try { $app.DisplayAlerts = 0 } catch {}
    try { $app.Visible = $false } catch {}
    $result.hwnd = [int64]$app.Hwnd
    $result.pid = Get-WindowPid -Hwnd $result.hwnd
    if ($result.pid -le 0) { $result.pid = [int]$started.Id }
    $result.process_image = Get-ProcessImage -ProcessId ([int]$result.pid)
    $result.identity_verified = ($result.application_path -ieq (Split-Path -Parent $winwordResolved)) -and
        (-not [string]::IsNullOrWhiteSpace($result.application_version)) -and
        ($result.process_image -ieq $winwordResolved)
    $result.ownership_verified = ([int]$result.pid -eq [int]$started.Id)
    if (-not $result.identity_verified -or -not $result.ownership_verified) {
        throw 'Word worker identity or ownership check failed.'
    }
    Write-WorkerTrace 'identity.after'

    while ([int]$app.Documents.Count -gt 0) {
        $startup = $app.Documents.Item(1)
        try {
            $result.startup_document_evidence += ,[ordered]@{
                name = [string]$startup.Name
                path = [string]$startup.Path
                full_name = [string]$startup.FullName
                saved = [bool]$startup.Saved
            }
            if (-not [string]::IsNullOrWhiteSpace([string]$startup.Path) -and
                ((Resolve-Path -LiteralPath ([string]$startup.FullName)).Path -ine $inputResolved)) {
                throw 'Word worker found a startup document outside this working copy.'
            }
            $startup.Close($false)
            $result.startup_documents_closed = [int]$result.startup_documents_closed + 1
        } finally {
            Release-ComReference -Reference $startup
        }
    }

    Write-WorkerTrace 'open.before'
    $doc = $app.Documents.Open($inputResolved, $false, $true, $false)
    if ($null -eq $doc) { throw 'Word worker document open returned null.' }
    $result.open_attempts += ,[ordered]@{ attempt = 1; opened = $true; documents_count = [int]$app.Documents.Count; error = '' }
    Write-WorkerTrace 'open.after'

    Write-WorkerTrace 'saveas2.before'
    $doc.SaveAs2($PdfPath, 17)
    Write-WorkerTrace 'saveas2.after'

    $pdfInfo = Get-PdfInfo -Path $PdfPath
    $result.bytes = [int64]$pdfInfo.bytes
    $result.status = 'PASS'
} catch {
    $result.status = 'FAIL'
    $result.message = $_.Exception.Message
} finally {
    if ($null -ne $doc) {
        try { $doc.Close($false); $result.cleanup.document_closed = $true } catch {}
        Release-ComReference -Reference $doc
    }
    if ($null -ne $app) {
        try { $app.Quit(); $result.cleanup.quit_called = $true } catch {}
        Release-ComReference -Reference $app
        $result.cleanup.com_released = $true
    }
    if ($result.started_pid -gt 0) {
        for ($i = 1; $i -le 30; $i++) {
            $remaining = @(Get-Process -Id ([int]$result.started_pid) -ErrorAction SilentlyContinue)
            if ($remaining.Count -eq 0) { break }
            Start-Sleep -Milliseconds 500
            $result.cleanup.exit_wait_ms = [int]$result.cleanup.exit_wait_ms + 500
        }
        $remaining = @(Get-Process -Id ([int]$result.started_pid) -ErrorAction SilentlyContinue)
        $result.cleanup.remaining_owned_pids = @($remaining | ForEach-Object { [int]$_.Id })
        $result.cleanup.exit_observed = $remaining.Count -eq 0
    }
    $json = $result | ConvertTo-Json -Depth 10
    $resultParent = Split-Path -Parent $ResultPath
    if ($resultParent) { [System.IO.Directory]::CreateDirectory($resultParent) | Out-Null }
    [System.IO.File]::WriteAllText($ResultPath, $json, [System.Text.UTF8Encoding]::new($false))
}

if ($result.status -eq 'PASS' -and $result.cleanup.exit_observed) { exit 0 }
exit 2
