"""Posting the days you approved in Baldur, and the daily run end to end.

Odin posts only approved minutes, only what Jira doesn't already hold, and never the same time
twice: a refusal is offered again, a lost answer is settled against Jira by its marker, and nothing
is posted at all unless the worklog sync in the same run finished.
"""
import contextlib
import io
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_jira import FIXTURES, FakeJira, LandsThenFails, OdinTestCase  # noqa: E402

from asgard import muninn  # noqa: E402
from asgard.muninn import baldur as approvals  # noqa: E402
from asgard.muninn import odin as mo  # noqa: E402
from odin import collect, posting, store  # noqa: E402
from odin.cli import main  # noqa: E402
from odin.jira import JiraError  # noqa: E402

STARTED = "2026-10-01T15:05:00Z"
DAY = datetime(2026, 10, 1, 15, 5, tzinfo=timezone.utc).astimezone().date().isoformat()


class PostingCase(OdinTestCase):
    def setUp(self):
        super().setUp()
        self.jira = FakeJira()
        self.jira.add_issue("XYZ-5", "Retry with backoff", assignee="jdoe")
        self.con = self.open()
        self.sid = store.jira_source(self.con, "https://jira.example.gov")
        store.remember_me(self.con, self.sid, self.jira.myself())

    def approve(self, minutes=120, key="XYZ-5", day=DAY, started=STARTED, n=1):
        peek = self.peek()
        run_id = peek.execute("INSERT INTO estimate_runs (date_from, date_to, model_version, params, params_hash) "
                              "VALUES (?, ?, 'baldur-1', '{}', 'h') RETURNING id", (day, day)).fetchone()[0]
        pid = peek.execute("INSERT INTO day_proposals (estimate_run_id, local_date, work_item_key, minutes_raw, "
                           "minutes_proposed, first_started_at, basis, basis_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                           "RETURNING id", (run_id, day, key, minutes + 5.5, minutes, started,
                                            "Baldur estimate: 2 commits", f"bh{n}")).fetchone()[0]
        approvals.approve(peek, pid)
        return pid

    def prepare(self):
        """What `odin post` does before posting: key lookups and the worklog sync."""
        ctx = collect.context(self.con, self.jira, self.sid)
        got = collect.CollectResult()
        collect.lookup_keys(self.con, self.jira, ctx, got)
        collect.sync_worklogs(self.con, self.jira, ctx, 365, 500, got)
        return got

    def post(self, cap=20, prepared=True):
        settled = posting.settle_stuck(self.con, self.jira)
        if prepared:
            self.assertTrue(self.prepare().worklogs_ok)
        ctx = collect.context(self.con, self.jira, self.sid)
        with muninn.Run(self.con, store.APP, self.sid, "post-check") as run:
            return settled, posting.post_approved(self.con, self.jira, cap, run=run, ctx=ctx)

    def run_cli(self, *argv, jira=None, settings=None):
        cfg = {"jira": {"base_url": "https://jira.example.gov", "default_parent": "PROJ-123", "assign_to_me": True,
                        "log_work": True},
               "filters": {"only_ended": False}, "notify": {"desktop_alert": False}}
        if settings:
            cfg["muninn"] = settings
        path = self.data / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        buffer = io.StringIO()
        with mock.patch("odin.cli.load_token", return_value=("t", "env")), \
                mock.patch("odin.cli.JiraClient.from_config", return_value=jira or self.jira), \
                contextlib.redirect_stdout(buffer):
            code = main([argv[0], "--config", str(path), *argv[1:]])
        self.output = buffer.getvalue()
        return code

    def last_run(self):
        return json.loads((self.data / "last_run.json").read_text(encoding="utf-8"))


class ApprovedDayTests(PostingCase):
    def test_an_approved_day_is_posted_once_with_its_marker(self):
        self.approve()
        _, result = self.post()
        self.assertEqual(result.posted, ["XYZ-5 2h00m"])
        [(key, seconds, started, comment)] = self.jira.posted
        self.assertEqual((key, seconds), ("XYZ-5", 7200))
        self.assertRegex(comment, r"Approved: 2h00m on .*\n\[asgard:b-[0-9a-f]{8}\]$")
        self.assertTrue(started.startswith("2026-10-01T"))
        row = self.peek().execute("SELECT origin, state, jira_worklog_id FROM worklogs").fetchone()
        self.assertEqual(tuple(row), ("baldur", "posted", self.jira.worklogs["XYZ-5"][0]["id"]))
        _, again = self.post()
        self.assertEqual((again.posted, len(self.jira.posted)), ([], 1))

    def test_only_what_jira_doesnt_already_hold(self):
        self.approve(120)
        self.jira.add_jira_worklog("XYZ-5", 3600, "2026-10-01T16:00:00.000+0000", "logged by hand")
        _, result = self.post()
        self.assertEqual(result.posted, ["XYZ-5 1h00m"])
        self.assertIn("Already in Jira: 1h00m, logged by hand", self.jira.posted[0][3])

    def test_a_refusal_is_recorded_and_offered_again(self):
        self.approve()
        self.jira.fail("add_worklog", JiraError("POST worklog -> HTTP 400: Issue is closed", 400))
        _, result = self.post()
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(self.peek().execute("SELECT state, error FROM worklogs").fetchone()[0], "failed")
        self.assertEqual(len(mo.posts_due(self.con)), 1)
        _, again = self.post()
        self.assertEqual(len(again.posted), 1)
        self.assertEqual(len(self.jira.worklogs["XYZ-5"]), 1)

    def test_a_lost_answer_is_settled_by_its_marker_and_never_posted_twice(self):
        self.jira = LandsThenFails()
        self.jira.add_issue("XYZ-5", "Retry with backoff", assignee="jdoe")
        self.jira.lose_answers = 1
        self.approve()
        _, result = self.post()
        self.assertEqual(len(result.unknown), 1)
        self.assertEqual(mo.posts_due(self.con), [], "nothing more is offered while a post is in doubt")
        self.peek().execute("UPDATE worklogs SET created_at = '2026-10-01T00:00:00Z'")
        settled, again = self.post()
        self.assertEqual(again.posted, [])
        self.assertEqual(len(self.jira.worklogs["XYZ-5"]), 1)
        self.assertEqual(self.peek().execute("SELECT state FROM worklogs").fetchone()[0], "posted")

    def test_the_worklog_sync_alone_settles_a_lost_answer_too(self):
        self.jira = LandsThenFails()
        self.jira.add_issue("XYZ-5", "Retry with backoff", assignee="jdoe")
        self.jira.lose_answers = 1
        self.approve()
        self.post()
        self.prepare()
        self.assertEqual(self.peek().execute("SELECT state FROM worklogs").fetchone()[0], "posted")

    def test_at_most_max_posts_per_run(self):
        for n, day in enumerate(("2026-09-28", "2026-09-29", "2026-09-30"), 1):
            self.approve(60, day=day, started=f"{day}T15:00:00Z", n=n)
        _, result = self.post(cap=2)
        self.assertEqual(len(result.posted), 2)
        _, rest = self.post(cap=2)
        self.assertEqual(len(rest.posted), 1)

    def test_a_worklog_sync_cut_short_by_its_limit_doesnt_count_as_finished(self):
        """Review 2026-10-10 #1: the sync reads issues oldest first, so stopping at the limit leaves
        out the newest, which hold this week's time logged by hand."""
        for n in range(3):
            self.jira.add_issue(f"OLD-{n}", "older work", assignee="jdoe")
            self.jira.add_jira_worklog(f"OLD-{n}", 600, "2026-09-01T15:00:00.000+0000")
        self.jira.add_jira_worklog("XYZ-5", 7200, "2026-10-01T16:00:00.000+0000", "logged by hand")
        self.approve(120)
        ctx = collect.context(self.con, self.jira, self.sid)
        got = collect.CollectResult()
        collect.sync_worklogs(self.con, self.jira, ctx, 365, 1, got)
        self.assertEqual((got.worklogs_ok, got.worklogs_left), (False, True))
        code = self.run_cli("post", jira=self.jira, settings={"max_issues_per_run": 1})
        self.assertEqual(self.jira.posted, [], "Jira already holds the 2h")
        self.assertEqual(code, 0, "catching up is a warning, not a failure")

    def test_time_logged_by_hand_after_the_sync_isnt_posted_again(self):
        """Review #1: each issue's worklogs are read again just before its time is posted."""
        self.approve(120)
        self.prepare()
        self.jira.add_jira_worklog("XYZ-5", 7200, "2026-10-01T16:00:00.000+0000", "logged by hand")
        _, result = self.post(prepared=False)
        self.assertEqual((result.posted, self.jira.posted), ([], []))
        self.assertTrue(any("no longer due" in s for s in result.skipped))

    def test_an_issue_whose_worklogs_cant_be_read_isnt_posted(self):
        self.approve(120)
        self.prepare()
        self.jira.fail("issue_worklogs", JiraError("GET worklog -> HTTP 503: busy", 503))
        _, result = self.post(prepared=False)
        self.assertEqual(self.jira.posted, [])
        self.assertTrue(any("couldn't read its worklogs" in s for s in result.skipped))
        self.assertEqual(len(mo.posts_due(self.con)), 1, "still due next run")

    def test_posting_without_reading_worklogs_first_is_refused(self):
        with self.assertRaises(ValueError):
            posting.post_approved(self.con, self.jira, 20)

    def test_a_404_while_settling_an_interrupted_post_leaves_it_in_doubt(self):
        """Review #5: Jira also answers 404 when you've lost access for now; offering the time again
        could post it twice once access returns."""
        self.jira = LandsThenFails()
        self.jira.add_issue("XYZ-5", "Retry with backoff", assignee="jdoe")
        self.jira.lose_answers = 1
        self.approve()
        self.post()
        self.peek().execute("UPDATE worklogs SET created_at = '2026-10-01T00:00:00Z'")
        self.jira.fail("issue_worklogs", JiraError("GET worklog -> HTTP 404: not found", 404))
        settled = posting.settle_stuck(self.con, self.jira)
        self.assertEqual((settled.settled, settled.unsettled), (0, 1))
        self.assertEqual(self.peek().execute("SELECT state FROM worklogs").fetchone()[0], "sending")
        self.assertEqual(mo.posts_due(self.con), [])

    def test_a_dry_run_lists_and_posts_nothing(self):
        self.approve()
        self.prepare()
        result = posting.post_approved(self.con, None, 20, dry_run=True)
        self.assertEqual(result.planned, [f"XYZ-5 {DAY} 2h00m"])
        self.assertEqual(self.jira.posted, [])


class CommandTests(PostingCase):
    def test_post_needs_a_finished_worklog_sync(self):
        self.approve()
        self.jira.fail("search", JiraError("GET search -> HTTP 503: try later", 503), times=5)
        self.assertEqual(self.run_cli("post"), 1)
        self.assertEqual(self.jira.posted, [])
        self.assertIn("weren't posted", self.last_run()["first_error"])

    def test_post_posts_and_records(self):
        self.approve()
        self.assertEqual(self.run_cli("post"), 0)
        self.assertEqual(len(self.jira.posted), 1)
        self.assertEqual((self.last_run()["command"], self.last_run()["approved_posted"]), ("post", 1))

    def post_checks(self):
        return self.peek().execute("SELECT count(*) FROM sync_runs WHERE stream = 'post-check'").fetchone()[0]

    def test_a_run_with_nothing_due_leaves_no_post_check_row(self):
        """Every daily run used to add a 'post-check' sync run to Muninn, due or not."""
        for _ in range(2):
            self.assertEqual(self.run_cli("post"), 0)
        self.assertEqual(self.post_checks(), 0)
        self.approve()
        self.assertEqual(self.run_cli("post"), 0)
        self.assertEqual((self.post_checks(), len(self.jira.posted)), (1, 1))

    def test_post_dry_run(self):
        self.approve()
        self.prepare()
        self.assertEqual(self.run_cli("post", "--dry-run"), 0)
        self.assertIn("WOULD    XYZ-5", self.output)
        self.assertEqual(self.jira.posted, [])

    def test_the_daily_run_does_it_all(self):
        self.jira.add_issue("PROJ-123", "Meetings", assignee="pat")
        self.jira.add_issue("PROJ-200", "Standups", assignee="pat")
        self.jira.add_issue("ADMIN-7", "Training", assignee="pat")
        self.approve()
        code = self.run_cli("daily", "--input", str(FIXTURES / "sample_export.json"))
        self.assertEqual(code, 0, self.output)
        record = self.last_run()
        self.assertEqual((record["command"], record["exit_code"]), ("daily", 0))
        self.assertGreaterEqual(record["created"], 3)
        self.assertEqual(record["approved_posted"], 1)
        self.assertGreater(record["issues_synced"], 0)
        peek = self.peek()
        meeting_logs = peek.execute("SELECT count(*) FROM worklogs WHERE origin = 'meeting' AND state = 'posted'")
        self.assertEqual(meeting_logs.fetchone()[0], record["created"])
        mine = {r[0] for r in peek.execute("SELECT key FROM work_items WHERE is_mine = 1")}
        self.assertTrue({"XYZ-5"} <= mine)
        again = self.run_cli("daily", "--input", str(FIXTURES / "sample_export.json"))
        self.assertEqual(again, 0)
        self.assertEqual(self.last_run()["created"], 0)
        self.assertEqual(len([p for p in self.jira.posted if "[asgard:b-" in p[3]]), 1)

    def test_daily_respects_post_approved_off(self):
        self.approve()
        settings = {"post_approved": False}
        self.assertEqual(self.run_cli("daily", "--csv", str(FIXTURES / "sample_outlook.csv"), settings=settings), 0)
        self.assertEqual([p for p in self.jira.posted if "[asgard:b-" in p[3]], [])

    def test_any_failure_leaves_its_breadcrumb_and_no_traceback(self):
        """Review 2026-10-10 #4: only five kinds of error used to reach last_run.json and the alert."""
        self.run_cli("post")                                    # an "OK" to overwrite
        self.assertEqual(self.last_run()["exit_code"], 0)
        err = io.StringIO()
        with mock.patch("odin.cli.collect.sync_worklogs", side_effect=KeyError("worklogs")), \
                contextlib.redirect_stderr(err):
            code = self.run_cli("post")
        self.assertEqual(code, 2)
        self.assertEqual((self.last_run()["exit_code"], self.last_run()["consecutive_failures"]), (2, 1))
        self.assertIn("worklogs", self.last_run()["first_error"])
        self.assertIn("stopped unexpectedly", self.output)
        self.assertNotIn("Traceback", self.output + err.getvalue())

    def test_a_meeting_worklog_jira_refuses_fails_the_run(self):
        """Review #8: it used to be a warning, while a refused approved day failed the run."""
        self.jira.add_issue("PROJ-123", "Meetings", assignee="pat")
        self.jira.fail("add_worklog", JiraError("POST worklog -> HTTP 400: closed issue", 400))
        code = self.run_cli("push", "--csv", str(FIXTURES / "sample_outlook.csv"))
        self.assertEqual(code, 1)
        self.assertIn("meeting worklog refused by Jira", self.last_run()["first_error"])

    def test_the_lock_and_journal_go_with_muninn_whatever_config_is_used(self):
        """Review #7: a run with --config elsewhere used to get its own lock and journal."""
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        cfg = {"jira": {"base_url": "https://jira.example.gov", "default_parent": "PROJ-123"}}
        (elsewhere / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        with store.RunLock(self.data):                           # the scheduled run, with the usual config
            with mock.patch("odin.cli.load_token", return_value=("t", "env")), \
                    mock.patch("odin.cli.JiraClient.from_config", return_value=self.jira), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                code = main(["post", "--config", str(elsewhere / "config.json")])
        self.assertEqual(code, 2)
        self.assertIn("another odin run", out.getvalue().lower())

    def test_status_shows_what_waits(self):
        self.approve()
        self.prepare()
        self.assertEqual(self.run_cli("status"), 0)
        self.assertIn("Approved Baldur days waiting to be posted: 1", self.output)


if __name__ == "__main__":
    unittest.main()
