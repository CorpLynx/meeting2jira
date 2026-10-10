<#
.SYNOPSIS
    Read-only preflight: reports what this machine allows and which Odin path to use.

.DESCRIPTION
    Run this first, and run it again whenever something stops working. It changes nothing: no
    files, no registry, no scheduled tasks, no network calls to Jira. Safe under Constrained
    Language Mode, which matters because one of the things it reports is whether you are in it.

    Reached as `odin doctor`.

    What it inspects:
      * PowerShell version and language mode. Constrained Language Mode blocks COM, which decides
        whether the automatic Outlook export (Path A) is available at all.
      * Execution policy, per scope, plus the internet-zone mark on the .ps1 files - the usual
        reason scripts refuse to run after being downloaded.
      * Python: which one Odin would run on (Asgard's packaged build, the Python Asgard was installed
        with, or another 3.9+ with SQLite 3.37+), and that ssl, sqlite3 and ctypes are present.
        Deliberately skips the Microsoft Store alias, which opens the Store instead of running.
      * Muninn, Asgard's database, which Odin keeps its records in: whether Asgard has made it.
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

# Duplicated verbatim in windows/Invoke-MeetingSync.ps1, windows/Test-Environment.ps1 and
# tools/Invoke-WindowsChecks.ps1 on purpose: dot-sourcing can fail across AppLocker trust levels.
# tests/test_odin_guardrails.py asserts the copies stay identical, so change one and change all.
#
# Odin is an Asgard app and runs on what Asgard runs on, in this order:
#   1. -Python, when given;
#   2. the packaged build's own program, asgard-cli.exe two folders above Odin's, which brings
#      Python and everything else with it and runs Odin's scripts as python.exe would;
#   3. the Python Asgard was installed with, from install-ledger.json;
#   4. any other Python 3.9+ whose SQLite is 3.37 or newer with FTS5, which Muninn needs (Windows
#      Python has that from 3.11): the launcher, PATH, the registry, the usual directories.
# Existence is not proof. A real agency install routinely has py.exe present with no 3.x registered,
# a working Python that was never added to PATH, or an old one ahead of a new one. So every real
# Python is *executed* and made to report its version and its SQLite's; the first that works wins.
# $script:OdinPythonAttempts records every candidate tried, which is what makes a failure diagnosable.
function Resolve-Python([string]$Override) {
    $script:OdinPythonAttempts = @()
    $candidates = @()

    if ($Override) { $candidates += @{ Exe = $Override; Prefix = @() } }

    # The packaged build: Odin's scripts are in apps\odin\windows and apps\odin\tools, two folders
    # below the build's programs. It can't run probe code, so it is trusted as it is.
    $asgardRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
    $frozen = Join-Path $asgardRoot 'asgard-cli.exe'
    if (-not $Override -and (Test-Path -LiteralPath $frozen)) {
        $script:OdinPythonAttempts += "ok   $frozen (Asgard's packaged build)"
        return @{ Exe = $frozen; Prefix = @(); Version = 'the packaged build'; Frozen = $true }
    }

    # The Python Asgard was installed with. A packaged install's asgard-cli.exe runs only the scripts
    # in its own folder, so it is no use to a copy of Odin anywhere else.
    $asgardData = if ($env:ASGARD_HOME) { $env:ASGARD_HOME } else { Join-Path $env:LOCALAPPDATA 'Asgard' }
    $ledgerPath = Join-Path $asgardData 'install-ledger.json'
    if (Test-Path -LiteralPath $ledgerPath) {
        $ledger = $null
        try {
            $ledger = Get-Content -LiteralPath $ledgerPath -Raw -ErrorAction Stop | ConvertFrom-Json
        } catch {
            $ledger = $null
        }
        if ($ledger -and $ledger.python -and ("$($ledger.python)" -notmatch 'asgard-cli\.exe$')) {
            $candidates += @{ Exe = "$($ledger.python)"; Prefix = @() }
        }
    }

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

    # Single-quoted so no double quote ever reaches the native command: Windows PowerShell 5.1
    # mangles embedded double quotes in native arguments. Exit 3 marks "runs, but can't run Muninn".
    $probe = 'import sqlite3, sys; con = sqlite3.connect('':memory:''); ' +
        'fts5 = any(''FTS5'' in row[0] for row in con.execute(''pragma compile_options'')); ' +
        'print(sys.version.split()[0] + '' with SQLite '' + sqlite3.sqlite_version); ' +
        'sys.exit(0 if sys.version_info >= (3, 9) and sqlite3.sqlite_version_info >= (3, 37, 0) and fts5 else 3)'
    $seen = @()
    foreach ($candidate in $candidates) {
        if (-not $candidate.Exe) { continue }
        $label = (@($candidate.Exe) + $candidate.Prefix) -join ' '
        if ($seen -contains $label) { continue }
        $seen += $label
        if (-not (Test-Path -LiteralPath $candidate.Exe)) {
            $script:OdinPythonAttempts += "gone $label"
            continue
        }
        $argv = @()
        $argv += $candidate.Prefix
        $argv += @('-c', $probe)
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
            $script:OdinPythonAttempts += "ok   $label -> $reportedVersion"
            return @{ Exe = $candidate.Exe; Prefix = $candidate.Prefix; Version = $reportedVersion; Frozen = $false }
        } elseif ($code -eq 3) {
            $script:OdinPythonAttempts += "old  $label -> $reportedVersion (needs 3.9+ with SQLite 3.37+ and FTS5)"
        } else {
            $script:OdinPythonAttempts += "fail $label -> $reportedVersion"
        }
    }
    return $null
}

$appRoot = Split-Path -Parent $PSScriptRoot
$cliPy = Join-Path $appRoot 'cli.py'
Write-Host "`nOdin environment check`n" -ForegroundColor Cyan

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
    Write-Check FAIL 'No Python that can run Odin found: it needs 3.9+ with SQLite 3.37+ (3.11+ on Windows). Every candidate tried is listed below.'
    Write-Check INFO "Searched Asgard's install record, PATH (py.exe, python3.exe, python.exe), the registry, and the usual install directories."
    Write-Check INFO 'If one of these is the Python you expect, the reason it was rejected is the thing to fix.'
    Write-Check INFO 'Escape hatch: pass -Python C:\path\to\python.exe'
} elseif ($py.Frozen) {
    Write-Check OK "Asgard's packaged build at $($py.Exe) (it brings Python and every module with it)"
    $pythonOk = $true
} else {
    # Resolve-Python already proved this one runs, with a SQLite Muninn can use, so only the module
    # check is left.
    # ssl, sqlite3 and ctypes are not optional here: Jira calls, Muninn, and DPAPI need them,
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
foreach ($attempt in @($script:OdinPythonAttempts)) {
    Write-Check INFO "  python candidate: $attempt"
}
if ($pythonOk) {
    Push-Location $appRoot
    try {
        $cli = @()
        $cli += $py.Prefix
        $cli += @($cliPy, '--version')
        $ver = & $py.Exe @cli 2>&1
        if ($LASTEXITCODE -eq 0) { Write-Check OK "Odin runs: $ver" } else { Write-Check FAIL "Odin didn't start: $ver" }
    } finally { Pop-Location }
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
# Odin's files live under Asgard's folder: ASGARD_HOME\odin when set (as in Asgard's own tests),
# else %LOCALAPPDATA%\Asgard\odin. Odin is an Asgard app; its records are in Muninn beside them.
$asgardDir = if ($env:ASGARD_HOME) { $env:ASGARD_HOME } else { Join-Path $env:LOCALAPPDATA 'Asgard' }
$dataDir = Join-Path $asgardDir 'odin'
$legacyDirs = @((Join-Path $env:LOCALAPPDATA 'odin'), (Join-Path $env:LOCALAPPDATA 'meeting2jira'))
$legacyFound = @($legacyDirs | Where-Object { Test-Path -LiteralPath $_ })
if (-not $env:ASGARD_HOME -and -not (Test-Path -LiteralPath $dataDir) -and $legacyFound.Count -gt 0) {
    if ($legacyFound.Count -gt 1) {
        Write-Check WARN "Odin has two old folders ($($legacyFound -join ' and ')); move the one with the newest state.db to $dataDir yourself"
    } else {
        Write-Check INFO "Odin's files are still in $($legacyFound[0]); the next run moves them to $dataDir"
    }
    $dataDir = $legacyFound[0]
}
if (Test-Path (Join-Path $asgardDir 'muninn.db')) {
    Write-Check OK "Muninn exists in $asgardDir"
} else {
    Write-Check WARN 'Muninn has not been made yet: open Asgard once, and it creates it'
}
if (Test-Path (Join-Path $dataDir 'state.db')) {
    Write-Check INFO "Odin's history from before Muninn (state.db) is there; the next run moves it into Muninn"
}
if (Test-Path (Join-Path $dataDir 'config.json')) { Write-Check OK "Config exists in $dataDir" } else { Write-Check INFO 'No config yet: odin setup' }
if (Test-Path (Join-Path $dataDir 'jira_token.dpapi')) { Write-Check OK 'Jira token stored' } else { Write-Check INFO 'No token yet: odin set-token' }

# A scheduled task runs hidden, so an expired token or a moved parent issue would otherwise fail
# every day unnoticed. Every real Odin run leaves this breadcrumb behind, even one that stopped early.
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
$alertFound = $null
foreach ($alertName in @('ATTENTION-Odin.txt', 'ATTENTION-meeting2jira.txt')) {
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
}
if ($alertFound) {
    Write-Check WARN "An unresolved alert is outstanding: $alertFound"
    Write-Check INFO '  It is removed automatically by the next successful run.'
}

# --- Verdict --------------------------------------------------------------------------------
Write-Host ''
if (-not $pythonOk) {
    Write-Host "Blocked: Odin needs Asgard's packaged build or the Python Asgard runs on (3.11+ on Windows)." -ForegroundColor Red
} elseif ($comAllowed -and $outlookCom) {
    Write-Host 'Use Path A (automatic Outlook COM export):  odin preview' -ForegroundColor Green
} else {
    Write-Host 'Use Path B (manual Outlook CSV export):  odin csv <file> -DryRun' -ForegroundColor Yellow
}
