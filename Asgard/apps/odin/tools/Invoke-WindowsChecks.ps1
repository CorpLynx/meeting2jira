<#
.SYNOPSIS
    Runs every Odin check that can only be proven on Windows, and prints a pass/fail summary.

.DESCRIPTION
    Covers what a macOS or Linux checkout cannot exercise: real Windows PowerShell 5.1 parsing, DPAPI
    credential storage, the PowerShell to Python JSON handoff (including 5.1's array-wrapping quirk),
    and the CSV push path end to end against a temporary Muninn.

    It does NOT touch Outlook, Jira, or Task Scheduler, so it is safe to run anywhere: on the
    throwaway AWS lab VM in infra/windows-test-vm, or on the real workstation. The Outlook COM
    export still has to be verified by hand against a live profile.

    Run it under Windows PowerShell 5.1 for the meaningful result:
        powershell.exe -NoProfile -File apps\odin\tools\Invoke-WindowsChecks.ps1   (or: odin selftest)

    Exits 1 if any check fails.

.PARAMETER Python
    Path to a python.exe, if py.exe should not be used.

.PARAMETER SkipUnitTests
    Skip Odin's unit tests; useful when iterating on the Windows-specific probes alone.
#>
[CmdletBinding()]
param(
    [string]$Python,
    [switch]$SkipUnitTests
)

$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot          # apps\odin: tools\ sits in Odin's folder
$asgardRoot = Split-Path -Parent (Split-Path -Parent $appRoot)
$cliPy = Join-Path $appRoot 'cli.py'
$results = New-Object System.Collections.ArrayList
$probeDir = Join-Path $env:TEMP ('odin-checks-' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
New-Item -ItemType Directory -Force -Path $probeDir | Out-Null

function Add-Result([string]$Status, [string]$Name, [string]$Detail) {
    $color = @{ PASS = 'Green'; FAIL = 'Red'; SKIP = 'Yellow'; INFO = 'Gray' }[$Status]
    Write-Host ('[{0,-4}] {1}' -f $Status, $Name) -ForegroundColor $color
    if ($Detail) { Write-Host ('       ' + $Detail) -ForegroundColor DarkGray }
    [void]$results.Add([pscustomobject]@{ Status = $Status; Name = $Name; Detail = $Detail })
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

# Runs a native command and merges stderr into the returned output, without letting
# $ErrorActionPreference = 'Stop' turn a program's routine stderr writes (e.g. unittest's
# verbose "... ok" lines) into a terminating error. PowerShell 5.1 treats every stderr line
# from a native command as a (non-terminating, unless EAP says otherwise) error record; under
# 'Stop' that throws on the first line and the real exit code is never reached.
function Invoke-Native([string]$Exe, [string[]]$NativeArgs) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $Exe @NativeArgs 2>&1
        return @{ Code = $LASTEXITCODE; Output = ($output | Out-String).Trim() }
    } finally {
        $ErrorActionPreference = $previous
    }
}

# Runs a standalone .py probe file that needs `import odin`. python.exe run against a file path
# only puts that file's own directory on sys.path, not the current directory -- so Push-Location
# alone does not make the package importable. Set PYTHONPATH (Odin's folder and Asgard's) for the
# duration of the call instead, and restore whatever was there before.
# Arguments are passed without embedded double quotes, because Windows PowerShell 5.1 mangles
# those when handing them to a native command.
function Invoke-Probe($Py, [string]$ScriptPath, [string[]]$ProbeArgs) {
    $argv = @()
    $argv += $Py.Prefix
    $argv += $ScriptPath
    $argv += $ProbeArgs
    $previousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = "$appRoot;$asgardRoot"
    try {
        return Invoke-Native $Py.Exe $argv
    } finally {
        $env:PYTHONPATH = $previousPythonPath
    }
}

Write-Host "`nOdin Windows checks`n" -ForegroundColor Cyan

# --- Environment banner -------------------------------------------------------------------------
$mode = $ExecutionContext.SessionState.LanguageMode
Add-Result INFO ('PowerShell {0} ({1}), language mode {2}' -f $PSVersionTable.PSVersion, $PSVersionTable.PSEdition, $mode) ''
Add-Result INFO ('OS: ' + [Environment]::OSVersion.VersionString) ''
# The culture matters: the exporter's Outlook Restrict filter formats dates with ToString('g'),
# which follows this culture. A non en-US short format is HANDOFF risk #1.
$culture = Get-Culture
Add-Result INFO ('Culture {0}; short date/time sample: {1}' -f $culture.Name, (Get-Date).ToString('g')) ''
if ($PSVersionTable.PSVersion.Major -ne 5) {
    Add-Result INFO 'Not running under Windows PowerShell 5.1, so the parse check is weaker than the real thing.' ''
}

# --- 1. PowerShell syntax under this engine -----------------------------------------------------
# powershell.exe -File is used (not dot-sourcing) so this behaves like the documented
# invocation and so a stray `exit` inside the syntax checker can't end this script too.
$syntaxScript = Join-Path $PSScriptRoot 'Test-PowerShellSyntax.ps1'
$syntaxResult = Invoke-Native 'powershell.exe' @('-NoProfile', '-File', $syntaxScript)
if ($syntaxResult.Code -eq 0) {
    Add-Result PASS 'PowerShell syntax check' ('{0} engine accepted every .ps1' -f $PSVersionTable.PSVersion)
} else {
    Add-Result FAIL 'PowerShell syntax check' $syntaxResult.Output
}

# --- Python -------------------------------------------------------------------------------------
$py = Resolve-Python $Python
if (-not $py) {
    Add-Result FAIL 'Python discovery' ("no Python that can run Odin (3.9+ with SQLite 3.37+); candidates tried:`n       " + (@($script:OdinPythonAttempts) -join "`n       "))
    Write-Host ''
    Write-Host 'Cannot continue without Python.' -ForegroundColor Red
    exit 1
}
# The packaged build runs only the scripts inside it, so the probes below that are files in %TEMP%
# can't run on it; `asgard-cli.exe --self-test` checks the same modules load instead.
$canProbe = -not $py.Frozen
if ($canProbe) {
    $versionProbe = Join-Path $probeDir 'version.py'
    Set-Content -Path $versionProbe -Encoding ascii -Value @'
import sqlite3, sys
print(sys.version.split()[0] + " with SQLite " + sqlite3.sqlite_version)
'@
    $r = Invoke-Probe $py $versionProbe @()
    Add-Result INFO ('Python {0} at {1}' -f $r.Output, $py.Exe) ''
} else {
    $r = Invoke-Native $py.Exe @('--self-test')
    if ($r.Code -eq 0) {
        Add-Result PASS "Asgard's packaged build (asgard-cli.exe --self-test)" (($r.Output -split "`n" | Select-Object -Last 1) -join '')
    } else {
        Add-Result FAIL "Asgard's packaged build (asgard-cli.exe --self-test)" $r.Output
    }
}

# --- 2. The unittest suite on Windows -----------------------------------------------------------
$testsDir = Join-Path $asgardRoot 'tests'
if ($SkipUnitTests) {
    Add-Result SKIP 'unittest suite' '-SkipUnitTests was passed'
} elseif (-not $canProbe -or -not (Test-Path -LiteralPath (Join-Path $testsDir 'test_odin_pipeline.py'))) {
    Add-Result SKIP 'unittest suite' "this copy of Asgard has no tests folder (a download or the packaged build)"
} else {
    Push-Location $asgardRoot
    try {
        $argv = @()
        $argv += $py.Prefix
        $argv += @('-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_odin_*.py', '-v')
        $testResult = Invoke-Native $py.Exe $argv
    } finally {
        Pop-Location
    }
    $tail = ($testResult.Output -split "`n" | Select-Object -Last 3) -join ' | '
    if ($testResult.Code -eq 0) {
        Add-Result PASS 'unittest suite' $tail
    } else {
        Add-Result FAIL 'unittest suite' $testResult.Output
    }
}

# --- 3. DPAPI round trip ------------------------------------------------------------------------
# HANDOFF risk #3: credstore.py's ctypes argtypes and LocalFree handling have never been executed.
$dpapiProbe = Join-Path $probeDir 'dpapi.py'
Set-Content -Path $dpapiProbe -Encoding ascii -Value @'
import os
import shutil
import sys
import tempfile

# The file must win, not the env var, so clear the override before importing.
os.environ.pop("JIRA_PAT", None)
from odin.credstore import load_token, save_token

secret = "probe-token-\u00e9\u20ac-" + "x" * 200
tmp = tempfile.mkdtemp()
try:
    path = save_token(tmp, secret)
    size = os.path.getsize(path)
    raw = open(path, "rb").read()
    if secret.encode("utf-8") in raw:
        sys.exit("FAIL: the token appears in plaintext inside " + str(path))
    token, where = load_token(tmp)
    if token != secret:
        sys.exit("FAIL: round trip changed the token")
    print("round trip ok, {0} bytes ciphertext, read from {1}".format(size, where))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
'@
if (-not $canProbe) {
    Add-Result SKIP 'DPAPI credential storage (credstore.py)' 'the packaged build runs only its own scripts'
} else {
    $r = Invoke-Probe $py $dpapiProbe @()
    if ($r.Code -eq 0) {
        Add-Result PASS 'DPAPI credential storage (credstore.py)' $r.Output
    } else {
        Add-Result FAIL 'DPAPI credential storage (credstore.py)' $r.Output
    }
}

# --- 4. PowerShell -> Python JSON handoff -------------------------------------------------------
# HANDOFF risk #4: 5.1 can serialize arrays as {"value":[...],"Count":n}. The exporter calls
# Remove-TypeData and Python calls unwrap_ps_array. This builds envelopes exactly the way
# Export-OutlookMeetings.ps1 does, for 0, 1 and 2 meetings, and asks Python to read them back.
$loadProbe = Join-Path $probeDir 'load_export.py'
Set-Content -Path $loadProbe -Encoding ascii -Value @'
import sys

from odin.sources import load_export

path, expected = sys.argv[1], int(sys.argv[2])
meetings = load_export(path)
if len(meetings) != expected:
    sys.exit("FAIL: expected {0} meeting(s), got {1}".format(expected, len(meetings)))
for m in meetings:
    if not m.key or m.minutes <= 0:
        sys.exit("FAIL: malformed meeting " + repr(m.key))
    if expected and m.categories != ["Training"]:
        sys.exit("FAIL: categories came through as " + repr(m.categories))
print("{0} meeting(s) parsed".format(len(meetings)))
'@

$inv = [System.Globalization.CultureInfo]::InvariantCulture
$isoFmt = "yyyy-MM-dd'T'HH:mm:ss'Z'"
$handoffOk = $true
$handoffDetail = @()
foreach ($count in @(0, 1, 2)) {
    $meetings = @()
    for ($i = 0; $i -lt $count; $i++) {
        $start = (Get-Date).ToUniversalTime().Date.AddHours(9 + $i)
        $meetings += [ordered]@{
            key          = ('probe-{0}|{1}' -f $i, $start.ToString($isoFmt, $inv))
            subject      = 'Handoff probe ' + $i
            start_utc    = $start.ToString($isoFmt, $inv)
            end_utc      = $start.AddMinutes(30).ToString($isoFmt, $inv)
            all_day      = $false
            is_meeting   = $true
            is_cancelled = $false
            response     = 'accepted'
            busy_status  = 'busy'
            is_private   = $false
            location     = 'Microsoft Teams Meeting'
            categories   = @('Training')
            organizer    = $null
            is_teams     = $true
        }
    }
    $envelope = [ordered]@{
        schema_version = 1
        source         = 'outlook-com'
        exported_at    = (Get-Date).ToUniversalTime().ToString($isoFmt, $inv)
        meetings       = $meetings
    }
    Remove-TypeData -TypeName System.Array -ErrorAction SilentlyContinue
    $json = ConvertTo-Json -InputObject $envelope -Depth 6
    $exportPath = Join-Path $probeDir ('handoff_{0}.json' -f $count)
    [System.IO.File]::WriteAllText($exportPath, $json, (New-Object System.Text.UTF8Encoding($false)))

    # Record whether this engine wrapped the arrays, because that is the thing worth knowing.
    $wrapped = 'no'
    if ($json -match '"Count"\s*:') { $wrapped = 'YES (unwrap_ps_array is doing real work)' }

    if (-not $canProbe) {
        $handoffDetail += ('{0} item(s): written; array wrapping: {1}' -f $count, $wrapped)
        continue
    }
    $r = Invoke-Probe $py $loadProbe @($exportPath, "$count")
    if ($r.Code -eq 0) {
        $handoffDetail += ('{0} item(s): {1}; array wrapping: {2}' -f $count, $r.Output, $wrapped)
    } else {
        $handoffOk = $false
        $handoffDetail += ('{0} item(s): {1}' -f $count, $r.Output)
    }
}
if ($handoffOk) {
    Add-Result PASS 'PowerShell -> Python export handoff' ($handoffDetail -join "`n       ")
} else {
    Add-Result FAIL 'PowerShell -> Python export handoff' ($handoffDetail -join "`n       ")
}

# --- 5. The CSV push path, end to end, no network ------------------------------------------------
# Exercises the real CLI: config load, CSV parse, filters, routing, templates, and Muninn, made for
# the occasion in a temporary ASGARD_HOME by Asgard itself (--muninn prepare). --dry-run creates
# nothing and needs no token.
$cliDir = Join-Path $probeDir 'cli'
New-Item -ItemType Directory -Force -Path $cliDir | Out-Null
$cfgPath = Join-Path $cliDir 'config.json'
$cfg = [ordered]@{
    jira    = [ordered]@{
        base_url       = 'https://jira.invalid.example'
        default_parent = 'PROBE-1'
        assign_to_me   = $false
    }
    filters = [ordered]@{
        only_ended = $false   # the fixture's dates are fixed, so don't depend on today
    }
}
Remove-TypeData -TypeName System.Array -ErrorAction SilentlyContinue
[System.IO.File]::WriteAllText($cfgPath, (ConvertTo-Json -InputObject $cfg -Depth 6),
    (New-Object System.Text.UTF8Encoding($false)))

$csvFixture = Join-Path $cliDir 'calendar.csv'
Set-Content -Path $csvFixture -Encoding ascii -Value @(
    'Subject,Start Date,Start Time,End Date,End Time,All day event,Meeting Organizer,Required Attendees,Location,Show time as,Private,Categories',
    'Sprint Planning,9/21/2026,10:00:00 AM,9/21/2026,11:00:00 AM,False,Alex Kim,me@agency.gov,Microsoft Teams Meeting,2,False,'
)
$savedHome = $env:ASGARD_HOME
$env:ASGARD_HOME = Join-Path $probeDir 'asgard-home'
Push-Location $appRoot
try {
    $argv = @()
    $argv += $py.Prefix
    $argv += @((Join-Path $asgardRoot 'Asgard.pyw'), '--muninn', 'prepare')
    $prepared = Invoke-Native $py.Exe $argv
    $argv = @()
    $argv += $py.Prefix
    $argv += @($cliPy, 'push', '--config', $cfgPath, '--csv', $csvFixture, '--dry-run')
    $cliResult = Invoke-Native $py.Exe $argv
} finally {
    Pop-Location
    $env:ASGARD_HOME = $savedHome
}
$cliText = $cliResult.Output
if ($cliResult.Code -eq 0 -and $cliText -match 'Would create') {
    Add-Result PASS 'CLI push --csv --dry-run (on a temporary Muninn)' (($cliText -split "`n" | Select-Object -Last 2) -join ' | ')
} else {
    Add-Result FAIL 'CLI push --csv --dry-run (on a temporary Muninn)' ("Muninn: " + $prepared.Output + "`n       " + $cliText)
}

# --- 6. The odin.cmd entry point ----------------------------------------------------------------
# Batch has no parse-only mode, so exercise the paths that need no config or Outlook. `help` and an
# unknown command both go through argument collection, which is where the bugs live.
$entryPoint = Join-Path $appRoot 'odin.cmd'
if (-not (Test-Path -LiteralPath $entryPoint)) {
    Add-Result FAIL 'odin.cmd entry point' 'file not found'
} else {
    $entryOk = $true
    $entryDetail = @()

    $help = Invoke-Native 'cmd.exe' @('/c', $entryPoint, 'help')
    if ($help.Code -ne 0 -or $help.Output -notmatch 'meetings to Jira sub-tasks') {
        $entryOk = $false
        $entryDetail += ('help: exit {0}; {1}' -f $help.Code, $help.Output)
    } else {
        $entryDetail += 'help: usage text printed'
    }

    $bogus = Invoke-Native 'cmd.exe' @('/c', $entryPoint, 'definitely-not-a-command')
    if ($bogus.Output -notmatch 'Unknown command') {
        $entryOk = $false
        $entryDetail += ('unknown command: {0}' -f $bogus.Output)
    } else {
        $entryDetail += 'unknown command: reported, usage shown'
    }

    # `csv` with a missing file must fail before touching config or Outlook, and must not pass
    # the action name through as an argument (the batch shift/%* trap).
    $missing = Invoke-Native 'cmd.exe' @('/c', $entryPoint, 'csv', 'C:\nope\does-not-exist.csv')
    if ($missing.Code -ne 2 -or $missing.Output -notmatch 'no such file') {
        $entryOk = $false
        $entryDetail += ('csv missing file: exit {0}; {1}' -f $missing.Code, $missing.Output)
    } else {
        $entryDetail += 'csv with a missing file: rejected with exit 2'
    }

    if ($entryOk) {
        Add-Result PASS 'odin.cmd entry point' ($entryDetail -join "`n       ")
    } else {
        Add-Result FAIL 'odin.cmd entry point' ($entryDetail -join "`n       ")
    }
}

# --- 7. Test-Environment.ps1 actually runs, including the last-run health block -------------------
# The syntax checker only parses it. This executes it, with a planted last_run.json, because the
# health block does ConvertFrom-Json and DateTime arithmetic that parsing cannot validate.
$doctorScript = Join-Path $appRoot 'windows\Test-Environment.ps1'
# Odin's files live under Asgard's folder: ASGARD_HOME\odin when set (as in Asgard's own tests),
# else %LOCALAPPDATA%\Asgard\odin. Odin is an Asgard app; its records are in Muninn beside them.
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
$lastRunPath = Join-Path $dataDir 'last_run.json'
$savedLastRun = $null
if (Test-Path -LiteralPath $lastRunPath) {
    $savedLastRun = Get-Content -LiteralPath $lastRunPath -Raw
}
try {
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    $doctorDetail = @()
    $doctorOk = $true

    # A recent, successful run: expect OK and no staleness warning.
    $recent = [ordered]@{
        finished_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", $inv)
        exit_code = 0; created = 3; existing = 1; skipped = 2; errors = 0; first_error = $null
    }
    Remove-TypeData -TypeName System.Array -ErrorAction SilentlyContinue
    [System.IO.File]::WriteAllText($lastRunPath, (ConvertTo-Json -InputObject $recent -Depth 4),
        (New-Object System.Text.UTF8Encoding($false)))
    $r = Invoke-Native 'powershell.exe' @('-NoProfile', '-File', $doctorScript)
    if ($r.Output -match 'Last sync OK') {
        $doctorDetail += 'recent successful run: reported OK'
    } else {
        $doctorOk = $false
        $doctorDetail += 'recent run not reported: ' + $r.Output
    }
    # The bug this guards: casting an ISO-8601 'Z' string gives a LOCAL DateTime, so pairing it
    # with .ToUniversalTime() overstates the age by the UTC offset and warns falsely.
    if ($r.Output -match 'days ago') {
        $doctorOk = $false
        $doctorDetail += 'FALSE staleness warning for a run that just happened (timezone mismatch)'
    } else {
        $doctorDetail += 'no false staleness warning (local/UTC frames agree)'
    }

    # A failed run from well in the past: expect FAIL and a staleness warning.
    $stale = [ordered]@{
        finished_utc = (Get-Date).ToUniversalTime().AddDays(-9).ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", $inv)
        exit_code = 1; created = 0; existing = 0; skipped = 0; errors = 2
        first_error = 'PROJ-1: HTTP 401'
    }
    [System.IO.File]::WriteAllText($lastRunPath, (ConvertTo-Json -InputObject $stale -Depth 4),
        (New-Object System.Text.UTF8Encoding($false)))
    $r = Invoke-Native 'powershell.exe' @('-NoProfile', '-File', $doctorScript)
    if ($r.Output -match 'Last sync FAILED' -and $r.Output -match 'HTTP 401') {
        $doctorDetail += 'failed run: reported with its first error'
    } else {
        $doctorOk = $false
        $doctorDetail += 'failed run not surfaced: ' + $r.Output
    }
    if ($r.Output -match '9 days ago' -or $r.Output -match '8 days ago') {
        $doctorDetail += 'stale run: flagged with a plausible age'
    } else {
        $doctorOk = $false
        $doctorDetail += 'stale run age wrong or missing: ' + $r.Output
    }

    if ($doctorOk) {
        Add-Result PASS 'Test-Environment.ps1 runtime + last-run health' ($doctorDetail -join "`n       ")
    } else {
        Add-Result FAIL 'Test-Environment.ps1 runtime + last-run health' ($doctorDetail -join "`n       ")
    }
} finally {
    if ($null -ne $savedLastRun) {
        [System.IO.File]::WriteAllText($lastRunPath, $savedLastRun, (New-Object System.Text.UTF8Encoding($false)))
    } elseif (Test-Path -LiteralPath $lastRunPath) {
        Remove-Item -LiteralPath $lastRunPath -Force -ErrorAction SilentlyContinue
    }
}

# --- 8. Scheduled-task registration and the tour-of-duty start time ------------------------------
# Registers for real, inspects the trigger, then removes it. This is the only way to check that
# -At is derived from tour_of_duty.end, since that path reads the config and does arithmetic.
$taskScript = Join-Path $appRoot 'windows\Register-MeetingSyncTask.ps1'
$probeTask = 'odin-selfcheck-task'
$configPath = Join-Path $dataDir 'config.json'
$savedConfig = $null
if (Test-Path -LiteralPath $configPath) { $savedConfig = Get-Content -LiteralPath $configPath -Raw }
try {
    $probeConfig = [ordered]@{
        jira          = [ordered]@{ base_url = 'https://jira.invalid.example'; default_parent = 'PROBE-1' }
        tour_of_duty  = [ordered]@{ enabled = $true; days = @('Mon', 'Tue', 'Wed', 'Thu', 'Fri')
            start = '07:00'; end = '15:30'; outside_action = 'label' }
    }
    Remove-TypeData -TypeName System.Array -ErrorAction SilentlyContinue
    [System.IO.File]::WriteAllText($configPath, (ConvertTo-Json -InputObject $probeConfig -Depth 6),
        (New-Object System.Text.UTF8Encoding($false)))

    $taskDetail = @()
    $taskOk = $true
    $r = Invoke-Native 'powershell.exe' @('-NoProfile', '-File', $taskScript, '-TaskName', $probeTask)
    # 15:30 + the default 30 minutes = 16:00. This is the part worth asserting: it reads the
    # config, parses the time, and does the arithmetic.
    if ($r.Output -match '16:00') {
        $taskDetail += 'run time derived from tour_of_duty.end 15:30 + 30m = 16:00'
    } else {
        $taskOk = $false
        $taskDetail += 'expected 16:00, got: ' + $r.Output
    }

    $registered = Get-ScheduledTask -TaskName $probeTask -ErrorAction SilentlyContinue
    if ($registered) {
        $trigger = "$($registered.Triggers[0].StartBoundary)"
        if ($trigger -match 'T16:00') {
            $taskDetail += "trigger registered at $trigger"
        } else {
            $taskOk = $false
            $taskDetail += "trigger is $trigger, expected 16:00"
        }
    } elseif ($r.Output -match 'No mapping between account names') {
        # The task runs as the logged-on user (LogonType Interactive), so registering it needs a
        # real interactive account. Under SSM the caller is SYSTEM, whose USERDOMAIN\USERNAME is
        # the machine account and has no interactive SID. Not a defect: registration has to be
        # confirmed on a workstation. See HANDOFF.md section 5.
        $taskDetail += 'registration not attempted here: needs an interactive logon (running as SYSTEM)'
    } else {
        $taskOk = $false
        $taskDetail += 'task did not register: ' + $r.Output
    }

    if ($taskOk) {
        Add-Result PASS 'Register-MeetingSyncTask.ps1 (tour-of-duty start time)' ($taskDetail -join "`n       ")
    } else {
        Add-Result FAIL 'Register-MeetingSyncTask.ps1 (tour-of-duty start time)' ($taskDetail -join "`n       ")
    }
} finally {
    Unregister-ScheduledTask -TaskName $probeTask -Confirm:$false -ErrorAction SilentlyContinue
    if ($null -ne $savedConfig) {
        [System.IO.File]::WriteAllText($configPath, $savedConfig, (New-Object System.Text.UTF8Encoding($false)))
    } elseif (Test-Path -LiteralPath $configPath) {
        Remove-Item -LiteralPath $configPath -Force -ErrorAction SilentlyContinue
    }
}

# --- Summary ------------------------------------------------------------------------------------
$failed = @($results | Where-Object { $_.Status -eq 'FAIL' })
$passed = @($results | Where-Object { $_.Status -eq 'PASS' })
Write-Host ''
Write-Host ('{0} passed, {1} failed. Probe files: {2}' -f $passed.Count, $failed.Count, $probeDir)
Write-Host ''
Write-Host 'Still NOT covered here (needs classic Outlook and a real profile):' -ForegroundColor Yellow
Write-Host '  Export-OutlookMeetings.ps1 - the Restrict locale filter, recurrence expansion,'
Write-Host '  and whether the object-model guard prompts. See HANDOFF.md section 5.'

if ($failed.Count -gt 0) {
    Write-Host ''
    foreach ($f in $failed) { Write-Host ('FAILED: ' + $f.Name) -ForegroundColor Red }
    exit 1
}
