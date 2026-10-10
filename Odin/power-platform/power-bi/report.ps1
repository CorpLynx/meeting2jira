<#
.SYNOPSIS
    Export Odin's meeting sub-tasks to CSV for Power BI.

.DESCRIPTION
    Power BI Desktop has no built-in SQLite connector, and an ODBC driver install usually needs admin
    rights. This sidesteps both: Odin (an Asgard app) reads its records from Muninn, Asgard's
    database, and writes a CSV (`odin report`). Read-only: it never changes anything in Muninn.

    The output contains meeting subjects, so treat it as being as sensitive as your calendar titles.
    Use -NoSubjects to leave them out, which still supports every time-per-parent question.

    Columns: issue_key, parent, summary (unless -NoSubjects), start_utc, minutes, worklog_logged,
    created_at, origin (odin, or state_db for history from before Muninn), hours, meeting_date.

.EXAMPLE
    .\report.ps1
.EXAMPLE
    .\report.ps1 -NoSubjects -OutFile "$HOME\Documents\meetings.csv"
#>
[CmdletBinding()]
param(
    [string]$OutFile,
    [switch]$NoSubjects,
    [string]$OdinCmd
)

$ErrorActionPreference = 'Stop'

# Odin is apps\odin\odin.cmd in Asgard's folder: the installed copy, else a checkout beside this one.
if (-not $OdinCmd) {
    $asgardDir = if ($env:ASGARD_HOME) { $env:ASGARD_HOME } else { Join-Path $env:LOCALAPPDATA 'Asgard' }
    $candidates = @(
        (Join-Path $asgardDir 'app\apps\odin\odin.cmd'),
        (Join-Path $PSScriptRoot '..\..\..\Asgard\apps\odin\odin.cmd')
    )
    foreach ($candidate in $candidates) {
        if (-not $OdinCmd -and (Test-Path -LiteralPath $candidate)) { $OdinCmd = $candidate }
    }
}
if (-not $OdinCmd -or -not (Test-Path -LiteralPath $OdinCmd)) {
    throw 'Odin was not found. Install Asgard, or pass -OdinCmd C:\path\to\Asgard\apps\odin\odin.cmd'
}

$arguments = @('report')
if ($OutFile) { $arguments += @('--out', $OutFile) }
if ($NoSubjects) { $arguments += '--no-subjects' }

# 5.1 turns any stderr line from a native command into an error record, which $ErrorActionPreference
# 'Stop' would make terminating. Relax it just around the call.
$previousEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $output = & cmd.exe '/c' $OdinCmd @arguments 2>&1
    $code = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousEap
}
Write-Host ($output | Out-String).Trim()
if ($code -ne 0) {
    throw "The export failed (exit $code). Run: odin check"
}
