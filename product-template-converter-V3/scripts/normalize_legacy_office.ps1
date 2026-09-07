param(
    [Parameter(Mandatory=$true)][ValidateSet('word','powerpoint')][string]$Kind,
    [Parameter(Mandatory=$true)][string]$InputPath,
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [string]$OriginalPdfPath = '',
    [string]$NormalizedPdfPath = ''
)

$ErrorActionPreference = 'Stop'
$inputResolved = (Resolve-Path -LiteralPath $InputPath).Path
$outputParent = Split-Path -Parent $OutputPath
if ($outputParent) { [System.IO.Directory]::CreateDirectory($outputParent) | Out-Null }
$app = $null
$document = $null
$applicationVersion = ''
$processNames = if ($Kind -eq 'word') { @('WINWORD', 'wps') } else { @('POWERPNT', 'wpp') }
$runStartedAt = Get-Date

function Stop-OwnedHiddenOfficeProcess {
    param(
        [int]$ProcessId,
        [string[]]$ProcessNames,
        [datetime]$RunStartedAt
    )

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $true }
    if (@($ProcessNames) -notcontains $process.ProcessName) { return $false }
    try {
        if ($process.StartTime -lt $RunStartedAt.AddSeconds(-2)) { return $false }
    } catch {
        return $false
    }
    if ([int64]$process.MainWindowHandle -ne 0) { return $false }
    if (-not [string]::IsNullOrWhiteSpace([string]$process.MainWindowTitle)) { return $false }
    try {
        Stop-Process -Id $ProcessId -Force -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

$baselinePids = @()
foreach ($name in $processNames) {
    $baselinePids += @(Get-Process -Name $name -ErrorAction SilentlyContinue | ForEach-Object { [int]$_.Id })
}
try {
    if ($Kind -eq 'word') {
        $app = New-Object -ComObject Word.Application
        $applicationVersion = [string]$app.Version
        $app.Visible = $false
        $app.DisplayAlerts = 0
        $document = $app.Documents.Open($inputResolved, $false, $true, $false)
        if ($OriginalPdfPath) {
            [System.IO.Directory]::CreateDirectory((Split-Path -Parent $OriginalPdfPath)) | Out-Null
            $document.ExportAsFixedFormat($OriginalPdfPath, 17)
        }
        $document.SaveAs2($OutputPath, 16)
        if ($NormalizedPdfPath) {
            [System.IO.Directory]::CreateDirectory((Split-Path -Parent $NormalizedPdfPath)) | Out-Null
            $document.ExportAsFixedFormat($NormalizedPdfPath, 17)
        }
    }
    else {
        $app = New-Object -ComObject PowerPoint.Application
        $applicationVersion = [string]$app.Version
        $document = $app.Presentations.Open($inputResolved, $true, $false, $false)
        if ($OriginalPdfPath) {
            [System.IO.Directory]::CreateDirectory((Split-Path -Parent $OriginalPdfPath)) | Out-Null
            $document.SaveAs($OriginalPdfPath, 32)
        }
        $document.SaveAs($OutputPath, 24)
        if ($NormalizedPdfPath) {
            [System.IO.Directory]::CreateDirectory((Split-Path -Parent $NormalizedPdfPath)) | Out-Null
            $document.SaveAs($NormalizedPdfPath, 32)
        }
    }
    if (-not (Test-Path -LiteralPath $OutputPath)) { throw 'Normalized file was not created' }
    if ($OriginalPdfPath -and -not (Test-Path -LiteralPath $OriginalPdfPath)) { throw 'Original PDF evidence was not created' }
    if ($NormalizedPdfPath -and -not (Test-Path -LiteralPath $NormalizedPdfPath)) { throw 'Normalized PDF evidence was not created' }
    $applicationName = if ($Kind -eq 'word') { 'Microsoft Word' } else { 'Microsoft PowerPoint' }
    [pscustomobject]@{
        status='PASS'
        kind=$Kind
        application=$applicationName
        application_version=$applicationVersion
        output=(Resolve-Path -LiteralPath $OutputPath).Path
        original_pdf=if($OriginalPdfPath){(Resolve-Path -LiteralPath $OriginalPdfPath).Path}else{''}
        normalized_pdf=if($NormalizedPdfPath){(Resolve-Path -LiteralPath $NormalizedPdfPath).Path}else{''}
    } | ConvertTo-Json -Compress
}
finally {
    if ($document -ne $null) {
        try { $document.Close() } catch {}
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document) } catch {}
    }
    if ($app -ne $null) {
        try { $app.Quit() } catch {}
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($app) } catch {}
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
    $newPids = @()
    foreach ($name in $processNames) {
        $newPids += @(Get-Process -Name $name -ErrorAction SilentlyContinue |
            Where-Object { @($baselinePids) -notcontains [int]$_.Id } |
            ForEach-Object { [int]$_.Id })
    }
    for ($probe = 1; $probe -le 30 -and @($newPids).Count -gt 0; $probe++) {
        Start-Sleep -Milliseconds 500
        $remaining = @()
        foreach ($ownedPid in $newPids) {
            if (Get-Process -Id $ownedPid -ErrorAction SilentlyContinue) {
                $remaining += ,$ownedPid
            }
        }
        $newPids = @($remaining)
    }
    if (@($newPids).Count -gt 0) {
        foreach ($ownedPid in @($newPids)) {
            [void](Stop-OwnedHiddenOfficeProcess -ProcessId ([int]$ownedPid) -ProcessNames @($processNames) -RunStartedAt $runStartedAt)
        }
        for ($probe = 1; $probe -le 10 -and @($newPids).Count -gt 0; $probe++) {
            Start-Sleep -Milliseconds 500
            $remaining = @()
            foreach ($ownedPid in $newPids) {
                if (Get-Process -Id $ownedPid -ErrorAction SilentlyContinue) {
                    $remaining += ,$ownedPid
                }
            }
            $newPids = @($remaining)
        }
    }
    if (@($newPids).Count -gt 0) {
        throw ('Office normalization left owned writer process running: ' + ([string]::Join(', ', @($newPids))))
    }
}
