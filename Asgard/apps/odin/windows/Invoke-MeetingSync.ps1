<#
.SYNOPSIS
    Export meetings (Outlook COM or a CSV you exported) and run Odin's daily run on them.

.DESCRIPTION
    Written to run under Constrained Language Mode too (cmdlets, arrays, native calls only), so the
    CSV path works even where COM is blocked. The COM path additionally needs FullLanguage mode.

    Window: midnight -DaysBack days ago until now. Re-running over the same window is safe; meetings
    already pushed, and time already posted, are recognised in Muninn and skipped.

    The daily run (odin daily) pushes the meetings, reads Jira into Muninn for Asgard's other apps,
    and posts the days you approved in Baldur. -DryRun previews all of it and changes nothing.

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
    [ValidateRange(1, 3650)][int]$TranscriptRetentionDays = 30,
    [ValidateRange(1, 3650)][int]$ExportRetentionDays = 7
)

$ErrorActionPreference = 'Stop'

# Duplicated verbatim in windows/Invoke-MeetingSync.ps1, windows/Test-Environment.ps1 and
# tools/Invoke-WindowsChecks.ps1 on purpose: dot-sourcing can fail across AppLocker trust levels.
# tests/test_odin_guardrails.py asserts the copies stay identical, so change one and change all.
#
# Odin is an Asgard app and runs on what Asgard runs on, in this order:
#   1. -Python, when given;
#   2. the packaged build's own program, asgard-cli.exe two folders above Odin's, which brings
#      Python and everything else with it and runs Odin's scripts as python.exe would;
#   3. the Python Asgard was installed with, from install-ledger.json;
#   4. any other Python 3.9+ whose SQLite is 3.37 or newer with FTS5, which Muninn needs (Windows
#      Python has that from 3.11): the launcher, PATH, the registry, the usual directories.
# Existence is not proof. A real agency install routinely has py.exe present with no 3.x registered,
# a working Python that was never added to PATH, or an old one ahead of a new one. So every real
# Python is *executed* and made to report its version and its SQLite's; the first that works wins.
# $script:OdinPythonAttempts records every candidate tried, which is what makes a failure diagnosable.
function Resolve-Python([string]$Override) {
    $script:OdinPythonAttempts = @()
    $candidates = @()

    if ($Override) { $candidates += @{ Exe = $Override; Prefix = @() } }

    # The packaged build: Odin's scripts are in apps\odin\windows and apps\odin\tools, two folders
    # below the build's programs. It can't run probe code, so it is trusted as it is.
    $asgardRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
    $frozen = Join-Path $asgardRoot 'asgard-cli.exe'
    if (-not $Override -and (Test-Path -LiteralPath $frozen)) {
        $script:OdinPythonAttempts += "ok   $frozen (Asgard's packaged build)"
        return @{ Exe = $frozen; Prefix = @(); Version = 'the packaged build'; Frozen = $true }
    }

    # The Python Asgard was installed with. A packaged install's asgard-cli.exe runs only the scripts
    # in its own folder, so it is no use to a copy of Odin anywhere else.
    $asgardData = if ($env:ASGARD_HOME) { $env:ASGARD_HOME } else { Join-Path $env:LOCALAPPDATA 'Asgard' }
    $ledgerPath = Join-Path $asgardData 'install-ledger.json'
    if (Test-Path -LiteralPath $ledgerPath) {
        $ledger = $null
        try {
            $ledger = Get-Content -LiteralPath $ledgerPath -Raw -ErrorAction Stop | ConvertFrom-Json
        } catch {
            $ledger = $null
        }
        if ($ledger -and $ledger.python -and ("$($ledger.python)" -notmatch 'asgard-cli\.exe$')) {
            $candidates += @{ Exe = "$($ledger.python)"; Prefix = @() }
        }
    }

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

    # Single-quoted so no double quote ever reaches the native command: Windows PowerShell 5.1
    # mangles embedded double quotes in native arguments. Exit 3 marks "runs, but can't run Muninn".
    $probe = 'import sqlite3, sys; con = sqlite3.connect('':memory:''); ' +
        'fts5 = any(''FTS5'' in row[0] for row in con.execute(''pragma compile_options'')); ' +
        'print(sys.version.split()[0] + '' with SQLite '' + sqlite3.sqlite_version); ' +
        'sys.exit(0 if sys.version_info >= (3, 9) and sqlite3.sqlite_version_info >= (3, 37, 0) and fts5 else 3)'
    $seen = @()
    foreach ($candidate in $candidates) {
        if (-not $candidate.Exe) { continue }
        $label = (@($candidate.Exe) + $candidate.Prefix) -join ' '
        if ($seen -contains $label) { continue }
        $seen += $label
        if (-not (Test-Path -LiteralPath $candidate.Exe)) {
            $script:OdinPythonAttempts += "gone $label"
            continue
        }
        $argv = @()
        $argv += $candidate.Prefix
        $argv += @('-c', $probe)
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
            $script:OdinPythonAttempts += "ok   $label -> $reportedVersion"
            return @{ Exe = $candidate.Exe; Prefix = $candidate.Prefix; Version = $reportedVersion; Frozen = $false }
        } elseif ($code -eq 3) {
            $script:OdinPythonAttempts += "old  $label -> $reportedVersion (needs 3.9+ with SQLite 3.37+ and FTS5)"
        } else {
            $script:OdinPythonAttempts += "fail $label -> $reportedVersion"
        }
    }
    return $null
}

# This script is apps\odin\windows\, so Odin's folder is one level up. cli.py there finds the
# odin and asgard packages itself, whatever the working directory.
$appRoot = Split-Path -Parent $PSScriptRoot
$cliPy = Join-Path $appRoot 'cli.py'
# Odin's files live under Asgard's folder: ASGARD_HOME\odin when set (as in Asgard's own tests),
# else %LOCALAPPDATA%\Asgard\odin. Odin is an Asgard app; its records are in Muninn beside them.
$asgardDir = if ($env:ASGARD_HOME) { $env:ASGARD_HOME } else { Join-Path $env:LOCALAPPDATA 'Asgard' }
$dataDir = Join-Path $asgardDir 'odin'
# A folder from before (%LOCALAPPDATA%\meeting2jira) moves here the first time, in one rename, so the
# DPAPI token files come across unchanged (they open for the same Windows user wherever they are).
$legacyDir = Join-Path $env:LOCALAPPDATA 'meeting2jira'
if (-not $env:ASGARD_HOME -and -not (Test-Path -LiteralPath $dataDir) -and (Test-Path -LiteralPath $legacyDir)) {
    New-Item -ItemType Directory -Force -Path $asgardDir | Out-Null
    try {
        Move-Item -LiteralPath $legacyDir -Destination $dataDir -ErrorAction Stop
    } catch {
        throw "Odin's files are moving to $dataDir, but $legacyDir couldn't be moved ($($_.Exception.Message)). Close anything using it and run again."
    }
}
if (-not $Config) { $Config = Join-Path $dataDir 'config.json' }
$logDir = Join-Path $dataDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# One transcript per run adds up on a daily schedule, so drop the old ones first. Best effort:
# a locked or unreadable file must never stop the sync itself.
$cutoff = (Get-Date).AddDays(-$TranscriptRetentionDays)
Get-ChildItem -Path $logDir -Filter 'sync_*.log' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt $cutoff } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# Exports hold calendar data - subjects, locations, sometimes the organizer. They are removed after a
# successful run, but a failed run keeps one for diagnosis and -KeepExport keeps them on purpose, so
# without this they accumulate in the profile indefinitely. Both the COM and OWA paths write here.
# Best effort, like the transcripts above: a locked file must never stop the sync.
$exportDir = Join-Path $dataDir 'exports'
if (Test-Path $exportDir) {
    $exportCutoff = (Get-Date).AddDays(-$ExportRetentionDays)
    Get-ChildItem -Path $exportDir -Filter '*.json' -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $exportCutoff } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

$transcript = Join-Path $logDir ('sync_{0}.log' -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
# A transcript already running in this session would otherwise make Start-Transcript throw.
Start-Transcript -Path $transcript -ErrorAction SilentlyContinue | Out-Null

$exitCode = 1
$exportPath = $null
try {
    if (-not (Test-Path $Config)) {
        throw "Config not found at $Config. Run: odin setup (odin.cmd is in $appRoot)"
    }
    $py = Resolve-Python $Python
    if (-not $py) {
        # Every candidate and its rejection reason, because "not found" alone is undiagnosable
        # on a machine that plainly has Python installed somewhere.
        $tried = (@($script:OdinPythonAttempts) -join "`n  ")
        throw "No Python that can run Odin was found: it needs 3.9+ with SQLite 3.37+ (3.11+ on Windows), as Asgard does. Candidates tried:`n  $tried`nRun odin doctor, or pass -Python C:\path\to\python.exe"
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
    $cliArgs += @($cliPy, 'daily', '--config', $Config)
    $cliArgs += $sourceArgs
    if ($DryRun) { $cliArgs += '--dry-run' }

    Write-Host "Running: $($py.Exe) $($cliArgs -join ' ')"
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
            $alertName = 'ATTENTION-Odin.txt'
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
                'Odin needs attention',
                '====================',
                '',
                'The sync failed before it reached the Jira step, so no meetings were pushed at all.',
                '',
                ('When (local):  ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')),
                ('Error:         ' + "$($_.Exception.Message)"),
                '',
                'What to do',
                '----------',
                "  1. Open a Command Prompt in Odin's folder ($appRoot)",
                '  2. Run:  odin doctor',
                '     It reports Outlook, Python, execution policy and the last run in one pass.',
                '  3. Fix what it names, then:  odin preview',
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
