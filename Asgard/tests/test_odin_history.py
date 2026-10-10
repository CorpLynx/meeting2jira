"""state.db moves into Muninn once, and nothing it records is pushed or logged again.

state.db was the only thing preventing duplicate sub-tasks before Muninn, so these tests care as
much about not losing a row as about the import itself.
"""
import contextlib
import io
import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_jira import FIXTURES, FakeJira, OdinTestCase  # noqa: E402

from odin import collect, history, posting, store  # noqa: E402
from odin.cli import main  # noqa: E402
from odin.config import build_config  # noqa: E402
from odin.models import Meeting  # noqa: E402
from odin.sources import load_export  # noqa: E402

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)

# Exactly the v0.1.0 schema, before worklog_id / worklog_comment / worklog_attempts existed.
V0_1_0_SCHEMA = """
CREATE TABLE synced (
    key            TEXT PRIMARY KEY,
    content_hash   TEXT NOT NULL,
    issue_key      TEXT NOT NULL,
    parent         TEXT NOT NULL,
    summary        TEXT NOT NULL,
    start_utc      TEXT NOT NULL,
    minutes        INTEGER NOT NULL,
    worklog_logged INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);
"""
LATER_COLUMNS = ("ALTER TABLE synced ADD COLUMN worklog_id TEXT",
                 "ALTER TABLE synced ADD COLUMN worklog_comment TEXT",
                 "ALTER TABLE synced ADD COLUMN worklog_attempts INTEGER NOT NULL DEFAULT 0",
                 "ALTER TABLE synced ADD COLUMN worklog_wanted INTEGER NOT NULL DEFAULT 0")


def sprint():
    return [m for m in load_export(FIXTURES / "sample_export.json") if m.key.startswith("GID-SPRINT|")][0]


class HistoryCase(OdinTestCase):
    def state_db(self, rows, v0_1_0=False):
        con = sqlite3.connect(str(self.data / "state.db"))
        try:
            con.executescript(V0_1_0_SCHEMA)
            if not v0_1_0:
                for ddl in LATER_COLUMNS:
                    con.execute(ddl)
            for row in rows:
                cols = ", ".join(row)
                con.execute(f"INSERT INTO synced ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
            con.commit()
        finally:
            con.close()

    def row(self, m=None, issue_key="PROJ-501", created_at=None, **extra):
        m = m or sprint()
        made = created_at or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = {"key": m.key, "content_hash": m.content_hash, "issue_key": issue_key, "parent": "PROJ-9",
               "summary": "Meeting: Sprint Planning", "start_utc": "2026-09-21T14:00:00Z", "minutes": 60,
               "worklog_logged": 0, "created_at": made}
        row.update(extra)
        return row

    def subtasks(self):
        return [dict(r) for r in self.peek().execute("SELECT * FROM meeting_subtasks ORDER BY id")]


class ImportTests(HistoryCase):
    def test_a_v0_1_0_database_moves_in_without_losing_rows(self):
        self.state_db([self.row()], v0_1_0=True)
        result = history.import_state_db(self.open(), self.data, NOW)
        self.assertEqual((result.imported, result.failed), (1, []))
        [row] = self.subtasks()
        self.assertEqual((row["issue_key"], row["origin"], row["calendar_event_id"]), ("PROJ-501", "state_db", None))
        # Deliberately not owed: a v0.1.0 row carries no record of whether log_work was on, so the
        # safe reading is "no worklog was wanted". Reading it as a failure would post worklogs
        # against every historical sub-task on the first run.
        self.assertEqual(row["worklog_wanted"], 0)
        self.assertEqual(result.renamed_to.name, "state.db.migrated-20260924")
        self.assertFalse((self.data / "state.db").exists())

    def test_the_import_runs_once_and_can_run_again_after_a_failure(self):
        self.state_db([self.row()])
        history.import_state_db(self.open(), self.data, NOW)
        self.state_db([self.row(), self.row(Meeting("outlook-com", "GID-2|x", "Other", NOW, NOW + timedelta(hours=1)),
                                            issue_key="PROJ-502")])
        again = history.import_state_db(self.open(), self.data, NOW)
        self.assertEqual((again.imported, again.already), (1, 1))
        self.assertEqual(again.renamed_to.name, "state.db.migrated-20260924-2")

    def test_only_a_worklog_still_owed_is_carried(self):
        owed = self.row(worklog_wanted=1, worklog_comment="Meeting: Sprint Planning", worklog_attempts=1)
        logged = self.row(Meeting("outlook-com", "GID-L|x", "Logged", NOW, NOW + timedelta(hours=1)),
                          issue_key="PROJ-502", worklog_wanted=1, worklog_logged=1, worklog_id="7")
        spent = self.row(Meeting("outlook-com", "GID-S|x", "Spent", NOW, NOW + timedelta(hours=1)),
                         issue_key="PROJ-503", worklog_wanted=1, worklog_attempts=3)
        self.state_db([owed, logged, spent])
        result = history.import_state_db(self.open(), self.data, NOW)
        self.assertEqual({r["issue_key"]: r["worklog_wanted"] for r in self.subtasks()},
                         {"PROJ-501": 1, "PROJ-502": 0, "PROJ-503": 0})
        self.assertEqual(result.owed, ["PROJ-501 2026-09-21 60m"])

    def test_an_owed_worklog_is_logged_once_by_the_next_run(self):
        self.state_db([self.row(worklog_wanted=1, worklog_comment="Meeting: Sprint Planning")])
        jira = FakeJira()
        jira.add_issue("PROJ-501", "Meeting: Sprint Planning", parent="PROJ-9", subtask=True)
        con = self.open()
        history.import_state_db(con, self.data, NOW)
        sid = store.jira_source(con, "https://jira.example.gov")
        store.remember_me(con, sid, jira.myself())
        ctx = collect.context(con, jira, sid)
        result = posting.PostResult()
        retry = lambda run, key, c: collect.collect_issue(run, key, c, jira)  # noqa: E731
        posting.retry_meetings(con, jira, sid, ctx, retry, result)
        self.assertEqual(len(result.posted), 1)
        self.assertEqual([(w["timeSpentSeconds"], w["comment"].split("\n")[0]) for w in jira.worklogs["PROJ-501"]],
                         [(3600, "Meeting: Sprint Planning")])
        posting.retry_meetings(con, jira, sid, ctx, retry, posting.PostResult())
        self.assertEqual(len(jira.worklogs["PROJ-501"]), 1)

    def test_an_owed_worklog_that_landed_after_all_isnt_sent_again(self):
        """The old code retried after an ambiguous failure; one of those may have landed."""
        self.state_db([self.row(worklog_wanted=1, worklog_comment="Meeting: Sprint Planning", worklog_attempts=1)])
        jira = FakeJira()
        jira.add_issue("PROJ-501", "Meeting: Sprint Planning", parent="PROJ-9", subtask=True)
        jira.add_jira_worklog("PROJ-501", 3600, "2026-09-21T14:00:00.000+0000", "Meeting: Sprint Planning")
        con = self.open()
        history.import_state_db(con, self.data, NOW)
        sid = store.jira_source(con, "https://jira.example.gov")
        store.remember_me(con, sid, jira.myself())
        ctx = collect.context(con, jira, sid)
        result = posting.PostResult()
        posting.retry_meetings(con, jira, sid, ctx, lambda run, key, c: collect.collect_issue(run, key, c, jira),
                               result)
        self.assertEqual((result.posted, len(jira.worklogs["PROJ-501"])), ([], 1))

    def test_a_row_muninn_refuses_keeps_state_db_and_its_meeting_still_isnt_pushed(self):
        self.state_db([self.row(issue_key="not a key")])
        result = history.import_state_db(self.open(), self.data, NOW)
        self.assertEqual(len(result.failed), 1)
        self.assertIsNone(result.renamed_to)
        self.assertTrue((self.data / "state.db").exists())
        legacy = history.Legacy.open(self.data)
        jira = FakeJira()
        pushed = self.push([sprint()], build_config({"jira": {"base_url": "https://jira.example.gov",
                                                              "default_parent": "PROJ-9"}}),
                           jira, seen_before=legacy.find)
        self.assertEqual((pushed.existing, jira.created), (1, []))

    def test_forget_reaches_a_state_db_that_hasnt_moved_in(self):
        self.state_db([self.row()])
        self.assertEqual(history.forget_legacy(self.data, "proj-501"), 1)
        self.assertEqual(len(history.Legacy.open(self.data)), 0)

    def test_migrated_copies_go_after_thirty_days(self):
        (self.data / "state.db.migrated-20260801").write_bytes(b"x")
        (self.data / "state.db.migrated-20260801-2").write_bytes(b"x")
        (self.data / "state.db.migrated-20260920").write_bytes(b"x")
        (self.data / "state.db.migrated-notadate").write_bytes(b"x")
        removed = history.prune_migrated(self.data, NOW)
        self.assertEqual(sorted(p.name for p in removed), ["state.db.migrated-20260801", "state.db.migrated-20260801-2"])
        self.assertTrue((self.data / "state.db.migrated-20260920").exists())


class ThroughTheCommandTests(HistoryCase):
    def _run(self, *extra, jira=None):
        cfg = {"jira": {"base_url": "https://jira.example.gov", "default_parent": "PROJ-9", "assign_to_me": False},
               "notify": {"desktop_alert": False}}
        path = self.data / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        buffer = io.StringIO()
        with mock.patch("odin.cli.load_token", return_value=("t", "env")), \
                mock.patch("odin.cli.JiraClient.from_config", return_value=jira or FakeJira()), \
                contextlib.redirect_stdout(buffer):
            code = main(["push", "--config", str(path), "--input", str(FIXTURES / "sample_export.json"), *extra])
        return code, buffer.getvalue()

    def test_a_preview_reads_state_db_and_moves_nothing(self):
        self.state_db([self.row()])
        code, output = self._run("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("state.db holds 1 sub-task record(s)", output)
        self.assertIn("EXISTS   PROJ-501", output)
        self.assertTrue((self.data / "state.db").exists())
        self.assertEqual(self.subtasks(), [])

    def test_the_first_real_run_moves_it_in_before_creating_anything(self):
        self.state_db([self.row()])
        jira = FakeJira()
        code, output = self._run(jira=jira)
        self.assertEqual(code, 0)
        self.assertIn("EXISTS   PROJ-501", output)
        self.assertNotIn("Sprint Planning", " ".join(f["summary"] for f in jira.created))
        self.assertFalse((self.data / "state.db").exists())
        self.assertEqual(len(list(self.data.glob("state.db.migrated-*"))), 1)


if __name__ == "__main__":
    unittest.main()
