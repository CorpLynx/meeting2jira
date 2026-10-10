"""Dependencies are declared, pinned and kept out of what everything imports (docs/dependency-policy.md).

Standard-library-only stopped being a rule in Oct 2026. These tests are what replaced it:
- every import in asgard/ and apps/ is the standard library, Asgard's own code, or a package
  pinned exactly in requirements.txt;
- asgard/muninn and the launcher's start-up path import only the standard library, because every
  app and Odin import Muninn, and setup has to run before anything is installed.
"""
import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._\-]*)(\[[A-Za-z0-9,._\-]+\])?==[A-Za-z0-9.+!_\-]+(\s*;.*)?$")
# What the launcher needs to open its window, show an error, install and uninstall.
STARTUP = ("__init__.py", "paths.py", "launcher.py", "catalog.py", "install.py", "valhalla.py", "runner.py",
           "winutil.py")
# Import names that differ from the package name on PyPI.
IMPORT_NAMES = {"pyyaml": "yaml", "pywin32": "win32api", "python_dateutil": "dateutil",
                "pyside6_essentials": "PySide6"}


def declared():
    names, loose = set(), []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = _PIN.match(line)
        if m:
            name = m.group(1).lower().replace("-", "_")
            names.add(IMPORT_NAMES.get(name, name))
        else:
            loose.append(line)
    return names, loose


def first_party():
    """asgard, each app's package (apps/<id>/<id>/), and each app folder's own top-level modules."""
    names = {"asgard"}
    for app in (ROOT / "apps").iterdir():
        if app.is_dir():
            names.update(p.name for p in app.iterdir() if p.is_dir() and (p / "__init__.py").exists())
            names.update(p.stem for p in app.glob("*.py"))
    return names


def imports(paths):
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    yield path, a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                yield path, node.module.split(".")[0]


def runtime_files():
    for folder in ("asgard", "apps"):
        yield from (p for p in (ROOT / folder).rglob("*.py") if "__pycache__" not in p.parts)


@unittest.skipIf(getattr(sys, "stdlib_module_names", None) is None, "needs Python 3.10+ (sys.stdlib_module_names)")
class DependencyTests(unittest.TestCase):
    def test_requirements_are_exact_pins(self):
        _, loose = declared()
        self.assertEqual(loose, [], "pin every requirement exactly (name==1.2.3)")

    def test_the_checks_see_real_code(self):
        files = list(runtime_files())
        self.assertGreater(len(files), 30)
        self.assertTrue(all((ROOT / "asgard" / name).is_file() for name in STARTUP), STARTUP)
        self.assertIn("baldur", first_party())

    def test_every_import_is_stdlib_ours_or_declared(self):
        known = set(sys.stdlib_module_names) | first_party() | declared()[0]  # novermin (skipped before 3.10)
        offenders = sorted({f"{p.relative_to(ROOT)}: {name}" for p, name in imports(runtime_files())
                            if name not in known})
        self.assertEqual(offenders, [], "declare each package, pinned, in Asgard/requirements.txt")

    def test_muninn_and_startup_use_only_the_standard_library(self):
        core = list((ROOT / "asgard" / "muninn").rglob("*.py")) + [ROOT / "asgard" / n for n in STARTUP]
        allowed = set(sys.stdlib_module_names) | {"asgard"}  # novermin (skipped before 3.10)
        offenders = sorted({f"{p.relative_to(ROOT)}: {name}" for p, name in imports(core) if name not in allowed})
        self.assertEqual(offenders, [], "a package here becomes every app's (and Odin's) dependency")

    def test_odins_daily_run_uses_only_the_standard_library(self):
        """The scheduled task runs on whatever Python Asgard found, which may carry no packages."""
        app = ROOT / "apps" / "odin"
        files = [app / "cli.py"] + sorted((app / "odin").glob("*.py"))
        allowed = set(sys.stdlib_module_names) | {"odin", "asgard"}  # novermin (skipped before 3.10)
        offenders = []
        for path in files:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    # "from asgard import ui" names asgard.ui
                    names = ([f"asgard.{a.name}" for a in node.names] if node.module == "asgard"
                             else [node.module])
                else:
                    continue
                for name in names:
                    top = name.split(".")[0]
                    # Only the standard-library parts of Asgard: Muninn and its paths, never asgard.ui.
                    if top not in allowed or (top == "asgard"
                                              and not name.startswith(("asgard.muninn", "asgard.paths"))):
                        offenders.append(f"{path.relative_to(ROOT)}: {name}")
        self.assertGreater(len(files), 10)
        self.assertEqual(offenders, [], "Odin's daily run stays standard library (AGENTS.md); the window may not")

    def test_every_declared_package_is_used(self):
        used = {name for _, name in imports(runtime_files())}
        self.assertEqual(sorted(declared()[0] - used), [], "remove packages nothing imports")


MODULES_DOC = ROOT / "MODULES.md"
DOC_ROWS = ("Used in", "If it's missing", "Stdlib alternative", "Package alternatives")


def pinned_packages():
    """{PyPI name, normalised: import name} for every pin in requirements.txt."""
    out = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        m = _PIN.match(line.split("#", 1)[0].strip())
        if m:
            name = m.group(1).lower().replace("-", "_")
            out[name] = IMPORT_NAMES.get(name, name)
    return out


def doc_sections(text):
    """{package heading, normalised: {row name: cell text}} from MODULES.md's "### name" tables."""
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


class ModulesDocTests(unittest.TestCase):
    """MODULES.md says, for every package, where it's used and what to use if it isn't on-prem."""

    def setUp(self):
        self.assertTrue(MODULES_DOC.is_file(), "write MODULES.md: each package, where it's used, alternatives")
        self.sections = doc_sections(MODULES_DOC.read_text(encoding="utf-8"))
        self.packages = pinned_packages()

    def test_every_package_has_a_section_and_nothing_else_does(self):
        self.assertTrue(self.packages, "requirements.txt pins nothing; this check would pass vacuously")
        self.assertEqual(sorted(self.sections), sorted(self.packages),
                         "MODULES.md needs one '### name' section per pinned package, and no others")

    def test_every_section_says_where_and_what_instead(self):
        for name in self.packages:
            for row in DOC_ROWS:
                with self.subTest(package=name, row=row):
                    self.assertTrue(self.sections.get(name, {}).get(row), f'MODULES.md "{name}" needs a "{row}" row')

    def test_every_file_that_imports_a_package_is_listed(self):
        for name, import_name in self.packages.items():
            used_in = self.sections.get(name, {}).get("Used in", "")
            files = sorted({p.relative_to(ROOT).as_posix() for p, mod in imports(runtime_files()) if mod == import_name})
            self.assertTrue(files, f"nothing imports {import_name}; remove it from requirements.txt")
            for path in files:
                with self.subTest(package=name, file=path):
                    self.assertIn(path, used_in, f'add {path} to "Used in" for {name} in MODULES.md')

    def test_the_doc_parser_reads_rows(self):
        parsed = doc_sections("### Some-Pkg\n| | |\n| --- | --- |\n| Used in | `a.py` |\n## Next\n| Used in | x |\n")
        self.assertEqual(parsed, {"some_pkg": {"Used in": "`a.py`"}})


if __name__ == "__main__":
    unittest.main()
