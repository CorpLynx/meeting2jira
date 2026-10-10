#Requires -Version 5.1
<#
.SYNOPSIS
    Asgard day-one preflight: read-only checks that show which toolset
    column (ideal or least-permission) each Asgard app lands in on this
    machine.

.DESCRIPTION
    Safe to run as a standard user. It only reads settings, except for one
    temporary venv folder under %TEMP% that it creates and deletes to test
    whether venv launchers may run, and one temporary .py file it deletes
    after use.

    Written to work in Constrained Language Mode (cmdlets only: no Add-Type,
    no COM, no .NET method calls), so it behaves the same under AppLocker or
    App Control enforcement.

    Writes a console summary plus two files to -OutDir:
        asgard-preflight-<COMPUTER>-<yyyyMMdd-HHmm>.md    (paste into a ticket)
        asgard-preflight-<COMPUTER>-<yyyyMMdd-HHmm>.json  (for Valkyrie later)

.PARAMETER JiraUrl
    Optional, for example https://jira.agency.gov. Checks reachability, the
    Jira version and whether the personal access token API exists.

.PARAMETER ConfluenceUrl
    Optional. Checks reachability.

.PARAMETER PythonPath
    Optional. Use this python.exe instead of searching for one.

.PARAMETER OutDir
    Folder for the reports. Defaults to the current folder.

.PARAMETER SkipNetwork
    Skip every check that opens a network connection.

.PARAMETER ShowCertThumbprints
    List client-certificate subjects and thumbprints (for curl.exe --cert).
    Off by default because certificate subjects can contain personal
    identifiers.

.EXAMPLE
    powershell -NoProfile -File .\asgard_preflight.ps1

.EXAMPLE
    powershell -NoProfile -File .\asgard_preflight.ps1 -JiraUrl https://jira.agency.gov -ConfluenceUrl https://confluence.agency.gov

.NOTES
    If PowerShell refuses to run this file, that is itself a result: your
    execution policy blocks unsigned scripts. A downloaded copy carries a
    "from the internet" mark; under RemoteSigned, Unblock-File clears it.
    Under AllSigned the script needs a signature from your agency's
    code-signing process. The same checks are listed as manual commands in
    the "Day-one preflight" table of the Asgard toolset evaluation doc.
#>
[CmdletBinding()]
param(
    [string]$JiraUrl = '',
    [string]$ConfluenceUrl = '',
    [string]$PythonPath = '',
    [string]$OutDir = '',
    [switch]$SkipNetwork,
    [switch]$ShowCertThumbprints
)

$ErrorActionPreference = 'Continue'
$script:Results = @()
$script:Verdict = @()
$script:Facts = @{}

# ---------------------------------------------------------------------------
# Helpers (all CLM-safe)
# ---------------------------------------------------------------------------

function Add-Result {
    param(
        [string]$Area,
        [string]$Check,
        [string]$Status,
        [string]$Detail,
        [string]$Affects = '',
        [string]$Next = ''
    )
    $script:Results += [pscustomobject]@{
        Status  = $Status
        Area    = $Area
        Check   = $Check
        Detail  = $Detail
        Affects = $Affects
        Next    = $Next
    }
    $color = 'Gray'
    switch ($Status) {
        'PASS' { $color = 'Green' }
        'WARN' { $color = 'Yellow' }
        'FAIL' { $color = 'Red' }
        'INFO' { $color = 'Cyan' }
    }
    Write-Host ('[{0}] {1} / {2}: {3}' -f $Status, $Area, $Check, $Detail) -ForegroundColor $color
    if ($Next) {
        Write-Host ('       next: {0}' -f $Next) -ForegroundColor DarkGray
    }
}

function Add-Verdict {
    param([string]$App, [string]$Column, [string]$Why)
    $script:Verdict += [pscustomobject]@{ App = $App; Column = $Column; Why = $Why }
}

function Invoke-Native {
    param([string]$FilePath, [string[]]$Arguments = @())
    try {
        $out = (& $FilePath @Arguments 2>&1 | Out-String)
        $code = $LASTEXITCODE
        $text = "$out"
        return [pscustomobject]@{ Ok = ($code -eq 0); Code = $code; Output = $text.Trim() }
    } catch {
        return [pscustomobject]@{ Ok = $false; Code = -1; Output = ("$($_.Exception.Message)").Trim() }
    }
}

function Find-Exe {
    param([string]$Name)
    $c = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.Source }
    return $null
}

function Test-InProfile {
    param([string]$Path)
    if (-not $Path -or -not $env:USERPROFILE) { return $false }
    return $Path -like ($env:USERPROFILE + '*')
}

function Get-FirstLine {
    param([string]$Text)
    $line = ($Text -split "`r?`n" | Where-Object { $_.Trim() -and $_ -notmatch '^HTTPSTATUS ' } | Select-Object -First 1)
    if ($line) { return $line.Trim() }
    return ''
}

function Format-Cell {
    param([string]$Text)
    return (($Text -replace '\|', '/') -replace "`r?`n", ' ')
}

function Invoke-Http {
    param([string]$Curl, [string]$Url)
    $r = Invoke-Native $Curl @('-s', '-S', '--max-time', '20', '-o', '-', '-w', '\nHTTPSTATUS %{http_code}', $Url)
    $code = 0
    if ($r.Output -match 'HTTPSTATUS (\d{3})\s*$') { $code = [int]$Matches[1] }
    $body = $r.Output -replace '(?s)\s*HTTPSTATUS \d{3}\s*$', ''
    return [pscustomobject]@{ Code = $code; Body = $body; Raw = $r.Output }
}

# Python-side checks, written to a temp file and run with the found python.
# Prints one JSON array on stdout.
$PyChecks = @'
import json, sys, importlib.util, subprocess, site

results = []

def add(area, check, status, detail, affects="", next_step=""):
    results.append({"area": area, "check": check, "status": status,
                    "detail": detail, "affects": affects, "next": next_step})

skip_net = "--skip-network" in sys.argv

# tkinter: every Asgard UI
try:
    import tkinter
    add("Python", "tkinter", "PASS", "Tk %s available" % tkinter.TkVersion, "All UIs")
except Exception as exc:
    add("Python", "tkinter", "FAIL", "import failed: %s" % exc, "All UIs",
        "Ask IT to install Python with the Tcl/Tk feature")

# SQLite features Muninn's schema needs
try:
    import sqlite3
    con = sqlite3.connect(":memory:")
    probes = [
        ("FTS5", "create virtual table t_fts using fts5(x)"),
        ("JSON", "select json_valid('{}')"),
        ("STRICT", "create table t_strict (a integer) strict"),
        ("RETURNING", "create table t_ret (a integer)"),
    ]
    have = []
    for name, sql in probes:
        try:
            con.execute(sql)
            if name == "RETURNING":
                con.execute("insert into t_ret values (1) returning a").fetchall()
            have.append(name)
        except Exception:
            pass
    missing = [n for n, _ in probes if n not in have]
    if missing:
        add("Muninn", "SQLite features", "WARN",
            "SQLite %s is missing %s" % (sqlite3.sqlite_version, ", ".join(missing)), "Muninn",
            "Muninn needs SQLite 3.37+ with FTS5 and JSON; ask for Python 3.11 or newer")
    else:
        add("Muninn", "SQLite features", "PASS",
            "SQLite %s with FTS5, JSON, STRICT and RETURNING" % sqlite3.sqlite_version, "Muninn")
except Exception as exc:
    add("Muninn", "SQLite features", "FAIL", "sqlite3 unavailable: %s" % exc, "Muninn")

# pip and its package index
try:
    out = subprocess.run([sys.executable, "-m", "pip", "--version"],
                         capture_output=True, text=True, timeout=120)
    if out.returncode == 0:
        ver = out.stdout.split()[1]
        parts = []
        for piece in ver.split(".")[:2]:
            digits = "".join(ch for ch in piece if ch.isdigit())
            parts.append(int(digits or 0))
        if tuple(parts) >= (24, 2):
            add("Python", "pip", "PASS", "pip %s uses the system certificate store by default" % ver, "Packaging")
        else:
            add("Python", "pip", "WARN", "pip %s predates system-certificate support" % ver, "Packaging",
                "python -m pip install --upgrade pip, or add --use-feature=truststore")
    else:
        add("Python", "pip", "WARN", "pip not available: %s" % out.stderr.strip()[:200], "Packaging",
            "python -m ensurepip --user, or ask IT")
    cfg = subprocess.run([sys.executable, "-m", "pip", "config", "list"],
                         capture_output=True, text=True, timeout=120)
    lines = [line.strip() for line in cfg.stdout.splitlines() if "index-url" in line]
    if lines:
        add("Python", "Package index", "INFO", "; ".join(lines), "Packaging")
    else:
        add("Python", "Package index", "INFO", "No index configured: pip will try pypi.org", "Packaging",
            "If pypi.org is blocked: pip config set global.index-url <agency mirror>")
except Exception as exc:
    add("Python", "pip", "WARN", "could not run pip: %s" % exc, "Packaging")

add("Python", "User site-packages", "INFO",
    "%s (enabled: %s)" % (site.getusersitepackages(), site.ENABLE_USER_SITE), "Packaging",
    "Fallback when venvs are blocked: pip install --user, then run with python -m")

has_truststore = importlib.util.find_spec("truststore") is not None
add("Python", "truststore", "INFO", "installed" if has_truststore else "not installed yet",
    "Odin, Bifrost, Ysildir", "" if has_truststore else "pip install truststore when you set up Asgard")


def probe(ctx):
    """HEAD https://pypi.org through the system proxy, return (status, issuer, proxy)."""
    import http.client, urllib.request, urllib.parse
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("http") or ""
    if proxy:
        u = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        conn = http.client.HTTPSConnection(u.hostname, u.port or 8080, context=ctx, timeout=15)
        conn.set_tunnel("pypi.org", 443)
    else:
        conn = http.client.HTTPSConnection("pypi.org", 443, context=ctx, timeout=15)
    try:
        conn.request("HEAD", "/simple/pip/")
        status = conn.getresponse().status
        cert = conn.sock.getpeercert() or {}
        issuer = {}
        for rdn in cert.get("issuer", ()):
            for key, value in rdn:
                issuer[key] = value
        return status, issuer.get("organizationName") or issuer.get("commonName") or "unknown", proxy
    finally:
        conn.close()


if not skip_net:
    import ssl
    public_cas = ("DigiCert", "GlobalSign", "Let's Encrypt", "Sectigo", "Google Trust",
                  "Amazon", "Entrust", "GoDaddy", "Fastly")
    first_ok = False
    try:
        status, issuer, proxy = probe(ssl.create_default_context())
        first_ok = True
        via = (" via proxy %s" % proxy) if proxy else ""
        if any(name in issuer for name in public_cas):
            add("Network", "TLS to pypi.org", "PASS",
                "HTTP %s, certificate from %s%s; no TLS inspection seen" % (status, issuer, via), "Packaging")
        else:
            add("Network", "TLS to pypi.org", "INFO",
                "HTTP %s, certificate from %s%s: TLS inspection likely; Python trusts it via the system store"
                % (status, issuer, via),
                "Odin, Bifrost, Ysildir", "Inject truststore at startup so requests and httpx trust it too")
    except ssl.SSLCertVerificationError as exc:
        msg = getattr(exc, "verify_message", "") or str(exc)
        hint = "Ask IT whether the inspection CA is in the Windows root store"
        if "critical" in msg.lower() or "key identifier" in msg.lower():
            hint = ("Python 3.13+ applies strict X.509 checks that some inspection CAs fail; "
                    "truststore validates with Windows instead")
        add("Network", "TLS to pypi.org", "FAIL", "certificate rejected: %s" % msg,
            "Packaging, Odin, Bifrost", hint)
    except Exception as exc:
        text = str(exc)
        hint = "Use the agency package mirror; ask IT for its URL"
        if "407" in text:
            hint = "The proxy wants NTLM or Kerberos authentication Python cannot do; use the agency mirror"
        add("Network", "Reach pypi.org", "WARN", "unreachable: %s" % text[:200], "Packaging", hint)

    if first_ok:
        cafile = None
        try:
            import certifi
            cafile = certifi.where()
        except Exception:
            try:
                from pip._vendor import certifi as vendored
                cafile = vendored.where()
            except Exception:
                cafile = None
        if cafile:
            try:
                probe(ssl.create_default_context(cafile=cafile))
                add("Network", "certifi bundle", "PASS",
                    "certifi also trusts the chain; requests works without truststore", "Odin, Bifrost")
            except ssl.SSLCertVerificationError:
                add("Network", "certifi bundle", "WARN",
                    "certifi rejects the chain: requests and httpx fail until truststore.inject_into_ssl() runs",
                    "Odin, Bifrost, Ysildir", "Add truststore and call inject_into_ssl() at startup")
            except Exception:
                pass

print(json.dumps(results))
'@

# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

$TempDir = $env:TEMP
if (-not $TempDir) { $TempDir = $env:TMPDIR }
if (-not $TempDir) { $TempDir = '/tmp' }
if (-not $OutDir) { $OutDir = (Get-Location).Path }

Write-Host ''
Write-Host 'Asgard preflight: read-only checks' -ForegroundColor White
Write-Host ('{0} on {1} as {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm'), $env:COMPUTERNAME, $env:USERNAME)
Write-Host ''

# ---------------------------------------------------------------------------
# System and account
# ---------------------------------------------------------------------------

try {
    $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
    Add-Result 'System' 'Windows' 'INFO' ('{0} (build {1})' -f $os.Caption, $os.BuildNumber)
} catch {
    Add-Result 'System' 'Windows' 'INFO' 'Could not read OS details'
}

$who = Invoke-Native 'whoami.exe' @('/groups', '/fo', 'csv', '/nh')
if ($who.Code -eq -1) {
    Add-Result 'System' 'Account' 'INFO' 'whoami.exe unavailable'
} else {
    $adminLine = ($who.Output -split "`r?`n") | Where-Object { $_ -match 'S-1-5-32-544' } | Select-Object -First 1
    if (-not $adminLine) {
        Add-Result 'System' 'Account' 'INFO' 'Standard user (not a local administrator)'
    } elseif ($adminLine -match 'Enabled group') {
        Add-Result 'System' 'Account' 'WARN' 'Running elevated: results may look better than a normal session' '' 'Re-run from a normal, non-elevated PowerShell window'
    } else {
        Add-Result 'System' 'Account' 'INFO' 'Local administrator, not elevated'
    }
}

# ---------------------------------------------------------------------------
# PowerShell
# ---------------------------------------------------------------------------

$lang = "$($ExecutionContext.SessionState.LanguageMode)"
$psv = "$($PSVersionTable.PSVersion)"
$script:Facts['languageMode'] = $lang
if ($lang -eq 'FullLanguage') {
    Add-Result 'PowerShell' 'Language mode' 'PASS' ('FullLanguage (PowerShell {0})' -f $psv) 'Valkyrie, Valhalla'
} else {
    Add-Result 'PowerShell' 'Language mode' 'WARN' ('{0} (PowerShell {1}): unsigned scripts run constrained' -f $lang, $psv) 'Valkyrie, Valhalla' 'Keep any .ps1 to cmdlets only (no Add-Type, .NET calls or COM), or write it in Python'
}

try {
    $pol = Get-ExecutionPolicy -List
    $eff = "$(Get-ExecutionPolicy)"
    $byGpo = @($pol | Where-Object { ("$($_.Scope)" -eq 'MachinePolicy' -or "$($_.Scope)" -eq 'UserPolicy') -and "$($_.ExecutionPolicy)" -ne 'Undefined' })
    $polText = (@($pol | ForEach-Object { '{0}={1}' -f $_.Scope, $_.ExecutionPolicy }) -join ', ')
    $src = 'set locally'
    if ($byGpo.Count -gt 0) { $src = 'set by Group Policy' }
    $script:Facts['executionPolicy'] = $eff
    if ($eff -eq 'AllSigned') {
        Add-Result 'PowerShell' 'Execution policy' 'WARN' ('AllSigned, {0} ({1})' -f $src, $polText) 'Valkyrie, Valhalla' 'Get Valkyrie and Valhalla scripts signed through your agency process, or keep them in Python'
    } elseif ($eff -eq 'Restricted') {
        Add-Result 'PowerShell' 'Execution policy' 'WARN' ('Restricted, {0}: no scripts run' -f $src) 'Valkyrie, Valhalla' 'Use Python for setup scripts'
    } else {
        Add-Result 'PowerShell' 'Execution policy' 'PASS' ('{0}, {1} ({2})' -f $eff, $src, $polText) 'Valkyrie, Valhalla'
    }
} catch {
    Add-Result 'PowerShell' 'Execution policy' 'INFO' 'Could not read the execution policy'
}

# ---------------------------------------------------------------------------
# AppLocker and App Control (WDAC)
# ---------------------------------------------------------------------------

$svc = Get-Service -Name AppIDSvc -ErrorAction SilentlyContinue
$svcText = 'AppIDSvc not found'
if ($svc) { $svcText = "AppIDSvc $($svc.Status)" }
try {
    $alXml = Get-AppLockerPolicy -Effective -Xml -ErrorAction Stop
    foreach ($type in @('Exe', 'Script', 'Dll', 'Msi', 'Appx')) {
        $mode = 'NotConfigured'
        if ($alXml -match ('<RuleCollection Type="' + $type + '" EnforcementMode="(\w+)"')) { $mode = $Matches[1] }
        $body = ''
        if ($alXml -match ('(?s)<RuleCollection Type="' + $type + '"[^>]*>(.*?)</RuleCollection>')) { $body = $Matches[1] }
        $nPath = 0; $nPub = 0; $nHash = 0
        if ($body) {
            $m = Select-String -InputObject $body -Pattern '<FilePathRule ' -AllMatches
            if ($m) { $nPath = $m.Matches.Count }
            $m = Select-String -InputObject $body -Pattern '<FilePublisherRule ' -AllMatches
            if ($m) { $nPub = $m.Matches.Count }
            $m = Select-String -InputObject $body -Pattern '<FileHashRule ' -AllMatches
            if ($m) { $nHash = $m.Matches.Count }
        }
        $counts = '{0} path, {1} publisher, {2} hash rules; {3}' -f $nPath, $nPub, $nHash, $svcText
        $script:Facts['applocker' + $type] = $mode
        $script:Facts['applocker' + $type + 'Publisher'] = $nPub
        if ($mode -ne 'Enabled') {
            $st = 'PASS'
            if ($mode -eq 'AuditOnly') { $st = 'INFO' }
            Add-Result 'AppLocker' ($type + ' rules') $st ('{0} ({1})' -f $mode, $counts)
        } elseif ($type -eq 'Exe') {
            if ($nPub -eq 0) {
                Add-Result 'AppLocker' 'Exe rules' 'WARN' ('Enforced, no publisher rules ({0}): executables in your profile are blocked' -f $counts) 'Packaging, Heimdall' 'Expect venv launchers, uv and Playwright''s node.exe to be blocked; see the venv check'
            } else {
                Add-Result 'AppLocker' 'Exe rules' 'INFO' ('Enforced ({0}): signed tools may run from your profile' -f $counts) 'Packaging, Heimdall' 'The venv check shows what actually runs'
            }
        } elseif ($type -eq 'Script') {
            Add-Result 'AppLocker' 'Script rules' 'INFO' ('Enforced ({0}): unallowed .ps1 files run constrained; .bat and .cmd outside allowed paths are blocked' -f $counts) 'Valkyrie, Valhalla'
        } elseif ($type -eq 'Dll') {
            Add-Result 'AppLocker' 'DLL rules' 'WARN' ('Enforced ({0}): compiled Python packages (.pyd) in your profile may be blocked' -f $counts) 'All' 'Use pure-Python wheels (py3-none-any)'
        } else {
            Add-Result 'AppLocker' ($type + ' rules') 'INFO' ('Enforced ({0})' -f $counts)
        }
    }
} catch {
    Add-Result 'AppLocker' 'Policy' 'INFO' ('Could not read the effective AppLocker policy ({0})' -f $svcText)
}

try {
    $dg = Get-CimInstance -Namespace 'root\Microsoft\Windows\DeviceGuard' -ClassName Win32_DeviceGuard -ErrorAction Stop
    $um = [int]$dg.UsermodeCodeIntegrityPolicyEnforcementStatus
    $script:Facts['wdacUser'] = $um
    switch ($um) {
        2 { Add-Result 'App Control' 'User-mode policy' 'WARN' 'Enforced: unsigned DLLs, including compiled .pyd files, can be blocked' 'All' 'Keep dependencies pure Python (py3-none-any wheels)' }
        1 { Add-Result 'App Control' 'User-mode policy' 'INFO' 'Audit mode: blocks are logged, not enforced' 'All' }
        default { Add-Result 'App Control' 'User-mode policy' 'PASS' 'Not enforced' 'All' }
    }
} catch {
    Add-Result 'App Control' 'User-mode policy' 'INFO' 'Could not query Win32_DeviceGuard'
}

# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

$PythonExe = $null
$candidates = @()
if ($PythonPath) { $candidates += [pscustomobject]@{ Exe = $PythonPath; Pre = @() } }
$pyLauncher = Find-Exe 'py.exe'
if ($pyLauncher) { $candidates += [pscustomobject]@{ Exe = $pyLauncher; Pre = @('-3') } }
foreach ($c in @(Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue)) {
    $candidates += [pscustomobject]@{ Exe = $c.Source; Pre = @() }
}
foreach ($c in $candidates) {
    $r = Invoke-Native $c.Exe (@($c.Pre) + @('-c', 'import sys; print(sys.executable)'))
    if ($r.Ok -and $r.Output -match '(?m)^(\S.*python[0-9.]*(\.exe)?)\s*$') {
        $PythonExe = $Matches[1].Trim()
        break
    }
}

if (-not $PythonExe) {
    $hint = ''
    if (Get-Command python.exe -ErrorAction SilentlyContinue) { $hint = ' (python.exe exists only as an alias that did not start Python)' }
    Add-Result 'Python' 'Interpreter' 'FAIL' ('No working Python found' + $hint) 'All' 'Request Python 3.11 or newer from the software catalog, with the Tcl/Tk feature'
} else {
    $ver = Invoke-Native $PythonExe @('-c', 'import sys; print(sys.version_info[0], sys.version_info[1], sys.version_info[2])')
    $maj = 0; $min = 0; $pat = 0
    if ($ver.Output -match '(\d+) (\d+) (\d+)') { $maj = [int]$Matches[1]; $min = [int]$Matches[2]; $pat = [int]$Matches[3] }
    $where = 'outside your profile'
    if (Test-InProfile $PythonExe) { $where = 'in your profile (per-user install)' }
    $vtext = '{0}.{1}.{2} at {3}, {4}' -f $maj, $min, $pat, $PythonExe, $where
    if ($maj -lt 3 -or ($maj -eq 3 -and $min -lt 10)) {
        Add-Result 'Python' 'Interpreter' 'WARN' $vtext 'All' 'truststore needs 3.10+; request 3.11 or newer'
    } else {
        Add-Result 'Python' 'Interpreter' 'PASS' $vtext 'All'
    }
    if ($pyLauncher) {
        $list = Invoke-Native $pyLauncher @('-0p')
        if ($list.Ok -and $list.Output) {
            $runtimes = (@($list.Output -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }) -join '; ')
            Add-Result 'Python' 'Installed runtimes' 'INFO' $runtimes
        }
    }

    # venv launcher test: create, run, delete
    $vt = Join-Path $TempDir 'asgard_preflight_venv'
    if (Test-Path $vt) { Remove-Item -Recurse -Force $vt -ErrorAction SilentlyContinue }
    $mk = Invoke-Native $PythonExe @('-m', 'venv', '--without-pip', $vt)
    $vpy = Join-Path (Join-Path $vt 'Scripts') 'python.exe'
    if (-not (Test-Path $vpy)) { $vpy = Join-Path (Join-Path $vt 'bin') 'python' }
    if (-not (Test-Path $vpy)) {
        Add-Result 'Python' 'venv launcher' 'WARN' ('Could not create a venv: {0}' -f (Get-FirstLine $mk.Output)) 'Packaging' 'Use the zipapp (python asgard.pyz) or pip install --user route'
        $script:Facts['venv'] = 'unknown'
    } else {
        $run = Invoke-Native $vpy @('-c', 'print(6*7)')
        if ($run.Ok -and $run.Output -match '42') {
            Add-Result 'Python' 'venv launcher' 'PASS' 'A venv python.exe in your profile runs' 'Packaging'
            $script:Facts['venv'] = 'ok'
        } else {
            Add-Result 'Python' 'venv launcher' 'FAIL' ('Blocked: {0}' -f (Get-FirstLine $run.Output)) 'Packaging' 'Run Asgard as a zipapp (python asgard.pyz), or pip install --user and launch with python -m'
            $script:Facts['venv'] = 'blocked'
        }
    }
    Remove-Item -Recurse -Force $vt -ErrorAction SilentlyContinue

    # library, SQLite and TLS checks inside Python
    $pyFile = Join-Path $TempDir 'asgard_preflight_checks.py'
    Set-Content -Path $pyFile -Value $PyChecks -Encoding Ascii
    $pyArgs = @($pyFile)
    if ($SkipNetwork) { $pyArgs += '--skip-network' }
    $raw = ''
    try { $raw = (& $PythonExe @pyArgs 2>$null | Out-String) } catch { $raw = '' }
    Remove-Item -Force $pyFile -ErrorAction SilentlyContinue
    $parsed = $null
    if ($raw -match '(?s)(\[.*\])') {
        try { $parsed = $Matches[1] | ConvertFrom-Json } catch { $parsed = $null }
    }
    if ($parsed) {
        foreach ($p in @($parsed)) {
            Add-Result $p.area $p.check $p.status $p.detail $p.affects $p.next
            if ($p.check -eq 'SQLite features') { $script:Facts['sqlite'] = $p.status }
        }
    } else {
        Add-Result 'Python' 'Library checks' 'WARN' 'The Python-side checks returned no results' 'All'
    }
}

# ---------------------------------------------------------------------------
# Edge policies (browser automation)
# ---------------------------------------------------------------------------

$edgePaths = @('HKLM:\SOFTWARE\Policies\Microsoft\Edge', 'HKCU:\SOFTWARE\Policies\Microsoft\Edge')
$rda = $null
$rdaFrom = ''
foreach ($k in $edgePaths) {
    if ($null -eq $rda) {
        $v = Get-ItemProperty -Path $k -Name 'RemoteDebuggingAllowed' -ErrorAction SilentlyContinue
        if ($v) { $rda = [int]$v.RemoteDebuggingAllowed; $rdaFrom = $k }
    }
}
if ($rda -eq 0) {
    Add-Result 'Edge' 'RemoteDebuggingAllowed' 'FAIL' ('Disabled by policy ({0}): Playwright, Selenium and Puppeteer cannot drive Edge' -f $rdaFrom) 'Heimdall' 'Use Power Automate for desktop or fill-assist, or request a policy exception'
    $script:Facts['edgeDebug'] = 'blocked'
} elseif ($rda -eq 1) {
    Add-Result 'Edge' 'RemoteDebuggingAllowed' 'PASS' 'Allowed by policy' 'Heimdall'
    $script:Facts['edgeDebug'] = 'allowed'
} else {
    Add-Result 'Edge' 'RemoteDebuggingAllowed' 'PASS' 'Not set by policy (allowed by default)' 'Heimdall'
    $script:Facts['edgeDebug'] = 'allowed'
}

$asc = 0
foreach ($k in $edgePaths) {
    $key = Get-Item -Path ($k + '\AutoSelectCertificateForUrls') -ErrorAction SilentlyContinue
    if ($key) { $asc += @($key.Property).Count }
}
if ($asc -gt 0) {
    Add-Result 'Edge' 'AutoSelectCertificateForUrls' 'INFO' ('{0} entries: Edge picks your certificate automatically on listed sites' -f $asc) 'Heimdall'
} else {
    Add-Result 'Edge' 'AutoSelectCertificateForUrls' 'INFO' 'Not set: expect a certificate prompt on PIV sites' 'Heimdall' 'Ask IT to list the security change URL if Heimdall will drive Edge'
}

$blockAll = $false
foreach ($k in $edgePaths) {
    $key = Get-Item -Path ($k + '\ExtensionInstallBlocklist') -ErrorAction SilentlyContinue
    if ($key) {
        foreach ($n in @($key.Property)) {
            $val = Get-ItemPropertyValue -Path $key.PSPath -Name $n -ErrorAction SilentlyContinue
            if ($val -eq '*') { $blockAll = $true }
        }
    }
}
if ($blockAll) {
    Add-Result 'Edge' 'Extensions' 'INFO' 'All extensions blocked unless allowlisted' 'Heimdall' 'Power Automate''s browser extension needs an allowlist entry'
} else {
    Add-Result 'Edge' 'Extensions' 'INFO' 'No blanket extension block found' 'Heimdall'
}

try {
    $pad = Get-AppxPackage -Name 'Microsoft.PowerAutomateDesktop' -ErrorAction Stop | Select-Object -First 1
    if ($pad) {
        Add-Result 'Automation' 'Power Automate for desktop' 'INFO' ('Installed, version {0}' -f $pad.Version) 'Heimdall, Bifrost'
        $script:Facts['pad'] = 'yes'
    } else {
        Add-Result 'Automation' 'Power Automate for desktop' 'INFO' 'Not installed' 'Heimdall'
        $script:Facts['pad'] = 'no'
    }
} catch {
    Add-Result 'Automation' 'Power Automate for desktop' 'INFO' 'Could not check'
}

# ---------------------------------------------------------------------------
# Tools on PATH
# ---------------------------------------------------------------------------

$tools = @(
    [pscustomobject]@{ Name = 'git';          Exe = 'git.exe';    Args = @('--version'); Affects = 'Baldur, Valkyrie'; Missing = 'WARN'; Next = 'Request Git for Windows from the catalog' },
    [pscustomobject]@{ Name = 'gh';           Exe = 'gh.exe';     Args = @('--version'); Affects = 'Baldur';           Missing = 'INFO'; Next = 'Baldur can use the GitHub REST API with a PAT instead' },
    [pscustomobject]@{ Name = 'curl.exe';     Exe = 'curl.exe';   Args = @('--version'); Affects = 'Odin, Bifrost';    Missing = 'WARN'; Next = 'curl.exe ships with Windows 10 and 11; ask IT why it is missing' },
    [pscustomobject]@{ Name = 'VS Code';      Exe = 'code';       Args = @('--version'); Affects = 'Ysildir';          Missing = 'INFO'; Next = 'Needed for Copilot agent mode and MCP' },
    [pscustomobject]@{ Name = 'winget';       Exe = 'winget.exe'; Args = @('--version'); Affects = 'Valkyrie';         Missing = 'INFO'; Next = 'Valkyrie falls back to catalog requests' },
    [pscustomobject]@{ Name = 'PowerShell 7'; Exe = 'pwsh.exe';   Args = @('--version'); Affects = 'Valkyrie';         Missing = 'INFO'; Next = 'Optional; Windows PowerShell 5.1 is enough' }
)
foreach ($t in $tools) {
    $path = Find-Exe $t.Exe
    if (-not $path) {
        Add-Result 'Tools' $t.Name $t.Missing 'Not found on PATH' $t.Affects $t.Next
        $script:Facts['tool_' + $t.Name] = 'missing'
        continue
    }
    $r = Invoke-Native $path $t.Args
    $first = Get-FirstLine $r.Output
    $note = ''
    if (Test-InProfile $path) { $note = ' (in your profile)' }
    if ($r.Ok) {
        Add-Result 'Tools' $t.Name 'PASS' ('{0} at {1}{2}' -f $first, $path, $note) $t.Affects
        $script:Facts['tool_' + $t.Name] = 'ok'
    } else {
        Add-Result 'Tools' $t.Name 'WARN' ('Found at {0}{1} but it did not run: {2}' -f $path, $note, $first) $t.Affects $t.Next
        $script:Facts['tool_' + $t.Name] = 'blocked'
    }
}

if ($script:Facts['tool_git'] -eq 'ok') {
    $sb = Invoke-Native (Find-Exe 'git.exe') @('config', '--get', 'http.sslBackend')
    $backend = $sb.Output
    if ($backend -eq 'schannel') {
        Add-Result 'Tools' 'git TLS backend' 'PASS' 'http.sslBackend = schannel (trusts the Windows certificate store)' 'Baldur, Valkyrie'
    } else {
        if (-not $backend) { $backend = 'not set (git''s own OpenSSL bundle)' }
        Add-Result 'Tools' 'git TLS backend' 'INFO' ('http.sslBackend = {0}' -f $backend) 'Baldur, Valkyrie' 'git config --global http.sslBackend schannel'
    }
}

# ---------------------------------------------------------------------------
# Where Muninn will live
# ---------------------------------------------------------------------------

$docs = (Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders' -Name 'Personal' -ErrorAction SilentlyContinue).Personal
if ($docs -and $docs -match 'OneDrive') {
    Add-Result 'Storage' 'Documents folder' 'INFO' ('Redirected to OneDrive ({0})' -f $docs) 'Muninn' 'Keep muninn.db in %LOCALAPPDATA%\Asgard, never in Documents or Desktop'
} elseif ($docs) {
    Add-Result 'Storage' 'Documents folder' 'INFO' ('Local ({0})' -f $docs) 'Muninn'
}
if ($env:LOCALAPPDATA) {
    if ($env:LOCALAPPDATA -match 'OneDrive') {
        Add-Result 'Storage' 'LOCALAPPDATA' 'WARN' ('Under OneDrive ({0})' -f $env:LOCALAPPDATA) 'Muninn' 'Pick a folder OneDrive does not sync for muninn.db'
    } else {
        Add-Result 'Storage' 'LOCALAPPDATA' 'PASS' ('Muninn will live in {0}' -f (Join-Path $env:LOCALAPPDATA 'Asgard')) 'Muninn'
    }
}

# ---------------------------------------------------------------------------
# Microsoft 365 cloud (heuristic, from OneDrive's account settings)
# ---------------------------------------------------------------------------

$cloud = 'unknown'
$cloudFrom = ''
$accts = @(Get-ChildItem -Path 'HKCU:\Software\Microsoft\OneDrive\Accounts' -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -like 'Business*' })
foreach ($a in $accts) {
    foreach ($n in @('SPOResourceId', 'ServiceEndpointUri')) {
        if ($cloud -eq 'unknown') {
            $v = Get-ItemPropertyValue -Path $a.PSPath -Name $n -ErrorAction SilentlyContinue
            if ($v) {
                $cloudFrom = "$v"
                if ($cloudFrom -match 'sharepoint-mil\.us') { $cloud = 'DoD' }
                elseif ($cloudFrom -match 'sharepoint\.us') { $cloud = 'GCC High' }
                elseif ($cloudFrom -match 'sharepoint\.com') { $cloud = 'Commercial or GCC' }
            }
        }
    }
}
$script:Facts['cloud'] = $cloud
if ($cloud -eq 'GCC High' -or $cloud -eq 'DoD') {
    Add-Result 'Microsoft 365' 'Cloud' 'INFO' ('{0} (from OneDrive: {1})' -f $cloud, $cloudFrom) 'Loki' 'No Meeting AI Insights API in this cloud: use the transcripts API or the paste path'
} elseif ($cloud -eq 'Commercial or GCC') {
    Add-Result 'Microsoft 365' 'Cloud' 'INFO' ('{0} (from OneDrive: {1})' -f $cloud, $cloudFrom) 'Loki' 'The Meeting AI Insights API may be available; it needs an app registration and a Copilot license'
} else {
    Add-Result 'Microsoft 365' 'Cloud' 'INFO' 'Could not detect from OneDrive settings' 'Loki' 'Check your Outlook on the web address: office365.us or apps.mil means GCC High or DoD'
}

# ---------------------------------------------------------------------------
# VS Code policies (MCP)
# ---------------------------------------------------------------------------

$vsKey = Get-Item -Path 'HKLM:\SOFTWARE\Policies\Microsoft\VSCode' -ErrorAction SilentlyContinue
if ($vsKey) {
    $pairs = @()
    foreach ($n in @($vsKey.Property)) {
        $val = "$(Get-ItemPropertyValue -Path $vsKey.PSPath -Name $n -ErrorAction SilentlyContinue)"
        if ($val.Length -gt 60) { $val = $val.Substring(0, 60) + '...' }
        $pairs += ('{0}={1}' -f $n, $val)
    }
    Add-Result 'VS Code' 'Machine policies' 'INFO' ($pairs -join '; ') 'Ysildir' 'ChatMCP decides which MCP servers VS Code may run'
} else {
    Add-Result 'VS Code' 'Machine policies' 'INFO' 'None found' 'Ysildir' 'Your GitHub org''s "MCP servers in Copilot" policy still applies; check MCP: List Servers in VS Code'
}

# ---------------------------------------------------------------------------
# Client certificates (PIV), for curl.exe --cert
# ---------------------------------------------------------------------------

try {
    $now = Get-Date
    $certs = @(Get-ChildItem -Path 'Cert:\CurrentUser\My' -ErrorAction Stop | Where-Object {
        $_.HasPrivateKey -and $_.NotAfter -gt $now -and (@($_.EnhancedKeyUsageList | Where-Object { $_.ObjectId -eq '1.3.6.1.5.5.7.3.2' }).Count -gt 0)
    })
    if ($certs.Count -eq 0) {
        Add-Result 'Certificates' 'Client authentication' 'INFO' 'No unexpired client-authentication certificates in your personal store' 'Odin, Bifrost' 'If Jira or Confluence require PIV at the proxy, insert your card and re-run'
    } else {
        Add-Result 'Certificates' 'Client authentication' 'INFO' ('{0} unexpired client-authentication certificate(s), likely your PIV' -f $certs.Count) 'Odin, Bifrost' 'curl.exe --cert "CurrentUser\MY\<thumbprint>" uses one without exporting the key'
        if ($ShowCertThumbprints) {
            foreach ($c in $certs) {
                Add-Result 'Certificates' 'Certificate' 'INFO' ('{0}; thumbprint {1}; expires {2}' -f $c.Subject, $c.Thumbprint, (Get-Date $c.NotAfter -Format 'yyyy-MM-dd')) 'Odin, Bifrost'
            }
        }
    }
} catch {
    Add-Result 'Certificates' 'Client authentication' 'INFO' 'Could not read client certificates'
}

# ---------------------------------------------------------------------------
# Jira and Confluence (optional, network)
# ---------------------------------------------------------------------------

$curlExe = Find-Exe 'curl.exe'
if ($JiraUrl -and -not $SkipNetwork) {
    if (-not $curlExe) {
        Add-Result 'Jira' 'Server' 'INFO' 'curl.exe not found; skipped'
    } else {
        $base = $JiraUrl.TrimEnd('/')
        $si = Invoke-Http $curlExe ($base + '/rest/api/2/serverInfo')
        if ($si.Code -eq 200) {
            $info = $null
            try { $info = $si.Body | ConvertFrom-Json } catch { $info = $null }
            if ($info -and $info.version) {
                Add-Result 'Jira' 'Server' 'PASS' ('Jira {0}, deployment {1}' -f $info.version, $info.deploymentType) 'Odin'
            } else {
                Add-Result 'Jira' 'Server' 'PASS' 'Reachable (HTTP 200)' 'Odin'
            }
        } elseif ($si.Code -eq 401 -or $si.Code -eq 403) {
            Add-Result 'Jira' 'Server' 'PASS' ('Reachable; serverInfo needs a login (HTTP {0})' -f $si.Code) 'Odin'
        } else {
            Add-Result 'Jira' 'Server' 'FAIL' ('HTTP {0}: {1}' -f $si.Code, (Get-FirstLine $si.Raw)) 'Odin' 'If the error mentions a certificate, the site may require your PIV certificate; see the certificate row'
        }
        $pat = Invoke-Http $curlExe ($base + '/rest/pat/latest/tokens')
        if ($pat.Code -eq 401) {
            Add-Result 'Jira' 'Personal access tokens' 'PASS' 'PAT API present (HTTP 401 without a login)' 'Odin, Bifrost, Ysildir'
            $script:Facts['jiraPat'] = 'present'
        } elseif ($pat.Code -eq 404) {
            Add-Result 'Jira' 'Personal access tokens' 'WARN' 'No PAT API (HTTP 404): Jira older than 8.14, Jira Cloud, or PATs turned off' 'Odin, Bifrost, Ysildir' 'Use Kerberos (curl.exe --negotiate -u :) or your PIV certificate'
            $script:Facts['jiraPat'] = 'absent'
        } elseif ($pat.Code -eq 0) {
            Add-Result 'Jira' 'Personal access tokens' 'INFO' 'Could not connect to check' 'Odin'
        } else {
            Add-Result 'Jira' 'Personal access tokens' 'INFO' ('PAT API answered HTTP {0}' -f $pat.Code) 'Odin'
        }
    }
}
if ($ConfluenceUrl -and -not $SkipNetwork -and $curlExe) {
    $cb = $ConfluenceUrl.TrimEnd('/')
    $cf = Invoke-Http $curlExe ($cb + '/rest/api/space?limit=1')
    if ($cf.Code -eq 200 -or $cf.Code -eq 401 -or $cf.Code -eq 403) {
        Add-Result 'Confluence' 'Server' 'PASS' ('Reachable (HTTP {0})' -f $cf.Code) 'Bifrost'
        $script:Facts['confluence'] = 'ok'
    } else {
        Add-Result 'Confluence' 'Server' 'FAIL' ('HTTP {0}: {1}' -f $cf.Code, (Get-FirstLine $cf.Raw)) 'Bifrost' 'If the error mentions a certificate, the site may require your PIV certificate'
        $script:Facts['confluence'] = 'fail'
    }
}

# ---------------------------------------------------------------------------
# Verdict: which column each app lands in
# ---------------------------------------------------------------------------

$venv = $script:Facts['venv']
if (-not $PythonExe) { Add-Verdict 'Packaging' 'Blocked' 'No working Python yet' }
elseif ($venv -eq 'ok') { Add-Verdict 'Packaging' 'Ideal' 'venv launchers run: standard install' }
else { Add-Verdict 'Packaging' 'Least-permission' 'venv launchers blocked or untested: ship asgard.pyz' }

if ($script:Facts['wdacUser'] -eq 2 -or $script:Facts['applockerDll'] -eq 'Enabled') {
    Add-Verdict 'Dependencies' 'Least-permission' 'Unsigned DLLs can be blocked: pure-Python wheels only'
} else {
    Add-Verdict 'Dependencies' 'Ideal' 'Compiled wheels can load (still prefer pure Python)'
}

if ($script:Facts['sqlite'] -eq 'PASS') { Add-Verdict 'Muninn' 'Ideal' 'Muninn runs as designed' }
elseif ($script:Facts['sqlite']) { Add-Verdict 'Muninn' 'Blocked' 'Needs SQLite 3.37+ with FTS5' }
else { Add-Verdict 'Muninn' 'Check' 'Python checks did not run' }

if ($script:Facts['tool_VS Code'] -ne 'ok') { Add-Verdict 'Ysildir' 'Least-permission' 'No VS Code: use the clipboard tier' }
else { Add-Verdict 'Ysildir' 'Check' 'Depends on your org''s MCP policy: run MCP: List Servers in VS Code' }

if ($script:Facts['jiraPat'] -eq 'present') { Add-Verdict 'Odin' 'Ideal' 'PAT + REST' }
elseif ($script:Facts['jiraPat'] -eq 'absent') { Add-Verdict 'Odin' 'Least-permission' 'Kerberos or PIV through curl.exe' }
else { Add-Verdict 'Odin' 'Check' 'Re-run with -JiraUrl' }

if ($script:Facts['tool_git'] -ne 'ok') { Add-Verdict 'Baldur' 'Blocked' 'Request Git' }
elseif ($script:Facts['tool_gh'] -eq 'ok') { Add-Verdict 'Baldur' 'Ideal' 'git + gh' }
else { Add-Verdict 'Baldur' 'Least-permission' 'git + GitHub REST with a PAT' }

if ($script:Facts['cloud'] -eq 'GCC High' -or $script:Facts['cloud'] -eq 'DoD') { Add-Verdict 'Loki' 'Least-permission' 'No AI Insights API: transcripts API or paste path' }
elseif ($script:Facts['cloud'] -eq 'Commercial or GCC') { Add-Verdict 'Loki' 'Check' 'AI Insights API possible with an app registration' }
else { Add-Verdict 'Loki' 'Check' 'Cloud not detected' }

Add-Verdict 'Freya' 'Least-permission' 'Clipboard tier works now; the API tier needs an approved endpoint'

if ($script:Facts['edgeDebug'] -eq 'blocked') {
    $padText = 'not installed'
    if ($script:Facts['pad'] -eq 'yes') { $padText = 'installed' }
    Add-Verdict 'Heimdall' 'Least-permission' ('Edge automation blocked: Power Automate for desktop ({0}) or fill-assist' -f $padText)
} elseif ($script:Facts['applockerExe'] -eq 'Enabled' -and $venv -ne 'ok') {
    Add-Verdict 'Heimdall' 'Check' 'Edge allows automation, but Playwright''s node.exe likely needs an allowed install path'
} else {
    Add-Verdict 'Heimdall' 'Ideal' 'Playwright with channel msedge'
}

if ($script:Facts['confluence'] -eq 'ok') { Add-Verdict 'Bifrost' 'Ideal' 'openpyxl + Confluence REST' }
elseif ($script:Facts['confluence'] -eq 'fail') { Add-Verdict 'Bifrost' 'Check' 'Confluence not reachable from this session' }
else { Add-Verdict 'Bifrost' 'Check' 'Re-run with -ConfluenceUrl' }

if ($script:Facts['languageMode'] -ne 'FullLanguage' -or $script:Facts['executionPolicy'] -eq 'AllSigned') {
    Add-Verdict 'Valkyrie, Valhalla' 'Least-permission' 'Python scripts, with cmdlets-only PowerShell where needed'
} else {
    Add-Verdict 'Valkyrie, Valhalla' 'Ideal' 'PowerShell or Python both work'
}

Write-Host ''
Write-Host 'Which column each app lands in' -ForegroundColor White
foreach ($v in $script:Verdict) {
    Write-Host ('  {0,-20} {1,-17} {2}' -f $v.App, $v.Column, $v.Why)
}

# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$name = 'asgard-preflight-{0}-{1}' -f $env:COMPUTERNAME, $stamp
$jsonPath = Join-Path $OutDir ($name + '.json')
$mdPath = Join-Path $OutDir ($name + '.md')

$report = [pscustomobject]@{
    generated = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
    computer  = $env:COMPUTERNAME
    verdict   = $script:Verdict
    results   = $script:Results
}
$report | ConvertTo-Json -Depth 5 | Out-File -FilePath $jsonPath -Encoding utf8

$md = @()
$md += '# Asgard preflight report'
$md += ''
$md += ('{0} on {1}. Read-only checks; the only writes were a temporary venv and .py file, both deleted.' -f (Get-Date -Format 'yyyy-MM-dd HH:mm'), $env:COMPUTERNAME)
$md += ''
$md += '## Which column each app lands in'
$md += ''
$md += '| App | Column | Why |'
$md += '| --- | --- | --- |'
foreach ($v in $script:Verdict) {
    $md += ('| {0} | {1} | {2} |' -f (Format-Cell $v.App), (Format-Cell $v.Column), (Format-Cell $v.Why))
}
$md += ''
$md += '## Checks'
$md += ''
$md += '| Status | Area | Check | Detail | Affects | Next step |'
$md += '| --- | --- | --- | --- | --- | --- |'
foreach ($r in $script:Results) {
    $md += ('| {0} | {1} | {2} | {3} | {4} | {5} |' -f $r.Status, (Format-Cell $r.Area), (Format-Cell $r.Check), (Format-Cell $r.Detail), (Format-Cell $r.Affects), (Format-Cell $r.Next))
}
($md -join "`r`n") | Out-File -FilePath $mdPath -Encoding utf8

$nFail = @($script:Results | Where-Object { $_.Status -eq 'FAIL' }).Count
$nWarn = @($script:Results | Where-Object { $_.Status -eq 'WARN' }).Count
Write-Host ''
Write-Host ('Done: {0} checks, {1} FAIL, {2} WARN.' -f $script:Results.Count, $nFail, $nWarn) -ForegroundColor White
Write-Host ('Report: {0}' -f $mdPath)
Write-Host ('Data:   {0}' -f $jsonPath)
