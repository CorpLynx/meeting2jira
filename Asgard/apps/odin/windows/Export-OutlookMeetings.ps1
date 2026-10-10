<#
.SYNOPSIS
    Exports calendar items from classic Outlook to Odin's JSON export format (schema v1).

.DESCRIPTION
    Uses the Outlook object model (COM) through your already signed-in Outlook profile, so it needs
    no app registration, admin rights, modules, or network auth of its own.

    Requirements: classic Outlook for Windows ("new Outlook" has no COM) and PowerShell in
    FullLanguage mode (Constrained Language Mode blocks COM - use the CSV path instead).

    By default it reads only fields that Outlook's object-model guard does not protect (no body,
    no attendee lists), so it should not trigger the "A program is trying to access e-mail address
    information" prompt. -IncludeOrganizer reads a guarded field and may trigger that prompt.

    This script makes no decisions about what to push. It dumps what's on the calendar, and the
    Python side filters. Writes the JSON file path to the output stream.

.EXAMPLE
    .\Export-OutlookMeetings.ps1
    .\Export-OutlookMeetings.ps1 -Start (Get-Date).Date.AddDays(-7) -End (Get-Date) -OutFile .\week.json
#>
[CmdletBinding()]
param(
    [datetime]$Start = (Get-Date).Date,
    [datetime]$End = (Get-Date).Date.AddDays(1),
    [string]$OutFile,
    [switch]$IncludeOrganizer
)

$ErrorActionPreference = 'Stop'

$mode = $ExecutionContext.SessionState.LanguageMode
if ($mode -ne 'FullLanguage') {
    throw "PowerShell is running in $mode mode, which blocks COM automation. Use the CSV export path instead (README: Path B)."
}
if ($End -le $Start) { throw '-End must be after -Start.' }

$olFolderCalendar = 9
$olAppointmentClass = 26
$responseMap = @{ 0 = 'none'; 1 = 'organizer'; 2 = 'tentative'; 3 = 'accepted'; 4 = 'declined'; 5 = 'not_responded' }
$busyMap     = @{ 0 = 'free'; 1 = 'tentative'; 2 = 'busy'; 3 = 'oof'; 4 = 'elsewhere' }
$cancelledStatuses = @(5, 7)   # olMeetingCanceled, olMeetingReceivedAndCanceled
$inv = [System.Globalization.CultureInfo]::InvariantCulture
$isoFmt = "yyyy-MM-dd'T'HH:mm:ss'Z'"

try {
    $outlook = New-Object -ComObject Outlook.Application
} catch {
    throw "Could not start classic Outlook via COM ($($_.Exception.Message)). 'New Outlook' has no COM interface; switch back to classic Outlook or use the CSV path."
}

$calendar = $outlook.GetNamespace('MAPI').GetDefaultFolder($olFolderCalendar)
$items = $calendar.Items
$items.Sort('[Start]')              # must sort BEFORE enabling recurrences
$items.IncludeRecurrences = $true   # expands recurring series into individual occurrences

# Jet filters compare dates using the machine's regional short date/time format; 'g' matches it.
# Overlap test: starts before the window ends AND ends after the window starts.
$filter = "[Start] < '{0}' AND [End] > '{1}'" -f $End.ToString('g'), $Start.ToString('g')
Write-Verbose "Outlook Restrict filter: $filter"
$restricted = $items.Restrict($filter)

$meetings = @()
$guard = 0
# With IncludeRecurrences, Count is meaningless; walk with GetFirst/GetNext.
$item = $restricted.GetFirst()
while ($null -ne $item -and $guard -lt 5000) {
    $guard++
    if ($item.Class -eq $olAppointmentClass) {
        if ($item.Start -ge $End) { break }

        $startUtc = $item.StartUTC.ToString($isoFmt, $inv)
        $endUtc   = $item.EndUTC.ToString($isoFmt, $inv)
        $location = [string]$item.Location
        $categories = @()
        if ($item.Categories) {
            $categories = @($item.Categories -split '[,;]' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        }
        $organizer = $null
        if ($IncludeOrganizer) { $organizer = [string]$item.Organizer }

        $meetings += [ordered]@{
            key          = "$($item.GlobalAppointmentID)|$startUtc"
            subject      = [string]$item.Subject
            start_utc    = $startUtc
            end_utc      = $endUtc
            all_day      = [bool]$item.AllDayEvent
            is_meeting   = ([int]$item.MeetingStatus -ne 0)
            is_cancelled = ($cancelledStatuses -contains [int]$item.MeetingStatus)
            response     = $(if ($responseMap.ContainsKey([int]$item.ResponseStatus)) { $responseMap[[int]$item.ResponseStatus] } else { 'unknown' })
            busy_status  = $(if ($busyMap.ContainsKey([int]$item.BusyStatus)) { $busyMap[[int]$item.BusyStatus] } else { 'unknown' })
            is_private   = ([int]$item.Sensitivity -ge 2)   # olPrivate or olConfidential
            location     = $location
            categories   = $categories
            organizer    = $organizer
            is_teams     = [bool]($location -match 'Microsoft Teams')
            # The meeting's own id and whether it belongs to a series, so Odin can tell a one-off
            # meeting that moved from a new one. Neither is a guarded property.
            global_id    = [string]$item.GlobalAppointmentID
            is_recurring = [bool]$item.IsRecurring
        }
    }
    $item = $restricted.GetNext()
}
$truncated = ($guard -ge 5000)
if ($truncated) { Write-Warning 'Stopped after 5000 items; narrow the date range.' }

if (-not $OutFile) {
    # Odin's folder, under Asgard's: ASGARD_HOME\odin when set, else %LOCALAPPDATA%\Asgard\odin.
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
    $exportDir = Join-Path $dataDir 'exports'
    New-Item -ItemType Directory -Force -Path $exportDir | Out-Null
    $OutFile = Join-Path $exportDir ('outlook_{0}.json' -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
}

$envelope = [ordered]@{
    schema_version = 1
    source         = 'outlook-com'
    exported_at    = (Get-Date).ToUniversalTime().ToString($isoFmt, $inv)
    range_start    = $Start.ToUniversalTime().ToString($isoFmt, $inv)
    range_end      = $End.ToUniversalTime().ToString($isoFmt, $inv)
    # An export that stopped early didn't read its whole range, so Odin mustn't take an item it
    # lacks as deleted from the calendar.
    truncated      = $truncated
    meetings       = $meetings
}
# Windows PowerShell 5.1's System.Array type data can turn arrays into {"value":..,"Count":..}.
Remove-TypeData -TypeName System.Array -ErrorAction SilentlyContinue
$json = ConvertTo-Json -InputObject $envelope -Depth 6
[System.IO.File]::WriteAllText($OutFile, $json, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Exported $($meetings.Count) calendar item(s) from $($Start.ToString('g')) to $($End.ToString('g')) -> $OutFile"
Write-Output $OutFile
