"""`odin settle`: a post in doubt can be listed, checked again, or settled by a person (refinements
spec, 2).

A post is in doubt when Odin sent it and never heard whether Jira took it. Each run asks Jira again
by the post's marker, but a 404 (a deleted issue, or browse permission lost) leaves it in doubt
until Jira answers, which may be never. Settling it wrongly costs money either way: called posted
when it isn't, the time is never logged; called not posted when it is, it's logged twice. So Odin
only decides on Jira's answer, and otherwise records what the person found.
"""
import contextlib
import io
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_jira import APP, LandsThenFails  # noqa: E402
from test_odin_posting import PostingCase  # noqa: E402

from asgard import muninn  # noqa: E402
from asgard.muninn import odin as mo  # noqa: E402
from odin import store  # noqa: E402
from odin.cli import main  # noqa: E402
from odin.jira import JiraError  # noqa: E402


class SettleCase(PostingCase):
    def in_doubt(self, landed=False):
        """An approved day whose post is in doubt: sent and lost on the way back (landed), or never sent."""
        pid = self.approve()
        if landed:
            self.jira = LandsThenFails()
            self.jira.add_issue("XYZ-5", "Retry with backoff", assignee="jdoe")
            self.jira.lose_answers = 1
            self.post()
        else:
            self.prepare()
            mo.begin_post(self.con, pid)
        (row,) = self.peek().execute("SELECT id, comment FROM worklogs WHERE state = 'sending'").fetchall()
        self.pid = pid
        self.marker = mo.marker_in(row["comment"])
        return row["id"]

    def state(self, wid):
        return tuple(self.peek().execute("SELECT state, jira_worklog_id FROM worklogs WHERE id = ?", (wid,)).fetchone())


class ListTests(SettleCase):
    def test_nothing_in_doubt(self):
        self.assertEqual(self.run_cli("settle"), 0)
        self.assertIn("No posts in doubt.", self.output)

    def test_each_post_in_doubt_is_listed_with_its_marker_and_how_to_settle_it(self):
        wid = self.in_doubt()
        self.peek().execute("UPDATE worklogs SET created_at = ? WHERE id = ?", (muninn.ago(3 * 86400 + 60), wid))
        self.assertEqual(self.run_cli("settle"), 0)
        line = next(x for x in self.output.splitlines() if "XYZ-5" in x)
        self.assertRegex(line, rf"^\s*{wid}\s+XYZ-5\s+\d{{4}}-\d\d-\d\d \d\d:\d\d\s+2h00m\s+approved day\s+sent 3 days ago\s")
        self.assertIn(self.marker, line)
        for how in ("odin settle ID ", "--posted WORKLOG", "--not-posted"):
            self.assertIn(how, self.output)
        self.assertEqual(self.state(wid), ("sending", None))     # listing changes nothing

    def test_status_lists_each_with_the_command_to_settle_it(self):
        wid = self.in_doubt()
        self.assertEqual(self.run_cli("status"), 0)
        self.assertIn("Posts in doubt: 1", self.output)
        self.assertIn(f"settle: odin settle {wid}", self.output)
        self.assertIn(self.marker, self.output)


class AskJiraAgainTests(SettleCase):
    def test_found_in_jira_is_recorded_as_posted_with_jiras_id(self):
        wid = self.in_doubt(landed=True)
        jira_id = self.jira.worklogs["XYZ-5"][0]["id"]
        self.assertEqual(self.run_cli("settle", str(wid)), 0, self.output)
        self.assertEqual(self.state(wid), ("posted", str(jira_id)))
        self.assertIn("Found in Jira", self.output)
        self.assertEqual(len(self.jira.posted), 1)

    def test_not_in_jira_is_recorded_as_not_posted_and_offered_again(self):
        wid = self.in_doubt()
        self.assertEqual(self.run_cli("settle", str(wid)), 0, self.output)
        self.assertEqual(self.state(wid), ("failed", None))
        self.assertIn("Not in XYZ-5's work log", self.output)
        self.assertEqual([d["proposal_id"] for d in mo.posts_due(self.con)], [self.pid])

    def test_a_404_changes_nothing_and_says_what_to_do(self):
        wid = self.in_doubt()
        self.jira.fail("issue_worklogs", JiraError("GET worklog -> HTTP 404: not found", 404))
        self.assertEqual(self.run_cli("settle", str(wid)), 1)
        self.assertEqual(self.state(wid), ("sending", None))
        self.assertIn("nothing changed", self.output)
        self.assertIn("was deleted", self.output)
        self.assertIn(f"odin settle {wid} --not-posted", self.output)

    def test_a_refusal_or_no_connection_changes_nothing_either(self):
        wid = self.in_doubt()
        for exc, hint in ((JiraError("GET worklog -> HTTP 403: forbidden", 403), "odin check"),
                          (JiraError("GET worklog failed: connection refused"), "Try again later")):
            with self.subTest(status=exc.status):
                self.jira.fail("issue_worklogs", exc)
                self.assertEqual(self.run_cli("settle", str(wid)), 1)
                self.assertEqual(self.state(wid), ("sending", None))
                self.assertIn(hint, self.output)

    def test_a_post_without_a_marker_cant_be_looked_for(self):
        wid = self.in_doubt()
        self.peek().execute("UPDATE worklogs SET comment = 'no marker here' WHERE id = ?", (wid,))
        self.assertEqual(self.run_cli("settle", str(wid)), 1)
        self.assertIn("no marker to look for", self.output)
        self.assertEqual(self.state(wid), ("sending", None))
        self.assertEqual(self.jira.calls.count("issue_worklogs"), 0)


class PersonSettlesTests(SettleCase):
    def test_posted_records_the_id_the_person_found(self):
        wid = self.in_doubt(landed=True)
        jira_id = str(self.jira.worklogs["XYZ-5"][0]["id"])
        asked = self.jira.calls.count("issue_worklogs")
        self.assertEqual(self.run_cli("settle", str(wid), "--posted", jira_id), 0, self.output)
        self.assertEqual(self.state(wid), ("posted", jira_id))
        self.assertEqual(self.jira.calls.count("issue_worklogs"), asked)      # the person's word, not Jira's
        # The next worklog sync sees Jira's worklog as this post, not as a second one.
        self.assertTrue(self.prepare().worklogs_ok)
        self.assertEqual(self.peek().execute("SELECT count(*) FROM worklogs").fetchone()[0], 1)
        self.assertEqual(mo.posts_due(self.con), [])

    def test_posted_needs_a_number(self):
        wid = self.in_doubt()
        self.assertEqual(self.run_cli("settle", str(wid), "--posted", "abc"), 2)
        self.assertEqual(self.state(wid), ("sending", None))

    def test_an_id_another_post_already_has_is_refused(self):
        day = datetime(2026, 10, 2, 15, 5, tzinfo=timezone.utc).astimezone().date().isoformat()
        self.approve(n=2, minutes=60, started="2026-10-02T15:05:00Z", day=day)
        self.assertEqual(self.run_cli("post"), 0, self.output)
        taken = str(self.jira.worklogs["XYZ-5"][0]["id"])
        wid = self.in_doubt()
        self.assertEqual(self.run_cli("settle", str(wid), "--posted", taken), 2)
        self.assertIn("already recorded for another post", self.output)
        self.assertEqual(self.state(wid), ("sending", None))

    def test_not_posted_offers_the_time_again_and_says_it_could_post_twice(self):
        wid = self.in_doubt()
        self.assertEqual(self.run_cli("settle", str(wid), "--not-posted"), 0, self.output)
        self.assertEqual(self.state(wid), ("failed", None))
        self.assertIn("twice", self.output)
        self.assertEqual([d["proposal_id"] for d in mo.posts_due(self.con)], [self.pid])
        self.assertEqual(self.jira.calls.count("issue_worklogs"), 0)

    def test_the_help_says_it_could_post_twice(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            main(["settle", "--help"])
        self.assertIn("posted twice", re.sub(r"\s+", " ", out.getvalue()))

    def test_both_or_neither_id_is_a_usage_error(self):
        self.assertEqual(self.run_cli("settle", "--not-posted"), 2)
        self.assertIn("Say which post", self.output)
        with self.assertRaises(SystemExit):
            self.run_cli("settle", "1", "--posted", "5", "--not-posted")


class WhichPostTests(SettleCase):
    def test_an_unknown_id(self):
        self.assertEqual(self.run_cli("settle", "999"), 2)
        self.assertIn("Muninn has no worklog 999", self.output)

    def test_a_post_that_isnt_in_doubt(self):
        self.approve()
        self.assertEqual(self.run_cli("post"), 0, self.output)
        wid, jira_id = self.peek().execute("SELECT id, jira_worklog_id FROM worklogs").fetchone()
        for extra in ((), ("--not-posted",), ("--posted", "1")):
            with self.subTest(extra=extra):
                self.assertEqual(self.run_cli("settle", str(wid), *extra), 2)
                self.assertIn(f"isn't in doubt: it's posted, as Jira worklog {jira_id}", self.output)
                self.assertEqual(self.state(wid), ("posted", jira_id))

    def test_it_waits_for_a_run_that_is_going(self):
        wid = self.in_doubt()
        with store.RunLock(self.data):
            self.assertEqual(self.run_cli("settle", str(wid), "--not-posted"), 2)
        self.assertIn("Another Odin run is in progress", self.output)
        self.assertEqual(self.state(wid), ("sending", None))


class EntryPointTests(unittest.TestCase):
    def test_odin_cmd_passes_settle_through(self):
        text = (APP / "odin.cmd").read_bytes().decode("ascii")
        self.assertIn('if /i "%ACTION%"=="settle"     goto :simplecli\r\n', text)
        self.assertIn("odin settle [ID]", text)


if __name__ == "__main__":
    unittest.main()
