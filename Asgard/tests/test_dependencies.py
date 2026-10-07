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
IMPORT_NAMES = {"pyyaml": "yaml", "pywin32": "win32api", "python_dateutil": "dateutil"}


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

    def test_every_declared_package_is_used(self):
        used = {name for _, name in imports(runtime_files())}
        self.assertEqual(sorted(declared()[0] - used), [], "remove packages nothing imports")


if __name__ == "__main__":
    unittest.main()
