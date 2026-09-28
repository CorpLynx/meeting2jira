<#
.SYNOPSIS
    Paste into a Power Automate for desktop "Run PowerShell script" action.

.DESCRIPTION
    Runs the existing meeting2jira entry point and turns its exit code into a single line the flow
    can show in its run history. It deliberately contains no filtering, routing, or Jira logic:
    duplicating any of that here would create a second set of rules that drifts from config.json.

    Edit $AppPath. Everything else should be left alone.

    Set the action's output variable to PsOutput and its error output to PsError, then branch on
    PsError being non-empty. Without that branch a failed run looks exactly like a good one.

    Note: this script is for pasting into Power Automate and is NOT part of the shipped app/ folder.
    It is not covered by the repo's Constrained Language Mode guardrails, though it happens to stay
    within them anyway.
#>

# ---- edit this ----------------------------------------------------------------------------------
$AppPath = "$env:USERPROFILE\meeting2jira"      # the folder containing meeting2jira.cmd
$DaysBack = 1                                    # re-scan yesterday too; re-runs cannot duplicate
$DryRun = $false                                 # $true to preview without creating anything
# -------------------------------------------------------------------------------------------------

$entryPoint = Join-Path $AppPath 'meeting2jira.cmd'
if (-not (Test-Path -LiteralPath $entryPoint)) {
    Write-Output "FAILED: no meeting2jira.cmd under $AppPath. Fix `$AppPath in the flow."
    exit 2
}

$arguments = @('sync', '-DaysBack', "$DaysBack")
if ($DryRun) { $arguments += '-DryRun' }

# cmd.exe is used because the entry point is a batch file. Output is captured so the flow can show
# it; stderr is merged with the preference relaxed, because Windows PowerShell 5.1 treats any stderr
# line from a native command as an error record and would abort on Python's first warning.
$previousEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $output = & cmd.exe '/c' $entryPoint @arguments 2>&1
    $code = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousEap
}

$text = ($output | Out-String).Trim()
# The summary line is the useful part in a run history; the rest is per-meeting detail.
$summary = ($text -split "`n" | Where-Object { $_ -match 'Created |Would create ' } |
    Select-Object -Last 1)
if (-not $summary) { $summary = ($text -split "`n" | Select-Object -Last 1) }

switch ($code) {
    0 { Write-Output "OK: $summary" }
    1 { Write-Output "PARTIAL: some meetings failed. $summary`n$text" }
    2 { Write-Output "CONFIG/CREDENTIAL PROBLEM: run '.\meeting2jira check'.`n$text" }
    130 { Write-Output "INTERRUPTED before finishing." }
    default { Write-Output "UNEXPECTED exit code $code.`n$text" }
}

# Non-zero propagates to the flow so the If/PsError branch fires.
exit $code
