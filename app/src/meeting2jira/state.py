"""Local record of which meetings already have sub-tasks (sqlite3, standard library).

Position in the flow
    sync.py consults `find` before creating anything and calls `record` immediately after Jira
    accepts a create. This database is the only thing standing between a re-run and a pile of
    duplicate sub-tasks, which shapes every decision below.

Consequences of that
    * Rows are written *before* the worklog and transition are attempted, so a failure in either
      cannot cause a duplicate on the next run.
    * `find` matches on key OR content_hash, so the COM and CSV paths recognise each other's work.
    * Because re-runs are idempotent, widening the scan window is free. That is why there is no
      "pending meeting" queue anywhere in this project: the window is not a correctness mechanism.

Schema changes
    Additive migrations only, applied on open in `_migrate` and guarded by PRAGMA table_info.
    Never drop or rewrite user state. `tests/test_state.py` migrates a real v0.1.0 database to
    prove no rows are lost.

Worklog bookkeeping
    `worklog_wanted` records whether log_work was enabled at create time. Without it, every row
    ever written would look like a worklog that failed, and the first run after enabling log_work
    would backfill months of old issues.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .models import Meeting, iso_utc

_SCHEMA = """
CREATE TABLE IF NOT EXISTS synced (
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
CREATE INDEX IF NOT EXISTS ix_synced_content ON synced(content_hash);
CREATE INDEX IF NOT EXISTS ix_synced_issue ON synced(issue_key);
"""

# Additive migrations only, applied on open. Never drop or rewrite user state: this database is
# the only thing standing between a re-run and a pile of duplicate sub-tasks.
#   worklog_id       the Jira worklog id, so a retry can tell "already logged" from "never logged"
#   worklog_comment  the rendered comment, so a retry on a later run doesn't need the Meeting
#   worklog_attempts bounded retries, so a permanently rejected worklog stops eventually
#   worklog_wanted   whether log_work was on when the sub-task was created. Without this, every
#                    row ever created looks like a worklog that never landed, and the first run
#                    after enabling log_work would backfill months of old issues.
_MIGRATIONS = (
    ("worklog_id", "ALTER TABLE synced ADD COLUMN worklog_id TEXT"),
    ("worklog_comment", "ALTER TABLE synced ADD COLUMN worklog_comment TEXT"),
    ("worklog_attempts", "ALTER TABLE synced ADD COLUMN worklog_attempts INTEGER NOT NULL DEFAULT 0"),
    ("worklog_wanted", "ALTER TABLE synced ADD COLUMN worklog_wanted INTEGER NOT NULL DEFAULT 0"),
)

MAX_WORKLOG_ATTEMPTS = 3

# How long to wait for a lock held by another run before giving up with "database is locked".
LOCK_TIMEOUT_SECONDS = 5.0


class State:
    def __init__(self, path: Path, timeout: Optional[float] = None):
        """Open (and migrate) the state database.

        timeout is how long to wait for a lock held by another run. The default gives an
        overlapping run (a manual sync while the scheduled task is going) a chance to finish
        rather than failing immediately. It is read at call time, not bound as a default
        argument, so tests can lower it.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        wait = LOCK_TIMEOUT_SECONDS if timeout is None else timeout
        self.conn = sqlite3.connect(str(path), timeout=wait)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(synced)")}
        with self.conn:
            for column, ddl in _MIGRATIONS:
                if column not in existing:
                    self.conn.execute(ddl)

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *exc) -> None:
        self.conn.close()

    def find(self, m: Meeting) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM synced WHERE key = ? OR content_hash = ? LIMIT 1", (m.key, m.content_hash)
        ).fetchone()

    def record(self, m: Meeting, issue_key: str, parent: str, summary: str,
               worklog_comment: Optional[str] = None, worklog_wanted: bool = False) -> None:
        """Record a created sub-task.

        worklog_wanted records whether log_work was on at the time, stated explicitly rather than
        inferred from worklog_comment: a template can legitimately render to an empty string, and
        that should not quietly mean "no worklog was ever intended".
        """
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO synced (key, content_hash, issue_key, parent, summary, start_utc,"
                " minutes, worklog_logged, created_at, worklog_comment, worklog_attempts, worklog_wanted)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?)",
                (m.key, m.content_hash, issue_key, parent, summary, iso_utc(m.start_utc), m.minutes,
                 iso_utc(datetime.now(timezone.utc)), worklog_comment, 1 if worklog_wanted else 0),
            )

    def mark_worklog(self, key: str, worklog_id: Optional[str] = None) -> None:
        with self.conn:
            self.conn.execute("UPDATE synced SET worklog_logged = 1, worklog_id = ? WHERE key = ?",
                              (worklog_id, key))

    def note_worklog_attempt(self, key: str) -> None:
        """Count a failed attempt, so a worklog Jira keeps rejecting doesn't retry forever."""
        with self.conn:
            self.conn.execute(
                "UPDATE synced SET worklog_attempts = worklog_attempts + 1 WHERE key = ?", (key,))

    def pending_worklogs(self, limit: int = 50) -> List[sqlite3.Row]:
        """Sub-tasks whose worklog was wanted but never landed, and are still retryable.

        worklog_wanted is what keeps this to genuine failures. Selecting on worklog_logged = 0
        alone would also match every sub-task created while log_work was off, so enabling the
        setting would post worklogs against all of them.
        """
        return self.conn.execute(
            "SELECT * FROM synced WHERE worklog_wanted = 1 AND worklog_logged = 0"
            " AND worklog_attempts < ? ORDER BY start_utc LIMIT ?",
            (MAX_WORKLOG_ATTEMPTS, limit)).fetchall()

    def forget(self, issue_key: str) -> int:
        with self.conn:
            return self.conn.execute("DELETE FROM synced WHERE issue_key = ?", (issue_key,)).rowcount

    def recent(self, limit: int = 20) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM synced ORDER BY start_utc DESC LIMIT ?", (limit,)).fetchall()
