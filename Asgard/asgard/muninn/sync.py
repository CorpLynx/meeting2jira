"""Provenance and change tracking: sources, identities, sync runs, events.

A sync is one Run. Fetch from the network outside any transaction, then
write; Muninn's single write lock is held only while writing:

    with Run(con, app="odin", source_id=jira, stream="issues") as run:
        for page in client.pages(updated_since=run.cursor):    # no lock held here
            with run.batch():                                   # one short transaction per page
                for issue in page:
                    odin.upsert_issue(run, issue, ctx)          # each item is all or nothing
            run.advance_cursor(newest_updated_in(page))

Write helpers open their own batch when called outside one, so batching is
only a speed-up. The cursor is saved when the run ends without an error;
after a failure the next run starts from the old cursor and re-reads the
same items, which the upserts take without writing anything twice.
"""
from __future__ import annotations

import json
import sqlite3
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set

from . import guard
from .db import MuninnError, retry_busy, transaction, utcnow
from .redact import redact_url, scrub, scrub_value

APPS = ("muninn", "huginn", "odin", "baldur", "loki", "freya", "heimdall", "bifrost", "ysildir", "valkyrie")
_LOST = "SQLite rolled this run's changes back on its own (disk full or I/O error), so the run stops here."


# --------------------------------------------------------------------------
# Sources and identities
# --------------------------------------------------------------------------

def ensure_source(con: sqlite3.Connection, kind: str, name: str, base_url: Optional[str] = None) -> int:
    """The id of a source, adding it the first time. A password or token in base_url isn't stored."""
    base_url = redact_url(base_url)
    row = con.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()
    if row:
        if base_url:
            con.execute("UPDATE sources SET base_url = ? WHERE id = ? AND base_url IS NOT ?",
                        (base_url, row[0], base_url))
        return int(row[0])
    return int(con.execute("INSERT INTO sources (kind, name, base_url) VALUES (?, ?, ?) RETURNING id",
                           (kind, name, base_url)).fetchone()[0])


def add_identity(con: sqlite3.Connection, kind: str, value: str, source_id: Optional[int] = None) -> None:
    con.execute("INSERT INTO identities (kind, value, source_id) VALUES (?, ?, ?) "
                "ON CONFLICT (kind, value) DO NOTHING", (kind, value.strip(), source_id))


def identities(con: sqlite3.Connection, kind: str) -> Set[str]:
    """Your values of one kind, lower-cased for matching."""
    return {r[0].lower() for r in con.execute("SELECT value FROM identities WHERE kind = ?", (kind,))}


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

def emit(con: sqlite3.Connection, app: str, kind: str, entity_type: str, entity_id: Optional[int] = None,
         ref: Optional[str] = None, payload: Optional[Dict[str, Any]] = None,
         run_id: Optional[int] = None) -> int:
    """Append one event. Call inside the transaction that made the change.

    Events are kept for the life of the database, so anything in the payload that looks like a
    credential is masked first (redact.py).
    """
    body = json.dumps(scrub_value(payload or {}), default=str)
    return int(con.execute(
        "INSERT INTO events (app, kind, entity_type, entity_id, ref, run_id, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (app, kind, entity_type, entity_id, ref, run_id, body)).fetchone()[0])


@dataclass
class Event:
    id: int
    at: str
    app: str
    kind: str
    entity_type: str
    entity_id: Optional[int]
    ref: Optional[str]
    payload: Dict[str, Any]


@dataclass
class EventBatch:
    events: List[Event] = field(default_factory=list)
    truncated: bool = False

    def __iter__(self) -> Iterator[Event]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)


@contextmanager
def consume(con: sqlite3.Connection, app: str, kinds: Iterable[str], limit: int = 500) -> Iterator[EventBatch]:
    """Read the events an app hasn't handled yet, and advance its cursor with its writes.

        with consume(con, "freya", ["work_item.done", "work_item.reopened"]) as batch:
            for event in batch:
                ...   # writes here commit together with the cursor

    If the block raises, nothing is written and the cursor stays put.
    """
    kinds = list(kinds)
    if not kinds:
        raise ValueError("consume() needs at least one event kind")
    marks = ",".join("?" * len(kinds))
    with transaction(con):
        row = con.execute("SELECT last_event_id FROM event_cursors WHERE app = ?", (app,)).fetchone()
        last = int(row[0]) if row else 0
        newest = int(con.execute("SELECT coalesce(max(id), 0) FROM events").fetchone()[0])
        rows = con.execute(f"SELECT * FROM events WHERE id > ? AND id <= ? AND kind IN ({marks}) ORDER BY id LIMIT ?",
                           [last, newest, *kinds, limit + 1]).fetchall()
        batch = EventBatch(truncated=len(rows) > limit)
        for r in rows[:limit]:
            batch.events.append(Event(r["id"], r["at"], r["app"], r["kind"], r["entity_type"], r["entity_id"],
                                      r["ref"], json.loads(r["payload"])))
        yield batch
        if not con.in_transaction:
            raise MuninnError(_LOST)
        # Skip ahead past events of other kinds, but never past ones emitted while handling this batch.
        new_last = batch.events[-1].id if batch.truncated else max(last, newest)
        con.execute("INSERT INTO event_cursors (app, last_event_id, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (app) DO UPDATE SET last_event_id = excluded.last_event_id, "
                    "updated_at = excluded.updated_at", (app, new_last, utcnow()))


# --------------------------------------------------------------------------
# Sync runs
# --------------------------------------------------------------------------

class Run:
    """One collector run: its provenance row, cursor, write batches and events."""

    def __init__(self, con: sqlite3.Connection, app: str, source_id: Optional[int], stream: str,
                 mode: str = "incremental") -> None:
        if app not in APPS:
            raise ValueError(f"unknown app {app!r}")
        if mode not in ("incremental", "full"):
            raise ValueError("mode is 'incremental' or 'full'")
        self.con, self.app, self.source_id, self.stream, self.mode = con, app, source_id, stream, mode
        self.id: Optional[int] = None
        self.now = ""
        self.cursor: Optional[str] = None
        self.cursor_before: Optional[str] = None
        self.items_seen = 0
        self.items_changed = 0
        self.problems: List[str] = []
        self.seen: Dict[str, Set[int]] = {}   # row ids this run touched, by table, for sweeps
        self._depth = 0

    # ---- context -------------------------------------------------------

    def __enter__(self) -> "Run":
        if self.con.in_transaction:
            raise MuninnError("A sync run can't start inside another transaction.")
        self.now = utcnow()
        self.id = int(retry_busy(lambda: self.con.execute(
            "INSERT INTO sync_runs (app, source_id, stream, mode, started_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
            (self.app, self.source_id, self.stream, self.mode, self.now)).fetchone()[0]))
        if self.source_id is not None:
            row = self.con.execute("SELECT cursor FROM sync_cursors WHERE source_id = ? AND stream = ?",
                                   (self.source_id, self.stream)).fetchone()
            self.cursor = self.cursor_before = row[0] if row else None
        return self

    def __exit__(self, exc_type: Any, exc: Optional[BaseException], tb: Any) -> bool:
        con = self.con
        if exc_type is None:
            try:
                with transaction(con):
                    # A run with problems keeps the old cursor, so the next run retries what failed;
                    # the items that worked are re-read without being written twice.
                    if self.problems:
                        self.cursor = self.cursor_before
                    if self.source_id is not None and self.cursor is not None and self.cursor != self.cursor_before:
                        con.execute("INSERT INTO sync_cursors (source_id, stream, cursor, updated_at, run_id) "
                                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT (source_id, stream) DO UPDATE SET "
                                    "cursor = excluded.cursor, updated_at = excluded.updated_at, "
                                    "run_id = excluded.run_id",
                                    (self.source_id, self.stream, self.cursor, utcnow(), self.id))
                    self._finish("partial" if self.problems else "ok", "\n".join(self.problems) or None)
                return False
            except BaseException as failure:
                self._record_failure(type(failure), failure)
                raise
        if con.in_transaction and self._depth == 0:
            con.execute("ROLLBACK")
        self._record_failure(exc_type, exc)
        return False

    def _finish(self, status: str, error: Optional[str]) -> None:
        self.con.execute(
            "UPDATE sync_runs SET finished_at = ?, status = ?, items_seen = ?, items_changed = ?, "
            "cursor_before = ?, cursor_after = ?, error = ? WHERE id = ?",
            (utcnow(), status, self.items_seen, self.items_changed, self.cursor_before, self.cursor, error, self.id))

    def _record_failure(self, exc_type: Any, exc: Optional[BaseException]) -> None:
        text = "".join(traceback.format_exception_only(exc_type, exc)).strip() if exc_type else "failed"
        if exc is not None and "not authorized" in text.lower():
            text = f"{exc_type.__name__}: {guard.describe(exc)}"    # say which table and why
        text = scrub(text) or "failed"
        try:
            if self.con.in_transaction:
                self.con.execute("ROLLBACK")
            self.con.execute("UPDATE sync_runs SET finished_at = ?, status = 'failed', items_seen = ?, "
                             "items_changed = ?, error = ? WHERE id = ?",
                             (utcnow(), self.items_seen, self.items_changed, text[:2000], self.id))
        except sqlite3.DatabaseError:
            pass  # recording the failure must never hide the original error

    # ---- writing -------------------------------------------------------

    @contextmanager
    def batch(self) -> Iterator["Run"]:
        """A write transaction (or, when nested, a savepoint) that saves all of the block or none of it."""
        con = self.con
        if self.id is None:
            raise MuninnError("Use the run in a with-block before writing.")
        if self._depth:
            if not con.in_transaction:
                raise MuninnError(_LOST)
            name = f"item{self._depth}"
            con.execute(f"SAVEPOINT {name}")
            self._depth += 1
            try:
                yield self
            except BaseException:
                if con.in_transaction:
                    con.execute(f"ROLLBACK TO {name}")
                    con.execute(f"RELEASE {name}")
                raise
            finally:
                self._depth -= 1
            if not con.in_transaction:
                raise MuninnError(_LOST)
            con.execute(f"RELEASE {name}")
            return
        self._depth = 1
        try:
            with transaction(con):
                yield self
        finally:
            self._depth = 0

    def emit(self, kind: str, entity_type: str, entity_id: Optional[int] = None, ref: Optional[str] = None,
             payload: Optional[Dict[str, Any]] = None) -> int:
        with self.batch():
            return emit(self.con, self.app, kind, entity_type, entity_id, ref, payload, self.id)

    def advance_cursor(self, value: Optional[str]) -> None:
        """Raise a timestamp high-water mark; it is saved only if the run ends without an error."""
        if value is not None and (self.cursor is None or str(value) > self.cursor):
            self.cursor = str(value)

    def set_cursor(self, value: str) -> None:
        """Replace the cursor with any value, such as a commit SHA."""
        self.cursor = str(value)

    def saw(self, table: str, row_id: int) -> None:
        self.seen.setdefault(table, set()).add(int(row_id))

    def problem(self, message: str) -> None:
        """Note an item that failed while the rest succeeded. The run ends 'partial' and its
        cursor doesn't move, so the next run tries the item again. Credentials in the text are masked."""
        self.problems.append(scrub(str(message)) or "")
