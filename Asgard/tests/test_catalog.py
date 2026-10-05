import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from asgard import catalog, paths  # noqa: E402
from asgard.catalog import COMING_SOON, MISSING, NEEDS_SETUP, READY, CatalogError  # noqa: E402


def contrast(a: str, b: str) -> float:
    def lum(h: str) -> float:
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._saved = os.environ.get("ASGARD_HOME")
        os.environ["ASGARD_HOME"] = str(self.home)
        self.local = self.home / "apps.local.json"

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("ASGARD_HOME", None)
        else:
            os.environ["ASGARD_HOME"] = self._saved
        self.tmp.cleanup()

    def write_local(self, data: dict) -> None:
        self.local.write_text(json.dumps(data), encoding="utf-8")

    def load(self):
        return catalog.load_catalog(local=self.local)

    def test_default_tiles(self) -> None:
        apps, warnings = self.load()
        ids = [a.id for a in apps]
        self.assertEqual(warnings, [])
        self.assertEqual(ids[0], "odin")
        self.assertEqual(ids[-1], "valhalla")
        self.assertEqual(len(ids), len(set(ids)))
        states = {a.id: catalog.state(a) for a in apps}
        self.assertEqual(states["odin"], NEEDS_SETUP)
        self.assertEqual(states["valhalla"], READY)
        self.assertEqual(states["baldur"], READY, "Baldur's window ships with Asgard (0.3.1)")
        for app_id in ids:
            if app_id not in ("odin", "valhalla", "baldur"):
                self.assertEqual(states[app_id], COMING_SOON, app_id)

    def test_monograms_are_readable(self) -> None:
        apps, _ = self.load()
        for app in apps:
            self.assertEqual(len(app.monogram), 2, app.id)
            self.assertGreaterEqual(contrast(app.color, "#FFFFFF"), 4.5, app.id)

    def test_odin_location_makes_tile_ready_then_missing(self) -> None:
        script = self.home / "Odin" / "odin.pyw"
        script.parent.mkdir()
        script.write_text("print('odin')\n", encoding="utf-8")
        self.write_local({"apps": {"odin": {"launch": {"type": "python", "target": str(script)}}}})
        apps, warnings = self.load()
        odin = next(a for a in apps if a.id == "odin")
        self.assertEqual(warnings, [])
        self.assertEqual((odin.name, odin.source), ("Odin", "local"))
        self.assertEqual(catalog.state(odin), READY)
        script.unlink()
        self.assertEqual(catalog.state(odin), MISSING)

    def test_add_hide_and_order(self) -> None:
        self.write_local({
            "apps": {"jira": {"name": "Jira", "color": "#0B5CAD",
                              "launch": {"type": "url", "target": "https://jira.example.gov"}}},
            "hidden": ["valkyrie"],
            "order": ["jira", "odin"],
        })
        apps, warnings = self.load()
        ids = [a.id for a in apps]
        self.assertEqual(warnings, [])
        self.assertEqual(ids[:2], ["jira", "odin"])
        self.assertNotIn("valkyrie", ids)
        jira = apps[0]
        self.assertEqual(jira.monogram, "Ji")
        self.assertEqual(catalog.state(jira), READY)
        self.assertEqual(catalog.build_spec(jira).kind, "url")

    def test_list_form_of_apps_is_accepted(self) -> None:
        self.write_local({"apps": [{"id": "wiki", "name": "Wiki",
                                    "launch": {"type": "url", "target": "https://wiki.example.gov"}}]})
        apps, warnings = self.load()
        self.assertEqual(warnings, [])
        self.assertIn("wiki", [a.id for a in apps])

    def test_broken_local_file_falls_back_to_defaults(self) -> None:
        self.local.write_text('{"apps": {"odin": }', encoding="utf-8")
        apps, warnings = self.load()
        self.assertEqual(len(apps), 9)
        self.assertEqual(len(warnings), 1)
        self.assertIn("line 1", warnings[0])

    def test_bad_entry_keeps_default_and_warns(self) -> None:
        self.write_local({"apps": {"odin": {"color": "blue"}}})
        apps, warnings = self.load()
        odin = next(a for a in apps if a.id == "odin")
        self.assertEqual(odin.color, "#2B5797")
        self.assertTrue(any("colour" in w for w in warnings))

    def test_notepad_byte_order_mark_is_accepted(self) -> None:
        self.local.write_bytes(b"\xef\xbb\xbf" + json.dumps({"hidden": ["loki"]}).encode())
        apps, warnings = self.load()
        self.assertEqual(warnings, [])
        self.assertNotIn("loki", [a.id for a in apps])

    def test_infer_launch_and_virtual_environment(self) -> None:
        odin = self.home / "Odin"
        script = odin / "src" / "odin.py"
        script.parent.mkdir(parents=True)
        script.write_text("", encoding="utf-8")
        launch = catalog.infer_launch(str(script))
        self.assertEqual(launch, {"type": "python", "target": str(script)})
        venv_python = odin / ".venv" / (Path("Scripts/pythonw.exe") if os.name == "nt" else Path("bin/python"))
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("", encoding="utf-8")
        self.assertEqual(catalog.infer_launch(str(script))["interpreter"], str(venv_python))
        self.assertEqual(catalog.infer_launch("run.ps1")["type"], "powershell")
        self.assertEqual(catalog.infer_launch("odin.exe")["type"], "exe")
        self.assertEqual(catalog.infer_launch("Odin.lnk")["type"], "open")
        self.assertEqual(catalog.infer_launch("start.CMD")["type"], "open")

    def test_build_spec_for_each_type(self) -> None:
        script = self.home / "tool.pyw"
        script.write_text("", encoding="utf-8")
        app = catalog.App(id="t", name="T", launch={"type": "python", "target": str(script),
                                                    "args": ["--data", "{data}"]})
        spec = catalog.build_spec(app)
        self.assertEqual(spec.kind, "process")
        self.assertEqual(spec.argv, [catalog.python_paths()[1], str(script), "--data", str(self.home)])
        self.assertEqual(spec.cwd, str(self.home))
        ps = catalog.build_spec(catalog.App(id="p", name="P", launch={"type": "powershell", "target": "run.ps1"}))
        self.assertEqual(ps.argv[1:], ["-NoProfile", "-File", "run.ps1"])
        self.assertEqual(catalog.build_spec(catalog.App(id="o", name="O", launch={"type": "open", "target": "x.lnk"})).kind,
                         "shell")
        self.assertEqual(catalog.build_spec(catalog.App(id="v", name="V", launch={"type": "internal",
                                                                                   "command": "uninstall"})).target,
                         "uninstall")
        with self.assertRaises(CatalogError):
            catalog.build_spec(catalog.App(id="u", name="U", launch={"type": "url", "target": "file:///C:/x"}))

    def test_expand_placeholders_and_environment(self) -> None:
        os.environ["ASGARD_TEST_VAR"] = "zz"
        try:
            self.assertEqual(catalog.expand("{app}"), str(paths.CODE_ROOT))
            self.assertEqual(catalog.expand("%ASGARD_TEST_VAR%" if os.name == "nt" else "$ASGARD_TEST_VAR"), "zz")
        finally:
            del os.environ["ASGARD_TEST_VAR"]

    def test_save_override_keeps_other_settings(self) -> None:
        self.write_local({"apps": {}, "hidden": ["loki"]})
        catalog.save_launch_override("odin", {"type": "exe", "target": "C:/Odin/odin.exe"}, self.local)
        data = json.loads(self.local.read_text(encoding="utf-8"))
        self.assertEqual(data["apps"]["odin"]["launch"]["target"], "C:/Odin/odin.exe")
        self.assertEqual(data["hidden"], ["loki"])

    def test_save_refuses_to_overwrite_a_file_it_cannot_read(self) -> None:
        self.local.write_text("{not json", encoding="utf-8")
        with self.assertRaises(CatalogError):
            catalog.save_launch_override("odin", {"type": "exe", "target": "x.exe"}, self.local)
        self.assertEqual(self.local.read_text(encoding="utf-8"), "{not json")

    def test_ensure_local_manifest_creates_template_once(self) -> None:
        path = catalog.ensure_local_manifest(self.local)
        self.assertIn("_help", json.loads(path.read_text(encoding="utf-8")))
        path.write_text('{"hidden": []}', encoding="utf-8")
        catalog.ensure_local_manifest(self.local)
        self.assertEqual(path.read_text(encoding="utf-8"), '{"hidden": []}')


if __name__ == "__main__":
    unittest.main()
