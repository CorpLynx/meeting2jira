"""The desktop window's standard-library half: theme layering, preferences, app discovery, and
Heimdall's backend. No Qt needed; tests/test_ui_qt.py drives the real window when PySide6 is there."""
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT, ROOT / "apps" / "heimdall"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from asgard.ui import prefs as prefs_mod  # noqa: E402
from asgard.ui import registry, theme  # noqa: E402
from heimdall import templates as tpl  # noqa: E402
from heimdall.form import FormError  # noqa: E402
from heimdall.templates import TemplateError  # noqa: E402
from heimdall.ui_backend import Backend  # noqa: E402


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name).resolve()
        self._home = os.environ.get("ASGARD_HOME")
        os.environ["ASGARD_HOME"] = str(self.dir)

    def tearDown(self):
        if self._home is None:
            os.environ.pop("ASGARD_HOME", None)
        else:
            os.environ["ASGARD_HOME"] = self._home
        self.tmp.cleanup()


class ThemeTests(unittest.TestCase):
    def test_every_token_is_defined_in_both_modes(self):
        self.assertEqual(set(theme.LIGHT), set(theme.DARK))
        for mode in theme.MODES:
            tokens, warnings = theme.resolve(mode)
            self.assertEqual(warnings, [])
            for key in theme.COLOR_TOKENS + theme.DERIVED + tuple(theme.SIZES):
                self.assertIn(key, tokens)

    def test_built_in_text_colours_meet_wcag_aa(self):
        for mode in theme.MODES:
            t, _ = theme.resolve(mode)
            for fg in ("text", "textMuted", "textDim", "accentInk", "error", "warning", "success"):
                for bg in ("surface", "background"):
                    with self.subTest(mode=mode, fg=fg, bg=bg):
                        self.assertGreaterEqual(theme.contrast(t[fg], t[bg]), 4.5)

    def test_layers_apply_in_order_and_modes_split(self):
        app = {"accent": "#111111", "dark": {"accent": "#EEEEEE"}}
        mine = {"fontSize": 15}
        mine_for_app = {"light": {"accent": "#5B4B9A"}}
        light, _ = theme.resolve("light", app, mine, mine_for_app)
        dark, _ = theme.resolve("dark", app, mine, mine_for_app)
        self.assertEqual(light["accent"], "#5B4B9A")      # the person's app layer wins in light
        self.assertEqual(dark["accent"], "#EEEEEE")       # ...and doesn't touch dark
        self.assertEqual((light["fontSize"], dark["fontSize"]), (15, 15))
        self.assertEqual(light["mode"], "light")

    def test_bad_values_warn_and_are_dropped(self):
        tokens, warnings = theme.resolve("light", ("ui.json", {
            "accent": "purple", "fontSize": 99, "sparkle": True, "accentText": "#000000",
            "dark": "nope", "_note": "comments are fine", "radiusSmall": 4}))
        self.assertEqual(tokens["accent"], theme.LIGHT["accent"])
        self.assertEqual(tokens["fontSize"], theme.SIZES["fontSize"])
        self.assertEqual(tokens["radiusSmall"], 4)
        joined = "\n".join(warnings)
        for needle in ('"accent" must be a colour', '"fontSize" must be a whole number', '"sparkle" is not',
                       '"accentText" is worked out', '"dark" must be an object'):
            self.assertIn(needle, joined)
        self.assertTrue(all(w.startswith("ui.json") for w in warnings), warnings)
        self.assertNotIn("_note", joined)

    def test_any_accent_stays_readable(self):
        rng = random.Random(7)
        accents = ["#FFFF00", "#FFFFFF", "#000000", "#777777", "#00FF00"] + [
            "#%06X" % rng.randrange(0x1000000) for _ in range(200)]
        for mode in theme.MODES:
            for accent in accents:
                t, _ = theme.resolve(mode, {"accent": accent})
                with self.subTest(mode=mode, accent=accent):
                    self.assertGreaterEqual(theme.contrast(t["accentText"], t["accent"]), 4.5)
                    self.assertGreaterEqual(theme.contrast(t["accentInk"], t["surface"]), 4.5)
                    self.assertGreaterEqual(theme.contrast(t["focus"], t["background"]), 3.0)

    def test_short_hex_and_case(self):
        t, w = theme.resolve("light", {"accent": "#abc"})
        self.assertEqual((t["accent"], w), ("#abc", []))
        self.assertEqual(theme.parse_hex("#abc"), (0xAA, 0xBB, 0xCC))


class PrefsTests(Home):
    def test_missing_file_gives_defaults(self):
        p = prefs_mod.load()
        self.assertEqual((p.mode, p.theme, p.apps, p.warnings, p.broken), ("system", {}, {}, [], None))
        self.assertEqual(p.path, self.dir / "settings" / "ui.json")

    def test_round_trip_and_clearing(self):
        p = prefs_mod.load()
        p.mode = "dark"
        p.set_token("heimdall", "accent", "#123456")
        p.set_token("odin", "accent", "#654321", mode="dark")
        p.set_token(None, "fontSize", 15)
        p.save()
        again = prefs_mod.load()
        self.assertEqual(again.mode, "dark")
        self.assertEqual(again.apps, {"heimdall": {"accent": "#123456"}, "odin": {"dark": {"accent": "#654321"}}})
        again.clear_token("odin", "accent")
        again.save()
        self.assertNotIn("odin", prefs_mod.load().apps)
        self.assertEqual(sorted(p.name for p in prefs_mod.prefs_path().parent.iterdir()), ["ui.json"])

    def test_broken_file_is_never_overwritten(self):
        path = prefs_mod.prefs_path()
        path.parent.mkdir(parents=True)
        path.write_text("{ not json", encoding="utf-8")
        p = prefs_mod.load()
        self.assertTrue(p.broken)
        self.assertIn("couldn't be used", p.warnings[0])
        p.mode = "dark"
        with self.assertRaises(prefs_mod.PrefsError):
            p.save()
        self.assertEqual(path.read_text(encoding="utf-8"), "{ not json")

    def test_bad_parts_warn_but_the_rest_loads(self):
        path = prefs_mod.prefs_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"mode": "neon", "theme": [], "apps": {"a": 1, "b": {"accent": "#000"}},
                                    "extra": 1, "_c": "x"}), encoding="utf-8")
        p = prefs_mod.load()
        self.assertEqual((p.mode, p.theme, p.apps), ("system", {}, {"b": {"accent": "#000"}}))
        self.assertEqual(len(p.warnings), 4, p.warnings)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def app(self, app_id, manifest, files=("View.qml",)):
        ui = self.root / "apps" / app_id / "ui"
        ui.mkdir(parents=True)
        for name in files:
            (ui / name).write_text("import QtQuick\nItem {}\n", encoding="utf-8")
        (ui / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return ui

    def good(self, app_id="demo", **changes):
        body = {"app": app_id, "name": "Demo", "views": [{"id": "main", "title": "Main", "qml": "View.qml"}]}
        body.update(changes)
        return body

    def test_heimdall_ships_a_valid_manifest(self):
        apps, warnings = registry.discover()
        self.assertEqual(warnings, [])
        heimdall = [a for a in apps if a.id == "heimdall"]
        self.assertEqual([v.key for v in heimdall[0].views], ["heimdall:templates", "heimdall:form"])
        self.assertTrue(all(v.qml.is_file() for v in heimdall[0].views))

    def test_discovers_apps_and_filters_one(self):
        self.app("demo", self.good())
        self.app("other", self.good("other", name="Other"))
        apps, warnings = registry.discover(self.root)
        self.assertEqual(([a.id for a in apps], warnings), (["demo", "other"], []))
        self.assertEqual(apps[0].views[0].icon, "Ma")
        only, _ = registry.discover(self.root, only="other")
        self.assertEqual([a.id for a in only], ["other"])
        none, warnings = registry.discover(self.root, only="nope")
        self.assertEqual(none, [])
        self.assertIn('No app "nope"', warnings[0])

    def test_a_broken_manifest_hides_only_that_app(self):
        self.app("demo", self.good())
        cases = {
            "wrongid": (self.good("someoneelse"), "but the folder is"),
            "noviews": (self.good("noviews", views=[]), '"views"'),
            "escape": (self.good("escape", views=[{"id": "x", "title": "X", "qml": "../../demo/ui/View.qml"}]),
                       "inside the app's ui folder"),
            "missing": (self.good("missing", views=[{"id": "x", "title": "X", "qml": "Gone.qml"}]),
                        "doesn't exist"),
            "dupe": (self.good("dupe", views=[{"id": "x", "title": "X", "qml": "View.qml"}] * 2), "two views"),
            "badbackend": (self.good("badbackend", backend="os.system"), '"backend"'),
            "badtheme": (self.good("badtheme", theme="purple"), '"theme"'),
        }
        for app_id, (body, _) in cases.items():
            self.app(app_id, body)
        apps, warnings = registry.discover(self.root)
        self.assertEqual([a.id for a in apps], ["demo"])
        for app_id, (_, needle) in cases.items():
            with self.subTest(app_id):
                self.assertTrue(any(needle in w and app_id in w for w in warnings), (needle, warnings))

    def test_backend_loads_from_the_app_folder(self):
        ui = self.app("demo", self.good(backend="demo_backend.mod:Thing"))
        pkg = ui.parent / "demo_backend"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "mod.py").write_text("class Thing:\n    def hello(self):\n        return 'hi'\n", encoding="utf-8")
        app = registry.discover(self.root)[0][0]
        try:
            self.assertEqual(app.load_backend().hello(), "hi")
        finally:
            sys.path.remove(str(app.folder))
            for name in [m for m in sys.modules if m.startswith("demo_backend")]:
                del sys.modules[name]


FORM = {"instance_url": "https://snow.example.gov", "catalog_sys_id": "0123456789abcdef0123456789abcdef",
        "fields": [{"label": "Short description", "required": True}, {"label": "Category", "kind": "select",
                                                                        "default": "Software"},
                   {"label": "I confirm", "kind": "checkbox"}]}


class HeimdallBackendTests(Home):
    def setUp(self):
        super().setUp()
        self.b = Backend()

    def write_form(self, body=FORM):
        path = self.dir / "settings" / "heimdall.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body), encoding="utf-8")

    def test_no_form_yet_then_init(self):
        st = self.b.state()
        self.assertTrue(st["form_missing"])
        self.assertIn("heimdall init", st["form_error"])
        self.assertEqual(self.b.dashboard()[0]["value"], "Set up")
        made = self.b.init_form()
        self.assertTrue(Path(made["path"]).is_file())
        self.assertTrue(self.b.state()["form"])
        with self.assertRaises(FormError):
            self.b.init_form()

    def test_save_edit_rename_delete_through_the_editor_shape(self):
        self.write_form()
        self.b.save_template("", {"name": "Monthly", "description": "d", "attachment": "",
                                  "values": {"Short description": "Scan $today", "Category": None, "I confirm": False}})
        t = self.b.template("monthly")
        rows = {r["label"]: (r["display"], r["source"]) for r in t["fields"]}
        self.assertEqual(rows, {"Short description": ("Scan $today", "template"),
                                "Category": ("Software", "default"), "I confirm": ("unchecked", "template")})
        # Rename while editing; the old name goes, nothing else is lost.
        self.b.save_template("Monthly", {"name": "Monthly v2", "values": {"Short description": "x", "Category": ""}})
        self.assertEqual([x["name"] for x in self.b.state()["templates"]], ["Monthly v2"])
        self.b.save_template("", {"name": "Other", "values": {"Short description": "y"}})
        with self.assertRaises(TemplateError):          # renaming onto another template is refused...
            self.b.save_template("Other", {"name": "monthly v2", "values": {"Short description": "z"}})
        self.assertEqual(sorted(tpl.Store().names()), ["Monthly v2", "Other"])   # ...and changes nothing
        self.b.delete_template("other")
        self.assertEqual([x["name"] for x in self.b.state()["templates"]], ["Monthly v2"])

    def test_fill_command_checks_the_plan_first(self):
        self.write_form()
        self.b.save_template("", {"name": "Bare", "values": {"Category": "Hardware"}})
        with self.assertRaises(TemplateError) as cm:
            self.b.fill_command("Bare")
        self.assertIn("Short description", str(cm.exception))
        self.b.save_template("", {"name": "Ok", "values": {"Short description": "x"}})
        argv = self.b.fill_command("Ok")
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(argv[1].endswith("cli.py"))
        self.assertEqual(argv[-3:], ["fill", "--template", "Ok"])
        self.assertTrue(any("stops before Submit" in line for line in self.b.dry_run("Ok")["lines"]))

    def test_dashboard_counts_and_flags_drift(self):
        self.write_form()
        self.b.save_template("", {"name": "A", "values": {"Short description": "x", "I confirm": True}})
        self.assertEqual([c["label"] for c in self.b.dashboard()], ["Templates", "Form fields"])
        self.write_form(dict(FORM, fields=FORM["fields"][:2]))
        cards = self.b.dashboard()
        self.assertEqual((cards[1]["label"], cards[1]["value"], cards[1]["tone"]), ("Need fixing", "1", "warning"))
        self.assertEqual(self.b.settings_files()[0]["path"], str(self.dir / "settings" / "heimdall.json"))


if __name__ == "__main__":
    unittest.main()
