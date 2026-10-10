"""A one-off meeting its organizer moved is the same meeting, not a new one (refinements spec, 1a).

Before this, a meeting pushed at 14:00 and then moved to 15:00 got a second sub-task, and its time
logged twice: the key and the content hash both cover the start. Exports may now say the calendar's
id for the meeting and whether it belongs to a series; a one-off meeting found by that id is
reported as moved and left alone, because Odin never edits a Jira issue.
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
from fake_jira import FakeJira, OdinTestCase  # noqa: E402

from odin import store  # noqa: E402
from odin.cli import main  # noqa: E402
from odin.config import build_config  # noqa: E402
from odin.models import Meeting  # noqa: E402
from odin.sources import load_export  # noqa: E402

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
WINDOW = (datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc), datetime(2026, 9, 23, 22, 0, tzinfo=timezone.utc))


def config():
    return build_config({"jira": {"base_url": "https://jira.example.gov", "default_parent": "PROJ-9",
                                  "assign_to_me": False, "log_work": True}})


def meeting(hour=14, minutes=60, gid="GID-1", recurring=False, key=None, subject="Design review"):
    start = datetime(2026, 9, 21, hour, 0, tzinfo=timezone.utc)
    return Meeting(source="outlook-com", key=key or f"{gid}|{start:%Y-%m-%dT%H:%M:%SZ}", subject=subject,
                   start_utc=start, end_utc=start + timedelta(minutes=minutes), global_id=gid,
                   is_recurring=recurring)


class MovedMeetingTests(OdinTestCase):
    def setUp(self):
        super().setUp()
        self.jira = FakeJira()

    def run_twice(self, first, second, window=WINDOW):
        self.push([first], config(), self.jira, now=NOW, window=window)
        with self.assertLogs("odin", "INFO") as logs:
            result = self.push(second if isinstance(second, list) else [second], config(), self.jira, now=NOW,
                               window=window)
        self.log = "\n".join(logs.output)
        return result

    def records(self):
        return [tuple(r) for r in self.peek().execute(
            "SELECT ms.issue_key, e.external_id, e.deleted_at IS NOT NULL FROM meeting_subtasks ms "
            "LEFT JOIN calendar_events e ON e.id = ms.calendar_event_id ORDER BY ms.id")]

    def test_a_moved_one_off_meeting_gets_no_second_sub_task(self):
        result = self.run_twice(meeting(14), meeting(15))
        self.assertEqual((result.created, result.moved, result.existing), ([], ["PROJ-901"], 0))
        self.assertEqual(len(self.jira.created), 1)
        self.assertEqual([w["timeSpentSeconds"] for w in self.jira.worklogs["PROJ-901"]], [3600])
        self.assertIn("MOVED", self.log)
        self.assertIn("PROJ-901", self.log)
        self.assertIn("was 2026-09-21", self.log)
        self.assertIn("left as it is", self.log)

    def test_its_record_follows_the_meeting_to_its_new_time(self):
        """The export swept the old time from the calendar, so the record points at the new one."""
        self.run_twice(meeting(14), meeting(15))
        self.assertEqual(self.records(), [("PROJ-901", "GID-1|2026-09-21T15:00:00Z", 0)])
        logged = self.peek().execute("SELECT external_id, logged_as_key FROM calendar_events ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in logged], [("GID-1|2026-09-21T14:00:00Z", "PROJ-901"),
                                                      ("GID-1|2026-09-21T15:00:00Z", "PROJ-901")])

    def test_and_it_stays_found_on_later_runs(self):
        self.run_twice(meeting(14), meeting(15))
        again = self.push([meeting(15)], config(), self.jira, now=NOW, window=WINDOW)
        self.assertEqual((again.created, again.moved, again.existing), ([], ["PROJ-901"], 0))
        self.assertEqual(len(self.jira.created), 1)

    def test_a_different_length_is_reported_and_the_worklog_left_alone(self):
        self.run_twice(meeting(14, minutes=60), meeting(15, minutes=90))
        self.assertIn("60m", self.log)
        self.assertIn("its worklog keeps the old length", self.log)
        self.assertEqual([w["timeSpentSeconds"] for w in self.jira.worklogs["PROJ-901"]], [3600])

    def test_without_a_window_the_old_time_isnt_swept_so_the_record_keeps_its_link(self):
        result = self.run_twice(meeting(14), meeting(15), window=None)
        self.assertEqual((result.created, result.moved), ([], ["PROJ-901"]))
        self.assertEqual(self.records(), [("PROJ-901", "GID-1|2026-09-21T14:00:00Z", 0)])

    def test_a_record_with_no_calendar_event_is_linked(self):
        con = self.open()
        rec = store.SubtaskRecord.for_meeting(meeting(14), "PROJ-500", "PROJ-9", "Meeting: Design review",
                                              None, False, None)
        rec.origin = "state_db"
        self.assertIsNone(store.record_subtask(con, rec, self.data))
        result = self.push([meeting(15)], config(), self.jira, now=NOW, window=WINDOW)
        self.assertEqual(result.moved, ["PROJ-500"])
        self.assertEqual(self.records(), [("PROJ-500", "GID-1|2026-09-21T15:00:00Z", 0)])

    def test_a_moved_occurrence_of_a_series_is_created_as_today(self):
        """Every occurrence shares the series' id, so the id can't say which one moved."""
        result = self.run_twice(meeting(14, recurring=True), meeting(15, recurring=True))
        self.assertEqual((result.created, result.moved), (["PROJ-902"], []))

    def test_an_export_that_doesnt_say_behaves_as_today(self):
        for n, (gid, recurring) in enumerate(((None, False), ("GID-3", None))):
            with self.subTest(global_id=gid, is_recurring=recurring):
                first = meeting(14, gid=f"GID-{n + 2}", recurring=recurring, subject=f"Review {n}")
                second = meeting(15, gid=f"GID-{n + 2}", recurring=recurring, subject=f"Review {n}")
                first.global_id = second.global_id = gid
                result = self.run_twice(first, second)
                self.assertEqual((len(result.created), result.moved), (1, []))

    def test_an_id_that_isnt_the_start_of_the_key_is_ignored(self):
        """An export that builds its keys some other way can't be matched by the id."""
        result = self.run_twice(meeting(14), meeting(15, key="OTHER|2026-09-21T15:00:00Z"))
        self.assertEqual((result.created, result.moved), (["PROJ-902"], []))

    def test_an_id_that_only_starts_the_same_is_another_meeting(self):
        result = self.run_twice(meeting(14, gid="GID-10"), meeting(15, gid="GID-1"))
        self.assertEqual((result.created, result.moved), (["PROJ-902"], []))

    def test_a_record_whose_meeting_is_still_on_the_calendar_is_another_meeting(self):
        """Two items sharing an id (a copied one can) are both pushed."""
        result = self.run_twice(meeting(14), [meeting(14), meeting(15, subject="Design review (copy)")])
        self.assertEqual((result.created, result.moved, result.existing), (["PROJ-902"], [], 1))

    def test_a_dry_run_shows_it_moved_and_changes_nothing(self):
        self.push([meeting(14)], config(), self.jira, now=NOW, window=WINDOW)
        with self.assertLogs("odin", "INFO") as logs:
            result = self.push([meeting(15)], config(), dry_run=True, now=NOW)
        self.assertEqual((result.planned, result.moved), (0, ["PROJ-901"]))
        self.assertIn("MOVED", "\n".join(logs.output))
        self.assertEqual(self.records(), [("PROJ-901", "GID-1|2026-09-21T14:00:00Z", 0)])


class ExportFieldTests(OdinTestCase):
    def export(self, **fields):
        item = {"key": "GID-1|2026-09-21T14:00:00Z", "subject": "Design review", "start_utc": "2026-09-21T14:00:00Z",
                "end_utc": "2026-09-21T15:00:00Z"}
        item.update(fields)
        path = self.data / "export.json"
        path.write_text(json.dumps({"schema_version": 1, "source": "outlook-com", "meetings": [item]}),
                        encoding="utf-8")
        (m,) = load_export(path)
        return m.global_id, m.is_recurring

    def test_both_fields_are_read(self):
        self.assertEqual(self.export(global_id=" GID-1 ", is_recurring=False), ("GID-1", False))
        self.assertEqual(self.export(global_id="GID-1", is_recurring=True), ("GID-1", True))

    def test_absent_blank_or_not_a_boolean_means_the_source_didnt_say(self):
        self.assertEqual(self.export(), (None, None))
        self.assertEqual(self.export(global_id="  ", is_recurring="false"), (None, None))
        self.assertEqual(self.export(global_id=None, is_recurring=0), (None, None))


class CommandLineTests(OdinTestCase):
    """What a person sees: the summary line, the preview, and last_run.json."""

    def setUp(self):
        super().setUp()
        self.jira = FakeJira()
        self.jira.add_issue("PROJ-9", "Meetings")
        self.config = self.data / "config.json"
        self.config.write_text(json.dumps({"jira": {"base_url": "https://jira.example.gov", "default_parent": "PROJ-9",
                                                    "assign_to_me": False},
                                           "notify": {"desktop_alert": False}}), encoding="utf-8")

    def write_export(self, hour):
        start = f"2026-09-21T{hour:02d}:00:00Z"
        path = self.data / f"export-{hour}.json"
        path.write_text(json.dumps({
            "schema_version": 1, "source": "outlook-com", "range_start": "2026-09-21T04:00:00Z",
            "range_end": "2026-09-23T22:00:00Z",
            "meetings": [{"key": f"GID-1|{start}", "subject": "Design review", "start_utc": start,
                          "end_utc": f"2026-09-21T{hour + 1:02d}:00:00Z", "is_meeting": True, "response": "accepted",
                          "busy_status": "busy", "global_id": "GID-1", "is_recurring": False}]}), encoding="utf-8")
        return path

    def cli(self, *argv):
        out = io.StringIO()
        with mock.patch("odin.cli.load_token", return_value=("secret-pat", "environment variable JIRA_PAT")), \
                mock.patch("odin.cli.JiraClient.from_config", return_value=self.jira), \
                mock.patch("odin.cli._desktop_dir", return_value=None), \
                contextlib.redirect_stdout(out):
            code = main([argv[0], "--config", str(self.config), *argv[1:]])
        self.output = out.getvalue()
        return code

    def test_the_summary_preview_and_last_run_report_it(self):
        self.assertEqual(self.cli("push", "--input", str(self.write_export(14))), 0, self.output)
        self.assertEqual(self.cli("push", "--input", str(self.write_export(15)), "--dry-run"), 0, self.output)
        self.assertIn("1 meeting(s) moved since their sub-task was made", self.output)
        self.assertEqual(self.cli("push", "--input", str(self.write_export(15))), 0, self.output)
        self.assertIn("Moved since their sub-task was made, left as they are: PROJ-901", self.output)
        last = json.loads((self.data / "last_run.json").read_text(encoding="utf-8"))
        self.assertEqual((last["created"], last["moved"]), (0, 1))
        self.assertEqual(len(self.jira.created), 1)
        self.assertEqual(self.cli("status"), 0)
        self.assertIn("1 meeting(s) moved since their sub-task was made", self.output)


if __name__ == "__main__":
    unittest.main()
