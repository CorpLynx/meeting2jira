"""Jira into Muninn: your issues, tracked parents, keys other apps mention, and your worklogs.

Nothing here writes to Jira; these tests check what Muninn ends up holding, and that each stream
reads only what changed after its first run.
"""
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_jira import FakeJira, OdinTestCase  # noqa: E402

from asgard.muninn import odin as mo  # noqa: E402
from odin import collect, store  # noqa: E402
from odin.jira import JiraError  # noqa: E402


class CollectCase(OdinTestCase):
    def setUp(self):
        super().setUp()
        self.jira = FakeJira()
        self.con = self.open()
        self.sid = store.jira_source(self.con, "https://jira.example.gov")
        store.remember_me(self.con, self.sid, self.jira.myself())

    def ctx(self):
        return collect.context(self.con, self.jira, self.sid)

    def items(self):
        return {r["key"]: dict(r) for r in self.peek().execute("SELECT * FROM work_items")}

    def mention(self, *keys):
        """Keys Baldur proposed time for, which Muninn can't resolve yet."""
        peek = self.peek()
        run_id = peek.execute("INSERT INTO estimate_runs (date_from, date_to, model_version, params, params_hash) "
                              "VALUES ('2026-10-01', '2026-10-01', 'baldur-1', '{}', 'h') RETURNING id").fetchone()[0]
        for n, key in enumerate(keys):
            peek.execute("INSERT INTO day_proposals (estimate_run_id, local_date, work_item_key, minutes_raw, "
                         "minutes_proposed, first_started_at, basis, basis_hash) VALUES (?, ?, ?, 30.5, 30, "
                         "'2026-10-01T15:00:00Z', 'b', ?)", (run_id, f"2026-10-0{n + 1}", key, f"bh{n}"))


class IssueTests(CollectCase):
    def test_yours_and_the_tracked_parents_with_their_children(self):
        self.jira.add_issue("PROJ-1", "Mine", assignee="jdoe")
        self.jira.add_issue("PROJ-2", "Someone else's", assignee="pat")
        self.jira.add_issue("PROJ-9", "Meetings parent", assignee="pat")
        self.jira.add_issue("PROJ-10", "A meeting", parent="PROJ-9", subtask=True, issuetype="Sub-task")
        result = collect.CollectResult()
        collect.sync_issues(self.con, self.jira, self.ctx(), ["PROJ-9"], 365, 500, result)
        items = self.items()
        self.assertEqual(sorted(items), ["PROJ-1", "PROJ-10", "PROJ-9"])
        self.assertEqual({k: v["is_mine"] for k, v in items.items()}, {"PROJ-1": 1, "PROJ-10": 0, "PROJ-9": 0})
        self.assertEqual(items["PROJ-10"]["parent_key"], "PROJ-9")
        self.assertEqual(result.problems, [])

    def test_later_runs_ask_only_for_what_changed(self):
        self.jira.add_issue("PROJ-1", "Mine", assignee="jdoe")
        collect.sync_issues(self.con, self.jira, self.ctx(), [], 365, 500, collect.CollectResult())
        self.assertIn("updated >= -365d", self.jira.searches[-1])
        self.jira.issues["PROJ-1"]["fields"]["summary"] = "Renamed"
        self.jira.issues["PROJ-1"]["fields"]["updated"] = self.jira.tick()
        collect.sync_issues(self.con, self.jira, self.ctx(), [], 365, 500, collect.CollectResult())
        self.assertRegex(self.jira.searches[-1], r"updated >= '\d{4}/\d\d/\d\d \d\d:\d\d'")
        self.assertEqual(self.items()["PROJ-1"]["summary"], "Renamed")

    def test_a_first_sync_bigger_than_the_limit_carries_on_next_run(self):
        for n in range(1, 6):
            self.jira.add_issue(f"PROJ-{n}", f"Issue {n}", assignee="jdoe")
            self.jira.clock += timedelta(minutes=10)
        collect.sync_issues(self.con, self.jira, self.ctx(), [], 365, 3, collect.CollectResult())
        self.assertEqual(len(self.items()), 3)
        collect.sync_issues(self.con, self.jira, self.ctx(), [], 365, 3, collect.CollectResult())
        self.assertEqual(len(self.items()), 5)

    def test_the_limit_never_stops_inside_a_burst_of_updates(self):
        """600 issues bulk-edited in one minute would otherwise be re-read every run, forever."""
        for n in range(1, 8):
            self.jira.add_issue(f"PROJ-{n}", f"Issue {n}", assignee="jdoe")
            self.jira.clock -= timedelta(seconds=59)        # all within a couple of minutes
        self.jira.clock += timedelta(minutes=30)
        self.jira.add_issue("PROJ-8", "Much later", assignee="jdoe")
        collect.sync_issues(self.con, self.jira, self.ctx(), [], 365, 3, collect.CollectResult())
        self.assertEqual(len(self.items()), 7, "the whole burst, then a stop at the gap")
        collect.sync_issues(self.con, self.jira, self.ctx(), [], 365, 3, collect.CollectResult())
        self.assertEqual(len(self.items()), 8)

    def test_keys_other_apps_mention_are_looked_up_one_at_a_time(self):
        self.jira.add_issue("XYZ-5", "Real")
        self.jira.add_issue("NEW-1", "Moved here")
        self.jira.moved["OLD-1"] = "NEW-1"
        self.mention("XYZ-5", "XYZ-404", "OLD-1")
        result = collect.CollectResult()
        collect.lookup_keys(self.con, self.jira, self.ctx(), result)
        aliases = {r["key"]: r["status"] for r in self.peek().execute("SELECT key, status FROM work_item_aliases")}
        self.assertEqual(aliases, {"XYZ-5": "current", "NEW-1": "current", "OLD-1": "moved", "XYZ-404": "not_found"})
        self.assertEqual((result.looked_up, result.not_found), (3, 1))
        self.assertEqual(mo.unknown_keys(self.con), [], "nothing left to look up for a week")

    def test_open_issues_that_arent_yours_are_refreshed_and_a_vanished_one_is_marked(self):
        self.jira.add_issue("XYZ-5", "Open")
        self.jira.add_issue("XYZ-6", "Doomed")
        self.mention("XYZ-5", "XYZ-6")
        collect.lookup_keys(self.con, self.jira, self.ctx(), collect.CollectResult())
        self.jira.issues["XYZ-5"]["fields"]["status"] = {"name": "Done", "statusCategory": {"key": "done"}}
        self.jira.issues["XYZ-5"]["fields"]["updated"] = self.jira.tick()
        del self.jira.issues["XYZ-6"]
        result = collect.CollectResult()
        collect.refresh_open(self.con, self.jira, self.ctx(), result)
        items = self.items()
        self.assertEqual(items["XYZ-5"]["status_category"], "done")
        self.assertIsNotNone(items["XYZ-6"]["deleted_at"])
        self.assertEqual(result.problems, [])
        kinds = [r[0] for r in self.peek().execute("SELECT kind FROM events WHERE kind LIKE 'work_item.%' ORDER BY id")]
        self.assertIn("work_item.done", kinds)
        self.assertIn("work_item.deleted", kinds)

    def test_an_issue_no_run_has_seen_for_a_week_is_checked_and_only_a_404_deletes_it(self):
        self.jira.add_issue("PROJ-1", "Still there", assignee="jdoe")
        self.jira.add_issue("PROJ-2", "Gone", assignee="jdoe")
        collect.sync_issues(self.con, self.jira, self.ctx(), [], 365, 500, collect.CollectResult())
        self.peek().execute("UPDATE work_items SET last_seen_at = '2026-01-01T00:00:00Z'")
        del self.jira.issues["PROJ-2"]
        result = collect.CollectResult()
        collect.check_unseen(self.con, self.jira, self.ctx(), result)
        items = self.items()
        self.assertIsNone(items["PROJ-1"]["deleted_at"])
        self.assertGreater(items["PROJ-1"]["last_seen_at"], "2026-01-01T00:00:00Z")
        self.assertIsNotNone(items["PROJ-2"]["deleted_at"])
        self.assertEqual(result.deleted, 1)


class WorklogTests(CollectCase):
    def setUp(self):
        super().setUp()
        self.jira.add_issue("PROJ-1", "Dev work", assignee="jdoe")

    def sync(self):
        result = collect.CollectResult()
        collect.sync_worklogs(self.con, self.jira, self.ctx(), 365, 500, result)
        return result

    def stored(self):
        return [tuple(r) for r in self.peek().execute(
            "SELECT jira_worklog_id, origin, state, seconds FROM worklogs ORDER BY id")]

    def test_only_your_worklogs_are_kept(self):
        mine = self.jira.add_jira_worklog("PROJ-1", 5400, "2026-10-01T14:00:00.000+0000", "by hand")
        self.jira.add_jira_worklog("PROJ-1", 1800, "2026-10-01T16:00:00.000+0000", "pat's", author="pat")
        result = self.sync()
        self.assertTrue(result.worklogs_ok)
        self.assertEqual(self.stored(), [(mine["id"], "jira", "posted", 5400)])
        self.assertIn("PROJ-1", self.items(), "the issue comes along, so the worklog has somewhere to live")

    def test_a_worklog_removed_from_an_issue_you_still_log_on_is_marked_deleted(self):
        first = self.jira.add_jira_worklog("PROJ-1", 3600, "2026-10-01T14:00:00.000+0000")
        self.jira.add_jira_worklog("PROJ-1", 3600, "2026-10-02T14:00:00.000+0000")
        self.sync()
        self.jira.delete_worklog("PROJ-1", first["id"])
        result = self.sync()
        self.assertEqual([s for _, _, s, _ in self.stored()], ["deleted", "posted"])
        self.assertGreaterEqual(result.worklogs_removed, 1)

    def test_your_only_worklog_removed_is_caught_by_jiras_deleted_list(self):
        """The issue drops out of `worklogAuthor = currentUser()` once your only worklog goes."""
        only = self.jira.add_jira_worklog("PROJ-1", 3600, "2026-10-01T14:00:00.000+0000")
        self.sync()
        self.jira.delete_worklog("PROJ-1", only["id"])
        self.sync()
        self.assertEqual(self.stored(), [(only["id"], "jira", "deleted", 3600)])

    def test_a_post_nobody_saw_finish_is_settled_by_the_sync(self):
        collect.sync_issues(self.con, self.jira, self.ctx(), [], 365, 500, collect.CollectResult())
        post = mo.begin_manual_post(self.con, "PROJ-1", "2026-10-01T14:00:00Z", 1800, "typed in")
        self.jira.add_jira_worklog("PROJ-1", 1800, "2026-10-01T14:00:00.000+0000", post.comment)
        self.sync()
        self.assertEqual([(o, s) for _, o, s, _ in self.stored()], [("manual", "posted")])

    def test_a_failed_search_means_the_worklogs_arent_current(self):
        self.jira.fail("search", JiraError("GET search -> HTTP 503", 503))
        result = self.sync()
        self.assertFalse(result.worklogs_ok)
        self.assertTrue(result.problems)

    def test_the_first_sync_reaches_back_history_days_then_reads_only_changes(self):
        self.jira.add_jira_worklog("PROJ-1", 3600, "2026-10-01T14:00:00.000+0000")
        self.sync()
        self.assertIn("worklogDate >= -365d", self.jira.searches[-1])
        self.sync()
        self.assertRegex(self.jira.searches[-1], r"updated >= '")


if __name__ == "__main__":
    unittest.main()
