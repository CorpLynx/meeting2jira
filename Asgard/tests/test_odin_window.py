"""Odin's window backend (odin/ui_backend.py): what the Today, Meetings and My issues views read, and
the command lines it hands the window to run. Plain Python: tests/test_ui_qt.py loads the pages."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_jira import FIXTURES, FakeJira, OdinTestCase  # noqa: E402

from odin import ui_backend  # noqa: E402
from odin.config import ConfigError, build_config  # noqa: E402
from odin.sources import load_export  # noqa: E402


class WindowBackendTests(OdinTestCase):
    def setUp(self):
        super().setUp()
        self.b = ui_backend.Backend(self.data)

    def configure(self):
        (self.data / "config.json").write_text(json.dumps({"jira": {"base_url": "https://jira.example.gov/",
                                                                    "default_parent": "PROJ-123"}}), encoding="utf-8")

    def test_before_setup_it_says_what_to_do(self):
        st = self.b.state()
        self.assertTrue(st["config_missing"])
        self.assertIn("odin setup", st["config_error"])
        self.assertFalse(st["token"])
        self.assertEqual(self.b.dashboard()[0]["value"], "Set up")
        with self.assertRaisesRegex(ConfigError, "Create Odin's settings first"):
            self.b.run_command("sync")

    def test_create_settings_once(self):
        path = Path(self.b.init_config()["path"])
        self.assertEqual(path, self.data / "config.json")
        self.assertIn("jira", json.loads(path.read_text(encoding="utf-8")))
        with self.assertRaisesRegex(ConfigError, "already exists"):
            self.b.init_config()

    def test_a_token_with_spaces_is_refused_before_anything_is_stored(self):
        with self.assertRaisesRegex(ConfigError, "no spaces"):
            self.b.save_token("abc def")
        self.assertFalse((self.data / "jira_token.dpapi").exists())

    def test_state_counts_and_lists_after_a_push(self):
        self.configure()
        cfg = build_config({"jira": {"base_url": "https://jira.example.gov", "default_parent": "PROJ-123",
                                     "log_work": True}})
        jira = FakeJira()
        made = len(self.push(load_export(FIXTURES / "sample_export.json"), cfg, jira).created)
        (self.data / "last_run.json").write_text(json.dumps({"finished_utc": "2026-10-09T21:00:00Z", "exit_code": 0,
                                                            "command": "daily", "created": 4}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"JIRA_PAT": "t"}):
            st = self.b.state()
        self.assertEqual((st["config_error"], st["muninn_error"], st["token"]), ("", "", True))
        self.assertEqual(st["base_url"], "https://jira.example.gov")
        self.assertEqual(st["counts"]["subtasks"], made)
        self.assertEqual(st["last_run"]["created"], 4)
        rows = self.b.meetings()
        self.assertEqual(len(rows), made)
        self.assertEqual(rows[0]["worklog"], "posted")
        self.assertTrue(rows[0]["url"].startswith("https://jira.example.gov/browse/PROJ-"))
        mine = self.b.issues()
        self.assertEqual(len(mine), made, "assign_to_me: the meeting sub-tasks are yours")
        self.assertEqual(mine[0]["category"], "todo")
        self.assertEqual([c["label"] for c in self.b.dashboard()], ["Last run", "Approved days to post", "Assigned to me"])

    def test_muninn_not_ready_is_a_message_not_a_crash(self):
        self.configure()
        (self.home / "muninn.db").unlink()
        st = self.b.state()
        self.assertIn("Open Asgard once", st["muninn_error"])
        self.assertEqual(st["counts"], {})

    def test_the_command_lines_it_runs(self):
        self.configure()
        cfg = str(self.data / "config.json")
        sync = self.b.run_command("sync")
        self.assertEqual(sync[1:], [str(ui_backend.CLI), "sync", "--config", cfg])
        self.assertEqual(self.b.run_command("post_preview")[2:], ["post", "--config", cfg, "--dry-run"])
        with self.assertRaisesRegex(ConfigError, "isn't something"):
            self.b.run_command("rm -rf")
        if os.name == "nt":
            daily = self.b.run_command("preview")
            self.assertTrue(daily[0].lower().endswith("powershell.exe"))
            self.assertIn(str(ui_backend.SYNC_PS), daily)
            self.assertEqual(daily[-1], "-DryRun")
        else:
            with self.assertRaisesRegex(ConfigError, "needs Windows"):
                self.b.run_command("daily")

    def test_settings_files(self):
        self.assertEqual([f["label"] for f in self.b.settings_files()], ["Odin's settings", "Odin's logs"])


if __name__ == "__main__":
    unittest.main()
