<#
.SYNOPSIS
    Read-only preflight: reports what this machine allows and which meeting2jira path to use.

.DESCRIPTION
    Run this first, and run it again whenever something stops working. It changes nothing: no
    files, no registry, no scheduled tasks, no network calls to Jira. Safe under Constrained
    Language Mode, which matters because one of the things it reports is whether you are in it.

    Reached as `.\meeting2jira doctor`.

    What it inspects:
      * PowerShell version and language mode. Constrained Language Mode blocks COM, which decides
        whether the automatic Outlook export (Path A) is available at all.
      * Execution policy, per scope, plus the internet-zone mark on the .ps1 files - the usual
        reason scripts refuse to run after being downloaded.
      * Python: the launcher it would pick, the version, and that ssl/sqlite3/ctypes are present.
        Deliberately skips the Microsoft Store alias, which opens the Store instead of running.
      * Classic Outlook COM registration, and whether "new Outlook" (olk.exe) is running instead.
      * Proxy configuration, including PAC, which Python does not evaluate.
      * Whether the ScheduledTasks cmdlets are available for the optional daily task.
      * Config and stored token presence, and the health of the last sync from last_run.json -
        a failed or stale run is how you find out a hidden scheduled task stopped working.

    It ends with a verdict naming the path to use. It never decides anything itself.

.EXAMPLE
    .\Test-Environment.ps1
.EXAMPLE
    .\Test-Environment.ps1 -Python 'C:\Python312\python.exe'
#>
[CmdletBinding()]
param([string]$Python)

function Write-Check([string]$Status, [string]$Message) {
    $color = @{ OK = 'Green'; WARN = 'Yellow'; FAIL = 'Red'; INFO = 'Gray' }[$Status]
    Write-Host ('[{0,-4}] {1}' -f $Status, $Message) -ForegroundColor $color
}

# Duplicated from Invoke-MeetingSync.ps1 on purpose (dot-sourcing can fail under AppLocker).
function Resolve-Python([string]$Override) {
    if ($Override) { return @{ Exe = $Override; Prefix = @() } }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher) { return @{ Exe = $launcher.Source; Prefix = @('-3') } }
    foreach ($name in @('python.exe', 'python3.exe')) {
        $cmd = Get-Command $name -All -ErrorAction SilentlyContinue |
            Where-Object { $_.Source -notmatch '\\WindowsApps\\' } | Select-Object -First 1
        if ($cmd) { return @{ Exe = $cmd.Source; Prefix = @() } }
    }
    return $null
}

$appRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$srcRoot = Join-Path $appRoot 'src'
Write-Host "`nmeeting2jira environment check`n" -ForegroundColor Cyan

# --- PowerShell -------------------------------------------------------------------------------
$mode = $ExecutionContext.SessionState.LanguageMode
Write-Check INFO "PowerShell $($PSVersionTable.PSVersion) ($($PSVersionTable.PSEdition))"
$comAllowed = ($mode -eq 'FullLanguage')
if ($comAllowed) {
    Write-Check OK 'Language mode: FullLanguage (COM automation allowed)'
} else {
    Write-Check WARN "Language mode: $mode - COM is blocked, so the Outlook COM export can't run"
}

$effective = Get-ExecutionPolicy
$scopes = Get-ExecutionPolicy -List | Where-Object { "$($_.ExecutionPolicy)" -ne 'Undefined' } |
    ForEach-Object { "$($_.Scope)=$($_.ExecutionPolicy)" }
Write-Check INFO "Execution policy: $effective  [$($scopes -join ', ')]"
if ("$effective" -eq 'AllSigned') {
    Write-Check WARN "AllSigned: the .ps1 files must be signed through your organization's code-signing process"
}
$blocked = Get-ChildItem -Path $PSScriptRoot -Filter *.ps1 |
    Where-Object { Get-Item -LiteralPath $_.FullName -Stream Zone.Identifier -ErrorAction SilentlyContinue }
if ($blocked) {
    Write-Check WARN "Downloaded-from-internet mark on: $(($blocked | ForEach-Object { $_.Name }) -join ', '). Under RemoteSigned, run: Get-ChildItem '$appRoot' -Recurse | Unblock-File"
}

# --- Python ---------------------------------------------------------------------------------
$py = Resolve-Python $Python
$pythonOk = $false
if (-not $py) {
    Write-Check FAIL 'Python 3 not found (checked py.exe, python.exe, python3.exe; ignored the Store alias)'
} else {
    $probe = @()
    $probe += $py.Prefix
    $probe += @('-c', 'import sys, ssl, sqlite3, ctypes, json; print(sys.version.split()[0])')
    $out = & $py.Exe @probe 2>&1
    if ($LASTEXITCODE -eq 0) {
        $version = "$($out | Select-Object -First 1)".Trim()
        $parts = $version -split '\.'
        if (([int]$parts[0] -gt 3) -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 8)) {
            Write-Check OK "Python $version at $($py.Exe) (ssl, sqlite3, ctypes present)"
            $pythonOk = $true
        } else {
            Write-Check FAIL "Python $version is too old; need 3.8+"
        }
    } else {
        Write-Check FAIL "Python at $($py.Exe) failed to run: $out"
    }
}
if ($pythonOk) {
    $previousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = $srcRoot
    Push-Location $appRoot
    try {
        $cli = @()
        $cli += $py.Prefix
        $cli += @('-m', 'meeting2jira', '--version')
        $ver = & $py.Exe @cli 2>&1
        if ($LASTEXITCODE -eq 0) { Write-Check OK "Package importable: $ver" } else { Write-Check FAIL "Package import failed: $ver" }
    } finally { $env:PYTHONPATH = $previousPythonPath; Pop-Location }
}

# --- Outlook --------------------------------------------------------------------------------
$outlookCom = Test-Path 'Registry::HKEY_CLASSES_ROOT\Outlook.Application'
if ($outlookCom) {
    Write-Check OK 'Classic Outlook COM (Outlook.Application) is registered'
} else {
    Write-Check WARN 'Outlook.Application is not registered; classic Outlook may not be installed'
}
if (Get-Process -Name olk -ErrorAction SilentlyContinue) {
    Write-Check WARN "'New Outlook' (olk.exe) is running. COM export needs classic Outlook (OUTLOOK.EXE)."
}

# --- Network --------------------------------------------------------------------------------
$inet = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
if ($inet -and $inet.AutoConfigURL) {
    Write-Check WARN "Proxy auto-config (PAC): $($inet.AutoConfigURL). Python ignores PAC; set jira.proxy if Jira needs the proxy."
} elseif ($inet -and $inet.ProxyEnable -eq 1) {
    Write-Check INFO "Static proxy $($inet.ProxyServer) will be used by Python (bypass: $($inet.ProxyOverride))"
} else {
    Write-Check INFO 'No per-user proxy configured'
}

# --- Scheduling & local setup -----------------------------------------------------------------
if (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue) {
    Write-Check OK 'ScheduledTasks cmdlets available (policy may still block task creation)'
} else {
    Write-Check WARN 'ScheduledTasks cmdlets unavailable; run syncs manually'
}
$dataDir = Join-Path $env:LOCALAPPDATA 'meeting2jira'
if (Test-Path (Join-Path $dataDir 'config.json')) { Write-Check OK "Config exists in $dataDir" } else { Write-Check INFO 'No config yet: py -3 -m meeting2jira init' }
if (Test-Path (Join-Path $dataDir 'jira_token.dpapi')) { Write-Check OK 'Jira token stored' } else { Write-Check INFO 'No token yet: py -3 -m meeting2jira set-token' }

# A scheduled task runs hidden, so an expired token or a moved parent issue would otherwise fail
# every day unnoticed. cmd_push leaves this breadcrumb behind on every real run.
$lastRunPath = Join-Path $dataDir 'last_run.json'
if (Test-Path $lastRunPath) {
    $lastRun = Get-Content -LiteralPath $lastRunPath -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
    if ($lastRun) {
        $when = $lastRun.finished_utc
        if ($lastRun.exit_code -eq 0) {
            Write-Check OK "Last sync OK at $when (created $($lastRun.created), skipped $($lastRun.skipped))"
        } else {
            Write-Check FAIL "Last sync FAILED at $when - $($lastRun.first_error)"
        }
        # Both sides must be in the same frame. PowerShell casts an ISO-8601 'Z' string to a LOCAL
        # DateTime (it converts, rather than keeping UTC), so pairing it with .ToUniversalTime()
        # would overstate the age by the UTC offset - 5 to 8 hours here, enough to warn falsely.
        $age = (Get-Date) - [datetime]$when
        if ($age.TotalDays -gt 4) {
            Write-Check WARN "That was $([int]$age.TotalDays) days ago; the scheduled task may not be running."
        }
    }
} else {
    Write-Check INFO 'No sync has run yet (no last_run.json)'
}

# --- Verdict --------------------------------------------------------------------------------
Write-Host ''
if (-not $pythonOk) {
    Write-Host 'Blocked: get Python 3.8+ from your software catalog first.' -ForegroundColor Red
} elseif ($comAllowed -and $outlookCom) {
    Write-Host 'Use Path A (automatic Outlook COM export):  .\powershell\Invoke-MeetingSync.ps1 -DryRun' -ForegroundColor Green
} else {
    Write-Host 'Use Path B (manual Outlook CSV export):  .\powershell\Invoke-MeetingSync.ps1 -Source Csv -CsvPath <file> -DryRun' -ForegroundColor Yellow
}
