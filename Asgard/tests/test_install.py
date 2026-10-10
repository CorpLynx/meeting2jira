import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from asgard import install, valhalla  # noqa: E402

try:
    import tkinter
    tkinter.Tcl()
    HAS_TK = True
except Exception:
    HAS_TK = False


@unittest.skipUnless(HAS_TK, "setup requires Tcl/Tk, like Asgard itself")
class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)   # after the test's own cleanups (open files)
        self.home = Path(self.tmp.name) / "Asgard"
        self._saved = {k: os.environ.get(k) for k in ("ASGARD_HOME", "APPDATA")}
        os.environ["ASGARD_HOME"] = str(self.home)
        os.environ.pop("APPDATA", None)
        self._cwd = os.getcwd()

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def run_setup(self) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return install.main(["--no-launch"])

    def test_install_upgrade_and_uninstall(self) -> None:
        self.assertEqual(self.run_setup(), 0)
        app = self.home / "app"
        for rel in ("Asgard.pyw", "VERSION", "README.md", "asgard/launcher.py", "asgard/apps.json",
                    "asgard/asgard.ico", "apps/README.md"):
            self.assertTrue((app / rel).exists(), rel)
        self.assertFalse(any(app.rglob("__pycache__")))
        self.assertFalse((app / "tests").exists())
        self.assertFalse((app / "tools").exists())
        ledger = json.loads((self.home / "install-ledger.json").read_text(encoding="utf-8"))
        self.assertIn({"kind": "dir", "path": str(app)}, ledger["items"])
        local = self.home / "apps.local.json"
        self.assertTrue(local.exists())

        local.write_text('{"apps": {}, "hidden": ["loki"]}', encoding="utf-8")
        self.assertEqual(self.run_setup(), 0)
        self.assertEqual(local.read_text(encoding="utf-8"), '{"apps": {}, "hidden": ["loki"]}')
        self.assertEqual(list(self.home.glob("app.old-*")), [])
        self.assertEqual(len(json.loads((self.home / "install-ledger.json").read_text())["history"]), 2)

        result = valhalla.uninstall(purge=False)
        self.assertTrue(result.ok, result.skipped)
        self.assertFalse(app.exists())
        self.assertFalse((self.home / "install-ledger.json").exists())
        self.assertTrue(local.exists())
        self.assertIn(str(local), result.kept)

        self.assertEqual(self.run_setup(), 0)
        # Apps keep their settings in settings\ (Baldur's baldur.json, Heimdall's files). The Windows
        # lab found that purging left them behind, so the data folder stayed and "Kept your data".
        (self.home / "settings").mkdir(exist_ok=True)
        (self.home / "settings" / "baldur.json").write_text("{}", encoding="utf-8")
        (self.home / "muninn.before-restore-20261006-120000.db").write_text("x", encoding="utf-8")   # a restore's copy
        result = valhalla.uninstall(purge=True)
        self.assertTrue(result.ok, result.skipped)
        self.assertEqual(result.kept, [])
        self.assertFalse(self.home.exists())

    def test_uninstall_never_touches_paths_outside_asgard(self) -> None:
        self.assertEqual(self.run_setup(), 0)
        outside = Path(self.tmp.name) / "Documents"
        outside.mkdir()
        keep = outside / "keep.txt"
        keep.write_text("mine", encoding="utf-8")
        ledger_path = self.home / "install-ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["items"] += [{"kind": "dir", "path": str(outside)},
                            {"kind": "file", "path": str(keep)},
                            {"kind": "dir", "path": str(self.home)},
                            {"kind": "regkey", "path": "HKCU\\Software\\Something"}]
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        result = valhalla.uninstall(purge=False)
        self.assertTrue(keep.exists())
        self.assertTrue(self.home.exists())
        self.assertEqual(len(result.skipped), 4)
        self.assertFalse((self.home / "app").exists())

    def test_uninstall_without_a_ledger_uses_known_locations(self) -> None:
        self.assertEqual(self.run_setup(), 0)
        (self.home / "install-ledger.json").unlink()
        result = valhalla.uninstall(purge=False)
        self.assertTrue(result.ok, result.skipped)
        self.assertFalse((self.home / "app").exists())


if __name__ == "__main__":
    unittest.main()
