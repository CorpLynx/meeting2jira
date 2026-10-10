"""The shared window itself, offscreen: every page loads with no QML errors, themes switch per app
and follow Settings live, a second app plugs in from a manifest alone, and spawn() streams a child.

Skipped when PySide6 isn't installed (the window is optional; the CLIs and launcher don't need it).
This proves the QML loads and binds; how it looks on Windows at real DPI needs a person.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT, ROOT / "apps" / "heimdall"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import Q_ARG, Q_RETURN_ARG, QCoreApplication, QMetaObject
    from asgard.ui import shell
except ImportError:
    shell = None

FORM = {"instance_url": "https://snow.example.gov", "catalog_sys_id": "0123456789abcdef0123456789abcdef",
        "fields": [{"label": "Short description", "required": True}, {"label": "I confirm", "kind": "checkbox"}]}

DEMO_QML = """import QtQuick
import QtQuick.Layouts
import AsgardUI

ScrollPage {
    id: page
    property var bridge: null
    property string greeting: bridge ? bridge.call("hello", ["Odin"]).value : ""
    PageHeader { title: "Assigned to me"; subtitle: page.greeting }
    Card { title: "Rules"; MetricCard { label: "Open"; value: "3" } }
}
"""
DEMO_BACKEND = """class Backend:
    def hello(self, name):
        return "Hello " + name
    def dashboard(self):
        return [{"label": "Assigned", "value": 3, "view": "tasks"}]
    def boom(self):
        raise RuntimeError("secret detail")
    def nope(self):
        raise ValueError("Set the parent issue first.")
"""


class QuietHome:
    """Builds a HomeBackend over a tiny tile list and a fake runner, and never starts Muninn's thread."""

    @staticmethod
    def make(folder):
        from asgard.ui import home
        from test_ui_home import FakeRunner
        script = Path(folder) / "alpha.pyw"
        script.write_text("print('hi')\n", encoding="utf-8")
        defaults = Path(folder) / "tiles.json"
        defaults.write_text(json.dumps({"version": 1, "apps": [
            {"id": "alpha", "name": "Alpha", "description": "The first app", "monogram": "Al", "color": "#2B5797",
             "status": "available", "launch": {"type": "python", "target": str(script)}},
            {"id": "soon", "name": "Soon", "description": "Not built yet", "monogram": "So", "color": "#A4376D",
             "status": "coming_soon"}]}), encoding="utf-8")

        class Backend(home.HomeBackend):
            def start_muninn(self):
                pass

        return Backend(defaults=defaults, local=Path(folder) / "local.json", runner=FakeRunner())


def pump(seconds=0.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)


@unittest.skipIf(shell is None, "PySide6 isn't installed (the shared window is optional)")
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = shell.make_app(["test"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)   # after the test's own cleanups (open files)
        self.dir = Path(self.tmp.name).resolve()
        self._home = os.environ.get("ASGARD_HOME")
        os.environ["ASGARD_HOME"] = str(self.dir)
        self.shells = []

    def tearDown(self):
        for s in self.shells:
            s.close()
        if self._home is None:
            os.environ.pop("ASGARD_HOME", None)
        else:
            os.environ["ASGARD_HOME"] = self._home

    def open(self, **kwargs):
        s = shell.Shell(**kwargs)
        self.shells.append(s)
        self.assertTrue(s.load(), s.qml_warnings)
        pump()
        return s

    def page_ready(self, s):
        # Through QML functions: a Python wrapper around a page item can delete it when collected.
        return QMetaObject.invokeMethod(s.engine.rootObjects()[0], "pageReady", Q_RETURN_ARG("QVariant"))

    def page_value(self, s, name):
        return QMetaObject.invokeMethod(s.engine.rootObjects()[0], "pageValue", Q_RETURN_ARG("QVariant"),
                                        Q_ARG("QVariant", name))

    def visit_all(self, s):
        for key in s.navigation.pages:
            with self.subTest(page=key):
                self.assertTrue(s.navigation.go(key))
                pump(0.2)
                self.assertTrue(self.page_ready(s), f"{key} didn't load: {s.qml_warnings}")
        self.assertEqual(s.qml_warnings, [])

    def write_form(self):
        path = self.dir / "settings" / "heimdall.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(FORM), encoding="utf-8")

    def demo_root(self):
        root = self.dir / "code"
        ui = root / "apps" / "odin" / "ui"
        ui.mkdir(parents=True)
        (ui / "Tasks.qml").write_text(DEMO_QML, encoding="utf-8")
        (ui / "manifest.json").write_text(json.dumps({
            "app": "odin", "name": "Odin", "subtitle": "Jira", "theme": {"accent": "#2B7A4B"},
            "backend": "odin_ui:Backend",
            "views": [{"id": "tasks", "title": "Assigned to me", "qml": "Tasks.qml"}]}), encoding="utf-8")
        (ui.parent / "odin_ui.py").write_text(DEMO_BACKEND, encoding="utf-8")
        return root

    # ------------------------------------------------------------ loading

    def test_heimdall_window_loads_every_page_cleanly_with_no_form(self):
        s = self.open(app="heimdall")
        self.assertEqual(s.shell.title, "Heimdall")
        self.visit_all(s)

    def test_odin_window_loads_every_page_cleanly_before_setup(self):
        """No settings, no token, no Muninn: every page still loads and says what to do."""
        s = self.open(app="odin")
        self.assertEqual(s.shell.title, "Odin")
        self.visit_all(s)

    def test_odin_window_loads_every_page_cleanly_with_muninn_and_settings(self):
        from asgard import muninn
        muninn.prepare(self.dir / "muninn.db", backups=self.dir / "backups")
        odin = self.dir / "odin"
        odin.mkdir()
        (odin / "config.json").write_text(json.dumps({"jira": {"base_url": "https://jira.example.gov",
                                                               "default_parent": "PROJ-1"}}), encoding="utf-8")
        (odin / "last_run.json").write_text(json.dumps({"finished_utc": "2026-10-09T21:00:00Z", "exit_code": 1,
                                                        "command": "daily", "first_error": "HTTP 401"}),
                                            encoding="utf-8")
        s = self.open(app="odin")
        self.visit_all(s)

    def test_heimdall_pages_load_cleanly_with_a_form_and_templates(self):
        self.write_form()
        from heimdall.ui_backend import Backend
        Backend().save_template("", {"name": "Monthly", "values": {"Short description": "x", "I confirm": True}})
        s = self.open(app="heimdall")
        self.visit_all(s)
        s.navigation.go("heimdall:templates")
        pump()
        self.assertEqual(self.page_value(s, "selected"), "Monthly")
        self.assertEqual(s.qml_warnings, [])

    def test_a_second_app_plugs_in_from_its_manifest_alone(self):
        s = self.open(root=self.demo_root())
        self.assertEqual([i["key"] for i in s.navigation.items if i["kind"] == "page"], ["dashboard", "odin:tasks"])
        self.visit_all(s)
        s.navigation.go("odin:tasks")
        pump()
        self.assertEqual(self.page_value(s, "greeting"), "Hello Odin")
        groups = s.dashboard.collect()
        self.assertEqual(groups[0]["cards"][0]["target"], "odin:tasks")

    # ------------------------------------------------------------ the Apps page (the launcher)
    def open_home(self, **kwargs):
        return self.open(home=True, home_backend=QuietHome.make(self.dir), **kwargs)

    def test_apps_page_is_first_and_every_page_loads_cleanly(self):
        s = self.open_home(root=self.demo_root())
        self.assertEqual(s.navigation.currentKey, "home")
        self.assertEqual([i["key"] for i in s.navigation.items if i["kind"] == "page"],
                         ["home", "dashboard", "odin:tasks"])
        self.assertEqual(s.shell.subtitle, "Apps and tools")
        self.visit_all(s)

    def test_apps_page_has_a_tile_for_each_app_and_search_narrows_them(self):
        s = self.open_home()
        self.assertEqual(self.page_value(s, "tileCount"), 2)
        s.home.setQuery("alp")
        pump()
        self.assertEqual([t["id"] for t in s.home.tiles], ["alpha"])
        self.assertEqual(self.page_value(s, "tileCount"), 1)
        s.home.setQuery("zzz")
        pump()
        self.assertEqual(self.page_value(s, "tileCount"), 0)
        self.assertEqual(s.qml_warnings, [])

    def test_opening_a_tile_starts_the_app_and_says_so(self):
        s = self.open_home()
        toasts = []
        s.shell.toastRequested.connect(lambda text, kind: toasts.append((text, kind)))
        reply = s.home.activate("alpha")
        self.assertEqual(reply["result"], "launched")
        self.assertIn(("Opening Alpha...", "info"), toasts)
        self.assertEqual(s.home.backend.runner.started[0][0], "alpha")
        pump()
        self.assertEqual(next(t for t in s.home.tiles if t["id"] == "alpha")["pill"], "Running")
        self.assertEqual(s.home.activate("soon")["result"], "coming_soon")
        self.assertEqual(s.qml_warnings, [])

    def test_apps_page_is_off_for_a_single_app_window(self):
        s = self.open(app="heimdall", home=True)
        self.assertIsNone(s.home)
        self.assertNotIn("home", s.navigation.pages)

    def test_unknown_app_is_refused(self):
        from asgard.ui import registry
        with self.assertRaises(registry.RegistryError):
            shell.Shell(app="nope")

    # ------------------------------------------------------------ theming

    def test_accent_follows_the_page_and_settings_change_it_live(self):
        s = self.open(root=self.demo_root())
        s.prefs.mode = "light"
        s.themes.apply()
        home = s.themes.tokens.value("accent")
        s.navigation.go("odin:tasks")
        pump()
        self.assertEqual(s.themes.tokens.value("accent"), "#2B7A4B")
        self.assertEqual(s.prefs_ctl.setAccent("odin", "6b4fbf"), "")
        pump()
        self.assertEqual(s.themes.tokens.value("accent"), "#6B4FBF")
        saved = json.loads((self.dir / "settings" / "ui.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["apps"], {"odin": {"accent": "#6B4FBF"}})
        self.assertIn("must be a colour", s.prefs_ctl.setAccent("odin", "#12"))
        s.navigation.go("dashboard")
        pump()
        self.assertEqual(s.themes.tokens.value("accent"), home)
        self.assertTrue(s.prefs_ctl.resetAccent("odin"))
        self.assertEqual(s.themes._get_accents()["odin"], "#2B7A4B")
        self.assertTrue(s.prefs_ctl.setMode("dark"))
        self.assertEqual(s.themes.tokens.value("mode"), "dark")
        self.assertEqual(s.qml_warnings, [])

    def test_font_size_setting_reaches_the_tokens(self):
        s = self.open(app="heimdall")
        self.assertTrue(s.prefs_ctl.setFontSize(16))
        self.assertEqual(s.themes.tokens.value("fontSize"), 16)
        self.assertFalse(s.prefs_ctl.setFontSize(99))

    # ------------------------------------------------------------ the bridge

    def test_bridge_turns_failures_into_messages(self):
        s = self.open(root=self.demo_root())
        bridge = s.bridges["odin"]
        self.assertEqual(bridge.call("hello", ["x"]), {"value": "Hello x"})
        self.assertEqual(bridge.call("nope", []), {"error": "Set the parent issue first."})
        boom = bridge.call("boom", [])["error"]
        self.assertIn("unexpected problem (RuntimeError)", boom)
        self.assertNotIn("secret detail", boom)
        self.assertIn("isn't something", bridge.call("__init__", [])["error"])
        self.assertIn("has no", bridge.call("missing", [])["error"])

    def test_spawn_streams_output_and_reports_the_exit_code(self):
        s = self.open(root=self.demo_root())
        bridge = s.bridges["odin"]
        backend = bridge.backend()
        backend.cmd = lambda: [sys.executable, "-c", "print('heimdall: one'); print('two'); raise SystemExit(3)"]
        lines, codes = [], []
        bridge.output.connect(lines.append)
        bridge.finished.connect(codes.append)
        self.assertTrue(bridge.spawn("cmd", []))
        self.assertTrue(bridge.running)
        deadline = time.monotonic() + 20
        while not codes and time.monotonic() < deadline:
            pump(0.05)
        self.assertEqual((lines, codes, bridge.running), (["heimdall: one", "two"], [3], False))


if __name__ == "__main__":
    unittest.main()
