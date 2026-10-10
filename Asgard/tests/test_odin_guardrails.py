"""Guardrails for Odin's non-negotiables (AGENTS.md, docs/integration/odin.md).

These fail loudly if an iteration weakens TLS, reads guarded Outlook properties by default, adds an
execution-policy bypass, breaks Constrained Language Mode safety, or lets an Odin exporter pin a
package without documenting it in Odin/MODULES.md. Odin's own imports are checked with the rest of
Asgard's (tests/test_dependencies.py). Change a rule here only with a deliberate, documented decision.
"""
import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # Asgard/
APP = ROOT / "apps" / "odin"
PACKAGE = APP / "odin"
PS_DIR = APP / "windows"
for folder in (ROOT, APP):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))


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
        for path in sorted(PS_DIR.glob("*.ps1")) + sorted(APP.glob("tools/*.ps1")):
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


_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._\-]*)(\[[A-Za-z0-9,._\-]+\])?==[A-Za-z0-9.+!_\-]+(\s*;.*)?$")


class PythonGuardrails(unittest.TestCase):
    def test_tls_verification_never_disabled(self):
        banned = re.compile(r"CERT_NONE|_create_unverified_context|check_hostname\s*=\s*False|verify\s*=\s*False")
        hits = [f"{p.name}:{i}" for p in _py_files()
                for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if banned.search(line)]
        self.assertEqual(hits, [], "TLS verification must never be disabled")

    def test_https_required_by_config_validation(self):
        from odin.config import ConfigError, build_config
        with self.assertRaises(ConfigError):
            build_config({"jira": {"base_url": "http://jira.example.gov", "default_parent": "P-1"}})


ODIN = ROOT.parent / "Odin"                             # Odin's optional exporters, outside Asgard
MODULES_DOC = ODIN / "MODULES.md"
# Every Odin deliverable that may pin packages, and the folder its code lives in. Odin itself is
# an Asgard app now: its packages are Asgard's (requirements.txt, MODULES.md).
DELIVERABLES = {"graph-app": ODIN / "graph-app", "playwright-app": ODIN / "playwright-app"}
DOC_ROWS = ("Used in", "If it's missing", "Stdlib alternative", "Package alternatives")


def _pins(path: Path):
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _PIN.match(line.split("#", 1)[0].strip())
        if m:
            names.add(m.group(1).lower().replace("-", "_"))
    return names


def _doc_sections(text: str):
    """{package heading, normalised: {row: cell}} from MODULES.md's "### name" tables."""
    sections, current = {}, None
    for line in text.splitlines():
        if line.startswith("### "):
            current = sections.setdefault(line[4:].strip().strip("`").lower().replace("-", "_"), {})
        elif line.startswith("## "):
            current = None
        elif current is not None and line.startswith("| ") and line.count("|") >= 3:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells[0] and not set(cells[0]) <= {"-", " "}:
                current[cells[0]] = " | ".join(cells[1:]).strip()
    return sections


class ModulesDocGuardrails(unittest.TestCase):
    """Odin/MODULES.md says, for every package an Odin exporter pins, where it's used and what
    to use instead if it isn't available on-prem.

    The exporters don't ship inside Asgard, so an installed copy (no ../Odin) skips this; in the
    repo it always runs.
    """

    def setUp(self):
        present = {name: folder for name, folder in DELIVERABLES.items() if (folder / "requirements.txt").is_file()}
        if not ODIN.is_dir():
            self.skipTest("Odin's exporters aren't beside this copy of Asgard")
        self.assertTrue(MODULES_DOC.is_file(), "write Odin/MODULES.md: each package, where it's used, alternatives")
        self.pins = {pkg: name for name, folder in present.items() for pkg in _pins(folder / "requirements.txt")}
        self.sections = _doc_sections(MODULES_DOC.read_text(encoding="utf-8"))
        self.present = present

    def test_every_package_has_a_section_and_nothing_else_does(self):
        self.assertEqual(sorted(self.sections), sorted(self.pins),
                         "Odin/MODULES.md needs one '### name' section per pinned package, and no others")

    def test_every_section_says_where_and_what_instead(self):
        for pkg in self.pins:
            for row in DOC_ROWS:
                with self.subTest(package=pkg, row=row):
                    self.assertTrue(self.sections.get(pkg, {}).get(row), f'MODULES.md "{pkg}" needs a "{row}" row')

    def test_every_file_that_imports_a_package_is_listed(self):
        for pkg, deliverable in self.pins.items():
            folder = self.present[deliverable]
            files = []
            for path in sorted(folder.rglob("*.py")):
                if "tests" in path.relative_to(folder).parts or "__pycache__" in path.parts:
                    continue
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                            else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                            else [])
                    if any(m.split(".")[0].lower() == pkg for m in mods):
                        files.append(path.relative_to(ODIN).as_posix())
                        break
            self.assertTrue(files, f"nothing in {deliverable}/ imports {pkg}; remove its pin")
            used_in = self.sections.get(pkg, {}).get("Used in", "")
            for rel in files:
                with self.subTest(package=pkg, file=rel):
                    self.assertIn(rel, used_in, f'add {rel} to "Used in" for {pkg} in Odin/MODULES.md')

    def test_the_doc_parser_reads_rows(self):
        parsed = _doc_sections("### Some-Pkg\n| | |\n| --- | --- |\n| Used in | `a.py` |\n## Next\n| Used in | x |\n")
        self.assertEqual(parsed, {"some_pkg": {"Used in": "`a.py`"}})


class PowerShellGuardrails(unittest.TestCase):
    def _read(self, name):
        return _ps_code(PS_DIR / name)

    def test_scripts_are_ascii_with_crlf(self):
        """Asgard's rule for every .cmd and .ps1 (AGENTS.md); Windows PowerShell 5.1 reads a file
        without a BOM in the ANSI code page, so anything but ASCII can change meaning."""
        scripts = sorted(APP.rglob("*.ps1")) + sorted(APP.glob("*.cmd"))
        self.assertGreaterEqual(len(scripts), 7)
        for path in scripts:
            with self.subTest(script=path.name):
                raw = path.read_bytes()
                self.assertTrue(raw.isascii(), "ASCII only")
                self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), "CRLF line endings")

    def test_the_scripts_call_odins_cli_by_path(self):
        """The packaged build runs scripts by path and has no -m, so nothing may use python -m odin."""
        for path in sorted(APP.rglob("*.ps1")) + sorted(APP.glob("*.cmd")):
            with self.subTest(script=path.name):
                self.assertNotRegex(path.read_text(encoding="ascii"), r"-m['\"]?,?\s*['\"]?(odin|meeting2jira)\b")

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
