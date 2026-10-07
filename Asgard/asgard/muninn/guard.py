"""Rule 5 in code: an app writes only the tables it owns.

open_app() installs an SQLite authorizer on the connection it returns. SQLite asks the authorizer
about every statement as it is compiled, so a write to a table the app doesn't own is refused
before it runs, with the same "not authorized" error whatever the statement. The guard also
refuses schema changes (DDL), ATTACH, and the PRAGMAs that could switch the protections off
(user_version, query_only, writable_schema, foreign_keys = OFF and the like).

What it allows, and why:
- Writes that come from a trigger (the FTS search index, status history) are part of the schema,
  which only Asgard can change, so they are always allowed. That's also how a delete in one
  table can cascade into another.
- Shared tables take the operations every app needs: sources and identities (insert, update;
  identities also delete), sync_runs and sync_cursors (insert, update), events (insert only),
  event_cursors (insert, update). Row rules (TEMP triggers, row_rules()) keep each app to its own
  rows in them: its own events, runs, cursors and identity kinds.
- PRAGMAs are an allow-list: an app may read any, and set only busy_timeout, cache_size and
  foreign_keys = ON.
- The "muninn" identity (Asgard itself) may write anything.
- The search index's own tables (search_data, search_content and the rest). FTS5 prepares its
  writes to them as separate statements, so the authorizer sees them without a trigger name; to
  keep ordinary SQL from writing them directly, install() also switches on SQLite's defensive
  mode where Python offers it (3.12 and newer), which makes those tables read-only to apps.

This is a guard against mistakes and against one app's bug corrupting another's data. It is not
a defence against an app that opens the file with plain sqlite3: the triggers and CHECKs in the
schema, and `Asgard.pyw --muninn check`, cover that case.
"""
from __future__ import annotations

import sqlite3
import threading
from collections import deque
from typing import Deque, Dict, FrozenSet, List, Optional, Tuple

# SQLite authorizer action codes (stable parts of SQLite's C API; sqlite3 only names them in 3.11+).
DELETE, INSERT, PRAGMA, READ, SELECT, TRANSACTION, UPDATE, ATTACH, DETACH = 9, 18, 19, 20, 21, 22, 23, 24, 25
FUNCTION, SAVEPOINT, RECURSIVE = 31, 32, 33
OK, DENY = 0, 1                      # SQLITE_OK, SQLITE_DENY
_DML = {INSERT: "insert into", UPDATE: "update", DELETE: "delete from"}
_HARMLESS = {READ, SELECT, TRANSACTION, FUNCTION, SAVEPOINT, RECURSIVE}

# Which app owns each table. Every table in the schema must appear here or in SHARED (a test checks).
OWNERS: Dict[str, FrozenSet[str]] = {}


def _own(app: str, *tables: str) -> None:
    for t in tables:
        OWNERS[t] = OWNERS.get(t, frozenset()) | {app}


_own("muninn", "meta")
_own("odin", "work_items", "work_item_transitions", "work_item_aliases", "calendar_events", "worklogs")
_own("baldur", "repos", "commits", "commit_work_items", "reflog_entries", "pull_requests", "pull_request_commits",
     "pr_reviews",
     "estimate_runs", "work_sessions", "session_commits", "session_allocations", "day_proposals",
     "calibration_runs", "time_actuals")
_own("loki", "meetings", "action_items", "blufs")
# A BLUF's citations live beside review drafts' (owner_type 'bluf'); asgard.muninn.citations keeps
# each app to its own owner_type, which a table-level guard can't see.
_own("loki", "citations")
_own("freya", "accomplishments", "review_periods", "review_drafts", "citations")
_own("heimdall", "submissions", "submission_status_history")
_own("bifrost", "submissions", "submission_status_history")

SHARED: Dict[str, FrozenSet[int]] = {
    "sources": frozenset({INSERT, UPDATE}),
    "identities": frozenset({INSERT, UPDATE, DELETE}),
    "sync_runs": frozenset({INSERT, UPDATE}),
    "sync_cursors": frozenset({INSERT, UPDATE}),
    "events": frozenset({INSERT}),
    "event_cursors": frozenset({INSERT, UPDATE}),
}

# PRAGMAs an app may *set*; everything else may only be read. An allow-list, because SQLite reads
# any value it doesn't recognise as false ("PRAGMA foreign_keys = banana" turns them off), and some
# PRAGMAs (hard_heap_limit, soft_heap_limit) reach every connection in the process.
_SETTABLE = {"busy_timeout", "cache_size"}
_FK_ON = {"1", "on", "true", "yes"}
# PRAGMAs that take an argument to *report* on something, not to change it.
_QUERY_WITH_ARG = {"table_info", "table_xinfo", "index_list", "index_info", "index_xinfo", "foreign_key_list",
                   "foreign_key_check", "integrity_check", "quick_check", "table_list"}
# SQLite's own tables an app's statements touch legitimately; sqlite_stat1 (the query planner's
# statistics every connection loads) and sqlite_dbpage (raw pages) are not among them.
_SQLITE_OK = {"sqlite_sequence"}
# FTS5 keeps the search index in these; it writes them itself whenever a trigger updates `search`.
FTS_SHADOW = frozenset({"search_data", "search_idx", "search_content", "search_docsize", "search_config"})

_lock = threading.Lock()
_denials: Deque[Tuple[str, str]] = deque(maxlen=20)


def known_apps() -> FrozenSet[str]:
    """The identities open_app() accepts (sync.APPS without re-importing it here)."""
    return frozenset({"muninn", "huginn", "odin", "baldur", "loki", "freya", "heimdall", "bifrost", "ysildir",
                      "valkyrie"})


def may_write(app: str, table: str, op: int) -> bool:
    """Whether `app` may run `op` (INSERT, UPDATE or DELETE) on `table`, when not inside a trigger."""
    if app == "muninn":
        return True
    name = table.lower()
    if app in OWNERS.get(name, ()):
        return True
    return op in SHARED.get(name, ())


def decide(app: str, action: int, arg1: Optional[str], arg2: Optional[str], source: Optional[str]) -> Tuple[int, str]:
    """(OK or DENY, reason) for one authorizer call."""
    if action in _HARMLESS:
        return OK, ""
    if app == "muninn":
        return OK, ""
    if action in _DML:
        if source is not None:                # a trigger of the schema is doing it
            return OK, ""
        table = arg1 or ""
        if table.lower() in FTS_SHADOW or table.lower() in _SQLITE_OK:
            return OK, ""                      # FTS5's and SQLite's own bookkeeping
        if table.lower().startswith("sqlite_"):
            return DENY, f"{app} may not write SQLite's own table {table}"
        if may_write(app, table, action):
            return OK, ""
        owners = sorted(OWNERS.get(table.lower(), ())) or ["Asgard"]
        return DENY, f"{app} may not {_DML[action]} {table}, which {'/'.join(owners)} owns"
    if action == PRAGMA:
        name = (arg1 or "").lower()
        if arg2 is None or name in _QUERY_WITH_ARG or name in _SETTABLE:
            return OK, ""
        if name == "foreign_keys":
            if str(arg2).strip().strip("'\"").lower() in _FK_ON:
                return OK, ""
            return DENY, f"{app} may not turn foreign key checks off"
        return DENY, f"{app} may not change PRAGMA {name}"
    if action in (ATTACH, DETACH):
        return DENY, f"{app} may not attach another database"
    # Everything left is schema change: CREATE/DROP/ALTER of tables, indexes, triggers, views, REINDEX, ANALYZE.
    return DENY, f"{app} may not change Muninn's schema; only Asgard migrates it"


# Which identity kinds each app may add, change or remove. Identities decide every is_mine flag,
# so Loki adding a git email would make someone else's commits count as yours.
IDENTITY_KINDS: Dict[str, FrozenSet[str]] = {
    "odin": frozenset({"jira_user", "m365_upn", "display_name"}),
    "baldur": frozenset({"git_email", "git_name", "github_login", "display_name"}),
    "loki": frozenset({"m365_upn", "display_name"}),
    "valkyrie": frozenset({"git_email", "git_name", "jira_user", "github_login", "m365_upn", "display_name"}),
}


def row_rules(app: str) -> List[str]:
    """TEMP triggers that keep an app to its own rows in the shared tables.

    The authorizer sees tables, not rows, so these do the rest: an app adds events and sync runs
    only under its own name, moves only its own event cursor, saves a sync cursor only for its own
    run, never renames or re-kinds a source, and touches only the identity kinds it collects.
    TEMP triggers live on this connection alone, and the guard refuses DROP TRIGGER afterwards.
    """
    a = app.replace("'", "")
    kinds = ", ".join(f"'{k}'" for k in sorted(IDENTITY_KINDS.get(app, frozenset({"display_name"}))))

    def rule(name: str, when: str, table: str, event: str, why: str) -> str:
        return (f"CREATE TEMP TRIGGER guard_{name} BEFORE {event} ON main.{table} WHEN {when} "
                f"BEGIN SELECT RAISE(ABORT, '{a}: {why}'); END")

    run_app = "(SELECT app FROM main.sync_runs WHERE id = {})"
    return [
        rule("events", f"new.app IS NOT '{a}'", "events", "INSERT", "events are added under your own app name"),
        rule("event_cursor_ins", f"new.app IS NOT '{a}'", "event_cursors", "INSERT", "only your own event cursor"),
        rule("event_cursor_upd", f"old.app IS NOT '{a}' OR new.app IS NOT '{a}'", "event_cursors", "UPDATE",
             "only your own event cursor"),
        rule("runs_ins", f"new.app IS NOT '{a}'", "sync_runs", "INSERT", "sync runs are recorded under your own name"),
        rule("runs_upd", f"old.app IS NOT '{a}' OR new.app IS NOT '{a}'", "sync_runs", "UPDATE",
             "only your own sync runs"),
        rule("cursor_ins", f"{run_app.format('new.run_id')} IS NOT '{a}'", "sync_cursors", "INSERT",
             "a sync cursor is saved by the run that read it"),
        rule("cursor_upd", f"{run_app.format('new.run_id')} IS NOT '{a}' OR (old.run_id IS NOT NULL AND "
             f"{run_app.format('old.run_id')} IS NOT '{a}')", "sync_cursors", "UPDATE",
             "only the cursors of your own sync runs"),
        rule("sources", "new.kind IS NOT old.kind OR new.name IS NOT old.name", "sources", "UPDATE",
             "a source keeps its kind and name"),
        rule("identities_ins", f"new.kind NOT IN ({kinds})", "identities", "INSERT",
             "that kind of identity belongs to another app"),
        rule("identities_upd", f"old.kind NOT IN ({kinds}) OR new.kind NOT IN ({kinds})", "identities", "UPDATE",
             "that kind of identity belongs to another app"),
        rule("identities_del", f"old.kind NOT IN ({kinds})", "identities", "DELETE",
             "that kind of identity belongs to another app"),
    ]


def install(con: sqlite3.Connection, app: str) -> None:
    """Make `con` refuse what `app` may not do. Call it last, after the connection's own PRAGMAs."""
    if app not in known_apps():
        raise ValueError(f"unknown app {app!r}")
    if app != "muninn" and not con.execute("PRAGMA query_only").fetchone()[0]:
        for sql in row_rules(app):            # before the authorizer, which refuses DDL
            con.execute(sql)

    def authorizer(action: int, arg1: Optional[str], arg2: Optional[str], dbname: Optional[str],
                   source: Optional[str]) -> int:
        verdict, reason = decide(app, action, arg1, arg2, source)
        if verdict != OK:
            with _lock:
                _denials.append((app, reason))
        return verdict

    con.set_authorizer(authorizer)
    defensive = getattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE", None)
    if defensive is not None and hasattr(con, "setconfig"):
        con.setconfig(defensive, True)        # shadow tables read-only to ordinary SQL (Python 3.12+)


def last_denial(app: Optional[str] = None) -> Optional[str]:
    """Why the most recent "not authorized" happened in this process (for an app, if given)."""
    with _lock:
        for who, reason in reversed(_denials):
            if app is None or who == app:
                return reason
    return None


def describe(exc: BaseException) -> str:
    """exc as text, with the reason added when SQLite only said "not authorized"."""
    text = str(exc)
    if "not authorized" in text.lower():
        reason = last_denial()
        if reason:
            return f"{text} ({reason})"
    return text
