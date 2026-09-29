<#
.SYNOPSIS
    Export the meeting2jira state database to CSV for Power BI.

.DESCRIPTION
    Power BI Desktop has no built-in SQLite connector, and an ODBC driver install usually needs admin
    rights. This sidesteps both by having Python (already required by the tool, and already able to
    read sqlite from the standard library) write a CSV.

    Read-only: it opens the database, selects, and writes a separate file. It never modifies state.

    The output contains meeting subjects, so treat it as being as sensitive as your calendar titles.
    Use -NoSubjects to leave them out, which still supports every time-per-parent question.

.EXAMPLE
    .\report.ps1
.EXAMPLE
    .\report.ps1 -NoSubjects -OutFile "$HOME\Documents\meetings.csv"
#>
[CmdletBinding()]
param(
    [string]$OutFile,
    [string]$StateDb,
    [switch]$NoSubjects,
    [string]$Python
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

$dataDir = Join-Path $env:LOCALAPPDATA 'meeting2jira'
if (-not $StateDb) { $StateDb = Join-Path $dataDir 'state.db' }
if (-not $OutFile) { $OutFile = Join-Path $dataDir 'meetings.csv' }

if (-not (Test-Path -LiteralPath $StateDb)) {
    throw "No state database at $StateDb. Run a real (non-dry-run) sync first; there is nothing to report on yet."
}

# Written to a temp file rather than passed with -c, because Windows PowerShell 5.1 mangles embedded
# double quotes in arguments to native commands.
$probe = Join-Path $env:TEMP ('m2j-report-{0}.py' -f (Get-Date -Format 'yyyyMMddHHmmss'))
Set-Content -Path $probe -Encoding ascii -Value @'
import csv
import sqlite3
import sys

db, out, include_subjects = sys.argv[1], sys.argv[2], sys.argv[3] == "1"

columns = ["issue_key", "parent", "start_utc", "minutes", "worklog_logged", "created_at"]
if include_subjects:
    columns.insert(2, "summary")

# Opened read-only so a report can never disturb the state that prevents duplicate sub-tasks.
conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM synced ORDER BY start_utc").fetchall()

with open(out, "w", newline="", encoding="utf-8-sig") as fh:   # BOM so Excel/Power BI detect UTF-8
    writer = csv.writer(fh)
    writer.writerow(columns + ["hours", "meeting_date"])
    for r in rows:
        keys = r.keys()
        values = [r[c] if c in keys else "" for c in columns]
        minutes = r["minutes"] if "minutes" in keys else 0
        writer.writerow(values + [round((minutes or 0) / 60.0, 2), (r["start_utc"] or "")[:10]])

conn.close()
print("%d row(s) -> %s" % (len(rows), out))
'@

$py = Resolve-Python $Python
if (-not $py) {
    $tried = (@($script:M2JPythonAttempts) -join "`n  ")
    throw "No working Python 3.8+ found. Candidates tried:`n  $tried`nPass -Python C:\path\to\python.exe"
}
$argv = @()
$argv += $py.Prefix
$argv += @($probe, $StateDb, $OutFile, $(if ($NoSubjects) { '0' } else { '1' }))

# 5.1 turns any stderr line from a native command into an error record, which $ErrorActionPreference
# 'Stop' would make terminating. Relax it just around the call.
$previousEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $output = & $py.Exe @argv 2>&1
    $code = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousEap
    Remove-Item -LiteralPath $probe -ErrorAction SilentlyContinue
}

if ($code -ne 0) {
    throw "Export failed: $(($output | Out-String).Trim())"
}

Write-Host ($output | Out-String).Trim()
if ($NoSubjects) {
    Write-Host 'Subjects were excluded.'
} else {
    Write-Host 'NOTE: this file contains meeting subjects. Treat it as you would your calendar; use -NoSubjects to omit them.'
}
Write-Host "In Power BI Desktop: Get Data > Text/CSV > $OutFile"
