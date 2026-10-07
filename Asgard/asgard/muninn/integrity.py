"""Keeping Muninn healthy: daily housekeeping, a full check, and repairs.

maintain() runs from prepare() once a day. It refreshes the query planner's statistics and folds
the -wal file back into the database. It prunes old operational rows only once you switch
retention on (`Asgard.pyw --muninn retention on`): how long federal records must be kept is your
records officer's call, and a year of one engineer's data is under 20 MB, so nothing is deleted
until you decide.

check() looks for everything that should never happen: file damage, broken links between rows, a
search index out of step with its tables, time posted twice, keys Odin can never resolve, posts
stuck in doubt, event cursors past the end of the log. It reads only. repair() fixes the two
things that can be fixed without judgement (the search index and event cursors); the rest each
say what to do.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import keys
from .db import (BusyError, MuninnError, _migration_statements, ago, available_migrations, retry_busy,
                 transaction, user_version, utcnow)

# How long pruned data is kept, in days, once retention is on (muninn-design.md, "retention").
RETENTION_DAYS = {"sync_runs": 180, "estimate_runs": 90, "reflog_entries": 365}
PRUNE_CHUNK = 500                 # rows per transaction, so other apps never wait long
PRUNE_BUDGET_SECONDS = 2.0        # per day; whatever is left waits for tomorrow
STALE_SENDING_SECONDS = 3600      # a post still 'sending' after an hour needs Odin to check Jira

# The search index: rowid = id * 16 + code. Title and body as the schema's triggers write them;
# a test checks these stay in step with the triggers.
SEARCH_SOURCES: Dict[int, Tuple[str, str, str]] = {
    1: ("work_items", "key || ' ' || summary",
        "coalesce(epic_key, '') || ' ' || issue_type || ' ' || labels || ' ' || components"),
    2: ("commits", "subject", "substr(sha, 1, 12) || ' ' || coalesce(branch_hint, '')"),
    3: ("pull_requests", "title", "'#' || number || ' ' || author || ' ' || head_ref"),
    4: ("meetings", "title", "coalesce(notes_summary, '')"),
    5: ("action_items", "text", "coalesce(owner, '') || ' ' || coalesce(work_item_key, '')"),
    6: ("blufs", "bottom_line", "body_md"),
    7: ("submissions", "title",
        "system || ' ' || coalesce(external_id, '') || ' ' || coalesce(work_item_key, '')"),
    8: ("accomplishments", "work_item_key || ' ' || summary",
        "coalesce(epic_key, '') || ' ' || coalesce(impact_note, '')"),
}

# Columns where one app writes a Jira key another app resolves.
KEY_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("commit_work_items", "work_item_key"), ("pull_requests", "work_item_key"),
    ("day_proposals", "work_item_key"), ("session_allocations", "work_item_key"),
    ("time_actuals", "work_item_key"), ("action_items", "work_item_key"),
    ("submissions", "work_item_key"), ("calendar_events", "logged_as_key"),
)


# --------------------------------------------------------------------------
# Daily housekeeping
# --------------------------------------------------------------------------

@dataclass
class MaintainReport:
    ran: bool = False                                    # False: already done today
    pruned: Dict[str, int] = field(default_factory=dict)
    more_to_prune: bool = False                          # the time budget ran out; tomorrow continues
    retention: bool = False
    checkpoint: Optional[Tuple[int, int, int]] = None    # (busy, wal pages, pages moved)
    seconds: float = 0.0


def _meta(con: sqlite3.Connection, key: str) -> Optional[str]:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value))


def retention_on(con: sqlite3.Connection) -> bool:
    return _meta(con, "retention") == "on"


def set_retention(con: sqlite3.Connection, on: bool) -> None:
    """Switch pruning of old operational rows on or off (Asgard only)."""
    with transaction(con):
        _set_meta(con, "retention", "on" if on else "off")
        _set_meta(con, "retention_changed_at", utcnow())


def _cutoff(days: int) -> str:
    return ago(days * 86400)


# Each statement deletes at most one chunk of rows that are past their time and that nothing
# still needs. Child rows go with them through the schema's ON DELETE rules.
_PRUNE_SQL = {
    # A run still 'running' is either live or will be closed as abandoned; never prune it.
    "sync_runs": "DELETE FROM sync_runs WHERE id IN (SELECT id FROM sync_runs WHERE status <> 'running' "
                 "AND started_at < ? ORDER BY id LIMIT ?)",
    # Keep any run with a decision in it (audit) or a day still waiting for review.
    "estimate_runs": "DELETE FROM estimate_runs WHERE id IN (SELECT e.id FROM estimate_runs e WHERE e.created_at < ? "
                     "AND NOT EXISTS (SELECT 1 FROM day_proposals p WHERE p.estimate_run_id = e.id "
                     "AND (p.decided_at IS NOT NULL OR p.status = 'proposed')) ORDER BY e.id LIMIT ?)",
    "reflog_entries": "DELETE FROM reflog_entries WHERE id IN (SELECT id FROM reflog_entries WHERE at < ? "
                      "ORDER BY id LIMIT ?)",
}


def prune(con: sqlite3.Connection, *, budget: float = PRUNE_BUDGET_SECONDS,
          chunk: int = PRUNE_CHUNK) -> Tuple[Dict[str, int], bool]:
    """Delete rows past RETENTION_DAYS, a chunk per transaction, within a time budget.

    Returns ({table: rows deleted}, whether rows were left for next time). Callers check
    retention_on() first; this does what it is told.
    """
    deadline = time.monotonic() + budget
    pruned: Dict[str, int] = {}
    for table, sql in _PRUNE_SQL.items():
        cutoff = _cutoff(RETENTION_DAYS[table])
        while True:
            if time.monotonic() >= deadline:
                return pruned, True
            with transaction(con):
                n = con.execute(sql, (cutoff, chunk)).rowcount
            if n:
                pruned[table] = pruned.get(table, 0) + n
            if n < chunk:
                break
    return pruned, False


def maintain(con: sqlite3.Connection, *, force: bool = False, today: Optional[dt.date] = None) -> MaintainReport:
    """Once a day: prune if retention is on, refresh statistics, checkpoint the -wal file.

    Run on Asgard's own connection, outside any transaction. A busy database just means less
    gets done today.
    """
    started = time.monotonic()
    stamp = (today or dt.date.today()).isoformat()
    report = MaintainReport(retention=retention_on(con))
    if not force and _meta(con, "maintained_on") == stamp:
        return report
    report.ran = True
    if report.retention:
        report.pruned, report.more_to_prune = prune(con)
    con.execute("PRAGMA optimize")
    row = con.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    report.checkpoint = (int(row[0]), int(row[1]), int(row[2])) if row else None
    with transaction(con):
        _set_meta(con, "maintained_on", stamp)
    report.seconds = time.monotonic() - started
    return report



# --------------------------------------------------------------------------
# The schema itself: Muninn's protections must all be there
# --------------------------------------------------------------------------

_SCHEMA_CACHE: Dict[int, Dict[Tuple[str, str], str]] = {}


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql or "").strip()


def expected_schema(version: int) -> Dict[Tuple[str, str], str]:
    """{(type, name): sql} for every table, index, trigger and view migrations 1..version create."""
    if version not in _SCHEMA_CACHE:
        mem = sqlite3.connect(":memory:", isolation_level=None)
        try:
            for m in available_migrations():
                if m.number > version:
                    break
                for stmt in _migration_statements(m):
                    mem.execute(stmt)
            _SCHEMA_CACHE[version] = _objects(mem)
        finally:
            mem.close()
    return _SCHEMA_CACHE[version]


def _objects(con: sqlite3.Connection) -> Dict[Tuple[str, str], str]:
    return {(r[0], r[1]): _norm(r[2]) for r in con.execute(
        "SELECT type, name, sql FROM main.sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}


@dataclass
class SchemaDrift:
    missing: List[Tuple[str, str]] = field(default_factory=list)
    changed: List[Tuple[str, str]] = field(default_factory=list)
    extra: List[Tuple[str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.missing or self.changed or self.extra)


def schema_drift(con: sqlite3.Connection) -> SchemaDrift:
    """How the file's schema differs from what its migrations made (a dropped trigger, an added one)."""
    want = expected_schema(user_version(con))
    have = _objects(con)
    return SchemaDrift(missing=sorted(k for k in want if k not in have),
                       changed=sorted(k for k in want if k in have and have[k] != want[k]),
                       extra=sorted(k for k in have if k not in want))


def repair_schema(con: sqlite3.Connection) -> Tuple[List[str], List[str]]:
    """Put back missing or altered triggers, indexes and views, and drop ones no migration made.

    Returns (what was done, what couldn't be). Tables can't be rebuilt this way: a missing or
    altered table is reported for a restore. Run on Asgard's own connection.
    """
    drift = schema_drift(con)
    if not drift:
        return [], []
    want = expected_schema(user_version(con))
    done: List[str] = []
    failed: List[str] = []
    for kind, name in drift.missing + drift.changed + drift.extra:
        if kind == "table":
            failed.append(f"table {name} is {'missing' if (kind, name) in drift.missing else 'not as Asgard made it'}")
    fixable = [k for k in drift.extra if k[0] in ("trigger", "view", "index")] + \
              [k for k in drift.changed if k[0] in ("trigger", "view", "index")] + \
              [k for k in drift.missing if k[0] in ("trigger", "view", "index")]
    for kind, name in fixable:
        try:
            with transaction(con):
                if (kind, name) in drift.extra or (kind, name) in drift.changed:
                    con.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')
                if (kind, name) not in drift.extra:
                    con.execute(want[(kind, name)])
            done.append(f"{'removed' if (kind, name) in drift.extra else 'restored'} {kind} {name}")
        except sqlite3.DatabaseError as exc:
            failed.append(f"{kind} {name}: {exc}")
    return done, failed


# --------------------------------------------------------------------------
# The full check
# --------------------------------------------------------------------------

@dataclass
class Finding:
    level: str          # 'error': something is wrong; 'warning': needs attention; 'info': for the record
    area: str
    message: str
    fix: str = ""

    def __str__(self) -> str:
        return f"[{self.level}] {self.area}: {self.message}" + (f"\n    Fix: {self.fix}" if self.fix else "")


@dataclass
class CheckReport:
    findings: List[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.level == "error" for f in self.findings)

    def add(self, level: str, area: str, message: str, fix: str = "") -> None:
        self.findings.append(Finding(level, area, message, fix))

    def of(self, area: str) -> List[Finding]:
        return [f for f in self.findings if f.area == area]


RESTORE_FIX = "Close every Asgard app, then run: Asgard.pyw --muninn restore"
REPAIR_FIX = "Run: Asgard.pyw --muninn repair"
_MARKER_SQL = "substr(comment, instr(comment, '[asgard:'), 19)"


def search_only(lines: List[str]) -> bool:
    """Whether SQLite's integrity report (or error) is about the search index alone, which a rebuild fixes."""
    return bool(lines) and all("fts5" in line.lower() for line in lines)


def file_check(con: sqlite3.Connection, *, full: bool = False) -> List[str]:
    """SQLite's own check as a list of lines; ['ok'] when healthy. A damaged search index can make
    the PRAGMA itself fail ('invalid fts5 file format'); that comes back as a line too."""
    try:
        return [str(r[0]) for r in con.execute("PRAGMA integrity_check(20)" if full else "PRAGMA quick_check(20)")]
    except sqlite3.DatabaseError as exc:
        if "fts5" in str(exc).lower():
            return [f"fts5: {exc}"]
        raise


def search_drift(con: sqlite3.Connection) -> Dict[str, Tuple[int, int, int]]:
    """{table: (missing, stale, orphaned)} search rows, only for tables that are out of step."""
    out: Dict[str, Tuple[int, int, int]] = {}
    for code, (table, title, body) in SEARCH_SOURCES.items():
        row = con.execute(
            f"WITH expected AS (SELECT id * 16 + {code} AS rid, ({title}) AS t, ({body}) AS b FROM {table}) "
            "SELECT (SELECT count(*) FROM expected e WHERE NOT EXISTS (SELECT 1 FROM search s WHERE s.rowid = e.rid)), "
            "       (SELECT count(*) FROM expected e JOIN search s ON s.rowid = e.rid "
            f"         WHERE s.title IS NOT e.t OR s.body IS NOT e.b OR s.kind IS NOT '{table}'), "
            f"       (SELECT count(*) FROM search s WHERE s.rowid % 16 = {code} "
            f"         AND NOT EXISTS (SELECT 1 FROM {table} x WHERE x.id = s.rowid / 16))").fetchone()
        if any(row):
            out[table] = (int(row[0]), int(row[1]), int(row[2]))
    unknown = con.execute("SELECT count(*) FROM search WHERE rowid % 16 NOT BETWEEN 1 AND ?",
                          (max(SEARCH_SOURCES),)).fetchone()[0]
    if unknown:
        out["(unknown)"] = (0, 0, int(unknown))
    return out


def _check_search(con: sqlite3.Connection, report: CheckReport) -> None:
    if con.execute("PRAGMA query_only").fetchone()[0]:
        report.add("info", "search", "The search index wasn't checked: this connection is read-only.")
        return
    try:
        # FTS5's own check is written as an INSERT, so it needs the write lock for a moment.
        retry_busy(lambda: con.execute("INSERT INTO search (search) VALUES ('integrity-check')"), total=10)
    except BusyError:
        report.add("info", "search", "The search index wasn't checked: another app was writing. Try again.")
        return
    except sqlite3.DatabaseError as exc:
        report.add("error", "search", f"The search index is damaged ({exc}). Your data is not affected.",
                   REPAIR_FIX)
        return
    try:
        drift = search_drift(con)
    except sqlite3.DatabaseError as exc:
        report.add("error", "search", f"The search index can't be read ({exc}).", REPAIR_FIX)
        return
    for table, (missing, stale, orphaned) in drift.items():
        parts = [f"{n} {what}" for n, what in ((missing, "missing"), (stale, "out of date"),
                                               (orphaned, "left over")) if n]
        report.add("warning", "search", f"Search entries for {table}: {', '.join(parts)}.", REPAIR_FIX)


def check(con: sqlite3.Connection, *, full: bool = True) -> CheckReport:
    """Everything that should never happen, as findings that each say what to do. Changes nothing."""
    report = CheckReport()
    lines = file_check(con, full=full)
    search_damaged = search_only(lines)
    if lines != ["ok"] and not search_damaged:
        report.add("error", "file", f"The database file is damaged: {'; '.join(lines[:3])}", RESTORE_FIX)
        return report                  # nothing below can be trusted on a damaged file
    if search_damaged:
        report.add("error", "search", f"The search index is damaged ({lines[0]}). Your data is not affected.",
                   REPAIR_FIX)

    drift = schema_drift(con)
    for kind, name in drift.missing:
        report.add("error", "schema", f"The {kind} {name} is missing, so a rule it enforced isn't enforced.",
                   RESTORE_FIX if kind == "table" else REPAIR_FIX)
    for kind, name in drift.changed:
        report.add("error", "schema", f"The {kind} {name} isn't as Asgard made it.",
                   RESTORE_FIX if kind == "table" else REPAIR_FIX)
    for kind, name in drift.extra:
        report.add("error" if kind == "trigger" else "warning", "schema",
                   f"The {kind} {name} isn't part of Muninn; something other than Asgard added it.",
                   "Remove it by hand" if kind == "table" else REPAIR_FIX)

    mode = con.execute("PRAGMA journal_mode").fetchone()[0].lower()
    if mode != "wal":
        report.add("warning", "file", f"The journal mode is {mode}, not wal, so apps block each other.",
                   "Open Asgard once; it switches the file back.")

    broken = con.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        tables = sorted({r[0] for r in broken})
        report.add("error", "links", f"{len(broken)} rows in {', '.join(tables)} point at rows that are gone.",
                   "Restore the newest backup from before this started, or ask for help; "
                   "these rows can't be read correctly.")

    if not search_damaged:
        _check_search(con, report)

    if user_version(con) >= 3:
        rows = con.execute("SELECT kind, ref_id, n, key, jira_worklog_ids FROM v_double_posts").fetchall()
    else:
        rows = []
    for r in rows:
        report.add("error", "worklogs",
                   f"{r['key']}: the time for {r['kind']} {r['ref_id']} is in Jira {r['n']} times "
                   f"(worklogs {r['jira_worklog_ids']}).",
                   f"Delete the extra worklog on {r['key']} in Jira; Odin's next sync records that.")
    for r in con.execute(f"SELECT {_MARKER_SQL} AS marker, count(*) AS n, group_concat(jira_worklog_id, ', ') AS ids "
                         "FROM worklogs WHERE state = 'posted' AND instr(comment, '[asgard:') > 0 "
                         "GROUP BY marker HAVING count(*) > 1"):
        report.add("error", "worklogs", f"One post ({r['marker']}) is in Jira {r['n']} times (worklogs {r['ids']}).",
                   "Delete the extra worklog in Jira; Odin's next sync records that.")
    for r in con.execute(
            "SELECT d.local_date, d.work_item_key, d.approved_minutes, (sum(w.seconds) + 59) / 60 AS sent "
            "FROM v_day_status d JOIN worklogs w ON w.work_item_id = d.work_item_id AND w.origin = 'baldur' "
            "AND w.state IN ('sending', 'posted') AND w.started_at >= d.day_start AND w.started_at < d.day_end "
            "GROUP BY d.local_date, d.work_item_key, d.approved_minutes HAVING sent > d.approved_minutes"):
        report.add("warning", "worklogs",
                   f"{r['work_item_key']} on {r['local_date']}: Asgard sent {r['sent']} min to Jira but you now "
                   f"approve {r['approved_minutes']} min (the figure changed after it was posted).",
                   f"Edit or delete the worklog on {r['work_item_key']} in Jira to match.")

    for table, col in KEY_COLUMNS:
        bad = [r[0] for r in con.execute(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL")
               if not keys.is_key(r[0])]
        if bad:
            shown = ", ".join(repr(k) for k in bad[:5]) + (" ..." if len(bad) > 5 else "")
            fix = ("Approve the day again with the key in capitals (Baldur: change the figure; it stores the "
                   "key correctly)" if table == "day_proposals" else
                   "Give the rows a proper key (PROJ-123) in the app that owns them")
            report.add("warning", "keys", f"{table}.{col} has {len(bad)} keys Odin can't match: {shown}.", fix + ".")

    stuck = con.execute("SELECT count(*) FROM worklogs WHERE state = 'sending' AND created_at < ?",
                        (ago(STALE_SENDING_SECONDS),)).fetchone()[0]
    if stuck:
        report.add("warning", "worklogs", f"{stuck} posts to Jira have been in doubt for over an hour.",
                   "Run Odin; it searches Jira for each one and settles it.")

    newest = int(con.execute("SELECT coalesce(max(id), 0) FROM events").fetchone()[0])
    ahead = con.execute("SELECT app, last_event_id FROM event_cursors WHERE last_event_id > ?", (newest,)).fetchall()
    for r in ahead:
        report.add("error", "events", f"{r['app']} has read up to event {r['last_event_id']}, but the newest is "
                   f"{newest}, so it would miss the next ones.", REPAIR_FIX)

    abandoned = con.execute("SELECT count(*) FROM sync_runs WHERE status = 'running' AND started_at < ?",
                            (ago(6 * 3600),)).fetchone()[0]
    if abandoned:
        report.add("info", "runs", f"{abandoned} sync runs stopped without finishing.",
                   "Nothing to do; Asgard marks them failed when it next starts.")
    return report


# --------------------------------------------------------------------------
# Repairs
# --------------------------------------------------------------------------

def _fts5_version() -> str:
    mem = sqlite3.connect(":memory:")
    try:
        mem.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return str(mem.execute("SELECT v FROM t_config WHERE k = 'version'").fetchone()[0])
    finally:
        mem.close()


def repair_search(con: sqlite3.Connection) -> int:
    """Rebuild the search index from its tables, in one transaction. Returns the entries written.

    Works on a damaged index too: FTS5's own 'rebuild' first (after putting back its format
    version if that was lost), then every entry rewritten from the tables it indexes. Run on
    Asgard's own connection: apps can't write the index's internal tables.
    """
    total = 0
    with transaction(con):
        try:
            con.execute("INSERT INTO search (search) VALUES ('rebuild')")
        except sqlite3.DatabaseError as exc:
            if "fts5 file format" not in str(exc).lower():
                raise
            con.execute("INSERT INTO search_config (k, v) VALUES ('version', ?) "
                        "ON CONFLICT (k) DO UPDATE SET v = excluded.v", (int(_fts5_version()),))
            con.execute("INSERT INTO search (search) VALUES ('rebuild')")
        con.execute("DELETE FROM search")
        for code, (table, title, body) in SEARCH_SOURCES.items():
            total += con.execute(f"INSERT INTO search (rowid, title, body, kind) "
                                 f"SELECT id * 16 + {code}, {title}, {body}, '{table}' FROM {table}").rowcount
        con.execute("INSERT INTO search (search) VALUES ('optimize')")
    return total


def repair_cursors(con: sqlite3.Connection) -> List[str]:
    """Bring event cursors that are past the newest event back to it. Returns the apps changed."""
    with transaction(con):
        newest = int(con.execute("SELECT coalesce(max(id), 0) FROM events").fetchone()[0])
        rows = con.execute("UPDATE event_cursors SET last_event_id = ?, updated_at = ? WHERE last_event_id > ? "
                           "RETURNING app", (newest, utcnow(), newest)).fetchall()
    return sorted(r[0] for r in rows)


def repair(con: sqlite3.Connection) -> List[str]:
    """The repairs that need no judgement. Returns what was done, for the person running it."""
    lines = file_check(con)
    if lines != ["ok"] and not search_only(lines):
        raise MuninnError("The database file itself is damaged, which a repair can't fix. " + RESTORE_FIX)
    done, failed = repair_schema(con)
    out = [f"Schema: {d}." for d in done]
    out.append(f"Rebuilt the search index ({repair_search(con)} entries).")
    moved = repair_cursors(con)
    if moved:
        out.append(f"Moved the event cursors of {', '.join(moved)} back to the newest event.")
    out.extend(f"Couldn't repair: {f}. {RESTORE_FIX}" for f in failed)
    return out
