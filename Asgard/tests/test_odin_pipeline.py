"""Odin's meeting push, offline: sources, routing, dedupe in Muninn, templating, the run's breadcrumb.

Each test gets its own ASGARD_HOME with a fresh Muninn (fake_jira.OdinTestCase) and an in-memory
Jira (fake_jira.FakeJira).
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_jira import APP, FIXTURES, FakeJira, OdinTestCase  # noqa: E402

from odin import store  # noqa: E402
from odin.cli import main  # noqa: E402
from odin.config import ConfigError, build_config, load_config  # noqa: E402
from odin.rules import Router  # noqa: E402
from odin.sources import load_export, load_outlook_csv  # noqa: E402

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def example_config(folder: Path, **jira_overrides) -> dict:
    """The shipped config.example.json, loaded the real way, so the example itself stays valid."""
    raw = json.loads((APP / "config.example.json").read_text(encoding="utf-8"))
    raw["jira"].update(jira_overrides)
    path = folder / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_config(path)


class PushCase(OdinTestCase):
    def recent(self):
        return store.recent(self.peek())


class PipelineTests(PushCase):
    def test_routing_filters_and_dedupe(self):
        cfg = example_config(self.data, log_work=True, transition_to="Done")
        meetings = load_export(FIXTURES / "sample_export.json")
        jira = FakeJira()
        result = self.push(meetings, cfg, jira)

        parents = sorted(f["parent"]["key"] for f in jira.created)
        self.assertEqual(parents, ["ADMIN-7", "PROJ-123", "PROJ-200", "PROJ-200"])
        self.assertEqual(len(result.created), 4)
        self.assertEqual(result.skipped["declined"], 1)
        self.assertEqual(result.skipped["all-day"], 1)
        self.assertEqual(result.skipped["cancelled"], 1)
        self.assertEqual(result.skipped["not ended yet"], 1)
        self.assertEqual(result.skipped["appointment (no attendees)"], 2)
        self.assertEqual(result.skipped["rule 'no 1:1s'"], 1)

        first = jira.created[0]
        self.assertEqual(first["issuetype"], {"name": "Sub-task"})
        self.assertEqual(first["assignee"], {"name": "jdoe"})
        # Configured labels, plus the deterministic m2j-<hash> marker that makes recovery
        # from an ambiguous create exact (jira.dedupe_label, on by default).
        self.assertEqual(first["labels"][0], "meeting")
        self.assertRegex(first["labels"][1], r"^m2j-[0-9a-f]{10}$")
        self.assertIn("Sprint Planning", first["summary"])
        self.assertEqual(len(jira.posted), 4)
        self.assertEqual(jira.posted[0][1], 3600)
        self.assertRegex(jira.posted[0][3], r"^Meeting: Sprint Planning\n\[asgard:m-[0-9a-f]{8}\]$")
        self.assertEqual(len(jira.transitions), 4)

        again = self.push(meetings, cfg, FakeJira())
        self.assertEqual(again.created, [])
        self.assertEqual(again.existing, 4)
        self.assertTrue(all(r["worklog_state"] == "posted" for r in self.recent()))

    def test_each_sub_task_is_recorded_with_its_meeting_and_issue(self):
        cfg = example_config(self.data, log_work=True)
        jira = FakeJira()
        self.push(load_export(FIXTURES / "sample_export.json"), cfg, jira)
        peek = self.peek()
        rows = peek.execute("SELECT ms.issue_key, ms.parent_key, ms.origin, e.logged_as_key, w.key, w.parent_key "
                            "FROM meeting_subtasks ms JOIN calendar_events e ON e.id = ms.calendar_event_id "
                            "JOIN work_items w ON w.key = ms.issue_key ORDER BY ms.started_at").fetchall()
        self.assertEqual(len(rows), 4)
        for issue_key, parent, origin, logged_as, item_key, item_parent in rows:
            self.assertEqual((origin, logged_as, item_key, item_parent), ("odin", issue_key, issue_key, parent))
        logs = peek.execute("SELECT origin, state, calendar_event_id IS NOT NULL FROM worklogs").fetchall()
        self.assertEqual([tuple(r) for r in logs], [("meeting", "posted", 1)] * 4)

    def test_dry_run_needs_no_client_and_writes_nothing(self):
        cfg = example_config(self.data)
        result = self.push(load_export(FIXTURES / "sample_export.json"), cfg, dry_run=True)
        self.assertEqual(result.planned, 4)
        peek = self.peek()
        for table in ("meeting_subtasks", "calendar_events", "sync_runs", "sources"):
            self.assertEqual(peek.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0, table)

    def test_max_creates_cap(self):
        cfg = example_config(self.data, max_creates_per_run=2)
        jira = FakeJira()
        result = self.push(load_export(FIXTURES / "sample_export.json"), cfg, jira)
        self.assertEqual(len(jira.created), 2)
        self.assertEqual(result.capped, 2)

    def test_bad_template_field_is_reported(self):
        cfg = example_config(self.data)
        cfg["templates"]["summary"] = "{subjct}"
        with self.assertRaises(ConfigError):
            self.push(load_export(FIXTURES / "sample_export.json"), cfg, dry_run=True)

    def test_text_exclusion_filters(self):
        """The *_contains lists are the knobs a user is expected to edit (e.g. OOO, PTO)."""
        meetings = load_export(FIXTURES / "sample_export.json")
        base = {"jira": {"base_url": "https://j.example.gov", "default_parent": "P-1"}}

        def reasons(**filters):
            cfg = build_config({"jira": base["jira"], "filters": filters})
            router = Router(cfg)
            return [router.decide(m, NOW).reason for m in meetings]

        # Matching is case-insensitive and matches anywhere in the subject.
        self.assertIn("subject contains 'standup'",
                      reasons(skip_subject_contains=["STANDUP"]))
        # Structural filters (declined, free, cancelled...) are evaluated before the text
        # filters, so both have to be off for the subject match to be the reported reason.
        self.assertIn("subject contains 'vendor demo'",
                      reasons(skip_subject_contains=["Vendor Demo"], skip_declined=False,
                              skip_free=False))
        # Whole-category exclusion, also case-insensitive.
        self.assertIn("category 'Training'", reasons(skip_categories=["training"]))
        # Location substring.
        self.assertIn("location contains 'microsoft teams'",
                      reasons(skip_location_contains=["Microsoft Teams"]))
        # An empty or whitespace-only entry must not silently match everything.
        self.assertNotIn("subject contains ''", reasons(skip_subject_contains=["", "   "]))

    def test_text_exclusion_filters_reject_bad_config(self):
        # A bare string is the likely typo: "OOO" would otherwise iterate as 'O','O','O'.
        for key in ("skip_subject_contains", "skip_categories"):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                build_config({"jira": {"base_url": "https://j.example.gov", "default_parent": "P-1"},
                              "filters": {key: "OOO"}})
        with self.assertRaises(ConfigError):
            build_config({"jira": {"base_url": "https://j.example.gov", "default_parent": "P-1"},
                          "filters": {"skip_subject_contains": ["ok", 7]}})

    def test_misconfiguration_is_caught_at_load_time(self):
        """Each of these used to surface as a Jira 400, an int()/float() error, or a traceback."""
        base = {"base_url": "https://j.example.gov", "default_parent": "PROJ-1"}
        cases = {
            "label with whitespace": {"jira": dict(base, labels=["my meeting"])},
            "empty label": {"jira": dict(base, labels=[""])},
            "unparseable cap": {"jira": dict(base, max_creates_per_run="lots")},
            # Cloud is no longer supported; a leftover setting must not be silently ignored.
            "leftover cloud auth": {"jira": dict(base, auth="basic")},
            "leftover cloud email": {"jira": dict(base, email="me@agency.gov")},
            "unparseable timeout": {"jira": dict(base, timeout_seconds="30s")},
            "blank subtask type": {"jira": dict(base, subtask_type="  ")},
            "non-string template": {"jira": base, "templates": {"summary": 42}},
            "unparseable min_minutes": {"jira": base, "filters": {"min_minutes": "five"}},
        }
        for name, cfg in cases.items():
            with self.subTest(case=name), self.assertRaises(ConfigError):
                build_config(cfg)

    def test_numeric_strings_are_still_accepted(self):
        """JSON written by hand often quotes numbers; that should keep working."""
        cfg = build_config({"jira": {"base_url": "https://j.example.gov", "default_parent": "PROJ-1",
                                     "max_creates_per_run": "40", "timeout_seconds": "30"}})
        self.assertEqual(cfg["jira"]["max_creates_per_run"], "40")

    def test_config_validation(self):
        with self.assertRaises(ConfigError):
            build_config({"jira": {"base_url": "http://jira.example.gov", "default_parent": "PROJ-1"}})
        with self.assertRaises(ConfigError):
            build_config({"jira": {"base_url": "https://jira.example.gov", "default_parent": "not-a-key"}})
        with self.assertRaises(ConfigError):
            build_config({"jira": {"base_url": "https://j.example.gov", "default_parent": "P-1"},
                          "rules": [{"match": {"subject_regex": "x"}}]})


class PrivacyTests(PushCase):
    def test_private_subjects_are_kept_out_of_the_logs(self):
        """skip_private keeps these out of scope entirely, not just out of Jira.

        Logs live in the user's profile and persist, so writing the subject there would leak
        exactly what the filter exists to protect.
        """
        cfg = example_config(self.data)
        cfg["filters"]["meetings_only"] = False     # so 'private' is the reason that fires
        cfg["filters"]["only_ended"] = False
        meetings = load_outlook_csv(FIXTURES / "sample_outlook.csv", cfg["csv"]["datetime_formats"])
        self.assertTrue(any(m.is_private and "Dentist" in m.subject for m in meetings))

        with self.assertLogs("odin.sync", level="INFO") as logged:
            result = self.push(meetings, cfg, dry_run=True)

        written = " ".join(logged.output)
        self.assertEqual(result.skipped["private"], 1)
        self.assertNotIn("Dentist", written)
        self.assertIn("subject withheld", written)
        # Non-private items are still identifiable, or a dry run would be useless.
        self.assertIn("Sprint Planning", written)

    def test_private_subjects_are_kept_out_of_muninn(self):
        cfg = example_config(self.data)
        meetings = load_outlook_csv(FIXTURES / "sample_outlook.csv", cfg["csv"]["datetime_formats"])
        self.push(meetings, cfg, FakeJira(), source="outlook-csv")
        titles = [r[0] for r in self.peek().execute("SELECT title FROM calendar_events")]
        self.assertIn(store.PRIVATE_TITLE, titles, "its time still counts for Baldur")
        self.assertFalse(any("Dentist" in t for t in titles))


class LastRunTests(OdinTestCase):
    """A hidden scheduled task must leave evidence that it ran and whether it worked."""

    def _push(self, extra_argv=(), jira=None, command="push"):
        raw = json.loads((APP / "config.example.json").read_text(encoding="utf-8"))
        raw["jira"]["base_url"] = "https://j.example.gov"
        raw["jira"]["default_parent"] = "PROJ-123"
        raw["filters"]["only_ended"] = False
        raw["notify"] = {"desktop_alert": True}
        cfg_path = self.data / "config.json"
        cfg_path.write_text(json.dumps(raw), encoding="utf-8")
        argv = [command, "--config", str(cfg_path), "--csv", str(FIXTURES / "sample_outlook.csv")]
        buffer = io.StringIO()
        with mock.patch("odin.cli.load_token", return_value=("tok", "env")), \
                mock.patch("odin.cli.JiraClient.from_config", return_value=jira or FakeJira()), \
                mock.patch("odin.cli._desktop_dir", return_value=None), contextlib.redirect_stdout(buffer):
            code = main(list(argv) + list(extra_argv))
        self.output = buffer.getvalue()
        return code

    def test_dry_run_leaves_no_breadcrumb(self):
        self.assertEqual(self._push(["--dry-run"]), 0)
        self.assertFalse((self.data / "last_run.json").exists())

    def test_push_records_the_outcome(self):
        self.assertEqual(self._push(), 0)
        recorded = json.loads((self.data / "last_run.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded["exit_code"], 0)
        self.assertEqual(recorded["command"], "push")
        self.assertEqual(recorded["created"], 1)
        self.assertIsNone(recorded["first_error"])
        self.assertTrue(recorded["finished_utc"].endswith("Z"))

    def test_a_run_that_stops_early_still_leaves_its_breadcrumb_and_alert(self):
        """An expired token used to exit 2 with nothing written: the hidden task failed silently."""
        from odin.jira import JiraError
        jira = FakeJira()
        jira.fail("myself", JiraError("GET /rest/api/2/myself -> HTTP 401: token rejected", 401))
        self.assertEqual(self._push(jira=jira), 2)
        recorded = json.loads((self.data / "last_run.json").read_text(encoding="utf-8"))
        self.assertEqual((recorded["exit_code"], recorded["consecutive_failures"]), (2, 1))
        self.assertIn("401", recorded["first_error"])
        self.assertTrue((self.data / "ATTENTION-Odin.txt").exists())
        self.assertEqual(self._push(), 0)
        self.assertFalse((self.data / "ATTENTION-Odin.txt").exists(), "a good run takes the notice down")


class ReportTests(OdinTestCase):
    def test_every_sub_task_as_a_csv_with_and_without_subjects(self):
        import csv as csvmod
        cfg = example_config(self.data, log_work=True)
        self.push(load_export(FIXTURES / "sample_export.json"), cfg, FakeJira())
        out = self.data / "meetings.csv"
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(main(["report", "--config", str(self.data / "config.json")]), 0)
        rows = list(csvmod.DictReader(open(out, encoding="utf-8-sig")))
        self.assertEqual(len(rows), 4)
        self.assertEqual({r["worklog_logged"] for r in rows}, {"1"})
        self.assertIn("Sprint Planning", rows[0]["summary"])
        self.assertIn("meeting subjects", said.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            main(["report", "--config", str(self.data / "config.json"), "--no-subjects", "--out", str(out)])
        self.assertNotIn("summary", next(csvmod.DictReader(open(out, encoding="utf-8-sig"))))


class FailureTests(OdinTestCase):
    """A broken state.db, a busy Odin or a missing Muninn must exit 2 with advice, not a traceback."""

    def setUp(self):
        super().setUp()
        cfg = {"jira": {"base_url": "https://j.example.gov", "default_parent": "PROJ-1"}}
        self.cfg_path = self.data / "config.json"
        self.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    def _push(self, dry_run=True):
        """Run push, returning (exit_code, console_output).

        stdout is captured rather than using assertLogs, because main() calls _setup_logging,
        which replaces the logger's handlers and would discard a capture handler.
        """
        buffer = io.StringIO()
        argv = ["push", "--config", str(self.cfg_path), "--csv", str(FIXTURES / "sample_outlook.csv")]
        with contextlib.redirect_stdout(buffer), \
                mock.patch("odin.cli.load_token", return_value=("tok", "env")), \
                mock.patch("odin.cli.JiraClient.from_config", return_value=FakeJira()), \
                mock.patch("odin.cli._desktop_dir", return_value=None):
            code = main(argv + (["--dry-run"] if dry_run else []))
        return code, buffer.getvalue().lower()

    def test_corrupt_state_db(self):
        (self.data / "state.db").write_bytes(b"not a sqlite database" * 20)
        code, output = self._push()
        self.assertEqual(code, 2)
        self.assertIn("corrupt", output)
        self.assertIn("state.db", output)      # tells you which file to move aside

    def test_another_run_in_progress(self):
        with store.RunLock(self.data):
            code, output = self._push(dry_run=False)
        self.assertEqual(code, 2)
        self.assertIn("another odin run", output)

    def test_muninn_not_set_up(self):
        (self.home / "muninn.db").unlink()
        code, output = self._push()
        self.assertEqual(code, 2)
        self.assertIn("open asgard once", output)


class ExportFormatTests(unittest.TestCase):
    def test_powershell_51_wrapped_arrays_are_unwrapped(self):
        doc = {"schema_version": 1, "source": "outlook-com", "meetings": {"Count": 1, "value": [
            {"key": "k", "subject": "Wrapped", "start_utc": "2026-09-21T14:00:00Z",
             "end_utc": "2026-09-21T15:00:00Z", "categories": {"Count": 1, "value": ["Training"]}}]}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wrapped.json"
            path.write_text(json.dumps(doc), encoding="utf-8-sig")   # 5.1 may also add a BOM
            meetings = load_export(path)
        self.assertEqual(len(meetings), 1)
        self.assertEqual(meetings[0].categories, ["Training"])

    def test_unknown_schema_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v9.json"
            path.write_text(json.dumps({"schema_version": 9, "meetings": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_export(path)


class CsvTests(unittest.TestCase):
    def test_outlook_csv(self):
        cfg = build_config({"jira": {"base_url": "https://j.example.gov", "default_parent": "P-1"}})
        with self.assertLogs("odin.sources", level="WARNING"):
            meetings = load_outlook_csv(FIXTURES / "sample_outlook.csv", cfg["csv"]["datetime_formats"])
        self.assertEqual(len(meetings), 3)   # the broken row is skipped with a warning

        sprint, cancelled, dentist = meetings
        self.assertEqual(sprint.start_utc, datetime(2026, 9, 21, 10, 0).astimezone(timezone.utc))
        self.assertEqual(sprint.minutes, 60)
        self.assertTrue(sprint.is_meeting and sprint.is_teams)
        self.assertEqual(sprint.busy_status, "busy")
        self.assertEqual(sprint.organizer, "Alex Kim")
        self.assertTrue(cancelled.is_cancelled)
        self.assertFalse(dentist.is_meeting)
        self.assertTrue(dentist.is_private)
        self.assertTrue(sprint.key.startswith("csv:"))


if __name__ == "__main__":
    unittest.main()
