"""Offline tests: sources, routing, dedupe, templating. Run: py -3 -m unittest discover -s tests -v"""
import contextlib
import io
import json
import sqlite3
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from meeting2jira.__main__ import main
from meeting2jira.config import ConfigError, build_config, load_config
from meeting2jira.rules import Router
from meeting2jira.sources import load_export, load_outlook_csv
from meeting2jira.state import State
from meeting2jira.sync import run

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


class FakeJira:
    def __init__(self):
        self.created, self.worklogs, self.transitions = [], [], []

    def myself(self):
        return {"name": "jdoe", "displayName": "Jordan Doe"}

    def personal_access_tokens(self):
        return None      # mirrors a Jira that does not expose token expiry

    def create_issue(self, fields):
        self.created.append(fields)
        return f"{fields['project']['key']}-{900 + len(self.created)}"

    def add_worklog(self, key, seconds, started, comment):
        self.worklogs.append((key, seconds, started, comment))

    def transition(self, key, name):
        self.transitions.append((key, name))
        return True


def example_config(tmp: Path, **jira_overrides) -> dict:
    """The shipped config.example.json, loaded the real way, so the example itself stays valid."""
    raw = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    raw["jira"].update(jira_overrides)
    path = tmp / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_config(path)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_routing_filters_and_dedupe(self):
        cfg = example_config(self.tmp, log_work=True, transition_to="Done")
        meetings = load_export(FIXTURES / "sample_export.json")
        jira = FakeJira()
        with State(self.tmp / "state.db") as state:
            result = run(meetings, cfg, state, jira, now=NOW)

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
            self.assertEqual(len(jira.worklogs), 4)
            self.assertEqual(jira.worklogs[0][1], 3600)
            self.assertEqual(len(jira.transitions), 4)

            again = run(meetings, cfg, state, FakeJira(), now=NOW)
            self.assertEqual(again.created, [])
            self.assertEqual(again.existing, 4)
            self.assertTrue(all(r["worklog_logged"] for r in state.recent()))

    def test_dry_run_needs_no_client_and_writes_nothing(self):
        cfg = example_config(self.tmp)
        with State(self.tmp / "state.db") as state:
            result = run(load_export(FIXTURES / "sample_export.json"), cfg, state, None, dry_run=True, now=NOW)
            self.assertEqual(result.planned, 4)
            self.assertEqual(state.recent(), [])

    def test_max_creates_cap(self):
        cfg = example_config(self.tmp, max_creates_per_run=2)
        jira = FakeJira()
        with State(self.tmp / "state.db") as state:
            result = run(load_export(FIXTURES / "sample_export.json"), cfg, state, jira, now=NOW)
        self.assertEqual(len(jira.created), 2)
        self.assertEqual(result.capped, 2)

    def test_bad_template_field_is_reported(self):
        cfg = example_config(self.tmp)
        cfg["templates"]["summary"] = "{subjct}"
        with State(self.tmp / "state.db") as state, self.assertRaises(ConfigError):
            run(load_export(FIXTURES / "sample_export.json"), cfg, state, None, dry_run=True, now=NOW)

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


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_private_subjects_are_kept_out_of_the_logs(self):
        """skip_private keeps these out of scope entirely, not just out of Jira.

        Logs live in the user's profile and persist, so writing the subject there would leak
        exactly what the filter exists to protect.
        """
        cfg = example_config(self.tmp)
        cfg["filters"]["meetings_only"] = False     # so 'private' is the reason that fires
        cfg["filters"]["only_ended"] = False
        meetings = load_outlook_csv(FIXTURES / "sample_outlook.csv", cfg["csv"]["datetime_formats"])
        self.assertTrue(any(m.is_private and "Dentist" in m.subject for m in meetings))

        with State(self.tmp / "state.db") as state, \
                self.assertLogs("meeting2jira.sync", level="INFO") as logged:
            result = run(meetings, cfg, state, None, dry_run=True, now=NOW)

        written = " ".join(logged.output)
        self.assertEqual(result.skipped["private"], 1)
        self.assertNotIn("Dentist", written)
        self.assertIn("subject withheld", written)
        # Non-private items are still identifiable, or a dry run would be useless.
        self.assertIn("Sprint Planning", written)


class LastRunTests(unittest.TestCase):
    """A hidden scheduled task must leave evidence that it ran and whether it worked."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _push(self, extra_argv=()):
        raw = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        raw["jira"]["base_url"] = "https://j.example.gov"
        raw["jira"]["default_parent"] = "PROJ-123"
        raw["filters"]["only_ended"] = False
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text(json.dumps(raw), encoding="utf-8")
        argv = ["push", "--config", str(cfg_path), "--csv", str(FIXTURES / "sample_outlook.csv")]
        return main(list(argv) + list(extra_argv))

    def test_dry_run_leaves_no_breadcrumb(self):
        self.assertEqual(self._push(["--dry-run"]), 0)
        self.assertFalse((self.tmp / "last_run.json").exists())

    def test_push_records_the_outcome(self):
        with mock.patch("meeting2jira.__main__.load_token", return_value=("tok", "env")), \
             mock.patch("meeting2jira.__main__.JiraClient.from_config", return_value=FakeJira()):
            self.assertEqual(self._push(), 0)
        recorded = json.loads((self.tmp / "last_run.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded["exit_code"], 0)
        self.assertEqual(recorded["created"], 1)
        self.assertIsNone(recorded["first_error"])
        self.assertTrue(recorded["finished_utc"].endswith("Z"))


class StateFailureTests(unittest.TestCase):
    """A broken or busy state database must exit 2 with advice, not raise a traceback."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        cfg = {"jira": {"base_url": "https://j.example.gov", "default_parent": "PROJ-1"}}
        self.cfg_path = self.tmp / "config.json"
        self.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _push(self):
        """Run push, returning (exit_code, console_output).

        stdout is captured rather than using assertLogs, because main() calls _setup_logging,
        which replaces the logger's handlers and would discard a capture handler.
        """
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["push", "--config", str(self.cfg_path),
                         "--csv", str(FIXTURES / "sample_outlook.csv"), "--dry-run"])
        return code, buffer.getvalue().lower()

    def test_corrupt_state_db(self):
        (self.tmp / "state.db").write_bytes(b"not a sqlite database" * 20)
        code, output = self._push()
        self.assertEqual(code, 2)
        self.assertIn("corrupt", output)
        self.assertIn("state.db", output)      # tells you which file to move aside

    def test_locked_state_db(self):
        conn = sqlite3.connect(str(self.tmp / "state.db"))
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS lockme (x)")
            conn.execute("BEGIN EXCLUSIVE")
            # Don't wait out the real lock timeout just to assert the message.
            with mock.patch("meeting2jira.state.LOCK_TIMEOUT_SECONDS", 0.05):
                code, output = self._push()
            self.assertEqual(code, 2)
            self.assertIn("another meeting2jira run", output)
        finally:
            conn.close()


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
        with self.assertLogs("meeting2jira.sources", level="WARNING"):
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
