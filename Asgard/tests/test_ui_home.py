"""The home page's logic (asgard/ui/home.py), with no Qt: tiles, search, starting apps, setting up where an
app lives, and what the page is told when something goes wrong. The window itself is in test_ui_qt.py.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from asgard import catalog, launcher, paths  # noqa: E402
from asgard.runner import Finished, LaunchError  # noqa: E402
from asgard.ui import home, theme  # noqa: E402


class FakeRunner:
    def __init__(self):
        self.running = set()
        self.started = []
        self.finished = []
        self.fail = None

    def is_running(self, app_id):
        return app_id in self.running

    def running_ids(self):
        return set(self.running)

    def start(self, app_id, name, spec):
        if self.fail:
            raise LaunchError(self.fail)
        self.started.append((app_id, spec.target))
        self.running.add(app_id)

    def poll(self):
        done, self.finished = self.finished, []
        return done

    def close(self):
        pass


class HomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name).resolve()
        self._saved = os.environ.get("ASGARD_HOME")
        os.environ["ASGARD_HOME"] = str(self.dir)
        self.addCleanup(self.restore)
        script = self.dir / "here.pyw"
        script.write_text("print('hi')\n", encoding="utf-8")
        self.script = script
        self.defaults = self.dir / "apps.json"
        self.defaults.write_text(json.dumps({"version": 1, "apps": [
            {"id": "alpha", "name": "Alpha", "description": "The first app", "monogram": "Al",
             "color": "#2B5797", "status": "available", "launch": {"type": "python", "target": str(script)}},
            {"id": "beta", "name": "Beta", "description": "Lives somewhere", "monogram": "Be",
             "color": "#9A5B00", "status": "external"},
            {"id": "gone", "name": "Gone", "description": "Moved", "monogram": "Go", "color": "#5B4B9A",
             "status": "available", "launch": {"type": "python", "target": str(self.dir / "nope.pyw")}},
            {"id": "soon", "name": "Soon", "description": "Not built", "monogram": "So", "color": "#A4376D",
             "status": "coming_soon"},
        ]}), encoding="utf-8")
        self.local = self.dir / "apps.local.json"
        self.runner = FakeRunner()
        self.home = home.HomeBackend(defaults=self.defaults, local=self.local, runner=self.runner)

    def restore(self):
        if self._saved is None:
            os.environ.pop("ASGARD_HOME", None)
        else:
            os.environ["ASGARD_HOME"] = self._saved

    def tile(self, app_id, query=""):
        return next(t for t in self.home.tiles(query) if t["id"] == app_id)

    def test_tiles_say_what_state_each_app_is_in(self):
        self.assertEqual([t["id"] for t in self.home.tiles()], ["alpha", "beta", "gone", "soon"])
        self.assertEqual((self.tile("alpha")["state"], self.tile("alpha")["pill"]), ("ready", ""))
        self.assertEqual(self.tile("beta")["pill"], "Set up")
        self.assertEqual(self.tile("gone")["pill"], "Not found")
        self.assertEqual((self.tile("soon")["pill"], self.tile("soon")["canOpen"]), ("Coming soon", False))
        self.assertEqual(self.home.total(), 4)

    def test_every_tile_icon_text_is_readable_on_its_colour(self):
        for t in self.home.tiles():
            with self.subTest(app=t["id"]):
                self.assertGreaterEqual(theme.contrast(t["ink"], t["color"]), 4.5)
                self.assertRegex(t["colorTop"], r"^#[0-9A-F]{6}$")

    def test_search_matches_name_description_and_id(self):
        self.assertEqual([t["id"] for t in self.home.tiles("alp")], ["alpha"])
        self.assertEqual([t["id"] for t in self.home.tiles("lives")], ["beta"])
        self.assertEqual([t["id"] for t in self.home.tiles("  SOON ")], ["soon"])
        self.assertEqual(self.home.tiles("zzz"), [])

    def test_running_apps_are_marked_and_polled(self):
        self.assertEqual(self.home.activate("alpha")["result"], "launched")
        self.assertEqual(self.runner.started[0][0], "alpha")
        self.assertEqual((self.tile("alpha")["pill"], self.tile("alpha")["tone"]), ("Running", "success"))
        self.assertTrue(self.home.poll()["changed"])
        self.assertFalse(self.home.poll()["changed"])        # nothing moved since

    def test_a_second_window_is_asked_about_first(self):
        self.home.activate("alpha")
        again = self.home.activate("alpha")
        self.assertEqual(again["result"], "already_running")
        self.assertIn("Open another window?", again["message"])
        self.assertEqual(self.home.activate("alpha", again=True)["result"], "launched")
        self.assertEqual(len(self.runner.started), 2)

    def test_apps_that_cannot_open_say_why_and_what_to_do(self):
        self.assertEqual(self.home.activate("soon")["result"], "coming_soon")
        self.assertEqual(self.home.activate("beta")["result"], "needs_setup")
        missing = self.home.activate("gone")
        self.assertEqual(missing["result"], "missing")
        self.assertIn("nope.pyw", missing["message"])
        self.assertEqual(self.home.activate("nobody")["result"], "error")
        self.assertEqual(self.runner.started, [])

    def test_a_failed_start_is_a_sentence_not_a_traceback(self):
        self.runner.fail = "Python wasn't found."
        reply = self.home.activate("alpha")
        self.assertEqual(reply["result"], "error")
        self.assertIn("Python wasn't found.", reply["message"])

    def test_set_up_saves_the_location_and_opens_the_app(self):
        reply = self.home.set_up("beta", str(self.script))
        self.assertEqual(reply["result"], "launched")
        self.assertTrue(self.local.exists())
        self.assertEqual(self.tile("beta")["state"], "running")
        self.assertEqual(self.home.set_up("beta", "")["result"], "cancelled")

    def test_a_crash_right_after_starting_is_reported_with_its_log(self):
        log_file = self.dir / "alpha.log"
        log_file.write_text("line one\nBoom: it broke\n", encoding="utf-8")
        self.runner.finished.append(Finished("alpha", "Alpha", 1, 0.4, log_file))
        problems = self.home.poll()["problems"]
        self.assertEqual(len(problems), 1)
        self.assertIn("closed right after starting", problems[0])
        self.assertIn("Boom: it broke", problems[0])

    def test_counts_from_muninn_land_on_the_right_tile(self):
        badge = SimpleNamespace(count=3, text="worklogs waiting to post")
        self.home.badges = {"alpha": [badge, SimpleNamespace(count=0, text="nothing")]}
        t = self.tile("alpha")
        self.assertEqual((t["badge"], t["badgeHint"]), (3, "3 worklogs waiting to post"))
        self.assertEqual(self.tile("beta")["badge"], 0)

    def test_a_damaged_local_tile_file_warns_and_keeps_the_defaults(self):
        self.local.write_text("{ not json", encoding="utf-8")
        warnings = self.home.reload()
        self.assertTrue(warnings)
        self.assertEqual(len(self.home.tiles()), 4)

    def test_about_names_the_version_and_folders(self):
        text = self.home.about()
        self.assertIn("Asgard ", text)
        self.assertIn(str(paths.data_dir()), text)
        self.assertEqual(self.home.muninn_state()["state"], "starting")     # not started yet

    def test_the_shipped_tiles_all_have_a_readable_icon(self):
        shipped = home.HomeBackend(runner=FakeRunner())
        self.assertTrue(shipped.tiles())
        for t in shipped.tiles():
            with self.subTest(app=t["id"]):
                self.assertGreaterEqual(theme.contrast(t["ink"], t["color"]), 4.5)
        self.assertIn("valhalla", [t["id"] for t in shipped.tiles()])
        self.assertEqual(shipped.activate("valhalla")["result"], "uninstall")
        self.assertTrue(catalog.default_manifest_path().exists())


class LauncherRoutingTests(unittest.TestCase):
    """launcher.main picks the Qt window when it can and the tkinter one when it can't."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._cwd = os.getcwd()
        self.addCleanup(os.chdir, self._cwd)         # main() moves into Asgard's data folder
        patcher = mock.patch.dict(os.environ, {"ASGARD_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ui_available_says_why_when_qt_cannot_load(self):
        from asgard import ui
        ok, why = ui.available()
        self.assertIsInstance(ok, bool)
        self.assertEqual(bool(why), not ok)             # a reason exactly when it is not available

    def test_asgard_ui_tk_switches_the_qt_window_off(self):
        with mock.patch.dict(os.environ, {"ASGARD_UI": "tk"}):
            self.assertFalse(launcher.qt_available())

    def test_the_qt_window_is_used_when_it_opens(self):
        with mock.patch.object(launcher, "qt_available", return_value=True), \
                mock.patch.object(launcher, "run_qt", return_value=0) as run_qt, \
                mock.patch.object(launcher, "tk", None):
            self.assertEqual(launcher.main([]), 0)
        run_qt.assert_called_once()

    def test_tkinter_takes_over_when_the_qt_window_cannot_be_built(self):
        with mock.patch.object(launcher, "qt_available", return_value=True), \
                mock.patch.object(launcher, "run_qt", return_value=None), \
                mock.patch.object(launcher, "tk", None), \
                mock.patch("sys.stderr") as err:
            self.assertEqual(launcher.main([]), 2)       # no tkinter either: it says what to ask IT for
        self.assertIn("PySide6", "".join(str(c.args[0]) for c in err.write.call_args_list))

    def test_command_line_flags_never_open_a_window(self):
        with mock.patch.object(launcher, "run_qt") as run_qt, mock.patch("asgard.valhalla.main", return_value=0):
            self.assertEqual(launcher.main(["--uninstall", "--yes"]), 0)
        run_qt.assert_not_called()

    def test_run_qt_reports_a_window_that_failed_instead_of_raising(self):
        with mock.patch("asgard.ui.run", return_value=0):
            self.assertEqual(launcher.run_qt(), 0)
        with mock.patch("asgard.ui.run", return_value=1):
            self.assertIsNone(launcher.run_qt())
        with mock.patch("asgard.ui.run", side_effect=RuntimeError("boom")):
            self.assertIsNone(launcher.run_qt())
        self.assertIn("boom", (paths.log_dir() / "launcher.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
