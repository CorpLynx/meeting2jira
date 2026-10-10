"""Odin's history from state.db, moved into Muninn once.

Before Muninn, Odin kept which meetings had sub-tasks in its own SQLite file, state.db, in one table
(`synced`). That record is what stops a re-run from creating every past meeting again, so it moves
into Muninn's meeting_subtasks before Odin creates anything:

  * Each row becomes a meeting_subtasks row with origin 'state_db' and the time it was made.
    Rows Muninn already has (the same calendar key) are left as they are, so the import can run
    again after a failure.
  * A worklog still owed (wanted, never logged, fewer than three failed attempts) is carried as
    worklog_wanted = 1; every other row as 0, so time already in Jira is never sent again. Owed
    worklogs are retried like any other for 14 days after their sub-task was made.
  * Then state.db is renamed state.db.migrated-YYYYMMDD, kept for 30 days in case you need to look
    at it, and deleted after that.
  * If any row can't be imported, state.db stays where it is and Odin keeps consulting it, so the
    meeting it records still isn't pushed again; the run's summary names the rows.

A dry run reads state.db without importing it (Legacy), so a preview never shows old meetings as new.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from asgard import muninn

from . import store
from .models import Meeting, iso_utc, parse_utc

log = logging.getLogger(__name__)

LEGACY = "state.db"
KEEP_DAYS = 30
MAX_WORKLOG_ATTEMPTS = 3
_MIGRATED_RE = re.compile(r"^state\.db\.migrated-(\d{8})(?:-\d+)?$")
_HASH_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class ImportResult:
    imported: int = 0
    already: int = 0
    owed: List[str] = field(default_factory=list)       # worklogs carried for a retry
    failed: List[str] = field(default_factory=list)     # rows Muninn refused; state.db is kept
    renamed_to: Optional[Path] = None
    removed: List[Path] = field(default_factory=list)   # migrated copies past KEEP_DAYS


class HistoryError(Exception):
    """state.db can't be read; the message says what to do."""


def legacy_path(data_dir: Path) -> Path:
    return Path(data_dir) / LEGACY


def _open(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=5)
    con.row_factory = sqlite3.Row
    return con


def read_rows(path: Path) -> List[Dict[str, Any]]:
    """Every row of state.db's synced table, whatever version wrote it (v0.1.0 had fewer columns)."""
    try:
        con = _open(path)
        try:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'synced'").fetchone():
                return []
            return [dict(r) for r in con.execute("SELECT * FROM synced ORDER BY start_utc")]
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        if "locked" in str(exc).lower():
            raise HistoryError(f"Odin's old history file {path} is locked by another program ({exc}). Close it "
                               "(an old copy of meeting2jira, or a database viewer) and run again.") from None
        raise HistoryError(
            f"Odin's old history file {path} looks corrupt ({exc}). It records which meetings already have "
            "sub-tasks. If it can't be repaired, move state.db aside and run `odin preview` before a real "
            "run: meetings already in Jira may be created a second time.") from None


def forget_legacy(data_dir: Path, issue_key: str) -> int:
    """`odin forget` for a state.db that hasn't moved into Muninn yet."""
    path = legacy_path(data_dir)
    if not path.is_file():
        return 0
    try:
        con = _open(path)
        try:
            with con:
                return con.execute("DELETE FROM synced WHERE issue_key = ?", (issue_key.strip().upper(),)).rowcount
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        raise HistoryError(f"Couldn't change {path}: {exc}") from None


class Legacy:
    """state.db, read but not imported: what a dry run (or a run after a failed import) consults."""

    def __init__(self, rows: List[Dict[str, Any]]):
        self.by_key = {str(r.get("key")): str(r.get("issue_key")) for r in rows}
        self.by_hash = {str(r.get("content_hash")): str(r.get("issue_key")) for r in rows}

    @classmethod
    def open(cls, data_dir: Path) -> Optional["Legacy"]:
        path = legacy_path(data_dir)
        return cls(read_rows(path)) if path.is_file() else None

    def find(self, m: Meeting) -> Optional[str]:
        return self.by_key.get(m.key) or self.by_hash.get(m.content_hash)

    def __len__(self) -> int:
        return len(self.by_key)


def _owed(row: Dict[str, Any]) -> bool:
    return (bool(row.get("worklog_wanted")) and not row.get("worklog_logged")
            and int(row.get("worklog_attempts") or 0) < MAX_WORKLOG_ATTEMPTS)


def _record(row: Dict[str, Any]) -> store.SubtaskRecord:
    """A meeting_subtasks record from a synced row, shaped to fit Muninn's checks where that is safe."""
    content_hash = str(row.get("content_hash") or "").strip().lower()
    if not _HASH_RE.match(content_hash):
        raise ValueError(f"its content hash {content_hash!r} isn't one Odin wrote")
    created = row.get("created_at")
    return store.SubtaskRecord(
        meeting_key=str(row["key"]),
        content_hash=content_hash,
        issue_key=muninn.normalize_key(str(row["issue_key"])),
        parent_key=muninn.normalize_key(str(row["parent"])),
        summary=(" ".join(str(row.get("summary") or "").split()) or "(meeting)")[:255],
        started_at=iso_utc(parse_utc(str(row["start_utc"]))),
        minutes=min(store.MAX_MINUTES, max(0, int(row.get("minutes") or 0))),
        worklog_wanted=_owed(row),
        worklog_comment=row.get("worklog_comment") or None,
        origin="state_db",
        created_at=iso_utc(parse_utc(str(created))) if created else None,
    )


def import_state_db(con: sqlite3.Connection, data_dir: Path, now: Optional[datetime] = None) -> ImportResult:
    """Move state.db's records into Muninn, then retire the file (see the module docstring)."""
    now = now or datetime.now(timezone.utc)
    result = ImportResult()
    path = legacy_path(data_dir)
    if path.is_file():
        rows = read_rows(path)
        with muninn.transaction(con):
            for row in rows:
                what = f"{row.get('issue_key', '?')} ({row.get('start_utc', '?')})"
                try:
                    rec = _record(row)
                    con.execute("SAVEPOINT row")
                    try:
                        added = store._insert(con, rec)
                    except sqlite3.DatabaseError:
                        con.execute("ROLLBACK TO row")
                        con.execute("RELEASE row")
                        raise
                    con.execute("RELEASE row")
                except (ValueError, KeyError, TypeError, sqlite3.DatabaseError, muninn.MuninnError) as exc:
                    result.failed.append(f"{what}: {exc}")
                    continue
                if added:
                    result.imported += 1
                    if rec.worklog_wanted:
                        result.owed.append(f"{rec.issue_key} {rec.started_at[:10]} {rec.minutes}m")
                else:
                    result.already += 1
        if not result.failed:
            target = path.with_name(f"{LEGACY}.migrated-{now:%Y%m%d}")
            n = 1
            while target.exists():
                n += 1
                target = path.with_name(f"{LEGACY}.migrated-{now:%Y%m%d}-{n}")
            os.replace(path, target)
            for suffix in ("-journal", "-wal", "-shm"):
                leftover = path.with_name(LEGACY + suffix)
                if leftover.exists():
                    try:
                        leftover.unlink()
                    except OSError:
                        pass
            result.renamed_to = target
            log.info("Moved %d sub-task record(s) from state.db into Muninn (%d already there); kept the old file "
                     "as %s for %d days.", result.imported, result.already, target.name, KEEP_DAYS)
        else:
            log.warning("%d record(s) in state.db couldn't move into Muninn, so state.db stays and Odin keeps "
                        "consulting it: %s", len(result.failed), "; ".join(result.failed[:5]))
    result.removed = prune_migrated(data_dir, now)
    return result


def prune_migrated(data_dir: Path, now: Optional[datetime] = None) -> List[Path]:
    """Delete migrated copies of state.db older than KEEP_DAYS."""
    now = now or datetime.now(timezone.utc)
    removed = []
    for path in sorted(Path(data_dir).glob(f"{LEGACY}.migrated-*")):
        m = _MIGRATED_RE.match(path.name)
        if not m:
            continue
        try:
            made = datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if now - made > timedelta(days=KEEP_DAYS):
            try:
                path.unlink()
                removed.append(path)
            except OSError as exc:
                log.debug("Could not remove %s: %s", path, exc)
    return removed
