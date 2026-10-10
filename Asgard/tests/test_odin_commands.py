"""Odin's setup and housekeeping commands, run through main() as odin.cmd runs them: init, set-token,
check, status and forget. Coverage showed none of them ran in a test; forget's lock and journal had
only been tested piece by piece.
"""
import contextlib
import io
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_jira import APP, FakeJira, OdinTestCase  # noqa: E402

from odin import store  # noqa: E402
from odin.cli import main  # noqa: E402
from odin.config import build_config  # noqa: E402
from odin.credstore import CredentialError  # noqa: E402
from odin.jira import JiraError  # noqa: E402
from odin.models import Meeting  # noqa: E402


def push_config():
    return build_config({"jira": {"base_url": "https://jira.example.gov", "default_parent": "PROJ-9",
                                  "assign_to_me": False}})


def one_meeting():
    start = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)
    return Meeting(source="outlook-com", key="GID-1|x", subject="Sprint Planning", start_utc=start,
                   end_utc=start + timedelta(hours=1))


class CommandCase(OdinTestCase):
    def setUp(self):
        super().setUp()
        self.jira = FakeJira()
        self.jira.add_issue("PROJ-9", "Meetings")
        self.config = self.data / "config.json"

    def write_config(self, **jira):
        body = {"base_url": "https://jira.example.gov", "default_parent": "PROJ-9", "assign_to_me": False}
        body.update(jira)
        self.config.write_text(json.dumps({"jira": body, "notify": {"desktop_alert": False}}), encoding="utf-8")

    def cli(self, *argv, getpass=None):
        out = io.StringIO()
        patches = [mock.patch("odin.cli.load_token", return_value=("secret-pat", "environment variable JIRA_PAT")),
                   mock.patch("odin.cli.JiraClient.from_config", return_value=self.jira),
                   mock.patch("odin.cli._desktop_dir", return_value=None)]
        if getpass is not None:
            patches.append(mock.patch("odin.cli.getpass.getpass", return_value=getpass))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(contextlib.redirect_stdout(out))
            code = main([argv[0], "--config", str(self.config), *argv[1:]])
        self.output = out.getvalue()
        return code


class InitTests(CommandCase):
    def test_init_copies_the_example_and_never_overwrites_without_force(self):
        self.assertEqual(self.cli("init"), 0)
        example = (APP / "config.example.json").read_text(encoding="utf-8")
        self.assertEqual(self.config.read_text(encoding="utf-8"), example)
        self.config.write_text("{}", encoding="utf-8")                 # your edits
        self.assertEqual(self.cli("init"), 0)
        self.assertEqual(self.config.read_text(encoding="utf-8"), "{}")
        self.assertIn("already exists", self.output)
        self.assertEqual(self.cli("init", "--force"), 0)
        self.assertEqual(self.config.read_text(encoding="utf-8"), example)


class SetTokenTests(CommandCase):
    def setUp(self):
        super().setUp()
        self.write_config()

    def test_the_token_is_saved_and_never_shown(self):
        with mock.patch("odin.cli.save_token", return_value=self.data / "jira_token.dpapi") as save:
            self.assertEqual(self.cli("set-token", getpass="  secret-pat  "), 0)
        save.assert_called_once_with(self.data, "secret-pat")
        self.assertIn("jira_token.dpapi", self.output)
        self.assertNotIn("secret-pat", self.output)

    def test_nothing_entered_is_refused(self):
        with mock.patch("odin.cli.save_token") as save:
            self.assertEqual(self.cli("set-token", getpass="   "), 2)
        save.assert_not_called()

    def test_off_windows_it_says_to_use_the_environment_variable(self):
        with mock.patch("odin.cli.save_token", side_effect=CredentialError("DPAPI storage is Windows-only. "
                                                                           "Elsewhere, set the JIRA_PAT environment variable.")):
            self.assertEqual(self.cli("set-token", getpass="secret-pat"), 2)
        self.assertIn("JIRA_PAT", self.output)

    def test_the_missing_token_message_names_a_command_that_exists(self):
        from odin import credstore
        with mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("JIRA_PAT", None)
            with self.assertRaises(CredentialError) as ctx:
                credstore.load_token(self.data)
        self.assertIn("odin set-token", str(ctx.exception))
        self.assertNotIn("meeting2jira", str(ctx.exception))


class CheckTests(CommandCase):
    def test_a_good_setup_passes_every_check(self):
        self.write_config()
        self.assertEqual(self.cli("check"), 0, self.output)
        for line in ("Muninn version 5", "token from environment variable JIRA_PAT", "authenticated as Jordan Doe",
                     "parent PROJ-9: Meetings", "project PROJ has sub-task type 'Sub-task'", "All checks passed."):
            self.assertIn(line, self.output)
        self.assertNotIn("secret-pat", self.output)

    def test_a_parent_that_is_a_sub_task_fails(self):
        self.jira.add_issue("PROJ-10", "A sub-task", subtask=True)
        self.write_config(default_parent="PROJ-10")
        self.assertEqual(self.cli("check"), 1)
        self.assertIn("PROJ-10 is itself a sub-task", self.output)

    def test_a_missing_parent_and_a_wrong_sub_task_type_fail(self):
        self.write_config(default_parent="PROJ-404", subtask_type="Subtask")
        self.assertEqual(self.cli("check"), 1)
        self.assertIn("[FAIL] parent PROJ-404", self.output)
        self.assertIn("project PROJ has no 'Subtask' type", self.output)

    def test_a_jira_cloud_site_fails(self):
        self.write_config()
        with mock.patch.object(self.jira, "server_info", return_value={"version": "1001.0", "deploymentType": "Cloud"}):
            self.assertEqual(self.cli("check"), 1)
        self.assertIn("Jira Data Center only", self.output)

    def test_an_unreachable_jira_stops_at_once(self):
        self.write_config()
        self.jira.fail("server_info", JiraError("GET /rest/api/2/serverInfo failed: connection refused"))
        self.assertEqual(self.cli("check"), 1)
        self.assertIn("[FAIL] server info", self.output)
        self.assertNotIn("authenticated", self.output)

    def test_muninn_not_set_up_is_a_failure_not_a_crash(self):
        self.write_config()
        (self.home / "muninn.db").unlink()
        self.assertEqual(self.cli("check"), 1)
        self.assertIn("[FAIL] Muninn", self.output)


class StatusTests(CommandCase):
    def setUp(self):
        super().setUp()
        self.write_config()

    def test_before_any_run(self):
        self.assertEqual(self.cli("status"), 0)
        self.assertIn("No run recorded yet", self.output)
        self.assertIn("No meeting sub-tasks yet", self.output)
        self.assertIn("Approved Baldur days waiting to be posted: 0", self.output)

    def test_after_a_run_it_lists_the_sub_tasks_and_warns_when_runs_stopped(self):
        self.push([one_meeting()], push_config(), self.jira)
        stale = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (self.data / "last_run.json").write_text(json.dumps(
            {"finished_utc": stale, "exit_code": 1, "command": "daily", "created": 0, "existing": 0, "skipped": 0,
             "errors": 1, "consecutive_failures": 3, "first_error": "HTTP 401"}), encoding="utf-8")
        self.assertEqual(self.cli("status"), 0)
        self.assertIn("PROJ-901", self.output)
        self.assertIn("Sprint Planning", self.output)
        self.assertIn("FAILED", self.output)
        self.assertIn("first error: HTTP 401", self.output)
        self.assertIn("the scheduled task may not be running", self.output)
        self.assertIn("3 consecutive failed runs", self.output)

    def test_muninn_not_set_up_is_reported_not_raised(self):
        (self.home / "muninn.db").unlink()
        self.assertEqual(self.cli("status"), 0)
        self.assertIn("Muninn:", self.output)


class ForgetTests(CommandCase):
    def setUp(self):
        super().setUp()
        self.write_config()
        self.meeting = one_meeting()

    def recorded(self):
        return [r[0] for r in self.peek().execute("SELECT issue_key FROM meeting_subtasks")]

    def test_forget_drops_the_record_from_muninn_and_the_journal(self):
        self.push([self.meeting], push_config(), self.jira)
        self.assertEqual(self.recorded(), ["PROJ-901"])
        journal = store.Journal(self.data)
        journal.append(store.SubtaskRecord.for_meeting(self.meeting, "PROJ-901", "PROJ-9", "s", None, False, None), "x")
        self.assertEqual(self.cli("forget", "proj-901"), 0)
        self.assertEqual(self.recorded(), [])
        self.assertEqual(journal.records(), [])
        self.assertIn("Removed 2 record(s) for proj-901", self.output)

    def test_forget_waits_for_a_run_that_is_going(self):
        """It mustn't drop a record while a run might be writing it."""
        with store.RunLock(self.data):
            self.assertEqual(self.cli("forget", "PROJ-901"), 2)
        self.assertIn("Another Odin run is in progress", self.output)


if __name__ == "__main__":
    unittest.main()
