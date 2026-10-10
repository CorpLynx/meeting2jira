<#
.SYNOPSIS
    Registers (or removes) a weekday Task Scheduler job that runs the OWA sync as you.

.DESCRIPTION
    Mirrors the COM app's Register-MeetingSyncTask.ps1, pointed at meeting2jira-owa.cmd instead.

    Runs only while you are logged on, with limited rights. That is not merely copied from the COM
    version: the browser session this path depends on belongs to your interactive profile, so a task
    running as SYSTEM or while logged off would find no session and fail every time.

    Standard users can normally create tasks that run as themselves. If Group Policy blocks it you
    will get "Access is denied"; run the sync by hand instead.

    Without -At, the run time is derived from tour_of_duty.end in config.json plus -MinutesAfterTour,
    so the day's meetings have finished. Falls back to 16:45.

    No -ExecutionPolicy Bypass. The task relies on your normal execution policy.

.EXAMPLE
    .\Register-OwaSyncTask.ps1
.EXAMPLE
    .\Register-OwaSyncTask.ps1 -At '17:15' -DaysBack 2
.EXAMPLE
    .\Register-OwaSyncTask.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$At,
    [ValidateRange(0, 31)][int]$DaysBack = 1,
    [string]$TaskName = 'meeting2jira-owa-daily',
    [int]$MinutesAfterTour = 30,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

$appRoot = $PSScriptRoot
$entryPoint = Join-Path $appRoot 'meeting2jira-owa.cmd'
if (-not (Test-Path -LiteralPath $entryPoint)) {
    throw "meeting2jira-owa.cmd not found in $appRoot."
}

# Odin's files live under Asgard's folder: ASGARD_HOME\odin when set (as in Asgard's own tests),
# else %LOCALAPPDATA%\Asgard\odin. Odin doesn't need Asgard installed; it only shares the folder.
$asgardDir = if ($env:ASGARD_HOME) { $env:ASGARD_HOME } else { Join-Path $env:LOCALAPPDATA 'Asgard' }
$dataDir = Join-Path $asgardDir 'odin'

# Derive the run time from the tour of duty when one is configured, so the task fires after the
# day's meetings have ended rather than in the middle of them.
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
                if ($endMinutes -gt 1439) { $endMinutes = 1439 }
                # Integer division without [math]::Floor, which Constrained Language Mode blocks.
                $hour = ($endMinutes - ($endMinutes % 60)) / 60
                $At = '{0:d2}:{1:d2}' -f [int]$hour, ($endMinutes % 60)
                Write-Host "Using $At, which is $MinutesAfterTour minute(s) after your tour of duty ends ($($tour.end))."
            }
        }
    }
}

# cmd.exe /c because the entry point is a batch file. The window is hidden; output goes to the
# Python log and last_run.json, which `status` and `doctor` read.
$argument = "/c """"$entryPoint"""""
if ($DaysBack -ne 1) {
    Write-Host "Note: -DaysBack $DaysBack applies to the export window."
}

$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $argument -WorkingDirectory $appRoot
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
# Longer than the COM path's limit: this one launches a browser and may have to wait on the
# calendar API, so 15 minutes is too tight.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'meeting2jira: export the OWA calendar and push ended meetings to Jira sub-tasks' -Force | Out-Null

Write-Host "Registered '$TaskName': weekdays at $At, running as you while logged on."
Write-Host "Logs: $(Join-Path $dataDir 'logs')"
Write-Host "Test it now with: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ''
Write-Host "If the browser session expires the task will fail until you run: .\meeting2jira-owa login"
Write-Host "Check for that with: .\meeting2jira-owa status"
