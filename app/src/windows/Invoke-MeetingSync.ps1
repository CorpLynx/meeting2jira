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

# Duplicated verbatim in src/windows/Invoke-MeetingSync.ps1, src/windows/Test-Environment.ps1,
# tools/Invoke-WindowsChecks.ps1 and power-platform/power-bi/report.ps1 on purpose: dot-sourcing can
# fail across AppLocker trust levels. tests/test_guardrails.py asserts the copies stay identical, so
# change one and you must change all of them.
#
# Existence is not proof. A real agency install routinely has py.exe present with no 3.x registered
# (so `py -3` fails), or a working Python that was never added to PATH, or a 2.x on PATH ahead of a
# 3.x. So candidates are gathered from PATH, the registry and the usual install directories, then
# each is *executed* and made to report its version. The first that actually works wins.
# $script:M2JPythonAttempts records every candidate tried, which is what makes a failure diagnosable.
function Resolve-Python([string]$Override) {
    $script:M2JPythonAttempts = @()
    $candidates = @()

    if ($Override) { $candidates += @{ Exe = $Override; Prefix = @() } }

    # The launcher, but only if it can really produce a 3.x - that is verified below, not assumed.
    foreach ($found in @(Get-Command 'py.exe' -All -ErrorAction SilentlyContinue)) {
        $candidates += @{ Exe = "$($found.Source)"; Prefix = @('-3') }
    }
    # Anything on PATH. The Microsoft Store alias stub is skipped: it opens the Store instead of
    # running Python, and it reports success to `where`.
    foreach ($name in @('python3.exe', 'python.exe')) {
        foreach ($found in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            if ("$($found.Source)" -notmatch '\\WindowsApps\\') {
                $candidates += @{ Exe = "$($found.Source)"; Prefix = @() }
            }
        }
    }
    # The registry, which is where an installer records itself even when PATH was left alone.
    $hives = @(
        'Registry::HKEY_CURRENT_USER\SOFTWARE\Python\PythonCore',
        'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Python\PythonCore',
        'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Python\PythonCore'
    )
    foreach ($hive in $hives) {
        if (-not (Test-Path $hive)) { continue }
        foreach ($version in @(Get-ChildItem -Path $hive -ErrorAction SilentlyContinue)) {
            $installKey = Join-Path "$($version.PSPath)" 'InstallPath'
            if (-not (Test-Path $installKey)) { continue }
            $install = Get-ItemProperty -Path $installKey -ErrorAction SilentlyContinue
            if (-not $install) { continue }
            if ($install.ExecutablePath) {
                $candidates += @{ Exe = "$($install.ExecutablePath)"; Prefix = @() }
            }
            $installRoot = "$($install.'(default)')"
            if ($installRoot) {
                $candidates += @{ Exe = (Join-Path $installRoot 'python.exe'); Prefix = @() }
            }
        }
    }
    # The usual directories, for an install that registered nothing at all.
    $globs = @()
    if ($env:LOCALAPPDATA) { $globs += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe') }
    if ($env:ProgramFiles) { $globs += (Join-Path $env:ProgramFiles 'Python3*\python.exe') }
    $globs += 'C:\Python3*\python.exe'
    foreach ($glob in $globs) {
        foreach ($found in @(Get-ChildItem -Path $glob -ErrorAction SilentlyContinue)) {
            $candidates += @{ Exe = "$($found.FullName)"; Prefix = @() }
        }
    }

    $seen = @()
    foreach ($candidate in $candidates) {
        if (-not $candidate.Exe) { continue }
        $label = (@($candidate.Exe) + $candidate.Prefix) -join ' '
        if ($seen -contains $label) { continue }
        $seen += $label
        if (-not (Test-Path -LiteralPath $candidate.Exe)) {
            $script:M2JPythonAttempts += "gone $label"
            continue
        }
        $argv = @()
        $argv += $candidate.Prefix
        # Single-quoted so no double quote ever reaches the native command: Windows PowerShell 5.1
        # mangles embedded double quotes in native arguments. Exit 3 marks "runs, but too old".
        $argv += @('-c', 'import sys; print(sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3, 8) else 3)')
        $previousEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $reported = ''
        $code = 9
        try {
            $reported = & $candidate.Exe @argv 2>&1
            $code = $LASTEXITCODE
        } catch {
            $reported = "$($_.Exception.Message)"
        } finally {
            $ErrorActionPreference = $previousEap
        }
        $reportedVersion = "$(@($reported) | Select-Object -First 1)".Trim()
        if ($code -eq 0) {
            $script:M2JPythonAttempts += "ok   $label -> $reportedVersion"
            return @{ Exe = $candidate.Exe; Prefix = $candidate.Prefix; Version = $reportedVersion }
        } elseif ($code -eq 3) {
            $script:M2JPythonAttempts += "old  $label -> $reportedVersion (needs 3.8+)"
        } else {
            $script:M2JPythonAttempts += "fail $label -> $reportedVersion"
        }
    }
    return $null
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
    if (-not $py) {
        # Every candidate and its rejection reason, because "not found" alone is undiagnosable
        # on a machine that plainly has Python installed somewhere.
        $tried = (@($script:M2JPythonAttempts) -join "`n  ")
        throw "No working Python 3.8+ found. Candidates tried:`n  $tried`nRun .\meeting2jira doctor, or pass -Python C:\path\to\python.exe"
    }
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

    # A failure here happened BEFORE Python ran - a missing config, or the Outlook export producing
    # nothing - so cmd_push never recorded it and never raised an alert. That is the case that most
    # needs surfacing: not one meeting was even attempted. Unlike a push failure, there is no streak
    # to weigh up, so this alerts on the first occurrence.
    #
    # Deliberately cmdlets and core types only: this script has to keep working under Constrained
    # Language Mode, which rules out toast notifications and .NET file helpers alike.
    try {
        $alertWanted = $true
        if (Test-Path -LiteralPath $Config) {
            $cfg = Get-Content -LiteralPath $Config -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
            if ($cfg -and $cfg.notify -and $cfg.notify.PSObject.Properties['desktop_alert']) {
                $alertWanted = [bool]$cfg.notify.desktop_alert
            }
        }
        if ($alertWanted) {
            $alertName = 'ATTENTION-meeting2jira.txt'
            $targets = @()
            # OneDrive Known Folder Move relocates the Desktop, which is common on managed machines.
            foreach ($base in @($env:OneDrive, $env:OneDriveCommercial, $env:USERPROFILE)) {
                if ($base) {
                    $candidate = Join-Path $base 'Desktop'
                    if (Test-Path -LiteralPath $candidate) { $targets += $candidate }
                }
            }
            $targets += $dataDir
            $lines = @(
                'meeting2jira needs attention',
                '============================',
                '',
                'The sync failed before it reached the Jira step, so no meetings were pushed at all.',
                '',
                ('When (local):  ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')),
                ('Error:         ' + "$($_.Exception.Message)"),
                '',
                'What to do',
                '----------',
                '  1. Open PowerShell in the meeting2jira folder',
                '  2. Run:  .\meeting2jira doctor',
                '     It reports Outlook, Python, execution policy and the last run in one pass.',
                '  3. Fix what it names, then:  .\meeting2jira preview',
                '',
                'Nothing was lost, and nothing was pushed twice: re-running is safe, because',
                'already-synced meetings are recognised and skipped.',
                '',
                ('Transcript: ' + $transcript)
            )
            foreach ($target in $targets) {
                $written = $false
                try {
                    Set-Content -LiteralPath (Join-Path $target $alertName) -Value $lines -Encoding UTF8 -ErrorAction Stop
                    Write-Host "Wrote $(Join-Path $target $alertName)"
                    $written = $true
                } catch {
                    $written = $false
                }
                if ($written) { break }
            }
        }
    } catch {
        # An alert is a convenience. Failing to write one must not change the exit code.
        Write-Host 'Could not write the desktop alert.'
    }
} finally {
    Stop-Transcript -ErrorAction SilentlyContinue | Out-Null
}
exit $exitCode
