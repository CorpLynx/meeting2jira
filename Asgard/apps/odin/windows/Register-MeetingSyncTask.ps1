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
    [string]$TaskName = 'Asgard Odin daily',
    [int]$MinutesAfterTour = 30,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

# The task's name before Odin moved into Asgard. Registering or removing the new one takes it away,
# so the two never both run.
$legacyTask = 'meeting2jira-daily'
if ($TaskName -ne $legacyTask -and (Get-ScheduledTask -TaskName $legacyTask -ErrorAction SilentlyContinue)) {
    Unregister-ScheduledTask -TaskName $legacyTask -Confirm:$false
    Write-Host "Removed the old scheduled task '$legacyTask' (from before Odin moved into Asgard)."
}

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    return
}

$syncScript = Join-Path $PSScriptRoot 'Invoke-MeetingSync.ps1'
$appRoot = Split-Path -Parent $PSScriptRoot

# Odin's files live under Asgard's folder: ASGARD_HOME\odin when set (as in Asgard's own tests),
# else %LOCALAPPDATA%\Asgard\odin. Odin is an Asgard app; its records are in Muninn beside them.
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
# 45 minutes: the first daily run also reads a year of your issues and worklogs from Jira into
# Muninn (later runs read only what changed, in a minute or two).
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 45)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'Odin (Asgard): meetings to Jira sub-tasks, Jira into Muninn, approved Baldur days to Jira' -Force | Out-Null

Write-Host "Registered '$TaskName': weekdays at $At, window = last $DaysBack day(s) through now."
Write-Host "Logs: $(Join-Path $dataDir 'logs')"
Write-Host "Test it now with: Start-ScheduledTask -TaskName '$TaskName'"
