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

    def test_resolve_python_copies_are_identical(self):
        """Resolve-Python is duplicated on purpose; make sure the copies never drift.

        Dot-sourcing can fail across AppLocker trust levels, so each script carries its own copy.
        That was cheap when the function was five lines. It is now the piece that decides whether
        the tool runs at all on a locked-down machine, so a fix applied to one copy and not the
        others would mean the sync works and the environment check disagrees, or vice versa.
        """
        pattern = re.compile(r"^function Resolve-Python\(\[string\]\$Override\) \{.*?^\}",
                             re.DOTALL | re.MULTILINE)
        bodies = {}
        for path in sorted(ROOT.glob("src/windows/*.ps1")) + sorted(ROOT.glob("tools/*.ps1")):
            text = path.read_text(encoding="utf-8")
            match = pattern.search(text)
            if match:
                bodies[path.name] = match.group()

        self.assertGreaterEqual(len(bodies), 3, f"expected several copies, found {sorted(bodies)}")
        distinct = set(bodies.values())
        if len(distinct) != 1:
            differing = sorted(bodies)
            self.fail("Resolve-Python has drifted between copies: " + ", ".join(differing))

    def test_paths_point_at_real_code(self):
        self.assertTrue(PACKAGE.is_dir(), f"{PACKAGE} is not a directory")
        self.assertTrue(PS_DIR.is_dir(), f"{PS_DIR} is not a directory")
        self.assertGreaterEqual(len(_py_files()), 9, "expected the whole Python package")
        found = sorted(p.name for p in PS_DIR.glob("*.ps1"))
        self.assertEqual(found, ["Export-OutlookMeetings.ps1", "Invoke-MeetingSync.ps1",
                                 "Register-MeetingSyncTask.ps1", "Test-Environment.ps1"])


REQUIREMENTS = ROOT / "requirements.txt"
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._\-]*)(\[[A-Za-z0-9,._\-]+\])?==[A-Za-z0-9.+!_\-]+(\s*;.*)?$")


def _declared():
    """Package names in requirements.txt, normalised, and any lines that aren't exact pins."""
    names, loose = set(), []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = _PIN.match(line)
        if m:
            names.add(m.group(1).lower().replace("-", "_"))
        else:
            loose.append(line)
    return names, loose


def _imports():
    for path in _py_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                for a in node.names:
                    yield path, a.name
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                yield path, node.module


class PythonGuardrails(unittest.TestCase):
    """Dependencies: allowed since Oct 2026 when declared and pinned (Asgard/docs/dependency-policy.md).

    This replaced test_stdlib_only. A package is a reviewed decision, so an import must appear in
    Odin/app/requirements.txt pinned with ==; nothing else gets in by accident.
    """

    def test_requirements_are_exact_pins(self):
        self.assertTrue(REQUIREMENTS.is_file(), f"{REQUIREMENTS} is missing")
        _, loose = _declared()
        self.assertEqual(loose, [], "pin every requirement exactly (name==1.2.3)")

    def test_every_import_is_stdlib_or_declared(self):
        stdlib = getattr(sys, "stdlib_module_names", None)
        if stdlib is None:
            self.skipTest("sys.stdlib_module_names needs Python 3.10+; run this check on a newer Python")
        declared, _ = _declared()
        offenders = [f"{path.name}: import {name}" for path, name in _imports()
                     if name.split(".")[0] not in stdlib and name.split(".")[0] != "meeting2jira"
                     and name.split(".")[0].lower() not in declared]
        self.assertEqual(offenders, [], "declare each package, pinned, in Odin/app/requirements.txt")

    def test_odin_never_imports_asgard(self):
        """Odin must run with no Asgard installed. Allowing an optional Muninn import is a decision
        recorded in Asgard/docs/integration/odin.md; change this test only with it."""
        hits = [f"{path.name}: import {name}" for path, name in _imports() if name.split(".")[0] == "asgard"]
        self.assertEqual(hits, [])
        declared, _ = _declared()
        self.assertNotIn("asgard", declared)

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
