"""Odin's side of Muninn: the connection, its sources, your calendar, and the record of meeting sub-tasks.

Position in the flow
    cli.py opens Muninn here. sync.py asks find_subtask() whether a meeting already has a sub-task
    and calls record_subtask() the moment Jira accepts a create; posting.py and collect.py use the
    same connection with Muninn's own Odin helpers (asgard.muninn.odin).

What makes re-runs safe
    meeting_subtasks (Muninn v5) is all that stands between a re-run and a pile of duplicate
    sub-tasks, so:
      * A meeting is found by its calendar item's key OR its content hash, so the Outlook, CSV,
        Graph and OWA paths recognise each other's work.
      * The record is written right after Jira accepts the create, before the worklog and the
        transition, so a failure in either can't cause a duplicate.
      * If Muninn can't take the record (busy past its timeout, disk full), it goes to a journal
        file in Odin's folder instead (unrecorded.jsonl), the run stops, and the next run writes
        the journal into Muninn before it creates anything.
      * RunLock keeps two Odin runs (the scheduled task and a manual one) from creating the same
        meeting's sub-task at once.

Calendar
    Every item of an export is stored in calendar_events, not only the meetings Odin pushes:
    Baldur reads them to keep meeting time out of its estimates. A private item is stored with
    its times only; its subject stays out of Muninn as it stays out of Jira and the logs.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from asgard import muninn
from asgard.muninn import odin as mo

from .models import Meeting, iso_utc, parse_utc

APP = "odin"
SCHEMA = (5, 5)            # 5: meeting_subtasks, which replaced state.db
JIRA_SOURCE = "jira-dc"
PRIVATE_TITLE = "Private appointment"
MAX_MINUTES = 1440         # meeting_subtasks.minutes, and a worklog Asgard sends
RETRY_DAYS = 14            # a meeting worklog that didn't land is retried for this long...
MAX_WORKLOG_ATTEMPTS = 3   # ...and at most this many refusals
JOURNAL = "unrecorded.jsonl"
LOCK = "odin.lock"
LOCK_STALE_SECONDS = 2 * 3600

_SHOW_AS = {"free": "free", "tentative": "tentative", "busy": "busy", "oof": "oof", "elsewhere": "free"}
_RESPONSE = {"organizer": "organizer", "accepted": "accepted", "tentative": "tentative", "declined": "declined"}
_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class StoreError(Exception):
    """Muninn can't be used for this run; the message says what to do."""


# --------------------------------------------------------------------------
# The connection and its sources
# --------------------------------------------------------------------------

def open_muninn(readonly: bool = False, path: Optional[Path] = None) -> sqlite3.Connection:
    """Muninn, as Odin. Asgard creates and upgrades it; Odin never migrates (AGENTS.md)."""
    return muninn.open_app(APP, supported=SCHEMA, readonly=readonly, path=path)


def jira_source(con: sqlite3.Connection, base_url: str) -> int:
    with muninn.transaction(con):
        return muninn.ensure_source(con, "jira", JIRA_SOURCE, base_url.rstrip("/"))


def calendar_source(con: sqlite3.Connection, export_source: str) -> int:
    """One source per export path (outlook-com, outlook-csv, graph-msal, owa-playwright): each has its
    own item ids, and a full export from one must never mark another's items deleted."""
    name = str(export_source or "").strip().lower()
    name = name if _SOURCE_RE.match(name) else "unknown"
    with muninn.transaction(con):
        return muninn.ensure_source(con, "calendar", f"calendar:{name}")


def find_source(con: sqlite3.Connection, name: str) -> Optional[int]:
    row = con.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()
    return int(row[0]) if row else None


def remember_me(con: sqlite3.Connection, jira_sid: int, me: Dict[str, Any]) -> None:
    """Your Jira user, which marks your issues and worklogs as yours."""
    values = {str(me.get(k)).strip() for k in ("name", "key") if me.get(k)}
    with muninn.transaction(con):
        for value in sorted(values):
            muninn.add_identity(con, "jira_user", value, jira_sid)


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------

def event_from_meeting(m: Meeting) -> Dict[str, Any]:
    """A calendar_events row from Odin's Meeting record."""
    return {
        "external_id": m.key,
        "title": PRIVATE_TITLE if m.is_private else (" ".join(m.subject.split())[:500] or "(no subject)"),
        "starts_at": iso_utc(m.start_utc),
        "ends_at": iso_utc(max(m.end_utc, m.start_utc)),
        "is_all_day": 1 if m.all_day else 0,
        "show_as": _SHOW_AS.get(m.busy_status, "unknown"),
        "response": _RESPONSE.get(m.response, "none"),
        "is_cancelled": 1 if m.is_cancelled else 0,
    }


@dataclass
class CalendarResult:
    stored: int = 0
    changed: int = 0
    removed: int = 0
    swept: bool = False
    problems: List[str] = field(default_factory=list)


def store_calendar(con: sqlite3.Connection, source_id: int, meetings: Iterable[Meeting],
                   window: Optional[Tuple[datetime, datetime]] = None) -> Tuple[Dict[str, int], CalendarResult]:
    """Store an export's items; returns ({meeting key: calendar event id}, what happened).

    window is the export's whole range, when the exporter read all of it. Only then is the run a
    full one, which marks items in the window that the export no longer has as deleted; a CSV
    export has no range, so it only adds and updates.
    """
    ids: Dict[str, int] = {}
    result = CalendarResult()
    mode = "full" if window else "incremental"
    with muninn.Run(con, APP, source_id, "calendar", mode=mode) as run:
        with run.batch():
            for m in meetings:
                if not m.key or len(m.key) > 1000:
                    run.problem(f"a calendar item at {iso_utc(m.start_utc)} has no usable id")
                    continue
                try:
                    event_id, changed = mo.upsert_calendar_event(run, event_from_meeting(m))
                except sqlite3.DatabaseError as exc:
                    run.problem(f"calendar item at {iso_utc(m.start_utc)}: {exc}")
                    continue
                ids[m.key] = event_id
                result.stored += 1
                result.changed += 1 if changed else 0
        if window:
            result.removed = mo.sweep_calendar(run, iso_utc(window[0]), iso_utc(window[1]))
            result.swept = not run.problems
        result.problems = list(run.problems)
    return ids, result


def export_window(doc: Dict[str, Any]) -> Optional[Tuple[datetime, datetime]]:
    """The range a JSON export read in full, or None (no range, or the exporter stopped early)."""
    if doc.get("truncated"):
        return None
    try:
        start, end = parse_utc(str(doc["range_start"])), parse_utc(str(doc["range_end"]))
    except (KeyError, ValueError, TypeError):
        return None
    return (start, end) if end > start else None


# --------------------------------------------------------------------------
# Meeting sub-tasks
# --------------------------------------------------------------------------

@dataclass
class SubtaskRecord:
    meeting_key: str
    content_hash: str
    issue_key: str
    parent_key: str
    summary: str
    started_at: str
    minutes: int
    worklog_wanted: bool = False
    worklog_comment: Optional[str] = None
    calendar_event_id: Optional[int] = None
    origin: str = "odin"
    created_at: Optional[str] = None

    @classmethod
    def for_meeting(cls, m: Meeting, issue_key: str, parent: str, summary: str, event_id: Optional[int],
                    worklog_wanted: bool, comment: Optional[str]) -> "SubtaskRecord":
        return cls(m.key, m.content_hash, issue_key, parent, summary, iso_utc(m.start_utc), m.minutes,
                   worklog_wanted, comment, event_id)


def unrecordable(m: Meeting) -> Optional[str]:
    """Why a meeting's sub-task couldn't be recorded, checked before anything is created in Jira:
    a sub-task Muninn can't record would be created again on every run."""
    if not m.key or len(m.key) > 1000:
        return "calendar item has no usable id"
    if m.minutes > MAX_MINUTES:
        return "over 24 hours"
    return None


def find_subtask(con: sqlite3.Connection, m: Meeting) -> Optional[sqlite3.Row]:
    """The record of this meeting's sub-task, by the calendar item's key or by its content hash."""
    return con.execute("SELECT * FROM meeting_subtasks WHERE meeting_key = ? OR content_hash = ? "
                       "ORDER BY meeting_key = ? DESC, id LIMIT 1", (m.key, m.content_hash, m.key)).fetchone()


def _insert(con: sqlite3.Connection, rec: SubtaskRecord) -> bool:
    """Insert one record (inside the caller's transaction). False if the meeting already has one."""
    if con.execute("SELECT 1 FROM meeting_subtasks WHERE meeting_key = ?", (rec.meeting_key,)).fetchone():
        return False
    event = rec.calendar_event_id
    if event is not None and not con.execute("SELECT 1 FROM calendar_events WHERE id = ?", (event,)).fetchone():
        event = None
    con.execute("INSERT INTO meeting_subtasks (meeting_key, content_hash, calendar_event_id, issue_key, parent_key, "
                "summary, started_at, minutes, worklog_wanted, worklog_comment, origin, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, coalesce(?, strftime('%Y-%m-%dT%H:%M:%SZ','now')))",
                (rec.meeting_key, rec.content_hash, event, rec.issue_key, rec.parent_key, rec.summary[:255],
                 rec.started_at, int(rec.minutes), 1 if rec.worklog_wanted else 0, rec.worklog_comment,
                 rec.origin, rec.created_at))
    if event is not None:
        mo.set_meeting_key(con, event, rec.issue_key)
    return True


def record_subtask(con: sqlite3.Connection, rec: SubtaskRecord, data_dir: Path) -> bool:
    """Record a sub-task Jira just created. True if Muninn has it; False if it went to the journal.

    Never raises for a database problem: the sub-task exists in Jira, so losing this record would
    re-create it next run. The journal is written instead and the caller stops creating.
    """
    try:
        with muninn.transaction(con):
            _insert(con, rec)
        return True
    except (sqlite3.DatabaseError, muninn.MuninnError) as exc:
        Journal(data_dir).append(rec, str(exc))
        return False


def link_event(con: sqlite3.Connection, record_id: int, event_id: int, issue_key: str) -> bool:
    """Link a record that has no calendar event yet (history from state.db) to the meeting's event,
    once that meeting turns up in an export: the meeting then shows which issue it went to."""
    try:
        with muninn.transaction(con):
            linked = con.execute("UPDATE meeting_subtasks SET calendar_event_id = ? WHERE id = ? "
                                 "AND calendar_event_id IS NULL", (event_id, record_id)).rowcount
            if linked:
                con.execute("UPDATE calendar_events SET logged_as_key = coalesce(logged_as_key, ?) WHERE id = ?",
                            (issue_key, event_id))
        return bool(linked)
    except (sqlite3.DatabaseError, muninn.MuninnError):
        return False      # only a convenience; the record itself is what prevents duplicates


def forget(con: sqlite3.Connection, issue_key: str) -> int:
    """Remove the records for a sub-task, so its meeting is pushed again on the next run."""
    key = muninn.normalize_key(issue_key)
    with muninn.transaction(con):
        events = [r[0] for r in con.execute("SELECT calendar_event_id FROM meeting_subtasks WHERE issue_key = ? "
                                            "AND calendar_event_id IS NOT NULL", (key,))]
        removed = con.execute("DELETE FROM meeting_subtasks WHERE issue_key = ?", (key,)).rowcount
        for event in events:
            con.execute("UPDATE calendar_events SET logged_as_key = NULL WHERE id = ? AND logged_as_key = ?",
                        (event, key))
    return removed


def recent(con: sqlite3.Connection, limit: int = 20) -> List[sqlite3.Row]:
    """The latest sub-tasks, with whether their meeting's time is in Jira (posted, waiting, or not wanted)."""
    return con.execute(
        "SELECT ms.*, (SELECT l.state FROM worklogs l WHERE l.state IN ('sending', 'posted', 'failed') AND "
        "  ((ms.calendar_event_id IS NOT NULL AND l.calendar_event_id = ms.calendar_event_id) OR "
        "   l.work_item_id = (SELECT a.work_item_id FROM work_item_aliases a WHERE a.key = ms.issue_key "
        "                     AND a.status <> 'not_found')) "
        "  ORDER BY l.state = 'posted' DESC, l.state = 'sending' DESC, l.id DESC LIMIT 1) AS worklog_state "
        "FROM meeting_subtasks ms ORDER BY ms.started_at DESC, ms.id DESC LIMIT ?", (limit,)).fetchall()


# Worklogs that count for a meeting sub-task: on its calendar event, or on the sub-task itself (the
# sub-task holds one meeting's time, so time logged on it by hand, or by Odin before Muninn, counts).
_LINKED = ("((ms.calendar_event_id IS NOT NULL AND l.calendar_event_id = ms.calendar_event_id) OR "
           " l.work_item_id = (SELECT a.work_item_id FROM work_item_aliases a WHERE a.key = ms.issue_key "
           "                   AND a.status <> 'not_found'))")


def pending_meeting_worklogs(con: sqlite3.Connection, now: Optional[datetime] = None,
                             limit: int = 50) -> List[sqlite3.Row]:
    """Sub-tasks whose meeting time was wanted in Jira and isn't there yet, and are still retryable.

    worklog_wanted keeps this to real failures: a sub-task made while log_work was off never had a
    worklog to send, so turning log_work on doesn't post months of old meetings. A row is retried
    for RETRY_DAYS after it was made and until Jira has refused it MAX_WORKLOG_ATTEMPTS times.
    """
    since = iso_utc((now or datetime.now(timezone.utc)) - timedelta(days=RETRY_DAYS))
    return con.execute(
        f"SELECT ms.* FROM meeting_subtasks ms WHERE ms.worklog_wanted = 1 AND ms.created_at >= ? "
        f"AND NOT EXISTS (SELECT 1 FROM worklogs l WHERE l.state IN ('sending', 'posted', 'deleted') AND {_LINKED}) "
        f"AND (SELECT count(*) FROM worklogs l WHERE l.state = 'failed' AND {_LINKED}) < ? "
        f"ORDER BY ms.started_at LIMIT ?", (since, MAX_WORKLOG_ATTEMPTS, limit)).fetchall()


def still_pending(con: sqlite3.Connection, record_id: int) -> bool:
    return con.execute(
        f"SELECT 1 FROM meeting_subtasks ms WHERE ms.id = ? AND ms.worklog_wanted = 1 "
        f"AND NOT EXISTS (SELECT 1 FROM worklogs l WHERE l.state IN ('sending', 'posted', 'deleted') AND {_LINKED})",
        (record_id,)).fetchone() is not None


# --------------------------------------------------------------------------
# The journal: records Muninn couldn't take
# --------------------------------------------------------------------------

class Journal:
    """unrecorded.jsonl in Odin's folder: sub-tasks Jira created that Muninn didn't record.

    One JSON line per record, appended and flushed to disk, so it survives whatever stopped
    Muninn. replay() writes them into Muninn and removes the file once all of them are in.
    """

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / JOURNAL

    def append(self, rec: SubtaskRecord, why: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(asdict(rec), why=why[:300])) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def records(self) -> List[SubtaskRecord]:
        if not self.path.is_file():
            return []
        out = []
        fields = set(SubtaskRecord.__dataclass_fields__)
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                data = json.loads(line)
                out.append(SubtaskRecord(**{k: v for k, v in data.items() if k in fields}))
            except (ValueError, TypeError):
                continue      # a half-written last line; the rest still count
        return out

    def replay(self, con: sqlite3.Connection) -> int:
        """Write the journal into Muninn. Returns how many records were added."""
        records = self.records()
        if not records:
            return 0
        added = 0
        with muninn.transaction(con):
            for rec in records:
                added += 1 if _insert(con, rec) else 0
        self.path.unlink()
        return added

    def holds(self, m: Meeting) -> Optional[SubtaskRecord]:
        return next((r for r in self.records() if r.meeting_key == m.key or r.content_hash == m.content_hash), None)


# --------------------------------------------------------------------------
# One run at a time
# --------------------------------------------------------------------------

class RunLock:
    """odin.lock in Odin's folder, held while a run may write to Jira.

    Created exclusively, so a second run (a manual sync while the scheduled task is going) stops
    with a message instead of creating the same meeting's sub-task twice. The lock names its
    process: one whose process has gone (a run stopped from Odin's window, or a crash) is taken
    over at once, and one older than two hours is taken over whatever it says.
    """

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / LOCK
        self.held = False

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    holder = self.path.read_text(encoding="utf-8").split()
                except OSError:
                    continue
                pid = int(holder[0]) if holder and holder[0].isdigit() else 0
                if age > LOCK_STALE_SECONDS or (pid and not _alive(pid)):
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                raise StoreError(f"Another Odin run is in progress (it started {int(age // 60)} minute(s) ago). "
                                 "Wait for it to finish, then try again. If none is running, delete "
                                 f"{self.path}.") from None
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"{os.getpid()} {iso_utc(datetime.now(timezone.utc))}\n")
            self.held = True
            return self
        raise StoreError(f"Couldn't take Odin's run lock at {self.path}.")

    def __exit__(self, *exc: Any) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.held = False


def _alive(pid: int) -> bool:
    """Whether a process with this id is still running (the run that holds the lock)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5                 # access denied: it exists
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259                            # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)                                         # signal 0 checks; it sends nothing
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
