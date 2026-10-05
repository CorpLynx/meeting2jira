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
    Write-Check FAIL 'No working Python 3.8+ found. Every candidate tried is listed below.'
    Write-Check INFO 'Searched PATH (py.exe, python3.exe, python.exe), the registry, and the usual install directories.'
    Write-Check INFO 'If one of these is the Python you expect, the reason it was rejected is the thing to fix.'
    Write-Check INFO 'Escape hatch: pass -Python C:\path\to\python.exe'
} else {
    # Resolve-Python already proved this one runs and is 3.8+, so only the module check is left.
    # ssl, sqlite3 and ctypes are not optional here: Jira calls, dedupe state, and DPAPI need them,
    # and a trimmed-down agency build can be missing them.
    $probe = @()
    $probe += $py.Prefix
    $probe += @('-c', 'import ssl, sqlite3, ctypes, json; print("modules ok")')
    $previousEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $py.Exe @probe 2>&1
        $moduleCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousEap
    }
    if ($moduleCode -eq 0) {
        Write-Check OK "Python $($py.Version) at $($py.Exe) (ssl, sqlite3, ctypes present)"
        $pythonOk = $true
    } else {
        Write-Check FAIL "Python $($py.Version) at $($py.Exe) is missing a required module: $(($out | Out-String).Trim())"
    }
}
# Always show the search trail. When discovery works this explains *which* Python was chosen and why,
# which matters on a machine with several installed; when it fails it is the whole diagnosis.
foreach ($attempt in @($script:M2JPythonAttempts)) {
    Write-Check INFO "  python candidate: $attempt"
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

# An outstanding alert file means a failure was surfaced and not yet resolved. Worth reporting here
# too, because this is where someone looks when they suspect a problem.
$alertName = 'ATTENTION-meeting2jira.txt'
$alertFound = $null
foreach ($base in @($env:OneDrive, $env:OneDriveCommercial, $env:USERPROFILE)) {
    if ($base -and -not $alertFound) {
        $candidate = Join-Path (Join-Path $base 'Desktop') $alertName
        if (Test-Path -LiteralPath $candidate) { $alertFound = $candidate }
    }
}
if (-not $alertFound) {
    $candidate = Join-Path $dataDir $alertName
    if (Test-Path -LiteralPath $candidate) { $alertFound = $candidate }
}
if ($alertFound) {
    Write-Check WARN "An unresolved alert is outstanding: $alertFound"
    Write-Check INFO '  It is removed automatically by the next successful run.'
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
