"""Guardrails for the project's non-negotiables (documented in the repo: HANDOFF.md, .kiro/steering/tech.md).

These fail loudly if an iteration adds a third-party dependency, weakens TLS, reads guarded Outlook
properties by default, adds an execution-policy bypass, or breaks Constrained Language Mode safety.
Change a rule here only with a deliberate, documented decision.
"""
import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # app/
PACKAGE = ROOT / "src" / "meeting2jira"
PS_DIR = ROOT / "src" / "windows"


def _ps_code(path: Path) -> str:
    """PowerShell source with comments removed, so documentation can mention banned things."""
    text = re.sub(r"<#.*?#>", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    return "\n".join(line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
                     for line in text.splitlines())


def _py_files():
    return sorted(PACKAGE.glob("*.py"))


class GuardrailWiringTests(unittest.TestCase):
    """Prove the guardrails are actually looking at files.

    Most checks below assert "no offenders found". If a path constant were wrong - say after the
    tree is reorganised - they would glob an empty directory and pass vacuously, quietly removing
    every protection in this file. These assertions fail loudly instead.
    """

    def test_paths_point_at_real_code(self):
        self.assertTrue(PACKAGE.is_dir(), f"{PACKAGE} is not a directory")
        self.assertTrue(PS_DIR.is_dir(), f"{PS_DIR} is not a directory")
        self.assertGreaterEqual(len(_py_files()), 9, "expected the whole Python package")
        found = sorted(p.name for p in PS_DIR.glob("*.ps1"))
        self.assertEqual(found, ["Export-OutlookMeetings.ps1", "Invoke-MeetingSync.ps1",
                                 "Register-MeetingSyncTask.ps1", "Test-Environment.ps1"])


class PythonGuardrails(unittest.TestCase):
    def test_stdlib_only(self):
        stdlib = getattr(sys, "stdlib_module_names", None)
        if stdlib is None:
            self.skipTest("sys.stdlib_module_names needs Python 3.10+; run this check on a newer Python")
        offenders = []
        for path in _py_files():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for name in names:
                    top = name.split(".")[0]
                    if top not in stdlib and top != "meeting2jira":
                        offenders.append(f"{path.name}: import {name}")
        self.assertEqual(offenders, [], "Third-party imports are not allowed (no pip on target machines)")

    def test_tls_verification_never_disabled(self):
        banned = re.compile(r"CERT_NONE|_create_unverified_context|check_hostname\s*=\s*False|verify\s*=\s*False")
        hits = [f"{p.name}:{i}" for p in _py_files()
                for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if banned.search(line)]
        self.assertEqual(hits, [], "TLS verification must never be disabled")

    def test_https_required_by_config_validation(self):
        from meeting2jira.config import ConfigError, build_config
        with self.assertRaises(ConfigError):
            build_config({"jira": {"base_url": "http://jira.example.gov", "default_parent": "P-1"}})


class PowerShellGuardrails(unittest.TestCase):
    def _read(self, name):
        return _ps_code(PS_DIR / name)

    def test_no_execution_policy_bypass(self):
        pattern = re.compile(r"ExecutionPolicy\s+(Bypass|Unrestricted)|Set-ExecutionPolicy", re.IGNORECASE)
        hits = [p.name for p in PS_DIR.glob("*.ps1") if pattern.search(_ps_code(p))]
        self.assertEqual(hits, [], "Never bypass or change execution policy from these scripts")

    def test_exporter_reads_no_guarded_outlook_properties(self):
        # Reading these triggers Outlook's object-model guard and pulls sensitive content.
        # (.Organizer is allowed only behind the -IncludeOrganizer switch.)
        guarded = re.compile(r"\$item\.(Body|HTMLBody|RTFBody|RequiredAttendees|OptionalAttendees|"
                             r"Resources|Recipients|GetOrganizer|SaveAs|Send|Respond)\b")
        self.assertIsNone(guarded.search(self._read("Export-OutlookMeetings.ps1")))
        organizer_lines = [line for line in self._read("Export-OutlookMeetings.ps1").splitlines()
                           if "$item.Organizer" in line]
        self.assertTrue(all("IncludeOrganizer" in line for line in organizer_lines),
                        "$item.Organizer may only be read behind -IncludeOrganizer")

    def test_clm_safe_scripts(self):
        # These must keep working under Constrained Language Mode: no COM, no Add-Type, no
        # New-Object, and no static method calls on .NET types.
        #
        # The static-call pattern matches any [Type]::Member, not just [System.X]::, because
        # Constrained Language Mode blocks [math]::Floor and [Convert]::ToInt32 just as firmly as
        # the fully-qualified spellings. A provider path like 'Registry::HKEY_...' is unaffected,
        # since it is not bracketed.
        banned = re.compile(r"-ComObject|Add-Type|New-Object|\[[A-Za-z_][\w.]*\]::|\[IO\.|\[Text\.")
        for name in ("Invoke-MeetingSync.ps1", "Test-Environment.ps1", "Register-MeetingSyncTask.ps1"):
            with self.subTest(script=name):
                found = banned.search(self._read(name))
                self.assertIsNone(found, f"{name} must stay CLM-safe, found {found and found.group()!r}")


if __name__ == "__main__":
    unittest.main()
