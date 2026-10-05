"""State schema migrations and worklog retry bookkeeping.

The state database is the only thing preventing duplicate sub-tasks on a re-run, so these
tests care as much about *not losing rows* as about the new behavior.
"""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from meeting2jira.jira import JiraError
from meeting2jira.models import Meeting
from meeting2jira.state import MAX_WORKLOG_ATTEMPTS, State
from meeting2jira.sync import RunResult, retry_pending_worklogs

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


def meeting(key="GID-1|2026-09-21T14:00:00Z", subject="Sprint Planning", minutes=60):
    start = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)
    return Meeting(source="outlook-com", key=key, subject=subject, start_utc=start,
                   end_utc=start.replace(hour=14 + minutes // 60, minute=minutes % 60))


class FakeWorklogJira:
    """Fails the first N add_worklog calls, then succeeds."""

    def __init__(self, fail_times=0, worklog_id="10501"):
        self.fail_times = fail_times
        self.worklog_id = worklog_id
        self.calls = []

    def add_worklog(self, key, seconds, started, comment):
        self.calls.append((key, seconds, started, comment))
        if len(self.calls) <= self.fail_times:
            raise JiraError("worklog rejected", 500)
        return self.worklog_id


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "state.db"

    def test_migrates_a_v0_1_0_database_without_losing_rows(self):
        conn = sqlite3.connect(str(self.db))
        conn.executescript(V0_1_0_SCHEMA)
        conn.execute(
            "INSERT INTO synced VALUES ('GID-OLD|x', 'hash-old', 'PROJ-1', 'PROJ-9', 'Old meeting',"
            " '2026-09-01T14:00:00Z', 30, 0, '2026-09-01T15:00:00Z')")
        conn.commit()
        conn.close()

        with State(self.db) as state:
            columns = {r["name"] for r in state.conn.execute("PRAGMA table_info(synced)")}
            self.assertLessEqual({"worklog_id", "worklog_comment", "worklog_attempts"}, columns)
            rows = state.recent()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["issue_key"], "PROJ-1")
            self.assertEqual(rows[0]["worklog_attempts"], 0)
            # Deliberately NOT a retry candidate. A v0.1.0 row carries no record of whether
            # log_work was on, so the safe reading is "no worklog was wanted". Treating it as a
            # pending failure would post worklogs against every historical sub-task on first run.
            self.assertEqual(rows[0]["worklog_wanted"], 0)
            self.assertEqual(state.pending_worklogs(), [])

    def test_migration_is_idempotent(self):
        for _ in range(3):
            with State(self.db) as state:
                state.conn.execute("SELECT worklog_id, worklog_comment, worklog_attempts FROM synced")


class WorklogRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "state.db"

    def test_failed_worklog_is_retried_once_and_not_duplicated(self):
        m = meeting()
        with State(self.db) as state:
            state.record(m, "PROJ-501", "PROJ-9", "Meeting: Sprint Planning", "Meeting: Sprint Planning",
                         worklog_wanted=True)
            state.note_worklog_attempt(m.key)          # first attempt failed during create
            self.assertEqual([r["key"] for r in state.pending_worklogs()], [m.key])

            jira = FakeWorklogJira()
            result = RunResult()
            retry_pending_worklogs(state, jira, result)

            self.assertEqual(result.worklogs_retried, 1)
            self.assertEqual(len(jira.calls), 1)
            key, seconds, _started, comment = jira.calls[0]
            self.assertEqual(key, "PROJ-501")
            self.assertEqual(seconds, 3600)            # the meeting's real duration
            self.assertEqual(comment, "Meeting: Sprint Planning")

            row = state.recent()[0]
            self.assertEqual(row["worklog_logged"], 1)
            self.assertEqual(row["worklog_id"], "10501")

            # Nothing left to retry, so a later run must not log the time a second time.
            self.assertEqual(state.pending_worklogs(), [])
            retry_pending_worklogs(state, jira, RunResult())
            self.assertEqual(len(jira.calls), 1)

    def test_retries_are_bounded(self):
        m = meeting()
        with State(self.db) as state:
            state.record(m, "PROJ-502", "PROJ-9", "summary", "comment", worklog_wanted=True)
            jira = FakeWorklogJira(fail_times=99)
            for _ in range(MAX_WORKLOG_ATTEMPTS + 2):
                retry_pending_worklogs(state, jira, RunResult())
            self.assertEqual(len(jira.calls), MAX_WORKLOG_ATTEMPTS)
            self.assertEqual(state.pending_worklogs(), [])
            self.assertEqual(state.recent()[0]["worklog_logged"], 0)

    def test_enabling_log_work_does_not_backfill_old_subtasks(self):
        """Turning log_work on must not retroactively log time against every past sub-task.

        Rows created while log_work was off have no worklog, which is correct and finished, not
        a failure waiting to be retried. Treating them as pending would silently post worklogs
        against months of old issues the first time the setting changed.
        """
        with State(self.db) as state:
            # Three sub-tasks created back when log_work was off: no comment stored.
            for i in range(3):
                m = meeting(key="GID-OLD-%d|x" % i, subject="Old meeting %d" % i)
                state.record(m, "PROJ-%d" % (600 + i), "PROJ-9", "Meeting: old %d" % i,
                             worklog_comment=None)

            self.assertEqual(state.pending_worklogs(), [])

            # A sub-task created with log_work on, whose worklog then failed, IS retryable.
            wanted = meeting(key="GID-NEW|x", subject="New meeting")
            state.record(wanted, "PROJ-700", "PROJ-9", "Meeting: new", worklog_comment="Meeting: new",
                         worklog_wanted=True)
            state.note_worklog_attempt(wanted.key)
            self.assertEqual([r["key"] for r in state.pending_worklogs()], [wanted.key])

    def test_retry_falls_back_to_summary_when_no_comment_stored(self):
        """A worklog was wanted but the comment template rendered empty: use the summary."""
        m = meeting()
        with State(self.db) as state:
            state.record(m, "PROJ-503", "PROJ-9", "Meeting: Sprint Planning", worklog_comment=None,
                         worklog_wanted=True)
            jira = FakeWorklogJira()
            retry_pending_worklogs(state, jira, RunResult())
        self.assertEqual(jira.calls[0][3], "Meeting: Sprint Planning")


if __name__ == "__main__":
    unittest.main()
