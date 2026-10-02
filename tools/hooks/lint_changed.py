#!/usr/bin/env python3
"""Kiro PostFileSave hook: report lint problems for the file the agent just saved.

- *.py         -> ruff check (no auto-fix; changing the file mid-edit breaks the agent's next edit).
                  Without ruff (e.g. on the no-pip workstation) it falls back to a stdlib syntax check.
- *.ps1/psm1   -> PowerShell parser errors (always) + PSScriptAnalyzer (if installed). Prefers
                  Windows PowerShell 5.1 over pwsh, because 5.1 is the target and its parser rejects
                  PS7-only syntax that pwsh would accept.

Prints NOTHING when clean, so a clean save adds zero tokens to the conversation.
With --format-changed (for the Stop hook): silently runs ruff format + safe fixes
on git-changed .py files.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _hookio import ROOT, event_paths, git_changed, read_event, rel  # noqa: E402

PY = (".py",)
PS = (".ps1", ".psm1", ".psd1")
MAX_LINES = 25


def ruff_cmd() -> list[str] | None:
    exe = shutil.which("ruff")
    if exe:
        return [exe]
    probe = subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True)
    return [sys.executable, "-m", "ruff"] if probe.returncode == 0 else None


def syntax_check(files: list[Path]) -> list[str]:
    lines = []
    for f in files:
        try:
            compile(f.read_bytes(), str(f), "exec", dont_inherit=True)
        except SyntaxError as exc:
            lines.append(f"{rel(f)}:{exc.lineno}:{exc.offset or 0}: SyntaxError {exc.msg}")
    return lines


def lint_python(files: list[Path]) -> list[str]:
    if not files:
        return []
    ruff = ruff_cmd()
    if not ruff:
        return syntax_check(files)
    proc = subprocess.run(
        [*ruff, "check", "--no-fix", "--output-format=concise", "--quiet", *map(str, files)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=25,
    )
    lines = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith(("Found ", "[*]", "No fixes")):
            continue
        p = line.split(":", 1)[0]
        try:
            line = rel(Path(p)) + line[len(p) :] if Path(p).is_absolute() else line
        except Exception:
            pass
        lines.append(line.replace("\\", "/"))
    return lines


PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$p = '{path}'
$errs = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$null, [ref]$errs)
foreach ($e in $errs) {{ 'L{{0}}: [ParseError] {{1}}' -f $e.Extent.StartLineNumber, $e.Message }}
if (-not $errs -and (Get-Module -ListAvailable -Name PSScriptAnalyzer)) {{
  Invoke-ScriptAnalyzer -Path $p |
    Where-Object {{ "$($_.Severity)" -ne 'Information' }} |
    ForEach-Object {{
      'L{{0}}: [{{1}}] {{2}} ({{3}})' -f $_.Line, $_.Severity, $_.Message, $_.RuleName
    }}
}}
"""


def lint_powershell(files: list[Path]) -> list[str]:
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell or not files:
        return []
    lines: list[str] = []
    for f in files:
        script = PS_SCRIPT.format(path=str(f.resolve()).replace("'", "''"))
        try:
            proc = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-Command", script],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=40,
            )
        except subprocess.TimeoutExpired:
            continue
        for out in proc.stdout.splitlines():
            out = out.strip()
            if out.startswith("L") and ": [" in out:
                lines.append(f"{rel(f)}:{out[1:]}")
    return lines


def format_changed() -> int:
    files = git_changed(PY)
    ruff = ruff_cmd()
    if not files or not ruff:
        return 0
    args = list(map(str, files))
    subprocess.run([*ruff, "format", "--quiet", *args], cwd=ROOT, capture_output=True)
    subprocess.run(
        [*ruff, "check", "--fix", "--quiet", "--exit-zero", *args], cwd=ROOT, capture_output=True
    )
    return 0


def main() -> int:
    if "--format-changed" in sys.argv:
        return format_changed()

    event = read_event()
    files = event_paths(event, PY + PS)
    if not files and not event:
        files = git_changed(PY + PS)  # no payload at all: fall back to changed files

    findings = lint_python([f for f in files if f.suffix.lower() in PY])
    findings += lint_powershell([f for f in files if f.suffix.lower() in PS])
    if findings:
        print(f"Lint: {len(findings)} issue(s). Fix these before running tests:")
        for line in findings[:MAX_LINES]:
            print("  " + line)
        if len(findings) > MAX_LINES:
            print(f"  (+{len(findings) - MAX_LINES} more)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # a broken hook must never block the agent
        print(f"lint hook error: {exc}", file=sys.stderr)
        sys.exit(0)
