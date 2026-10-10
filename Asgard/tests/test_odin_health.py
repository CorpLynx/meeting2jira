"""Visible health for scheduled runs (HANDOFF backlog P1-B).

A scheduled task has no window. These are the two mechanisms that make a failure noticeable without
anyone going to look: an alert file where the user cannot miss it, and a warning before the token
expires rather than a run of 401s afterwards.
"""
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "apps" / "odin"
for folder in (ROOT, APP):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from odin.cli import (ALERT_FILE, clear_alert, token_expiry_warning, write_alert)  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_jira import FIXTURES, FakeJira, OdinTestCase  # noqa: E402

from odin.config import build_config  # noqa: E402

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def token(name="automation", days_from_now=None):
    entry = {"id": 1, "name": name, "createdAt": "2026-01-01T00:00:00.000+0000"}
    if days_from_now is not None:
        expires = NOW + timedelta(days=days_from_now)
        entry["expiringAt"] = expires.strftime("%Y-%m-%dT%H:%M:%S.000+0000")
    return entry


class TokenExpiryTests(unittest.TestCase):
    def test_silent_when_the_endpoint_is_unavailable(self):
        """Older Data Center versions have no such endpoint; that is not a problem to report."""
        self.assertIsNone(token_expiry_warning(None, 14, now=NOW))
        self.assertIsNone(token_expiry_warning([], 14, now=NOW))

    def test_silent_when_disabled(self):
        self.assertIsNone(token_expiry_warning([token(days_from_now=1)], 0, now=NOW))

    def test_silent_when_expiry_is_far_off(self):
        self.assertIsNone(token_expiry_warning([token(days_from_now=90)], 14, now=NOW))

    def test_silent_for_a_token_that_never_expires(self):
        self.assertIsNone(token_expiry_warning([token()], 14, now=NOW))

    def test_warns_inside_the_window(self):
        warning = token_expiry_warning([token(days_from_now=5)], 14, now=NOW)
        self.assertIsNotNone(warning)
        self.assertIn("5 day", warning)
        self.assertIn("set-token", warning)      # says what to do, not just what is wrong

    def test_warns_on_the_day_and_after(self):
        self.assertIn("today", token_expiry_warning([token(days_from_now=0)], 14, now=NOW))
        expired = token_expiry_warning([token(days_from_now=-3)], 14, now=NOW)
        self.assertIn("expired", expired)

    def test_reports_the_soonest_of_several_and_admits_the_ambiguity(self):
        """Jira never says which token a request used, so the message must not pretend to know."""
        tokens = [token("old-ci", days_from_now=40), token("laptop", days_from_now=3)]
        warning = token_expiry_warning(tokens, 14, now=NOW)
        self.assertIn("laptop", warning)
        self.assertIn("does not reveal which token", warning)

    def test_single_token_is_stated_plainly(self):
        warning = token_expiry_warning([token(days_from_now=2)], 14, now=NOW)
        self.assertIn("your token", warning)
        self.assertNotIn("does not reveal which token", warning)

    def test_unparseable_expiry_is_ignored_not_fatal(self):
        self.assertIsNone(token_expiry_warning([{"name": "x", "expiringAt": "not a date"}], 14, now=NOW))


class AlertFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Point the Desktop lookup at a temp dir so the real Desktop is never touched by tests.
        self.desktop = self.tmp / "Desktop"
        self.desktop.mkdir()
        self.patcher = mock.patch("odin.cli._desktop_dir", return_value=self.desktop)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, **notify):
        body = {"jira": {"base_url": "https://j.example.gov", "default_parent": "P-1"}}
        if notify:
            body["notify"] = notify
        return build_config(body)

    def test_alert_is_written_and_says_what_to_do(self):
        write_alert(self.config(), self.tmp, "The last sync finished with 2 error(s).",
                    "PROJ-1: HTTP 401", consecutive_failures=1)
        body = (self.desktop / ALERT_FILE).read_text(encoding="utf-8")
        self.assertIn("HTTP 401", body)
        self.assertIn("odin check", body)
        # Reassurance matters: the user should not fear duplicates before they investigate.
        self.assertIn("re-running is safe", body)

    def test_threshold_rides_out_a_single_blip(self):
        cfg = self.config(desktop_alert=True, alert_after_failures=2)
        write_alert(cfg, self.tmp, "one-off", "transient", consecutive_failures=1)
        self.assertFalse((self.desktop / ALERT_FILE).exists())
        write_alert(cfg, self.tmp, "twice now", "still failing", consecutive_failures=2)
        self.assertTrue((self.desktop / ALERT_FILE).exists())

    def test_can_be_switched_off(self):
        write_alert(self.config(desktop_alert=False), self.tmp, "s", "d", consecutive_failures=5)
        self.assertFalse((self.desktop / ALERT_FILE).exists())

    def test_success_clears_a_stale_alert(self):
        write_alert(self.config(), self.tmp, "s", "d", consecutive_failures=1)
        self.assertTrue((self.desktop / ALERT_FILE).exists())
        clear_alert(self.tmp)
        self.assertFalse((self.desktop / ALERT_FILE).exists())

    def test_clearing_when_nothing_is_there_is_harmless(self):
        clear_alert(self.tmp)     # must not raise

    def test_falls_back_to_the_data_dir_when_the_desktop_is_unwritable(self):
        with mock.patch("odin.cli._desktop_dir", return_value=None):
            write_alert(self.config(), self.tmp, "s", "d", consecutive_failures=1)
        self.assertTrue((self.tmp / ALERT_FILE).exists())


class FailureStreakTests(OdinTestCase):
    """The streak lives in last_run.json, so `status` and the alert threshold agree on it."""

    def setUp(self):
        super().setUp()
        cfg = json.loads((APP / "config.example.json").read_text(encoding="utf-8"))
        cfg["jira"].update(base_url="https://j.example.gov", default_parent="PROJ-1")
        cfg["rules"] = []
        cfg["filters"]["only_ended"] = False
        self.cfg_path = self.data / "config.json"
        self.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    def _last_run(self):
        return json.loads((self.data / "last_run.json").read_text(encoding="utf-8"))

    def test_streak_increments_on_failure_and_resets_on_success(self):
        import contextlib
        import io
        from odin.cli import main

        argv = ["push", "--config", str(self.cfg_path), "--csv", str(FIXTURES / "sample_outlook.csv")]

        class FailingJira(FakeJira):
            def create_issue(self, fields):
                from odin.jira import JiraError
                raise JiraError("POST -> HTTP 401: token rejected", 401)

        def run_with(client):
            with mock.patch("odin.cli.load_token", return_value=("t", "env")), \
                 mock.patch("odin.cli.JiraClient.from_config", return_value=client), \
                 mock.patch("odin.cli._desktop_dir", return_value=None), \
                 contextlib.redirect_stdout(io.StringIO()):
                return main(list(argv))

        self.assertEqual(run_with(FailingJira()), 1)
        self.assertEqual(self._last_run()["consecutive_failures"], 1)
        self.assertEqual(run_with(FailingJira()), 1)
        self.assertEqual(self._last_run()["consecutive_failures"], 2)
        # An alert should be outstanding by now.
        self.assertTrue((self.data / ALERT_FILE).exists())

        self.assertEqual(run_with(FakeJira()), 0)
        self.assertEqual(self._last_run()["consecutive_failures"], 0)
        self.assertFalse((self.data / ALERT_FILE).exists())

    def test_the_old_alert_file_is_taken_down_too(self):
        old = self.data / "ATTENTION-meeting2jira.txt"
        old.write_text("from before the move", encoding="utf-8")
        with mock.patch("odin.cli._desktop_dir", return_value=None):
            clear_alert(self.data)
        self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
