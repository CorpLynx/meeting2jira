<#
.SYNOPSIS
    Export meetings (Outlook COM or a CSV you exported) and push them to Jira via the Python CLI.

.DESCRIPTION
    Written to run under Constrained Language Mode too (cmdlets, arrays, native calls only), so the
    CSV path works even where COM is blocked. The COM path additionally needs FullLanguage mode.

    Window: midnight -DaysBack days ago until now. Re-running over the same window is safe; meetings
    already pushed are skipped by the Python side's local state.

.EXAMPLE
    .\Invoke-MeetingSync.ps1 -DryRun
.EXAMPLE
    .\Invoke-MeetingSync.ps1 -DaysBack 5
.EXAMPLE
    .\Invoke-MeetingSync.ps1 -Source Csv -CsvPath "$HOME\Documents\calendar.csv" -DryRun
#>
[CmdletBinding()]
param(
    [ValidateRange(0, 31)][int]$DaysBack = 0,
    [ValidateSet('Com', 'Csv')][string]$Source = 'Com',
    [string]$CsvPath,
    [switch]$DryRun,
    [switch]$KeepExport,
    [switch]$IncludeOrganizer,
    [string]$Python,
    [string]$Config,
    [ValidateRange(1, 3650)][int]$TranscriptRetentionDays = 30
)

$ErrorActionPreference = 'Stop'

# Duplicated in Test-Environment.ps1 on purpose: dot-sourcing can fail across AppLocker trust levels.
function Resolve-Python([string]$Override) {
    if ($Override) { return @{ Exe = $Override; Prefix = @() } }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher) { return @{ Exe = $launcher.Source; Prefix = @('-3') } }
    foreach ($name in @('python.exe', 'python3.exe')) {
        # Skip the Microsoft Store alias stub, which opens the Store instead of running Python.
        $cmd = Get-Command $name -All -ErrorAction SilentlyContinue |
            Where-Object { $_.Source -notmatch '\\WindowsApps\\' } | Select-Object -First 1
        if ($cmd) { return @{ Exe = $cmd.Source; Prefix = @() } }
    }
    throw 'Python 3 not found. Install it from your agency software catalog, or pass -Python C:\path\to\python.exe'
}

# This script is app/src/windows/, so the app root is two levels up. The Python package lives in
# app/src, which is put on PYTHONPATH rather than relying on the working directory.
$appRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$srcRoot = Join-Path $appRoot 'src'
$dataDir = Join-Path $env:LOCALAPPDATA 'meeting2jira'
if (-not $Config) { $Config = Join-Path $dataDir 'config.json' }
$logDir = Join-Path $dataDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# One transcript per run adds up on a daily schedule, so drop the old ones first. Best effort:
# a locked or unreadable file must never stop the sync itself.
$cutoff = (Get-Date).AddDays(-$TranscriptRetentionDays)
Get-ChildItem -Path $logDir -Filter 'sync_*.log' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt $cutoff } |
    Remove-Item -Force -ErrorAction SilentlyContinue

$transcript = Join-Path $logDir ('sync_{0}.log' -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
# A transcript already running in this session would otherwise make Start-Transcript throw.
Start-Transcript -Path $transcript -ErrorAction SilentlyContinue | Out-Null

$exitCode = 1
$exportPath = $null
try {
    if (-not (Test-Path $Config)) {
        throw "Config not found at $Config. From $appRoot run: .\meeting2jira setup"
    }
    $py = Resolve-Python $Python
    $windowStart = (Get-Date).Date.AddDays(-$DaysBack)
    $windowEnd = Get-Date

    if ($Source -eq 'Com') {
        $exportScript = Join-Path $PSScriptRoot 'Export-OutlookMeetings.ps1'
        # The exporter chats to the host with Write-Host and returns the file path on the output
        # stream, so the last output object is the path. Verify it before handing it to Python:
        # an empty or missing path would otherwise become a confusing `--input ` argument.
        $exportPath = & $exportScript -Start $windowStart -End $windowEnd -IncludeOrganizer:$IncludeOrganizer |
            Select-Object -Last 1
        if (-not $exportPath -or -not (Test-Path -LiteralPath $exportPath)) {
            throw "The Outlook export did not produce a file. Run Export-OutlookMeetings.ps1 -Verbose on its own to see why."
        }
        $sourceArgs = @('--input', $exportPath)
    } else {
        if (-not $CsvPath -or -not (Test-Path $CsvPath)) { throw '-CsvPath must point to an Outlook calendar CSV export.' }
        $sourceArgs = @('--csv', (Resolve-Path $CsvPath).Path)
    }

    $cliArgs = @()
    $cliArgs += $py.Prefix
    $cliArgs += @('-m', 'meeting2jira', 'push', '--config', $Config)
    $cliArgs += $sourceArgs
    if ($DryRun) { $cliArgs += '--dry-run' }

    Write-Host "Running: $($py.Exe) $($cliArgs -join ' ')"
    # PYTHONPATH makes `-m meeting2jira` resolve without depending on the working directory.
    $previousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = $srcRoot
    Push-Location $appRoot
    # Windows PowerShell 5.1 turns every stderr write from a native command into an error record,
    # and $ErrorActionPreference = 'Stop' makes that terminating. Python writes tracebacks and
    # warnings to stderr, so without this a single stderr line would abort the sync, discard
    # Python's real exit code, and skip the export cleanup below. Relax it only around the call.
    $previousEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $py.Exe @cliArgs
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousEap
        $env:PYTHONPATH = $previousPythonPath
        Pop-Location
    }
    if ($null -eq $exitCode) { $exitCode = 1 }   # native command never ran

    # The export holds calendar data; don't leave it lying around. Kept on failure for debugging.
    if ($exportPath -and $exitCode -eq 0 -and -not $KeepExport) {
        Remove-Item -LiteralPath $exportPath -ErrorAction SilentlyContinue
    }
} catch {
    Write-Error $_ -ErrorAction Continue
    $exitCode = 2
} finally {
    Stop-Transcript -ErrorAction SilentlyContinue | Out-Null
}
exit $exitCode
