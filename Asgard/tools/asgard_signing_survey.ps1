#Requires -Version 5.1
<#
.SYNOPSIS
    Asgard signing survey: a read-only walk through how this workstation
    decides which programs may run, how the software already here is signed,
    and what that means for signing Asgard's packaged build.
.DESCRIPTION
    Run it as a standard user, on the workstation, in seven steps. Each step
    prints what it is looking at and why it matters before it shows the result.
    Nothing is changed. The two optional switches that run something say so
    below (-TryRun, -Certutil).

    Steps:
      1  This computer and PowerShell (language mode, execution policy, domain)
      2  What enforces code trust: AppLocker rules, App Control for Business,
         Smart App Control
      3  What the policy has blocked, or would block in audit mode, lately
      4  How the software already installed is signed, and by whom
      5  Certificates: which roots and publishers this account trusts, and
         whether you already hold a code-signing certificate
      6  Asgard's own folder, file by file (the unzipped build or the installed
         copy): which files are signed and which are not
      7  What the findings suggest, and the questions to take to IT

    Written to work in Constrained Language Mode (cmdlets only: no Add-Type, no
    COM, no .NET static calls), like asgard_preflight.ps1.

    Writes a console summary plus two files to -OutDir:
        asgard-signing-<COMPUTER>-<yyyyMMdd-HHmm>.md
        asgard-signing-<COMPUTER>-<yyyyMMdd-HHmm>.json
    The reports contain file paths from the event logs and the names of
    certificate issuers. Read them before you send them anywhere.
.PARAMETER AsgardFolder
    The folder to check in step 6: an extracted Asgard zip or the installed
    copy. Defaults to %LOCALAPPDATA%\Asgard\app when it exists.
.PARAMETER SampleFolder
    Extra folders whose programs to sample in step 4, for example the folder an
    approved tool of your agency lives in.
.PARAMETER AgencyHint
    A regular expression. A certificate issuer that matches it is labelled
    "agency/government CA" in step 4. It is only a guess; the full list of
    issuers is always shown so you can judge.
.PARAMETER SamplePerFolder
    Most programs sampled from each folder in step 4. Default 30.
.PARAMETER MaxFiles
    Most files checked in step 6. Default 6000.
.PARAMETER EventDays
    How many days back to read the policy event logs in step 3. Default 30.
.PARAMETER OutDir
    Folder for the reports. Defaults to the current folder.
.PARAMETER Pause
    Wait for Enter after each step, so you can read it.
.PARAMETER TryRun
    In step 6, run asgard-cli.exe --self-test from -AsgardFolder with ASGARD_HOME
    pointing at a temporary folder (deleted afterwards). This is the one place
    the script starts a program of Asgard's, to show whether the policy lets it
    run.
.PARAMETER Certutil
    In step 5, ask certutil which certificate templates your account can see
    (certutil -user -template). It reads from the directory and can be slow.
.PARAMETER ShowCertDetails
    Show certificate subjects and thumbprints. Off by default because a
    subject can contain your name or ID number.
.EXAMPLE
    powershell -NoProfile -File .\asgard_signing_survey.ps1 -Pause
.EXAMPLE
    powershell -NoProfile -File .\asgard_signing_survey.ps1 -AsgardFolder C:\Users\me\Downloads\Asgard-0.4.0-windows-x64 -TryRun
.NOTES
    If PowerShell refuses to run this file, that is itself a result: see the
    note in asgard_preflight.ps1. docs/packaging.md ("Signing") explains what to
    do with the answers.
#>
[CmdletBinding()]
param(
    [string]$AsgardFolder = '',
    [string[]]$SampleFolder = @(),
    [string]$AgencyHint = 'Government|DoD|Federal|\.gov|\.mil|Agency|Department|Enterprise|Internal',
    [int]$SamplePerFolder = 30,
    [int]$MaxFiles = 6000,
    [int]$EventDays = 30,
    [string]$OutDir = '',
    [switch]$Pause,
    [switch]$TryRun,
    [switch]$Certutil,
    [switch]$ShowCertDetails
)
$ErrorActionPreference = 'Continue'
$script:Results = @()
$script:Facts = @{}
if (-not $OutDir) { $OutDir = (Get-Location).Path }
# ---------------------------------------------------------------------------
# Helpers (all CLM-safe)
# ---------------------------------------------------------------------------
function Add-Result {
    param(
        [string]$Step,
        [string]$Check,
        [string]$Status,
        [string]$Detail,
        [string]$Next = ''
    )
    $script:Results += [pscustomobject]@{
        Status = $Status
        Step   = $Step
        Check  = $Check
        Detail = $Detail
        Next   = $Next
    }
    $color = 'Gray'
    switch ($Status) {
        'PASS' { $color = 'Green' }
        'WARN' { $color = 'Yellow' }
        'FAIL' { $color = 'Red' }
        'INFO' { $color = 'Cyan' }
    }
    Write-Host ('[{0}] {1}: {2}' -f $Status, $Check, $Detail) -ForegroundColor $color
    if ($Next) {
        Write-Host ('       next: {0}' -f $Next) -ForegroundColor DarkGray
    }
}
function Write-Step {
    param([int]$Number, [string]$Title, [string[]]$Why)
    Write-Host ''
    Write-Host ('Step {0} of 7: {1}' -f $Number, $Title) -ForegroundColor White
    foreach ($line in $Why) {
        Write-Host ('  ' + $line) -ForegroundColor DarkGray
    }
    Write-Host ''
}
function Wait-Step {
    if ($Pause) {
        Read-Host 'Press Enter to continue' | Out-Null
    }
}
function Show-Table {
    param($Rows, [string[]]$Properties)
    if (-not $Rows -or @($Rows).Count -eq 0) { return }
    $text = ($Rows | Format-Table -Property $Properties -AutoSize -Wrap | Out-String -Width 200)
    foreach ($line in ($text -split "`r?`n")) {
        if ($line.Trim()) { Write-Host ('  ' + $line) }
    }
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
function Format-Cell {
    param([string]$Text)
    if (-not $Text) { return '' }
    return (($Text -replace '\|', '/') -replace "[\r\n]+", ' ')
}
function Get-Cn {
    param([string]$Dn)
    if (-not $Dn) { return '' }
    if ($Dn -match 'CN=("[^"]+"|[^,]+)') { return $Matches[1].Trim('"').Trim() }
    if ($Dn -match 'O=("[^"]+"|[^,]+)') { return $Matches[1].Trim('"').Trim() }
    return $Dn
}
function Get-SigInfo {
    param([string]$Path)
    $s = Get-AuthenticodeSignature -FilePath $Path -ErrorAction SilentlyContinue
    if (-not $s) { return $null }
    $subject = ''; $issuer = ''; $expires = ''
    if ($s.SignerCertificate) {
        $subject = "$($s.SignerCertificate.Subject)"
        $issuer = "$($s.SignerCertificate.Issuer)"
        $expires = Get-Date $s.SignerCertificate.NotAfter -Format 'yyyy-MM-dd'
    }
    $info = [pscustomobject]@{
        Name        = (Split-Path -Leaf $Path)
        Path        = $Path
        Status      = "$($s.Status)"
        Signer      = (Get-Cn $subject)
        Issuer      = (Get-Cn $issuer)
        IssuerFull  = $issuer
        Expires     = $expires
        Timestamped = [bool]$s.TimeStamperCertificate
        Kind        = "$($s.SignatureType)"
        Class       = ''
    }
    if ($info.Status -eq 'NotSigned') {
        $info.Class = 'unsigned'
    } elseif ($info.Status -eq 'Valid') {
        if ($issuer -match 'Microsoft') {
            $info.Class = 'Microsoft'
        } elseif ($issuer -match $AgencyHint) {
            $info.Class = 'agency/government CA'
        } else {
            $info.Class = 'other CA'
        }
    } else {
        $info.Class = 'signed, not trusted (' + $info.Status + ')'
    }
    return $info
}
function Get-RuleValues {
    param([string]$Body, [string]$Pattern)
    $values = @()
    if (-not $Body) { return $values }
    $m = Select-String -InputObject $Body -Pattern $Pattern -AllMatches
    if ($m) {
        foreach ($hit in $m.Matches) { $values += $hit.Groups[1].Value }
    }
    return @($values | Sort-Object -Unique)
}
Write-Host 'Asgard signing survey (read-only)' -ForegroundColor White
Write-Host ('Computer {0}, user {1}, {2}' -f $env:COMPUTERNAME, $env:USERNAME, (Get-Date -Format 'yyyy-MM-dd HH:mm'))
# ---------------------------------------------------------------------------
# Step 1: this computer and PowerShell
# ---------------------------------------------------------------------------
Write-Step 1 'This computer and PowerShell' @(
    'Unsigned scripts run constrained when a code-trust policy is on, and the execution policy',
    'shows whether the agency already expects signed scripts. This script is unsigned, so how',
    'it ran is the first result.'
)
$lang = "$($ExecutionContext.SessionState.LanguageMode)"
$script:Facts['languageMode'] = $lang
$st = 'INFO'
if ($lang -ne 'FullLanguage') { $st = 'WARN' }
Add-Result '1' 'PowerShell language mode' $st ('{0} (PowerShell {1})' -f $lang, $PSVersionTable.PSVersion)
try {
    $eff = "$(Get-ExecutionPolicy)"
    $script:Facts['executionPolicy'] = $eff
    $list = (Get-ExecutionPolicy -List | ForEach-Object { '{0}={1}' -f $_.Scope, $_.ExecutionPolicy }) -join '; '
    $st = 'INFO'
    if ($eff -eq 'AllSigned') { $st = 'WARN' }
    Add-Result '1' 'Execution policy' $st ('{0} ({1})' -f $eff, $list) 'Under AllSigned every script needs a signature from a publisher in TrustedPublisher'
} catch {
    Add-Result '1' 'Execution policy' 'INFO' 'Could not read it'
}
try {
    $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
    $cs = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop
    $script:Facts['domainJoined'] = [bool]$cs.PartOfDomain
    Add-Result '1' 'Windows' 'INFO' ('{0}, build {1}; domain joined: {2}' -f $os.Caption, $os.BuildNumber, $cs.PartOfDomain)
} catch {
    Add-Result '1' 'Windows' 'INFO' 'Could not query Win32_OperatingSystem'
}
Wait-Step
# ---------------------------------------------------------------------------
# Step 2: what enforces code trust
# ---------------------------------------------------------------------------
Write-Step 2 'What enforces code trust here' @(
    'Two systems can decide whether a program or DLL may run. AppLocker has path, publisher and',
    'hash rules. App Control for Business (WDAC) has signer, hash and, with limits, path rules.',
    'Which one is on, and in what mode, decides what signing would buy you.'
)
$svc = Get-Service -Name AppIDSvc -ErrorAction SilentlyContinue
$svcText = 'AppIDSvc not found'
if ($svc) { $svcText = "AppIDSvc $($svc.Status)" }
$script:Facts['appLockerEnforcedExe'] = $false
$script:Facts['appLockerEnforcedDll'] = $false
$script:Facts['appLockerPublishers'] = @()
$script:Facts['appLockerUserPath'] = $false
try {
    $alXml = Get-AppLockerPolicy -Effective -Xml -ErrorAction Stop
    $allPublishers = @()
    $userPaths = @()
    foreach ($type in @('Exe', 'Dll', 'Script', 'Msi', 'Appx')) {
        $mode = 'NotConfigured'
        if ($alXml -match ('<RuleCollection Type="' + $type + '" EnforcementMode="(\w+)"')) { $mode = $Matches[1] }
        $body = ''
        if ($alXml -match ('(?s)<RuleCollection Type="' + $type + '"[^>]*>(.*?)</RuleCollection>')) { $body = $Matches[1] }
        $paths = Get-RuleValues $body '<FilePathCondition Path="([^"]*)"'
        $pubs = Get-RuleValues $body 'PublisherName="([^"]*)"'
        $nHash = 0
        if ($body) {
            $m = Select-String -InputObject $body -Pattern '<FileHashRule ' -AllMatches
            if ($m) { $nHash = $m.Matches.Count }
        }
        $allPublishers += $pubs
        foreach ($p in $paths) {
            if ($p -match '(?i)LOCALAPPDATA|USERPROFILE|\\Users\\|^\*$|^%OSDRIVE%\\\*$') { $userPaths += ('{0}: {1}' -f $type, $p) }
        }
        if ($type -eq 'Exe' -and $mode -eq 'Enabled') { $script:Facts['appLockerEnforcedExe'] = $true }
        if ($type -eq 'Dll' -and $mode -eq 'Enabled') { $script:Facts['appLockerEnforcedDll'] = $true }
        $st = 'INFO'
        if ($mode -eq 'Enabled' -and ($type -eq 'Exe' -or $type -eq 'Dll')) { $st = 'WARN' }
        Add-Result '2' ('AppLocker ' + $type) $st ('{0}: {1} path, {2} publisher, {3} hash rules' -f $mode, $paths.Count, $pubs.Count, $nHash)
    }
    $allPublishers = @($allPublishers | Sort-Object -Unique)
    $script:Facts['appLockerPublishers'] = $allPublishers
    $script:Facts['appLockerUserPath'] = ($userPaths.Count -gt 0)
    if ($allPublishers.Count -gt 0) {
        Write-Host '  Publishers the AppLocker rules name (a program signed by one of these, matching its other conditions, is allowed):' -ForegroundColor DarkGray
        foreach ($p in ($allPublishers | Select-Object -First 15)) { Write-Host ('    ' + $p) }
        if ($allPublishers.Count -gt 15) { Write-Host ('    ... and {0} more (all are in the JSON report)' -f ($allPublishers.Count - 15)) }
    }
    if ($userPaths.Count -gt 0) {
        Write-Host '  Path rules that cover a user-writable location:' -ForegroundColor DarkGray
        foreach ($p in ($userPaths | Select-Object -First 10)) { Write-Host ('    ' + $p) }
    }
} catch {
    Add-Result '2' 'AppLocker policy' 'INFO' ('Could not read the effective policy ({0}); it may be off, or need rights you lack' -f $svcText)
}
$script:Facts['wdacUser'] = -1
$script:Facts['wdacKernel'] = -1
try {
    $dg = Get-CimInstance -Namespace 'root\Microsoft\Windows\DeviceGuard' -ClassName Win32_DeviceGuard -ErrorAction Stop
    $um = [int]$dg.UsermodeCodeIntegrityPolicyEnforcementStatus
    $km = [int]$dg.CodeIntegrityPolicyEnforcementStatus
    $script:Facts['wdacUser'] = $um
    $script:Facts['wdacKernel'] = $km
    $names = @{ 0 = 'off'; 1 = 'audit mode'; 2 = 'enforced' }
    $st = 'INFO'
    if ($um -eq 2) { $st = 'WARN' }
    Add-Result '2' 'App Control for Business (user mode)' $st ("$($names[$um]); kernel mode: $($names[$km])") 'Enforced means unsigned DLLs and programs not named by the policy are blocked'
} catch {
    Add-Result '2' 'App Control for Business' 'INFO' 'Could not query Win32_DeviceGuard'
}
try {
    $cipDir = Join-Path $env:windir 'System32\CodeIntegrity\CiPolicies\Active'
    $cips = @(Get-ChildItem -Path $cipDir -Filter *.cip -ErrorAction Stop)
    $single = Test-Path (Join-Path $env:windir 'System32\CodeIntegrity\SiPolicy.p7b')
    $script:Facts['wdacPolicyFiles'] = $cips.Count
    Add-Result '2' 'App Control policy files' 'INFO' ('{0} multiple-policy file(s) active; single-policy file present: {1}' -f $cips.Count, $single) 'More than one policy file usually means a base policy plus supplemental policies. A supplemental policy is how IT would add Asgard.'
} catch {
    Add-Result '2' 'App Control policy files' 'INFO' 'Could not list the active policy folder (it may need admin rights to read)'
}
try {
    $sac = Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -Name VerifiedAndReputablePolicyState -ErrorAction Stop
    $sacNames = @{ 0 = 'off'; 1 = 'on (enforced)'; 2 = 'evaluation' }
    $v = [int]$sac.VerifiedAndReputablePolicyState
    $script:Facts['smartAppControl'] = $v
    Add-Result '2' 'Smart App Control' 'INFO' ($sacNames[$v]) 'Smart App Control blocks unsigned programs that Microsoft has no reputation for; a valid signature from a trusted CA helps there too'
} catch {
    Add-Result '2' 'Smart App Control' 'INFO' 'Not reported on this computer'
}
Wait-Step
# ---------------------------------------------------------------------------
# Step 3: what the policy has blocked lately
# ---------------------------------------------------------------------------
Write-Step 3 'What the policy blocked, or would block in audit mode, lately' @(
    'The event logs name the exact files the policy refused. In audit mode they show what would',
    'be refused once it is enforced. Run Asgard (or the self-test) first, then this step, and IT',
    'gets the list from you instead of guessing.'
)
$since = (Get-Date).AddDays(-1 * $EventDays)
$logs = @(
    @{ Name = 'Microsoft-Windows-CodeIntegrity/Operational'; Ids = @(3076, 3077); Label = 'App Control'; Audit = 3076 },
    @{ Name = 'Microsoft-Windows-AppLocker/EXE and DLL'; Ids = @(8003, 8004); Label = 'AppLocker exe/dll'; Audit = 8003 },
    @{ Name = 'Microsoft-Windows-AppLocker/MSI and Script'; Ids = @(8006, 8007); Label = 'AppLocker msi/script'; Audit = 8006 }
)
$script:Facts['blockedAsgard'] = 0
$script:Facts['blockedTotal'] = 0
$blockedFiles = @()
foreach ($l in $logs) {
    try {
        $events = @(Get-WinEvent -FilterHashtable @{ LogName = $l.Name; Id = $l.Ids; StartTime = $since } -MaxEvents 500 -ErrorAction Stop)
        $nAudit = @($events | Where-Object { $_.Id -eq $l.Audit }).Count
        $nBlock = $events.Count - $nAudit
        foreach ($e in $events) {
            $msg = "$($e.Message)"
            $first = ($msg -split "`r?`n")[0]
            $file = $first
            if ($msg -match 'attempted to load (.+?) that did not meet') { $file = $Matches[1] }
            elseif ($first -match '^(.+?) was (prevented|allowed)') { $file = $Matches[1] }
            if ($file.Length -gt 200) { $file = $file.Substring(0, 200) }
            $kind = 'blocked'
            if ($e.Id -eq $l.Audit) { $kind = 'would be blocked' }
            $blockedFiles += [pscustomobject]@{ Log = $l.Label; Kind = $kind; File = $file }
        }
        $script:Facts['blockedTotal'] = [int]$script:Facts['blockedTotal'] + $events.Count
        Add-Result '3' $l.Label 'WARN' ('{0} event(s) in {1} day(s): {2} blocked, {3} audit-only' -f $events.Count, $EventDays, $nBlock, $nAudit)
    } catch {
        if ("$($_.FullyQualifiedErrorId)" -match 'NoMatchingEventsFound') {
            Add-Result '3' $l.Label 'PASS' ('No block or audit events in the last {0} day(s)' -f $EventDays)
        } else {
            Add-Result '3' $l.Label 'INFO' 'Could not read this log (it may be missing, empty or need rights)'
        }
    }
}
if ($blockedFiles.Count -gt 0) {
    $asgardHits = @($blockedFiles | Where-Object { $_.File -match '(?i)asgard' })
    $script:Facts['blockedAsgard'] = $asgardHits.Count
    Write-Host ''
    Write-Host '  Files named most often:' -ForegroundColor DarkGray
    $top = $blockedFiles | Group-Object -Property File | Sort-Object -Property Count -Descending | Select-Object -First 10
    foreach ($g in $top) { Write-Host ('    {0,4} x {1}' -f $g.Count, $g.Name) }
    $byExt = $blockedFiles | ForEach-Object {
        if ($_.File -match '\.(\w{2,5})\b[^\\]*$') { $Matches[1].ToLower() } else { '(none)' }
    } | Group-Object | Sort-Object -Property Count -Descending | Select-Object -First 6
    Write-Host '  By file type:' -ForegroundColor DarkGray
    foreach ($g in $byExt) { Write-Host ('    {0,4} x .{1}' -f $g.Count, $g.Name) }
    if ($asgardHits.Count -gt 0) {
        Add-Result '3' 'Asgard files in the logs' 'WARN' ('{0} event(s) name a path containing "asgard"' -f $asgardHits.Count) 'Give IT these file names; they are the ones a rule must cover'
    }
}
$script:BlockedFiles = $blockedFiles
Wait-Step
# ---------------------------------------------------------------------------
# Step 4: how installed software is signed
# ---------------------------------------------------------------------------
Write-Step 4 'How the software already here is signed' @(
    'If the agency runs tools that are signed by its own CA, the same CA can sign Asgard. The',
    'issuer column is the useful one: it is who vouched for the publisher. Programs found under',
    '%LOCALAPPDATA%\Programs are in a user-writable place, so if they run, the policy allows',
    'something in that kind of location.'
)
$targets = @()
foreach ($name in @('python.exe', 'py.exe', 'git.exe', 'node.exe', 'pwsh.exe', 'powershell.exe')) {
    $c = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c -and $c.Source) { $targets += [pscustomobject]@{ Origin = 'on PATH'; Path = $c.Source } }
}
$known = @(
    (Join-Path $env:ProgramFiles 'Microsoft Office\root\Office16\OUTLOOK.EXE'),
    (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Office\root\Office16\OUTLOOK.EXE'),
    (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
    (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
)
foreach ($k in $known) {
    if ($k -and (Test-Path -Path $k)) { $targets += [pscustomobject]@{ Origin = 'known app'; Path = $k } }
}
$roots = @(
    @{ Label = 'Program Files'; Path = $env:ProgramFiles },
    @{ Label = 'Program Files (x86)'; Path = ${env:ProgramFiles(x86)} },
    @{ Label = 'LOCALAPPDATA\Programs'; Path = (Join-Path $env:LOCALAPPDATA 'Programs') }
)
foreach ($extra in $SampleFolder) { $roots += @{ Label = 'sample folder'; Path = $extra } }
foreach ($r in $roots) {
    if (-not $r.Path -or -not (Test-Path -Path $r.Path)) { continue }
    $count = 0
    $vendors = @(Get-ChildItem -Path $r.Path -Directory -ErrorAction SilentlyContinue)
    foreach ($v in $vendors) {
        if ($count -ge $SamplePerFolder) { break }
        $exes = @(Get-ChildItem -Path $v.FullName -Filter *.exe -Recurse -Depth 2 -File -ErrorAction SilentlyContinue | Select-Object -First 2)
        foreach ($x in $exes) {
            $targets += [pscustomobject]@{ Origin = $r.Label; Path = $x.FullName }
            $count++
        }
    }
    if ($vendors.Count -eq 0) {
        foreach ($x in @(Get-ChildItem -Path $r.Path -Filter *.exe -File -ErrorAction SilentlyContinue | Select-Object -First $SamplePerFolder)) {
            $targets += [pscustomobject]@{ Origin = $r.Label; Path = $x.FullName }
        }
    }
}
$targets = @($targets | Sort-Object -Property Path -Unique)
$infos = @()
$i = 0
$total = $targets.Count
if ($total -lt 1) { $total = 1 }
foreach ($t in $targets) {
    $i++
    Write-Progress -Activity 'Reading signatures' -Status $t.Origin -PercentComplete ([int](100 * $i / $total))
    $info = Get-SigInfo $t.Path
    if ($info) {
        $info | Add-Member -NotePropertyName Origin -NotePropertyValue $t.Origin -Force
        $infos += $info
    }
}
Write-Progress -Activity 'Reading signatures' -Completed
$script:Facts['sampled'] = $infos.Count
$agencyIssuers = @()
$script:Facts['agencyIssuers'] = @()
if ($infos.Count -eq 0) {
    Add-Result '4' 'Sampled programs' 'INFO' 'None found to sample'
} else {
    Add-Result '4' 'Sampled programs' 'INFO' ('{0} program(s) read' -f $infos.Count)
    Write-Host ''
    Write-Host '  By who vouches for them:' -ForegroundColor DarkGray
    $byClass = $infos | Group-Object -Property Class | Sort-Object -Property Count -Descending
    foreach ($g in $byClass) { Write-Host ('    {0,4} x {1}' -f $g.Count, $g.Name) }
    $valid = @($infos | Where-Object { $_.Status -eq 'Valid' })
    $nonMs = @($valid | Where-Object { $_.Class -ne 'Microsoft' })
    if ($nonMs.Count -gt 0) {
        Write-Host ''
        Write-Host '  Issuers (the CA behind each publisher) other than Microsoft:' -ForegroundColor DarkGray
        $byIssuer = $nonMs | Group-Object -Property Issuer | Sort-Object -Property Count -Descending
        foreach ($g in ($byIssuer | Select-Object -First 15)) {
            $label = ''
            if (@($g.Group | Where-Object { $_.Class -eq 'agency/government CA' }).Count -gt 0) { $label = '   <- looks like an agency or government CA' }
            Write-Host ('    {0,4} x {1}{2}' -f $g.Count, $g.Name, $label)
        }
        $agencyIssuers = @($nonMs | Where-Object { $_.Class -eq 'agency/government CA' } | Select-Object -ExpandProperty Issuer -Unique)
        Write-Host ''
        Write-Host '  Some of those programs, with their publisher:' -ForegroundColor DarkGray
        Show-Table ($nonMs | Select-Object -First 12) @('Name', 'Signer', 'Issuer', 'Expires', 'Origin')
    }
    $script:Facts['agencyIssuers'] = $agencyIssuers
    $userSide = @($infos | Where-Object { $_.Origin -eq 'LOCALAPPDATA\Programs' })
    if ($userSide.Count -gt 0) {
        Write-Host ''
        Write-Host '  Programs installed in your profile (user-writable):' -ForegroundColor DarkGray
        Show-Table $userSide @('Name', 'Class', 'Signer')
        $unsignedUser = @($userSide | Where-Object { $_.Class -eq 'unsigned' }).Count
        Add-Result '4' 'Programs in your profile' 'INFO' ('{0} found; {1} unsigned' -f $userSide.Count, $unsignedUser) 'Unsigned programs that exist here may or may not be allowed to run; the event log in step 3 tells you'
    }
    $unsigned = @($infos | Where-Object { $_.Class -eq 'unsigned' })
    if ($unsigned.Count -gt 0) {
        Add-Result '4' 'Unsigned programs in the sample' 'INFO' ('{0} of {1}' -f $unsigned.Count, $infos.Count) 'If unsigned programs run here, the policy allows them another way (path or hash)'
    }
    $stamped = @($valid | Where-Object { $_.Timestamped }).Count
    if ($valid.Count -gt 0) {
        Add-Result '4' 'Timestamped signatures' 'INFO' ('{0} of {1} valid signatures carry a timestamp' -f $stamped, $valid.Count) 'A timestamp keeps a signature valid after the certificate expires; sign Asgard with one'
    }
}
Wait-Step
# ---------------------------------------------------------------------------
# Step 5: certificates
# ---------------------------------------------------------------------------
Write-Step 5 'Certificates: what this account trusts, and what it could sign with' @(
    'A signature is only as good as the roots and publishers the computer trusts. Roots your',
    'agency added are listed first. A certificate with the code-signing purpose in your own',
    'store means you may be able to sign without asking for anything.'
)
$script:Facts['codeSigningCerts'] = 0
foreach ($loc in @('LocalMachine', 'CurrentUser')) {
    try {
        $roots = @(Get-ChildItem -Path ('Cert:\' + $loc + '\Root') -ErrorAction Stop)
        $mine = @($roots | Where-Object { "$($_.Subject)" -match $AgencyHint -or "$($_.Issuer)" -match $AgencyHint })
        Add-Result '5' ($loc + ' trusted roots') 'INFO' ('{0} root(s); {1} look like an agency or government CA' -f $roots.Count, $mine.Count)
        foreach ($c in ($mine | Select-Object -First 12)) {
            $line = '    ' + (Get-Cn "$($c.Subject)") + ' (expires ' + (Get-Date $c.NotAfter -Format 'yyyy-MM-dd') + ')'
            if ($ShowCertDetails) { $line += ' ' + $c.Thumbprint }
            Write-Host $line
        }
    } catch {
        Add-Result '5' ($loc + ' trusted roots') 'INFO' 'Could not read this store'
    }
}
foreach ($loc in @('LocalMachine', 'CurrentUser')) {
    try {
        $pubs = @(Get-ChildItem -Path ('Cert:\' + $loc + '\TrustedPublisher') -ErrorAction Stop)
        Add-Result '5' ($loc + ' trusted publishers') 'INFO' ('{0} certificate(s)' -f $pubs.Count) 'These are the publishers whose signed scripts and installers the machine accepts without asking'
        foreach ($c in ($pubs | Select-Object -First 10)) {
            $line = '    ' + (Get-Cn "$($c.Subject)") + ' (issued by ' + (Get-Cn "$($c.Issuer)") + ')'
            if ($ShowCertDetails) { $line += ' ' + $c.Thumbprint }
            Write-Host $line
        }
    } catch {
        Add-Result '5' ($loc + ' trusted publishers') 'INFO' 'Could not read this store'
    }
}
$held = @()
foreach ($loc in @('CurrentUser', 'LocalMachine')) {
    try {
        $held += @(Get-ChildItem -Path ('Cert:\' + $loc + '\My') -CodeSigningCert -ErrorAction Stop)
    } catch {
        Write-Host ('  Could not search {0}\My for code-signing certificates' -f $loc) -ForegroundColor DarkGray
    }
}
$script:Facts['codeSigningCerts'] = $held.Count
if ($held.Count -eq 0) {
    Add-Result '5' 'Code-signing certificates you hold' 'INFO' 'None' 'Ask IT whether the agency has a code-signing service or a certificate template you can enroll in'
} else {
    Add-Result '5' 'Code-signing certificates you hold' 'PASS' ('{0} found' -f $held.Count) 'Whether the policy trusts the issuer is a separate question for IT'
    foreach ($c in $held) {
        $until = Get-Date $c.NotAfter -Format 'yyyy-MM-dd'
        $line = '    issued by {0}, expires {1}, private key here: {2}' -f (Get-Cn "$($c.Issuer)"), $until, $c.HasPrivateKey
        if ($ShowCertDetails) { $line += ', subject ' + $c.Subject + ', ' + $c.Thumbprint }
        Write-Host $line
    }
}
if ($Certutil) {
    Write-Host ''
    Write-Host '  Running certutil -user -template (read-only; it asks the directory and can take a minute) ...' -ForegroundColor DarkGray
    $ct = Invoke-Native 'certutil.exe' @('-user', '-template')
    if ($ct.Output) {
        $names = @($ct.Output -split "`r?`n" | Where-Object { $_ -match '(?i)^\s*(TemplatePropCommonName|Name)\b|code\s*sign' } | Select-Object -First 20)
        $signing = @($names | Where-Object { $_ -match '(?i)code\s*sign' })
        $script:Facts['codeSigningTemplates'] = $signing.Count
        if ($signing.Count -gt 0) {
            Add-Result '5' 'Certificate templates' 'PASS' ('{0} template line(s) mention code signing' -f $signing.Count) 'Ask IT whether you may enroll in it'
            foreach ($n in $signing) { Write-Host ('    ' + $n.Trim()) }
        } else {
            Add-Result '5' 'Certificate templates' 'INFO' 'No code-signing template visible to this account'
        }
    } else {
        Add-Result '5' 'Certificate templates' 'INFO' 'certutil returned nothing (no directory, or the call failed)'
    }
} else {
    Write-Host '  (Add -Certutil to also list certificate templates your account can see.)' -ForegroundColor DarkGray
}
Wait-Step
# ---------------------------------------------------------------------------
# Step 6: Asgard's own folder
# ---------------------------------------------------------------------------
Write-Step 6 'Asgard''s own folder, file by file' @(
    'A publisher rule needs every program and DLL that loads to be signed, not just Asgard.exe.',
    'This counts which files in the build are signed, and which of the unsigned ones are ours',
    'and which come with Python and the packages.'
)
if (-not $AsgardFolder) {
    $candidate = Join-Path $env:LOCALAPPDATA 'Asgard\app'
    if (Test-Path -Path $candidate) { $AsgardFolder = $candidate }
}
$script:Facts['asgardFolder'] = $AsgardFolder
$script:Facts['asgardUnsigned'] = -1
$script:Facts['asgardUnsignedOwn'] = -1
$script:Facts['asgardUserWritable'] = $false
$script:Facts['asgardRunOk'] = $null
if (-not $AsgardFolder -or -not (Test-Path -Path $AsgardFolder)) {
    Add-Result '6' 'Asgard folder' 'INFO' 'No folder to check' 'Run again with -AsgardFolder pointing at the extracted zip, or at %LOCALAPPDATA%\Asgard\app after setup'
} else {
    $AsgardFolder = (Resolve-Path -Path $AsgardFolder).Path
    $script:Facts['asgardFolder'] = $AsgardFolder
    if ($env:LOCALAPPDATA -and $AsgardFolder -like ($env:LOCALAPPDATA + '*')) {
        $script:Facts['asgardUserWritable'] = $true
        Add-Result '6' 'Folder location' 'INFO' ('{0} is under %LOCALAPPDATA%, which you can write to' -f $AsgardFolder) 'App Control for Business path rules usually refuse folders a standard user can write to; confirm with IT'
    }
    $files = @(Get-ChildItem -Path $AsgardFolder -Recurse -File -Include *.exe, *.dll, *.pyd, *.sys -ErrorAction SilentlyContinue | Select-Object -First $MaxFiles)
    $own = @('asgard.exe', 'asgard-cli.exe')
    $rows = @()
    $n = 0
    foreach ($f in $files) {
        $n++
        if (($n % 50) -eq 0) {
            Write-Progress -Activity 'Reading signatures in the Asgard folder' -Status ('{0} of {1}' -f $n, $files.Count) -PercentComplete ([int](100 * $n / $files.Count))
        }
        $info = Get-SigInfo $f.FullName
        if (-not $info) { continue }
        $rel = $f.FullName.Substring($AsgardFolder.Length).TrimStart('\')
        $top = '(root)'
        if ($rel -match '\\') { $top = ($rel -split '\\')[0] }
        $info | Add-Member -NotePropertyName Top -NotePropertyValue $top -Force
        $info | Add-Member -NotePropertyName Ours -NotePropertyValue ($own -contains $f.Name.ToLower()) -Force
        $info | Add-Member -NotePropertyName Ext -NotePropertyValue $f.Extension.ToLower() -Force
        $rows += $info
    }
    Write-Progress -Activity 'Reading signatures in the Asgard folder' -Completed
    if ($rows.Count -eq 0) {
        Add-Result '6' 'Files' 'INFO' 'No .exe, .dll, .pyd or .sys files found there. Is this the build folder?'
    } else {
        $unsignedRows = @($rows | Where-Object { $_.Class -eq 'unsigned' })
        $ownUnsigned = @($unsignedRows | Where-Object { $_.Ours })
        $script:Facts['asgardUnsigned'] = $unsignedRows.Count
        $script:Facts['asgardUnsignedOwn'] = $ownUnsigned.Count
        $st = 'INFO'
        if ($unsignedRows.Count -gt 0) { $st = 'WARN' }
        Add-Result '6' 'Signature coverage' $st ('{0} file(s) checked: {1} unsigned ({2} of them Asgard''s own programs)' -f $rows.Count, $unsignedRows.Count, $ownUnsigned.Count)
        if ($files.Count -ge $MaxFiles) {
            Write-Host ('  Stopped at -MaxFiles {0}; raise it to check the rest.' -f $MaxFiles) -ForegroundColor Yellow
        }
        Write-Host ''
        Write-Host '  By who vouches for them:' -ForegroundColor DarkGray
        foreach ($g in ($rows | Group-Object -Property Class | Sort-Object -Property Count -Descending)) {
            Write-Host ('    {0,5} x {1}' -f $g.Count, $g.Name)
        }
        $signers = @($rows | Where-Object { $_.Status -eq 'Valid' } | Group-Object -Property Signer | Sort-Object -Property Count -Descending | Select-Object -First 10)
        if ($signers.Count -gt 0) {
            Write-Host '  Most common signers:' -ForegroundColor DarkGray
            foreach ($g in $signers) { Write-Host ('    {0,5} x {1}' -f $g.Count, $g.Name) }
        }
        if ($unsignedRows.Count -gt 0) {
            Write-Host '  Unsigned files, by folder:' -ForegroundColor DarkGray
            foreach ($g in ($unsignedRows | Group-Object -Property Top | Sort-Object -Property Count -Descending | Select-Object -First 10)) {
                Write-Host ('    {0,5} x {1}' -f $g.Count, $g.Name)
            }
            Write-Host '  Unsigned files, by type:' -ForegroundColor DarkGray
            foreach ($g in ($unsignedRows | Group-Object -Property Ext | Sort-Object -Property Count -Descending)) {
                Write-Host ('    {0,5} x {1}' -f $g.Count, $g.Name)
            }
        }
        foreach ($o in @($rows | Where-Object { $_.Ours })) {
            Add-Result '6' ('Asgard program ' + $o.Name) 'INFO' ('{0}; signer: {1}' -f $o.Class, $(if ($o.Signer) { $o.Signer } else { '(none)' }))
        }
    }
    if ($TryRun) {
        $cli = Join-Path $AsgardFolder 'asgard-cli.exe'
        if (Test-Path -Path $cli) {
            Write-Host ''
            Write-Host '  Running asgard-cli.exe --self-test with a temporary ASGARD_HOME ...' -ForegroundColor DarkGray
            $tmp = Join-Path $env:TEMP ('asgard-signing-survey-' + (Get-Date -Format 'yyyyMMddHHmmss'))
            New-Item -ItemType Directory -Path $tmp -Force | Out-Null
            $old = $env:ASGARD_HOME
            $env:ASGARD_HOME = $tmp
            try {
                $run = Invoke-Native $cli @('--self-test')
            } finally {
                if ($null -eq $old) { Remove-Item -Path 'Env:\ASGARD_HOME' -ErrorAction SilentlyContinue } else { $env:ASGARD_HOME = $old }
                Remove-Item -Path $tmp -Recurse -Force -ErrorAction SilentlyContinue
            }
            $script:Facts['asgardRunOk'] = $run.Ok
            $tail = (($run.Output -split "`r?`n") | Select-Object -Last 8) -join ' | '
            if ($run.Ok) {
                Add-Result '6' 'Run asgard-cli.exe --self-test' 'PASS' 'Ran and passed from this folder'
            } else {
                Add-Result '6' 'Run asgard-cli.exe --self-test' 'FAIL' ('Exit {0}: {1}' -f $run.Code, $tail) 'A message about group policy or a blocked file is the policy refusing it; then run step 3 again for the file names'
            }
        } else {
            Add-Result '6' 'Run asgard-cli.exe --self-test' 'INFO' 'asgard-cli.exe is not in that folder'
        }
    } else {
        Write-Host '  (Add -TryRun to start asgard-cli.exe --self-test from that folder and see whether the policy lets it run.)' -ForegroundColor DarkGray
    }
}
Wait-Step
# ---------------------------------------------------------------------------
# Step 7: what it suggests
# ---------------------------------------------------------------------------
Write-Step 7 'What this suggests' @(
    'These are readings of the results above, not decisions. IT owns the policy.'
)
$notes = @()
$enforcedAny = ($script:Facts['appLockerEnforcedExe'] -or $script:Facts['appLockerEnforcedDll'] -or [int]$script:Facts['wdacUser'] -eq 2)
if (-not $enforcedAny) {
    $notes += 'No enforcing policy was visible to this account. Either nothing blocks Asgard here, or the policy is out of its sight; the self-test (-TryRun) settles it.'
}
if ([int]$script:Facts['wdacUser'] -eq 2) {
    $notes += 'App Control for Business is enforced. Expect IT to allow Asgard by signer or by hash, probably with a supplemental policy. Path rules for a folder you can write to are usually not honoured.'
}
if ([int]$script:Facts['wdacUser'] -eq 1) {
    $notes += 'App Control is in audit mode: step 3 lists what enforcement would block. That list is the request to IT.'
}
if ($script:Facts['appLockerEnforcedExe'] -or $script:Facts['appLockerEnforcedDll']) {
    if (@($script:Facts['appLockerPublishers']).Count -gt 0) {
        $notes += 'AppLocker has publisher rules. A signature from a publisher already named there, or a new publisher rule for the agency CA, is the cleanest route.'
    } else {
        $notes += 'AppLocker is enforced with no publisher rules, so signing alone does not help; IT would need path or hash rules.'
    }
    if ($script:Facts['appLockerUserPath']) {
        $notes += 'An AppLocker path rule covers part of the user profile. Ask IT whether it covers %LOCALAPPDATA%\Asgard\app.'
    } else {
        $notes += 'No AppLocker path rule covers your profile, so Asgard in %LOCALAPPDATA% needs a publisher or hash rule.'
    }
}
$agencyList = @($script:Facts['agencyIssuers'] | Where-Object { $_ })
if ($agencyList.Count -gt 0) {
    $notes += ('Software here is already signed by: {0}. Ask whether that CA can sign Asgard (an internal code-signing service).' -f ($agencyList -join '; '))
}
if ([int]$script:Facts['codeSigningCerts'] -gt 0) {
    $notes += 'You hold a code-signing certificate, so you could sign the build yourself. Whether the policy trusts its issuer is the question for IT.'
} else {
    $notes += 'You hold no code-signing certificate. If IT signs, the build workflow should produce the unsigned zip and IT signs and regenerates payload.sha256.'
}
if ([int]$script:Facts['asgardUnsigned'] -gt 0) {
    $theirs = [int]$script:Facts['asgardUnsigned'] - [int]$script:Facts['asgardUnsignedOwn']
    $notes += ('{0} file(s) in the build are unsigned, {1} of them third-party. Signing has to cover all of them (re-signing is allowed), or IT allows those by hash.' -f $script:Facts['asgardUnsigned'], $theirs)
}
if ($script:Facts['asgardRunOk'] -eq $true) {
    $notes += 'The self-test ran from the Asgard folder, so the policy already lets this build run here.'
}
if ($script:Facts['asgardRunOk'] -eq $false) {
    $notes += 'The self-test did not run from the Asgard folder. The refusal text in step 6 and the events in step 3 are what to show IT.'
}
foreach ($n in $notes) { Write-Host ('  - ' + $n) }
$questions = @(
    'Does the agency have an internal code-signing service or certificate template, and who may use it?',
    'Is App Control for Business in audit or enforce mode, and is there a process for a supplemental policy?',
    'Is AppLocker also on? Does any path rule cover %LOCALAPPDATA%\Asgard\app?',
    'Would IT rather allow a publisher (the agency CA signs each release) or file hashes (every release changes them)?',
    'Will IT sign the third-party files in the build (Python, Qt, the packages), or allow them by hash?',
    'Who renews the signing certificate, and does the policy keep trusting timestamped signatures after it expires?'
)
Write-Host ''
Write-Host 'Questions for IT' -ForegroundColor White
$q = 0
foreach ($line in $questions) { $q++; Write-Host ('  {0}. {1}' -f $q, $line) }
# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$name = 'asgard-signing-{0}-{1}' -f $env:COMPUTERNAME, $stamp
$jsonPath = Join-Path $OutDir ($name + '.json')
$mdPath = Join-Path $OutDir ($name + '.md')
$report = [pscustomobject]@{
    generated = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
    computer  = $env:COMPUTERNAME
    facts     = $script:Facts
    notes     = $notes
    questions = $questions
    results   = $script:Results
    events    = @($script:BlockedFiles | Select-Object -First 200)
}
$report | ConvertTo-Json -Depth 5 | Out-File -FilePath $jsonPath -Encoding utf8
$md = @()
$md += '# Asgard signing survey'
$md += ''
$md += ('{0} on {1}. Read-only. Contains file paths from the event logs and certificate issuer names; read it before sending it.' -f (Get-Date -Format 'yyyy-MM-dd HH:mm'), $env:COMPUTERNAME)
$md += ''
$md += '## What it suggests'
$md += ''
foreach ($n in $notes) { $md += ('- ' + $n) }
$md += ''
$md += '## Questions for IT'
$md += ''
$q = 0
foreach ($line in $questions) { $q++; $md += ('{0}. {1}' -f $q, $line) }
$md += ''
$md += '## Checks'
$md += ''
$md += '| Status | Step | Check | Detail | Next step |'
$md += '| --- | --- | --- | --- | --- |'
foreach ($r in $script:Results) {
    $md += ('| {0} | {1} | {2} | {3} | {4} |' -f $r.Status, $r.Step, (Format-Cell $r.Check), (Format-Cell $r.Detail), (Format-Cell $r.Next))
}
if ($script:BlockedFiles.Count -gt 0) {
    $md += ''
    $md += '## Files the policy blocked or would block'
    $md += ''
    $md += '| Log | Kind | File |'
    $md += '| --- | --- | --- |'
    foreach ($b in ($script:BlockedFiles | Select-Object -First 100)) {
        $md += ('| {0} | {1} | {2} |' -f (Format-Cell $b.Log), (Format-Cell $b.Kind), (Format-Cell $b.File))
    }
}
($md -join "`r`n") | Out-File -FilePath $mdPath -Encoding utf8
Write-Host ''
Write-Host ('Done: {0} checks.' -f $script:Results.Count) -ForegroundColor White
Write-Host ('Report: {0}' -f $mdPath)
Write-Host ('Data:   {0}' -f $jsonPath)
