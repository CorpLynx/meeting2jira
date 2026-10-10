<#
.SYNOPSIS
    Parse-checks every .ps1 in Odin's windows\ and tools\, and flags PowerShell 7-only syntax.

.DESCRIPTION
    The scripts must run on Windows PowerShell 5.1. Running this under 5.1 is the real test, because
    the 5.1 parser rejects 7-only syntax outright. Under pwsh 7 it also walks the AST for ternaries,
    pipeline chains (&&, ||), null-coalescing (??, ??=), and null-conditional access (?.).
    Exits 1 on any problem.

.EXAMPLE
    powershell.exe -NoProfile -File tools\Test-PowerShellSyntax.ps1
    pwsh -NoProfile -File tools/Test-PowerShellSyntax.ps1
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$windowsDir = Join-Path $root 'windows'
$files = @(Get-ChildItem -Path $windowsDir -Filter *.ps1) + @(Get-ChildItem -Path $PSScriptRoot -Filter *.ps1)
$failed = $false

foreach ($f in $files) {
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$tokens, [ref]$errors)
    if ($errors.Count -gt 0) {
        $failed = $true
        foreach ($e in $errors) { Write-Host ('FAIL {0}:{1}: {2}' -f $f.Name, $e.Extent.StartLineNumber, $e.Message) -ForegroundColor Red }
        continue
    }
    # Match on type/operator *names* so this also runs on 5.1, where these AST types don't exist.
    $ps7 = $ast.FindAll({
            param($n)
            $type = $n.GetType().Name
            ($type -eq 'TernaryExpressionAst') -or ($type -eq 'PipelineChainAst') -or
            ($type -eq 'BinaryExpressionAst' -and "$($n.Operator)" -eq 'QuestionQuestion') -or
            ($type -eq 'AssignmentStatementAst' -and "$($n.Operator)" -eq 'QuestionQuestionEquals') -or
            ($n.PSObject.Properties['NullConditional'] -and $n.NullConditional)
        }, $true)
    if ($ps7.Count -gt 0) {
        $failed = $true
        foreach ($n in $ps7) { Write-Host ('FAIL {0}:{1}: PowerShell 7-only syntax: {2}' -f $f.Name, $n.Extent.StartLineNumber, $n.Extent.Text) -ForegroundColor Red }
    } else {
        Write-Host ('OK   {0}' -f $f.Name) -ForegroundColor Green
    }
}

Write-Host ("Checked {0} file(s) with PowerShell {1}." -f $files.Count, $PSVersionTable.PSVersion)
if ($failed) { exit 1 }
