<#
.SYNOPSIS
    Registers (or removes) a weekday Task Scheduler job that runs Invoke-MeetingSync.ps1 as you.

.DESCRIPTION
    Runs only while you're logged on (Outlook COM needs your interactive session), with limited
    rights. Standard users can normally create tasks that run as themselves; if Group Policy blocks
    task creation you'll get "Access is denied" and should run the sync manually instead.

    The task does not add -ExecutionPolicy Bypass. It relies on your normal execution policy.

.EXAMPLE
    .\Register-MeetingSyncTask.ps1 -At '16:45'
.EXAMPLE
    .\Register-MeetingSyncTask.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$At,
    [ValidateRange(0, 31)][int]$DaysBack = 1,
    [string]$TaskName = 'meeting2jira-daily',
    [int]$MinutesAfterTour = 30,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

$syncScript = Join-Path $PSScriptRoot 'Invoke-MeetingSync.ps1'
$appRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

# Odin's files live under Asgard's folder: ASGARD_HOME\odin when set (as in Asgard's own tests),
# else %LOCALAPPDATA%\Asgard\odin. Odin doesn't need Asgard installed; it only shares the folder.
$asgardDir = if ($env:ASGARD_HOME) { $env:ASGARD_HOME } else { Join-Path $env:LOCALAPPDATA 'Asgard' }
$dataDir = Join-Path $asgardDir 'odin'

# Without -At, run shortly after the tour of duty ends, so the day's meetings have finished and
# `only_ended` does not have to defer them to tomorrow. Anything later than the tour still gets
# picked up by the next run's -DaysBack window; re-runs cannot duplicate.
if (-not $At) {
    $At = '16:45'
    $configPath = Join-Path $dataDir 'config.json'
    if (Test-Path $configPath) {
        $config = Get-Content -LiteralPath $configPath -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
        $tour = $null
        if ($config) { $tour = $config.tour_of_duty }
        if ($tour -and $tour.enabled -and $tour.end) {
            $parts = "$($tour.end)" -split ':'
            if ($parts.Count -eq 2) {
                $endMinutes = ([int]$parts[0] * 60) + [int]$parts[1] + $MinutesAfterTour
                # Keep it on the same day; a tour ending near midnight would otherwise wrap.
                if ($endMinutes -gt 1439) { $endMinutes = 1439 }
                # Integer division without [math]::Floor, which Constrained Language Mode blocks.
                $hour = ($endMinutes - ($endMinutes % 60)) / 60
                $At = '{0:d2}:{1:d2}' -f [int]$hour, ($endMinutes % 60)
                Write-Host "Using $At, which is $MinutesAfterTour minute(s) after your tour of duty ends ($($tour.end))."
            }
        }
    }
}
# DaysBack 1 re-scans yesterday too, catching meetings that ended after yesterday's run.
$argument = "-NoProfile -NonInteractive -WindowStyle Hidden -File `"$syncScript`" -DaysBack $DaysBack"

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argument -WorkingDirectory $appRoot
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'meeting2jira: push ended calendar meetings to Jira sub-tasks' -Force | Out-Null

Write-Host "Registered '$TaskName': weekdays at $At, window = last $DaysBack day(s) through now."
Write-Host "Logs: $(Join-Path $dataDir 'logs')"
Write-Host "Test it now with: Start-ScheduledTask -TaskName '$TaskName'"
