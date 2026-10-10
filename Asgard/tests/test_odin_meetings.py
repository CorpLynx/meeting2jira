"""Odin on Muninn: the calendar, the sub-task record, and meeting time logged once.

The worklog cases are the ones that would cost someone money if they went wrong: a refusal, an
answer lost on the way back, a retry after time was logged by hand. In every one Jira must end up
holding the meeting's time exactly once, or not at all; never twice.
"""
import os
import sqlite3
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_jira import FIXTURES, FakeJira, LandsThenFails, OdinTestCase  # noqa: E402

from odin import collect, posting, store  # noqa: E402
from odin.config import build_config  # noqa: E402
from odin.jira import JiraError  # noqa: E402
from odin.models import Meeting  # noqa: E402
from odin.sources import load_export, load_outlook_csv  # noqa: E402

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
WINDOW = (datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc), datetime(2026, 9, 23, 22, 0, tzinfo=timezone.utc))


def config(**jira):
    body = {"base_url": "https://jira.example.gov", "default_parent": "PROJ-9", "assign_to_me": False,
            "log_work": True}
    body.update(jira)
    return build_config({"jira": body})


def meeting(key="GID-1|2026-09-21T14:00:00Z", subject="Sprint Planning", minutes=60, day=21, hour=14):
    start = datetime(2026, 9, day, hour, 0, tzinfo=timezone.utc)
    return Meeting(source="outlook-com", key=key, subject=subject, start_utc=start,
                   end_utc=start + timedelta(minutes=minutes))


class MeetingCase(OdinTestCase):
    def retry(self, jira):
        """The start of the next run: settle posts nobody saw finish, then retry meeting worklogs."""
        con = self.open()
        sid = store.jira_source(con, "https://jira.example.gov")
        store.remember_me(con, sid, jira.myself())
        ctx = collect.context(con, jira, sid)
        settled = posting.settle_stuck(con, jira)
        result = posting.PostResult()
        posting.retry_meetings(con, jira, sid, ctx, lambda run, key, c: collect.collect_issue(run, key, c, jira),
                               result)
        return settled, result

    def worklogs(self):
        return [tuple(r) for r in self.peek().execute(
            "SELECT origin, state, seconds FROM worklogs ORDER BY id").fetchall()]

    def in_jira(self, jira, key):
        return [(w["timeSpentSeconds"], w["comment"].split("\n")[0]) for w in jira.worklogs[key]]


class CalendarTests(MeetingCase):
    def test_every_item_is_stored_for_baldur_not_only_the_pushed_ones(self):
        meetings = load_export(FIXTURES / "sample_export.json")
        self.push(meetings, config(), FakeJira(), window=WINDOW)
        rows = {r["external_id"]: r for r in self.peek().execute("SELECT * FROM calendar_events")}
        self.assertEqual(len(rows), len(meetings))
        vendor = rows["GID-VENDOR|2026-09-22T17:00:00Z"]
        self.assertEqual((vendor["response"], vendor["show_as"]), ("declined", "free"))
        self.assertEqual(rows["GID-DOCTOR|2026-09-22T19:00:00Z"]["title"], store.PRIVATE_TITLE)
        self.assertEqual(rows["GID-HOLIDAY|2026-09-21T04:00:00Z"]["is_all_day"], 1)

    def test_a_full_export_marks_items_it_no_longer_has_as_deleted(self):
        meetings = load_export(FIXTURES / "sample_export.json")
        self.push(meetings, config(), FakeJira(), window=WINDOW)
        gone = "GID-ONEONE|2026-09-23T18:00:00Z"
        self.push([m for m in meetings if m.key != gone], config(), FakeJira(), window=WINDOW)
        deleted = [r[0] for r in self.peek().execute("SELECT external_id FROM calendar_events WHERE deleted_at IS NOT NULL")]
        self.assertEqual(deleted, [gone])
        self.assertTrue(self.calendar.swept)

    def test_a_csv_export_has_no_window_so_it_never_deletes(self):
        cfg = config()
        meetings = load_outlook_csv(FIXTURES / "sample_outlook.csv", cfg["csv"]["datetime_formats"])
        self.push(meetings, cfg, FakeJira(), source="outlook-csv")
        self.push(meetings[:1], cfg, FakeJira(), source="outlook-csv")
        self.assertEqual(self.peek().execute("SELECT count(*) FROM calendar_events WHERE deleted_at IS NOT NULL")
                         .fetchone()[0], 0)

    def test_an_export_that_stopped_early_has_no_window(self):
        self.assertIsNone(store.export_window({"range_start": "2026-09-21T04:00:00Z",
                                               "range_end": "2026-09-23T22:00:00Z", "truncated": True}))
        self.assertIsNone(store.export_window({"range_start": "2026-09-21T04:00:00Z"}))
        self.assertEqual(store.export_window({"range_start": "2026-09-21T04:00:00Z",
                                              "range_end": "2026-09-23T22:00:00Z"}), WINDOW)

    def test_each_export_path_is_its_own_source(self):
        con = self.open()
        self.assertNotEqual(store.calendar_source(con, "outlook-com"), store.calendar_source(con, "graph-msal"))
        self.assertEqual(store.calendar_source(con, "Outlook-COM"), store.calendar_source(con, "outlook-com"))
        names = [r[0] for r in self.peek().execute("SELECT name FROM sources WHERE kind = 'calendar'")]
        self.assertEqual(sorted(names), ["calendar:graph-msal", "calendar:outlook-com"])
        self.assertEqual(store.calendar_source(con, "../../etc"), store.calendar_source(con, "unknown"))


class RecordTests(MeetingCase):
    def test_the_outlook_and_csv_paths_recognise_each_others_work(self):
        com = meeting()
        csv = meeting(key="csv:" + com.content_hash)
        jira = FakeJira()
        self.push([com], config(), jira, now=NOW)
        again = self.push([csv], config(), jira, now=NOW, source="outlook-csv")
        self.assertEqual((len(jira.created), again.existing), (1, 1))

    def test_a_meeting_longer_than_a_day_is_skipped_before_anything_is_created(self):
        """Muninn can't record it, so creating it would re-create it on every run."""
        jira = FakeJira()
        result = self.push([meeting(minutes=1500)], build_config({"jira": {
            "base_url": "https://jira.example.gov", "default_parent": "PROJ-9"}, "filters": {"max_minutes": 0}}),
            jira, now=datetime(2026, 9, 30, tzinfo=timezone.utc))
        self.assertEqual(jira.created, [])
        self.assertEqual(result.skipped["over 24 hours"], 1)

    def test_a_record_muninn_refuses_goes_to_the_journal_and_the_run_stops_creating(self):
        jira = FakeJira()
        first, second = meeting(), meeting(key="GID-2|x", subject="Design review", hour=16)
        with mock.patch.object(store, "_insert", side_effect=sqlite3.OperationalError("database is locked")):
            result = self.push([first, second], config(), jira, now=NOW)
        self.assertEqual(len(jira.created), 1, "nothing more is created once a record can't be kept")
        self.assertIn("unrecorded.jsonl", result.errors[0])
        journal = store.Journal(self.data)
        self.assertEqual([r.issue_key for r in journal.records()], ["PROJ-901"])

        preview = self.push([first, second], config(), dry_run=True, now=NOW)
        self.assertEqual((preview.existing, preview.planned), (1, 1), "a preview consults the journal too")

        self.assertEqual(journal.replay(self.open()), 1)
        self.assertFalse(journal.path.exists())
        again = self.push([first, second], config(), jira, now=NOW)
        self.assertEqual((again.existing, len(again.created)), (1, 1))
        self.assertEqual(len(jira.created), 2, "the first meeting is never created twice")

    def test_a_half_written_journal_line_doesnt_hide_the_others(self):
        journal = store.Journal(self.data)
        journal.append(store.SubtaskRecord.for_meeting(meeting(), "PROJ-5", "PROJ-9", "s", None, False, None), "x")
        with open(journal.path, "a", encoding="utf-8") as fh:
            fh.write('{"meeting_key": "half')
        self.assertEqual([r.issue_key for r in journal.records()], ["PROJ-5"])

    def test_forget_lets_a_meeting_be_pushed_again_without_logging_its_time_twice(self):
        jira = FakeJira()
        self.push([meeting()], config(), jira, now=NOW)
        self.assertEqual(store.forget(self.open(), "proj-901"), 1)
        self.assertIsNone(self.peek().execute("SELECT logged_as_key FROM calendar_events").fetchone()[0])
        again = self.push([meeting()], config(), jira, now=NOW)
        self.assertEqual(again.created, ["PROJ-902"])
        self.assertEqual(len(jira.posted), 1, "the meeting's time is already in Jira")
        self.assertTrue(any("already logged" in w for w in again.warnings))

    def test_history_without_a_calendar_event_is_linked_when_the_meeting_shows_up(self):
        con = self.open()
        m = meeting()
        rec = store.SubtaskRecord.for_meeting(m, "PROJ-500", "PROJ-9", "Meeting: Sprint Planning", None, False, None)
        rec.origin = "state_db"
        self.assertTrue(store.record_subtask(con, rec, self.data))
        result = self.push([m], config(), FakeJira(), now=NOW)
        self.assertEqual(result.existing, 1)
        row = self.peek().execute("SELECT ms.calendar_event_id IS NOT NULL, e.logged_as_key FROM meeting_subtasks ms "
                                  "LEFT JOIN calendar_events e ON e.id = ms.calendar_event_id").fetchone()
        self.assertEqual(tuple(row), (1, "PROJ-500"))


class LockTests(OdinTestCase):
    def test_one_run_at_a_time(self):
        with store.RunLock(self.data):
            with self.assertRaisesRegex(store.StoreError, "Another Odin run is in progress"):
                with store.RunLock(self.data):
                    pass
        with store.RunLock(self.data):
            pass                                # released on the way out

    def test_a_lock_left_by_a_run_that_died_is_taken_over(self):
        lock = self.data / store.LOCK
        lock.write_text("4242 2026-09-24T00:00:00Z\n", encoding="utf-8")
        old = time.time() - store.LOCK_STALE_SECONDS - 60
        os.utime(lock, (old, old))
        with store.RunLock(self.data):
            self.assertIn(str(os.getpid()), lock.read_text(encoding="utf-8"))
        self.assertFalse(lock.exists())


class MeetingWorklogTests(MeetingCase):
    def test_logged_once_with_a_marker_on_its_calendar_event(self):
        jira = FakeJira()
        self.push([meeting()], config(), jira, now=NOW)
        self.assertEqual(self.in_jira(jira, "PROJ-901"), [(3600, "Meeting: Sprint Planning")])
        self.assertRegex(jira.posted[0][3], r"\[asgard:m-[0-9a-f]{8}\]$")
        self.assertEqual(self.worklogs(), [("meeting", "posted", 3600)])
        self.retry(jira)
        self.push([meeting()], config(), jira, now=NOW)
        self.assertEqual(len(jira.posted), 1)

    def test_a_refusal_is_retried_on_the_next_run_and_lands_once(self):
        jira = FakeJira()
        jira.fail("add_worklog", JiraError("POST worklog -> HTTP 400: closed issue", 400))
        result = self.push([meeting()], config(), jira, now=NOW)
        self.assertTrue(any("closed issue" in w for w in result.warnings))
        self.assertEqual(self.worklogs(), [("meeting", "failed", 3600)])
        _, retried = self.retry(jira)
        self.assertEqual(len(retried.posted), 1)
        self.assertEqual(self.in_jira(jira, "PROJ-901"), [(3600, "Meeting: Sprint Planning")])
        _, again = self.retry(jira)
        self.assertEqual((again.posted, len(jira.posted)), ([], 1))

    def test_retries_stop_after_three_refusals(self):
        jira = FakeJira()
        jira.fail("add_worklog", JiraError("POST worklog -> HTTP 400: no", 400), times=10)
        self.push([meeting()], config(), jira, now=NOW)
        for _ in range(4):
            self.retry(jira)
        self.assertEqual(jira.calls.count("add_worklog"), store.MAX_WORKLOG_ATTEMPTS)
        self.assertEqual(store.pending_meeting_worklogs(self.peek()), [])

    def test_an_answer_lost_after_jira_logged_it_is_settled_not_sent_again(self):
        jira = LandsThenFails()
        jira.lose_answers = 1
        result = self.push([meeting()], config(), jira, now=NOW)
        self.assertTrue(any("checked against Jira on the next run" in w for w in result.warnings))
        self.assertEqual(self.worklogs(), [("meeting", "sending", 3600)])
        self.peek().execute("UPDATE worklogs SET created_at = '2026-09-24T00:00:00Z'")   # older than the timeout
        settled, retried = self.retry(jira)
        self.assertEqual((settled.settled, retried.posted), (1, []))
        self.assertEqual(self.worklogs(), [("meeting", "posted", 3600)])
        self.assertEqual(len(jira.worklogs["PROJ-901"]), 1, "Jira holds the meeting's time once")

    def test_an_answer_lost_before_jira_logged_it_is_sent_again_once(self):
        jira = FakeJira()
        jira.fail("add_worklog", JiraError("POST worklog failed: connection reset", ambiguous=True))
        self.push([meeting()], config(), jira, now=NOW)
        self.peek().execute("UPDATE worklogs SET created_at = '2026-09-24T00:00:00Z'")
        settled, retried = self.retry(jira)
        self.assertEqual(settled.settled, 1)
        self.assertEqual(len(retried.posted), 1)
        self.assertEqual(len(jira.worklogs["PROJ-901"]), 1)

    def test_a_server_error_is_never_taken_as_a_refusal(self):
        """A 500 on a POST may have been applied: the row waits to be checked, it isn't offered again."""
        jira = FakeJira()
        jira.fail("add_worklog", JiraError("POST worklog -> HTTP 500: oops", 500))
        self.push([meeting()], config(), jira, now=NOW)
        self.assertEqual(self.worklogs(), [("meeting", "sending", 3600)])
        _, retried = self.retry(jira)        # too new to settle, and not pending while it is sending
        self.assertEqual(retried.posted, [])

    def test_time_logged_by_hand_on_the_sub_task_counts(self):
        jira = FakeJira()
        jira.fail("add_worklog", JiraError("POST worklog -> HTTP 403: not now", 403))
        self.push([meeting()], config(), jira, now=NOW)
        jira.add_jira_worklog("PROJ-901", 3600, "2026-09-21T14:00:00.000+0000", "logged it myself")
        _, retried = self.retry(jira)
        self.assertEqual(retried.posted, [])
        self.assertEqual(len(jira.worklogs["PROJ-901"]), 1)

    def test_turning_log_work_on_later_doesnt_post_old_meetings(self):
        jira = FakeJira()
        self.push([meeting(key=f"GID-OLD-{i}|x", subject=f"Old {i}", hour=10 + i) for i in range(3)],
                  config(log_work=False), jira, now=NOW)
        self.assertEqual(store.pending_meeting_worklogs(self.peek()), [])
        self.retry(jira)
        self.assertEqual(jira.posted, [])

    def test_a_sub_task_jira_lost_isnt_retried_forever(self):
        jira = FakeJira()
        jira.fail("add_worklog", JiraError("POST worklog -> HTTP 400: no", 400))
        self.push([meeting()], config(), jira, now=NOW)
        later = datetime.now(timezone.utc) + timedelta(days=store.RETRY_DAYS + 1)
        self.assertEqual(store.pending_meeting_worklogs(self.peek(), now=later), [])


if __name__ == "__main__":
    unittest.main()
