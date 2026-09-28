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

# Duplicated from the app's scripts on purpose: dot-sourcing can fail across AppLocker trust levels.
function Resolve-Python([string]$Override) {
    if ($Override) { return @{ Exe = $Override; Prefix = @() } }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher) { return @{ Exe = $launcher.Source; Prefix = @('-3') } }
    foreach ($name in @('python.exe', 'python3.exe', 'python3')) {
        $cmd = Get-Command $name -All -ErrorAction SilentlyContinue |
            Where-Object { $_.Source -notmatch '\\WindowsApps\\' } | Select-Object -First 1
        if ($cmd) { return @{ Exe = $cmd.Source; Prefix = @() } }
    }
    throw 'Python 3 not found. Pass -Python C:\path\to\python.exe'
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
