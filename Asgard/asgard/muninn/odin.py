"""Odin's side of Muninn: Jira issues, key lookups, calendar and worklogs.

Odin is the only app that writes these tables and the only app that writes
to Jira. This module does the database half; Odin's existing JiraClient and
calendar code do the network half and pass the JSON they already receive:

    ctx = odin.JiraContext.load(con, jira_source_id)
    with Run(con, "odin", jira_source_id, "issues") as run:
        since = odin.jql_time(run.cursor) if run.cursor else None
        for page in jira.search_pages(jql, expand="changelog", updated_since=since):   # no lock held
            with run.batch():                                                         # one short write
                for issue in page:
                    odin.upsert_issue(run, issue, ctx)
                    run.advance_cursor(odin.parse_time(issue["fields"]["updated"]))

Posting goes through a 'sending' row written before the Jira call, with a
marker such as [asgard:b-9f3c1a2b] at the end of the comment, so a crash or
a timeout can never post the same time twice:

    for due in odin.posts_due(con):
        post = odin.begin_post(con, due["proposal_id"])
        if post is None:
            continue                          # someone else posted it first
        try:
            created = jira.add_worklog(post.key, post.started, post.seconds, post.comment)
        except JiraRejected as exc:           # a definite 4xx answer
            odin.fail_post(con, post.worklog_id, str(exc))
        except (Timeout, ConnectionError):
            pass                              # unknown outcome: settled later, below
        else:
            odin.finish_post(con, post.worklog_id, created["id"])

    for stuck in odin.stuck_posts(con):       # posts whose outcome nobody saw
        found = jira.find_worklog_with(stuck["key"], stuck["marker"])
        odin.resolve_stuck(con, stuck["worklog_id"], found and found["id"], searched=True)
"""
from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import baldur as baldur_rules
from .db import MuninnError, ago, from_ts, to_ts, transaction, utcnow
from .keys import normalize_key
from .redact import scrub
from .sync import Run, emit, identities

# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

_TIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?"
                      r"\s*(Z|[+-]\d{2}:?\d{2})?$")


def parse_time(text: Optional[str], *, naive: str = "utc") -> Optional[str]:
    """Jira or Graph time text as Muninn's UTC text.

    Accepts 2026-09-30T09:00:00.000-0400, ...Z, +00:00 and Graph's seven-digit
    fractions. Text without an offset is read as UTC, or as local time with
    naive="local".
    """
    if not text:
        return None
    m = _TIME_RE.match(text.strip())
    if not m:
        raise ValueError(f"unrecognised time {text!r}")
    y, mo, d, h, mi, s, _frac, off = m.groups()
    try:
        value = dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0))
        if off in (None, "Z"):
            if off is None and naive == "local":
                return to_ts(value.astimezone())
            return to_ts(value.replace(tzinfo=dt.timezone.utc))
        sign = -1 if off[0] == "-" else 1
        digits = off[1:].replace(":", "")
        offset = dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
        return to_ts(value.replace(tzinfo=dt.timezone(sign * offset)))
    except (OverflowError, OSError) as exc:      # year 1 or 9999 shifted past the calendar's ends
        raise ValueError(f"time {text!r} is outside the dates Muninn stores") from exc


def jira_time(ts: str) -> str:
    """Muninn UTC text as Jira's 'started' format in local time: 2026-10-01T09:20:00.000-0400."""
    return from_ts(ts).astimezone().strftime("%Y-%m-%dT%H:%M:%S.000%z")


# Jira reads a JQL date in the Jira profile's time zone, which needn't be this computer's: a
# laptop in Berlin with a New York profile would otherwise skip six hours of updates every run.
# Zones are at most 26 hours apart (UTC-12 to UTC+14), and Windows has no time-zone database
# to convert with, so the cursor is simply read back that far. Re-reads write nothing twice.
JQL_OVERLAP_MINUTES = 26 * 60


def jql_time(ts: str, overlap_minutes: int = JQL_OVERLAP_MINUTES) -> str:
    """A cursor as a JQL date ('2026/09/30 11:05'), early enough for any Jira profile's time zone,
    the fall-back hour, and JQL's whole minutes."""
    return (from_ts(ts) - dt.timedelta(minutes=overlap_minutes)).astimezone().strftime("%Y/%m/%d %H:%M")


# --------------------------------------------------------------------------
# Jira context
# --------------------------------------------------------------------------

_CATEGORY = {"new": "todo", "undefined": "todo", "indeterminate": "in_progress", "done": "done"}
_DONE_NAMES = {"done", "closed", "resolved", "complete", "completed", "cancelled", "canceled"}
_TODO_NAMES = {"to do", "todo", "open", "backlog", "new", "reopened", "selected for development"}


@dataclass
class JiraContext:
    """What Odin knows about its Jira that the JSON alone doesn't say."""
    source_id: int
    base_url: str
    me: Set[str] = field(default_factory=set)          # your usernames, keys or account ids, lower-case
    epic_field: Optional[str] = None                   # e.g. customfield_10008 ("Epic Link")
    story_points_field: Optional[str] = None
    sprint_field: Optional[str] = None
    status_categories: Dict[str, str] = field(default_factory=dict)   # status name, lower-case -> category

    @classmethod
    def load(cls, con: sqlite3.Connection, source_id: int, **settings: Any) -> "JiraContext":
        row = con.execute("SELECT base_url FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise MuninnError(f"Muninn has no source with id {source_id}.")
        base = settings.pop("base_url", None) or row[0] or ""
        return cls(source_id=source_id, base_url=base.rstrip("/"), me=identities(con, "jira_user"), **settings)

    def is_me(self, person: Optional[Dict[str, Any]]) -> bool:
        if not person:
            return False
        return any(str(person.get(k, "")).lower() in self.me
                   for k in ("name", "key", "accountId", "emailAddress") if person.get(k))

    def category(self, status_name: str) -> str:
        known = self.status_categories.get(status_name.lower())
        if known:
            return known
        name = status_name.lower()
        return "done" if name in _DONE_NAMES else "todo" if name in _TODO_NAMES else "in_progress"


def field_ids(fields_json: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """Custom field ids by role, from GET /rest/api/2/field."""
    wanted = {"epic link": "epic_field", "story points": "story_points_field",
              "story point estimate": "story_points_field", "sprint": "sprint_field"}
    found: Dict[str, str] = {}
    for f in fields_json:
        role = wanted.get(str(f.get("name", "")).lower())
        if role and role not in found:
            found[role] = str(f["id"])
    return found


def status_categories(statuses_json: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """Status name -> category, from GET /rest/api/2/status."""
    out = {}
    for s in statuses_json:
        key = (s.get("statusCategory") or {}).get("key")
        if s.get("name") and key in _CATEGORY:
            out[s["name"].lower()] = _CATEGORY[key]
    return out


# --------------------------------------------------------------------------
# Issues
# --------------------------------------------------------------------------

_SPRINT_NAME_RE = re.compile(r"name=([^,\]]+)")

_ISSUE_COLUMNS = ("source_id", "jira_id", "key", "project_key", "issue_type", "summary", "status",
                  "status_category", "resolution", "priority", "epic_key", "parent_key", "assignee",
                  "reporter", "is_mine", "labels", "components", "story_points", "sprint", "url",
                  "created_at", "updated_at", "resolved_at", "due_on")
_WATCHED = ("key", "summary", "status", "status_category", "resolution", "assignee")


def _person(p: Optional[Dict[str, Any]]) -> Optional[str]:
    if not p:
        return None
    return p.get("name") or p.get("key") or p.get("accountId") or p.get("displayName")


def _sprint(value: Any) -> Optional[str]:
    if not value:
        return None
    last = value[-1] if isinstance(value, list) else value
    if isinstance(last, dict):
        return last.get("name")
    m = _SPRINT_NAME_RE.search(str(last))
    return m.group(1) if m else str(last)


def issue_row(raw: Dict[str, Any], ctx: JiraContext, mine: Optional[bool] = None) -> Dict[str, Any]:
    """A work_items row from Jira's REST issue JSON."""
    f = raw.get("fields") or {}
    status = (f.get("status") or {})
    cat_key = (status.get("statusCategory") or {}).get("key")
    status_name = status.get("name") or "Unknown"
    epic = f.get(ctx.epic_field) if ctx.epic_field else None
    points = f.get(ctx.story_points_field) if ctx.story_points_field else None
    key = raw["key"]
    return {
        "source_id": ctx.source_id,
        "jira_id": str(raw["id"]),
        "key": key,
        "project_key": (f.get("project") or {}).get("key") or key.rsplit("-", 1)[0],
        "issue_type": (f.get("issuetype") or {}).get("name") or "Issue",
        "summary": f.get("summary") or "",
        "status": status_name,
        "status_category": _CATEGORY.get(cat_key) or ctx.category(status_name),
        "resolution": (f.get("resolution") or {}).get("name"),
        "priority": (f.get("priority") or {}).get("name"),
        "epic_key": epic if isinstance(epic, str) else (epic or {}).get("key") if isinstance(epic, dict) else None,
        "parent_key": (f.get("parent") or {}).get("key"),
        "assignee": _person(f.get("assignee")),
        "reporter": _person(f.get("reporter")),
        "is_mine": 1 if (mine if mine is not None else ctx.is_me(f.get("assignee"))) else 0,
        "labels": json.dumps(list(f.get("labels") or [])),
        "components": json.dumps([c.get("name") for c in (f.get("components") or []) if c.get("name")]),
        "story_points": float(points) if isinstance(points, (int, float)) else None,
        "sprint": _sprint(f.get(ctx.sprint_field)) if ctx.sprint_field else None,
        "url": f"{ctx.base_url}/browse/{key}",
        "created_at": parse_time(f.get("created")) or utcnow(),
        "updated_at": parse_time(f.get("updated")) or utcnow(),
        "resolved_at": parse_time(f.get("resolutiondate")),
        "due_on": f.get("duedate") or None,
    }


def issue_transitions(raw: Dict[str, Any], ctx: JiraContext) -> List[Dict[str, Any]]:
    """Status changes from the issue's changelog (request it with expand=changelog)."""
    out = []
    current = ((raw.get("fields") or {}).get("status") or {})
    current_name = current.get("name")
    current_cat = _CATEGORY.get((current.get("statusCategory") or {}).get("key"))
    for history in (raw.get("changelog") or {}).get("histories") or []:
        n = 0
        for item in history.get("items") or []:
            if item.get("field") != "status":
                continue
            to = item.get("toString") or ""
            cat = current_cat if (to == current_name and current_cat) else ctx.category(to)
            out.append({"changelog_id": f"{history['id']}" + (f":{n}" if n else ""),
                        "at": parse_time(history.get("created")) or utcnow(),
                        "from_status": item.get("fromString"), "to_status": to, "to_category": cat,
                        "author": _person(history.get("author")), "by_me": 1 if ctx.is_me(history.get("author")) else 0})
            n += 1
    return out


@dataclass
class IssueResult:
    id: int
    key: str
    inserted: bool
    changed: bool
    events: List[str] = field(default_factory=list)


_UPSERT_ISSUE = (
    f"INSERT INTO work_items ({', '.join(_ISSUE_COLUMNS)}, first_seen_at, last_seen_at, run_id) "
    f"VALUES ({', '.join(':' + c for c in _ISSUE_COLUMNS)}, :now, :now, :run_id) "
    "ON CONFLICT (source_id, jira_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _ISSUE_COLUMNS
                if c not in ("source_id", "jira_id", "is_mine", "created_at"))
    + ", is_mine = max(work_items.is_mine, excluded.is_mine), deleted_at = NULL, run_id = excluded.run_id "
    # Jira's milliseconds are dropped, so a second change in the same second compares the values too.
    "WHERE excluded.updated_at > work_items.updated_at "
    "OR (excluded.updated_at = work_items.updated_at AND ("
    + " OR ".join(f"excluded.{c} IS NOT work_items.{c}" for c in _ISSUE_COLUMNS
                  if c not in ("source_id", "jira_id", "is_mine", "created_at", "updated_at"))
    + ")) OR excluded.is_mine > work_items.is_mine OR work_items.deleted_at IS NOT NULL "
    "RETURNING id")


def set_current_key(con: sqlite3.Connection, work_item_id: int, key: str, now: Optional[str] = None) -> None:
    """Make key the issue's current alias; its earlier current key becomes 'moved'."""
    now = now or utcnow()
    con.execute("UPDATE work_item_aliases SET status = 'moved', checked_at = ? "
                "WHERE work_item_id = ? AND status = 'current' AND key <> ?", (now, work_item_id, key))
    con.execute("INSERT INTO work_item_aliases (key, work_item_id, status, checked_at) VALUES (?, ?, 'current', ?) "
                "ON CONFLICT (key) DO UPDATE SET work_item_id = excluded.work_item_id, status = 'current', "
                "checked_at = excluded.checked_at "
                "WHERE work_item_aliases.status <> 'current' OR work_item_aliases.work_item_id IS NOT excluded.work_item_id",
                (key, work_item_id, now))


def upsert_issue(run: Run, raw: Dict[str, Any], ctx: JiraContext, mine: Optional[bool] = None) -> IssueResult:
    """Store one issue from Jira, with its aliases, status history and events.

    mine=True marks issues from an `assignee was currentUser()` query, which
    were yours at some point even if the assignee has changed since.
    """
    row = issue_row(raw, ctx, mine)             # parse everything before writing anything
    transitions = issue_transitions(raw, ctx)
    with run.batch():
        return _upsert_issue(run, row, transitions)


def _upsert_issue(run: Run, row: Dict[str, Any], transitions: List[Dict[str, Any]]) -> IssueResult:
    con = run.con
    run.items_seen += 1
    before = con.execute("SELECT * FROM work_items WHERE source_id = ? AND jira_id = ?",
                         (row["source_id"], row["jira_id"])).fetchone()
    got = con.execute(_UPSERT_ISSUE, dict(row, now=run.now, run_id=run.id)).fetchone()
    if got is None:  # unchanged in Jira
        item_id = int(before["id"])
        con.execute("UPDATE work_items SET last_seen_at = ? WHERE id = ?", (run.now, item_id))
        set_current_key(con, item_id, before["key"], run.now)
        _store_transitions(con, item_id, transitions)
        return IssueResult(item_id, before["key"], inserted=False, changed=False)

    item_id = int(got[0])
    con.execute("UPDATE work_items SET last_seen_at = ? WHERE id = ?", (run.now, item_id))
    set_current_key(con, item_id, row["key"], run.now)
    _store_transitions(con, item_id, transitions)
    run.items_changed += 1
    result = IssueResult(item_id, row["key"], inserted=before is None, changed=True)

    if before is None:
        result.events.append(run_emit(run, "work_item.created", item_id, row["key"],
                                      {"status": row["status"], "summary": row["summary"]}))
    else:
        changes = {c: [before[c], row[c]] for c in _WATCHED if before[c] != row[c]}
        result.events.append(run_emit(run, "work_item.updated", item_id, row["key"], changes))
        if before["key"] != row["key"]:
            result.events.append(run_emit(run, "work_item.moved", item_id, row["key"],
                                          {"from": before["key"], "to": row["key"]}))
    was_done = before is not None and before["status_category"] == "done" and before["deleted_at"] is None
    if row["status_category"] == "done" and not was_done:
        result.events.append(run_emit(run, "work_item.done", item_id, row["key"],
                                      {"resolution": row["resolution"], "resolved_at": row["resolved_at"]}))
    elif was_done and row["status_category"] != "done":
        result.events.append(run_emit(run, "work_item.reopened", item_id, row["key"], {"status": row["status"]}))
    return result


def run_emit(run: Run, kind: str, item_id: int, key: str, payload: Dict[str, Any]) -> str:
    run.emit(kind, "work_items", item_id, key, payload)
    return kind


def _store_transitions(con: sqlite3.Connection, item_id: int, transitions: Iterable[Dict[str, Any]]) -> None:
    for t in transitions:
        con.execute("INSERT INTO work_item_transitions (work_item_id, changelog_id, at, from_status, to_status, "
                    "to_category, author, by_me) VALUES (:id, :changelog_id, :at, :from_status, :to_status, "
                    ":to_category, :author, :by_me) ON CONFLICT (work_item_id, changelog_id) DO NOTHING",
                    dict(t, id=item_id))


def mark_issue_deleted(run: Run, jira_id: str) -> bool:
    """Record that Jira answered 404 for an issue Muninn holds (a deletion, not a scope change)."""
    with run.batch():
        row = run.con.execute("UPDATE work_items SET deleted_at = ? WHERE source_id = ? AND jira_id = ? "
                              "AND deleted_at IS NULL RETURNING id, key",
                              (run.now, run.source_id, str(jira_id))).fetchone()
        if row:
            run.emit("work_item.deleted", "work_items", row[0], row[1])
    return row is not None


def not_seen_since(con: sqlite3.Connection, source_id: int, since: str) -> List[sqlite3.Row]:
    """Issues no sync has seen since a time. Check each with Jira before calling it deleted:
    an issue can leave Odin's scope without being deleted."""
    return con.execute("SELECT id, jira_id, key FROM work_items WHERE source_id = ? AND last_seen_at < ? "
                       "AND deleted_at IS NULL ORDER BY id", (source_id, since)).fetchall()


# --------------------------------------------------------------------------
# Keys other apps mention
# --------------------------------------------------------------------------

def unknown_keys(con: sqlite3.Connection, limit: int = 100) -> List[str]:
    """Keys to look up one at a time with GET /rest/api/2/issue/{key}."""
    return [r[0] for r in con.execute("SELECT key FROM v_unknown_keys ORDER BY key LIMIT ?", (limit,))]


def record_lookup(run: Run, key: str, raw: Optional[Dict[str, Any]], ctx: JiraContext) -> Optional[IssueResult]:
    """Store what Jira answered for a key: the issue (perhaps under a new key) or None for a 404."""
    if raw is None:
        with run.batch():
            run.con.execute("INSERT INTO work_item_aliases (key, work_item_id, status, checked_at) "
                            "VALUES (?, NULL, 'not_found', ?) ON CONFLICT (key) DO UPDATE SET "
                            "checked_at = excluded.checked_at WHERE work_item_aliases.status = 'not_found'",
                            (key, run.now))
        return None
    row, transitions = issue_row(raw, ctx), issue_transitions(raw, ctx)
    with run.batch():
        result = _upsert_issue(run, row, transitions)
        if result.key != key:
            run.con.execute("INSERT INTO work_item_aliases (key, work_item_id, status, checked_at) "
                            "VALUES (?, ?, 'moved', ?) ON CONFLICT (key) DO UPDATE SET "
                            "work_item_id = excluded.work_item_id, status = 'moved', checked_at = excluded.checked_at",
                            (key, result.id, run.now))
    return result


def refresh_batches(con: sqlite3.Connection, source_id: int, size: int = 50) -> List[List[str]]:
    """Keys of open issues that aren't yours, in JQL `key in (...)` batches, so their move to done is seen."""
    keys = [r[0] for r in con.execute(
        "SELECT key FROM work_items WHERE source_id = ? AND deleted_at IS NULL AND status_category <> 'done' "
        "AND is_mine = 0 ORDER BY key", (source_id,))]
    return [keys[i:i + size] for i in range(0, len(keys), size)]


def issue_by_key(con: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
    """The issue a key names now, following moves."""
    return con.execute("SELECT w.* FROM work_item_aliases a JOIN work_items w ON w.id = a.work_item_id "
                       "WHERE a.key = ? AND a.status <> 'not_found'", (key,)).fetchone()


def assigned_to_me(con: sqlite3.Connection, include_done: bool = False) -> List[sqlite3.Row]:
    """Odin's Assigned to Me view: issues whose current assignee is you."""
    done = "" if include_done else "AND w.status_category <> 'done' "
    return con.execute(
        "SELECT w.* FROM work_items w WHERE w.is_mine = 1 AND w.deleted_at IS NULL " + done +
        "AND EXISTS (SELECT 1 FROM identities i WHERE i.kind = 'jira_user' AND i.value = w.assignee) "
        "ORDER BY w.status_category, w.updated_at DESC").fetchall()


def children_of(con: sqlite3.Connection, parent_key: str) -> List[sqlite3.Row]:
    """Issues under a tracked parent: sub-tasks by parent, stories by epic link."""
    return con.execute("SELECT * FROM work_items WHERE deleted_at IS NULL AND (parent_key = ? OR epic_key = ?) "
                       "ORDER BY key", (parent_key, parent_key)).fetchall()


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------

_GRAPH_SHOW_AS = {"free": "free", "tentative": "tentative", "busy": "busy", "oof": "oof",
                  "workingelsewhere": "free", "unknown": "unknown"}
_GRAPH_RESPONSE = {"none": "none", "organizer": "organizer", "tentativelyaccepted": "tentative",
                   "accepted": "accepted", "declined": "declined", "notresponded": "none"}
_OUTLOOK_BUSY = {0: "free", 1: "tentative", 2: "busy", 3: "oof", 4: "free"}
_OUTLOOK_RESPONSE = {0: "none", 1: "organizer", 2: "tentative", 3: "accepted", 4: "declined", 5: "none"}


def event_from_graph(raw: Dict[str, Any]) -> Dict[str, Any]:
    """A calendar event from Microsoft Graph JSON (request times in UTC, Graph's default)."""
    for part in ("start", "end"):
        zone = (raw.get(part) or {}).get("timeZone", "UTC")
        if zone.upper() not in ("UTC", "ETC/UTC"):
            raise ValueError(f"Graph event times are in {zone}; ask Graph for UTC")
    return {
        "external_id": raw["id"],
        "title": raw.get("subject") or "(no title)",
        "starts_at": parse_time(raw["start"]["dateTime"]),
        "ends_at": parse_time(raw["end"]["dateTime"]),
        "is_all_day": 1 if raw.get("isAllDay") else 0,
        "show_as": _GRAPH_SHOW_AS.get(str(raw.get("showAs", "busy")).lower(), "unknown"),
        "response": _GRAPH_RESPONSE.get(str((raw.get("responseStatus") or {}).get("response", "none")).lower(), "none"),
        "is_cancelled": 1 if raw.get("isCancelled") else 0,
    }


def event_from_outlook(item: Any) -> Dict[str, Any]:
    """A calendar event from an Outlook AppointmentItem (pywin32 COM), using StartUTC and EndUTC."""
    start = item.StartUTC
    end = item.EndUTC
    start_ts = to_ts(start if isinstance(start, dt.datetime) else dt.datetime.fromisoformat(str(start)))
    end_ts = to_ts(end if isinstance(end, dt.datetime) else dt.datetime.fromisoformat(str(end)))
    gid = str(getattr(item, "GlobalAppointmentID", "") or getattr(item, "EntryID", ""))
    recurring = bool(getattr(item, "IsRecurring", False))
    return {
        "external_id": f"{gid}:{start_ts}" if recurring else gid,
        "title": str(getattr(item, "Subject", "") or "(no title)"),
        "starts_at": start_ts,
        "ends_at": end_ts,
        "is_all_day": 1 if getattr(item, "AllDayEvent", False) else 0,
        "show_as": _OUTLOOK_BUSY.get(int(getattr(item, "BusyStatus", 2)), "unknown"),
        "response": _OUTLOOK_RESPONSE.get(int(getattr(item, "ResponseStatus", 0)), "none"),
        "is_cancelled": 1 if int(getattr(item, "MeetingStatus", 0)) in (5, 7) else 0,
    }


def upsert_calendar_event(run: Run, event: Dict[str, Any]) -> Tuple[int, bool]:
    """Store one calendar event; returns (id, changed). Keeps the Jira key you chose for it."""
    with run.batch():
        return _upsert_calendar_event(run, event)


def _upsert_calendar_event(run: Run, event: Dict[str, Any]) -> Tuple[int, bool]:
    con = run.con
    run.items_seen += 1
    before = con.execute("SELECT * FROM calendar_events WHERE source_id = ? AND external_id = ?",
                         (run.source_id, event["external_id"])).fetchone()
    fields = ("title", "starts_at", "ends_at", "is_all_day", "show_as", "response", "is_cancelled")
    if before is not None:
        changed = before["deleted_at"] is not None or any(before[f] != event[f] for f in fields)
        con.execute("UPDATE calendar_events SET title = :title, starts_at = :starts_at, ends_at = :ends_at, "
                    "is_all_day = :is_all_day, show_as = :show_as, response = :response, "
                    "is_cancelled = :is_cancelled, last_seen_at = :now, deleted_at = NULL, "
                    "run_id = CASE WHEN :changed THEN :run_id ELSE run_id END WHERE id = :id",
                    dict(event, now=run.now, run_id=run.id, changed=1 if changed else 0, id=before["id"]))
        if changed:
            run.items_changed += 1
        run.saw("calendar_events", before["id"])
        return int(before["id"]), changed
    new_id = con.execute("INSERT INTO calendar_events (source_id, external_id, title, starts_at, ends_at, is_all_day, "
                         "show_as, response, is_cancelled, first_seen_at, last_seen_at, run_id) VALUES "
                         "(:source_id, :external_id, :title, :starts_at, :ends_at, :is_all_day, :show_as, :response, "
                         ":is_cancelled, :now, :now, :run_id) RETURNING id",
                         dict(event, source_id=run.source_id, now=run.now, run_id=run.id)).fetchone()[0]
    run.items_changed += 1
    run.saw("calendar_events", new_id)
    return int(new_id), True


def sweep_calendar(run: Run, window_start: str, window_end: str) -> int:
    """After syncing a whole window in this run, mark events in it that the run didn't see as deleted.

    Only a run in mode="full" may sweep: an incremental run sees only what changed, so everything
    else would look deleted. A run with problems doesn't sweep (returns 0): an event that failed
    to store wasn't seen either, and the next full run sweeps instead.
    """
    if run.mode != "full":
        raise MuninnError("sweep_calendar needs a Run with mode='full' that read the whole window; "
                          "an incremental run would mark every event it didn't re-read as deleted.")
    if run.problems:
        return 0
    seen = json.dumps(sorted(run.seen.get("calendar_events", ())))
    with run.batch():
        rows = run.con.execute("UPDATE calendar_events SET deleted_at = ? WHERE source_id = ? AND deleted_at IS NULL "
                               "AND starts_at < ? AND ends_at > ? AND id NOT IN (SELECT value FROM json_each(?)) "
                               "RETURNING id", (run.now, run.source_id, window_end, window_start, seen)).fetchall()
    return len(rows)


def set_meeting_key(con: sqlite3.Connection, calendar_event_id: int, key: Optional[str]) -> None:
    """Remember which Jira issue a meeting's time goes to (None to clear). Keys are stored upper-case."""
    key = None if key is None else _key(key)
    con.execute("UPDATE calendar_events SET logged_as_key = ? WHERE id = ?", (key, calendar_event_id))


def _key(text: str) -> str:
    """A key you typed, as Muninn stores it; MuninnError (for the screen) if it isn't one."""
    try:
        return normalize_key(text)
    except ValueError as exc:
        raise MuninnError(f"{text!r} isn't a Jira key like PROJ-123.") from exc


# --------------------------------------------------------------------------
# Worklogs from Jira
# --------------------------------------------------------------------------

MARKER_RE = re.compile(r"\[asgard:[bmo]-[0-9a-f]{8}\]")
_MARKER_KINDS = {"baldur": "b", "meeting": "m", "manual": "o"}


def new_marker(origin: str) -> str:
    return f"[asgard:{_MARKER_KINDS[origin]}-{secrets.token_hex(4)}]"


def marker_in(text: Optional[str]) -> Optional[str]:
    m = MARKER_RE.search(text or "")
    return m.group(0) if m else None


def _comment_text(comment: Any) -> Optional[str]:
    if comment is None or isinstance(comment, str):
        return comment
    return json.dumps(comment)  # Jira Cloud's rich-text document; the marker still survives as text


@dataclass
class WorklogResult:
    status: str          # inserted, updated, unchanged, reconciled, not_mine, missing_issue
    worklog_id: Optional[int] = None
    issue_id: Optional[str] = None


def upsert_worklog(run: Run, raw: Dict[str, Any], ctx: JiraContext) -> WorklogResult:
    """Store one worklog from /rest/api/2/worklog/list or an issue's worklog list.

    Only your worklogs are kept. A worklog carrying an Asgard marker settles the
    'sending' or 'failed' row that posted it. 'missing_issue' means fetch that
    issue (GET /rest/api/2/issue/{issue_id}), upsert it, then call this again.
    """
    if not ctx.is_me(raw.get("author")):
        return WorklogResult("not_mine")
    issue_id = str(raw.get("issueId") or "")
    jira_wid = str(raw["id"])
    started = parse_time(raw.get("started"))
    seconds = int(raw.get("timeSpentSeconds") or 0)
    comment = _comment_text(raw.get("comment"))
    posted_at = parse_time(raw.get("created")) or run.now
    if seconds <= 0 or started is None:
        return WorklogResult("unchanged")
    with run.batch():
        item = run.con.execute("SELECT id FROM work_items WHERE source_id = ? AND jira_id = ?",
                               (ctx.source_id, issue_id)).fetchone()
        if item is None:
            return WorklogResult("missing_issue", issue_id=issue_id)
        run.items_seen += 1
        return _upsert_worklog(run, int(item[0]), jira_wid, started, seconds, comment, posted_at)


def _upsert_worklog(run: Run, item_id: int, jira_wid: str, started: str, seconds: int,
                    comment: Optional[str], posted_at: str) -> WorklogResult:
    con = run.con
    known = con.execute("SELECT id, started_at, seconds, comment, state FROM worklogs "
                        "WHERE work_item_id = ? AND jira_worklog_id = ?", (item_id, jira_wid)).fetchone()
    if known is not None:
        if (known["started_at"], known["seconds"], known["comment"], known["state"]) == (started, seconds, comment, "posted"):
            return WorklogResult("unchanged", int(known["id"]))
        con.execute("UPDATE worklogs SET started_at = ?, seconds = ?, comment = ?, state = 'posted', "
                    "posted_at = coalesce(posted_at, ?) WHERE id = ?",
                    (started, seconds, comment, posted_at, known["id"]))
        run.items_changed += 1
        return WorklogResult("updated", int(known["id"]))

    marker = marker_in(comment)
    if marker:
        mine = con.execute("SELECT id FROM worklogs WHERE work_item_id = ? AND state IN ('sending', 'failed') "
                           "AND instr(comment, ?) > 0 ORDER BY id LIMIT 1", (item_id, marker)).fetchone()
        if mine is not None:
            _settle(con, int(mine[0]), jira_wid, posted_at, run_id=run.id)
            run.items_changed += 1
            return WorklogResult("reconciled", int(mine[0]))

    new_id = con.execute("INSERT INTO worklogs (work_item_id, jira_worklog_id, origin, state, started_at, seconds, "
                         "comment, posted_at) VALUES (?, ?, 'jira', 'posted', ?, ?, ?, ?) RETURNING id",
                         (item_id, jira_wid, started, seconds, comment, posted_at)).fetchone()[0]
    run.items_changed += 1
    return WorklogResult("inserted", int(new_id))


def mark_worklog_deleted(run: Run, jira_worklog_id: str) -> bool:
    """Apply /rest/api/2/worklog/deleted: the time is gone from Jira, so it no longer counts."""
    with run.batch():
        rows = run.con.execute("UPDATE worklogs SET state = 'deleted' WHERE jira_worklog_id = ? AND state = 'posted' "
                               "RETURNING id", (str(jira_worklog_id),)).fetchall()
    run.items_changed += len(rows)
    return bool(rows)


# --------------------------------------------------------------------------
# Posting to Jira
# --------------------------------------------------------------------------

_HELD_BY = {"jira": "logged by hand", "baldur": "posted from an earlier approval", "manual": "typed into Odin"}


def _hm(minutes: int) -> str:
    minutes = int(minutes)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


@dataclass
class PendingPost:
    """One worklog to send to Jira. Pass started, seconds and comment as they are."""
    worklog_id: int
    key: str
    started_at: str
    seconds: int
    comment: str
    marker: str
    origin: str
    proposal_id: Optional[int] = None
    calendar_event_id: Optional[int] = None

    @property
    def started(self) -> str:
        return jira_time(self.started_at)


def posts_due(con: sqlite3.Connection) -> List[sqlite3.Row]:
    """Approved Baldur days Jira is missing time for, oldest first."""
    return con.execute("SELECT d.proposal_id, d.local_date, d.work_item_id, w.key, d.approved_minutes, "
                       "d.logged_minutes, d.minutes_to_post, d.first_started_at "
                       "FROM v_worklogs_to_post d JOIN work_items w ON w.id = d.work_item_id "
                       "ORDER BY d.local_date, w.key").fetchall()


def begin_post(con: sqlite3.Connection, proposal_id: int) -> Optional[PendingPost]:
    """Write the 'sending' row for an approved day. None if it's no longer due."""
    with transaction(con):
        row = con.execute("SELECT d.*, w.key FROM v_worklogs_to_post d JOIN work_items w ON w.id = d.work_item_id "
                          "WHERE d.proposal_id = ?", (proposal_id,)).fetchone()
        if row is None:
            return None
        marker = new_marker("baldur")
        lines = [row["basis"]]
        # When the approved figure is an AI-assisted one you took, the comment says so.
        review = con.execute("SELECT review FROM day_proposals WHERE id = ?", (proposal_id,)).fetchone()
        reviewed = baldur_rules.review_line(review[0] if review else None, row["approved_minutes"])
        if reviewed:
            lines.append(reviewed)
        if row["logged_minutes"]:
            origins = [r[0] for r in con.execute(
                "SELECT DISTINCT origin FROM worklogs WHERE work_item_id = ? AND state IN ('sending', 'posted') "
                "AND origin <> 'meeting' AND started_at >= ? AND started_at < ? ORDER BY origin",
                (row["work_item_id"], row["day_start"], row["day_end"]))]
            how = " and ".join(_HELD_BY.get(o, o) for o in origins)
            lines.append(f"Already in Jira: {_hm(row['logged_minutes'])}{', ' + how if how else ''}; "
                         f"this worklog adds {_hm(row['minutes_to_post'])}")
        decided = from_ts(row["decided_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
        lines.append(f"Approved: {_hm(row['approved_minutes'])} on {decided}")
        comment = "\n".join(lines + [marker])
        seconds = int(row["minutes_to_post"]) * 60
        # Jira files a worklog under its start time, so keep the start inside the approved day:
        # a lead-in that began before midnight would otherwise count toward the day before.
        last_start = to_ts(from_ts(row["day_end"]) - dt.timedelta(minutes=1))
        started = min(max(row["first_started_at"], row["day_start"]), last_start)
        wid = con.execute("INSERT INTO worklogs (work_item_id, origin, state, started_at, seconds, comment, proposal_id) "
                          "VALUES (?, 'baldur', 'sending', ?, ?, ?, ?) RETURNING id",
                          (row["work_item_id"], started, seconds, comment, proposal_id)).fetchone()[0]
    return PendingPost(int(wid), row["key"], started, seconds, comment, marker, "baldur", proposal_id=proposal_id)


def _resolve_item(con: sqlite3.Connection, key: str) -> sqlite3.Row:
    item = issue_by_key(con, key)
    if item is None:
        raise MuninnError(f"{key} isn't in Muninn yet. Let Odin sync or look it up first.")
    if item["deleted_at"] is not None:
        raise MuninnError(f"{key} was deleted in Jira.")
    return item


def begin_meeting_post(con: sqlite3.Connection, calendar_event_id: int, key: Optional[str] = None,
                       comment: Optional[str] = None, *, again: bool = False) -> Optional[PendingPost]:
    """Write the 'sending' row for a meeting's time.

    None if the meeting is already logged, or was logged and then deleted in
    Jira; pass again=True when you mean to log a deleted one a second time.
    """
    with transaction(con):
        ev = con.execute("SELECT * FROM calendar_events WHERE id = ? AND deleted_at IS NULL",
                         (calendar_event_id,)).fetchone()
        if ev is None:
            raise MuninnError("That meeting isn't on your calendar any more.")
        key = _key(key) if key else ev["logged_as_key"]
        if not key:
            raise MuninnError(f"Choose a Jira issue for “{ev['title']}” first.")
        item = _resolve_item(con, key)
        states = {r[0] for r in con.execute("SELECT state FROM worklogs WHERE calendar_event_id = ?",
                                            (calendar_event_id,))}
        if states & {"sending", "posted"} or ("deleted" in states and not again):
            return None
        # The same meeting synced from another calendar (Outlook and Graph), or sent again after the
        # first copy left the calendar, is the same time: logged once is logged.
        twin = con.execute("SELECT 1 FROM worklogs w JOIN calendar_events e ON e.id = w.calendar_event_id "
                           "WHERE e.id <> ? AND lower(trim(e.title)) = lower(trim(?)) "
                           "AND e.starts_at = ? AND e.ends_at = ? AND w.state IN ('sending', 'posted') LIMIT 1",
                           (calendar_event_id, ev["title"], ev["starts_at"], ev["ends_at"])).fetchone()
        if twin:
            return None
        seconds = int(round((from_ts(ev["ends_at"]) - from_ts(ev["starts_at"])).total_seconds()))
        if seconds <= 0:
            raise MuninnError(f"“{ev['title']}” has no length to log.")
        if seconds > 86400:
            raise MuninnError(f"“{ev['title']}” runs over 24 hours; log it by hand, one worklog per day.")
        marker = new_marker("meeting")
        text = f"{comment or 'Meeting: ' + ev['title']}\n{marker}"
        if ev["logged_as_key"] != key:
            set_meeting_key(con, calendar_event_id, key)
        wid = con.execute("INSERT INTO worklogs (work_item_id, origin, state, started_at, seconds, comment, "
                          "calendar_event_id) VALUES (?, 'meeting', 'sending', ?, ?, ?, ?) RETURNING id",
                          (item["id"], ev["starts_at"], seconds, text, calendar_event_id)).fetchone()[0]
    return PendingPost(int(wid), item["key"], ev["starts_at"], seconds, text, marker, "meeting",
                       calendar_event_id=calendar_event_id)


def begin_manual_post(con: sqlite3.Connection, key: str, started_at: str, seconds: int,
                      comment: str = "") -> PendingPost:
    """Write the 'sending' row for time you typed into Odin."""
    if seconds <= 0:
        raise MuninnError("A worklog needs some time on it.")
    if seconds > 86400:
        raise MuninnError("One worklog can hold at most 24 hours; split the time across days.")
    with transaction(con):
        item = _resolve_item(con, _key(key))
        marker = new_marker("manual")
        text = f"{comment}\n{marker}" if comment else marker
        wid = con.execute("INSERT INTO worklogs (work_item_id, origin, state, started_at, seconds, comment) "
                          "VALUES (?, 'manual', 'sending', ?, ?, ?) RETURNING id",
                          (item["id"], started_at, seconds, text)).fetchone()[0]
    return PendingPost(int(wid), item["key"], started_at, seconds, text, marker, "manual")


def _settle(con: sqlite3.Connection, worklog_id: int, jira_worklog_id: str, posted_at: str,
            run_id: Optional[int] = None) -> None:
    """Mark a sending or failed row posted; drop a copy a sync stored before it knew the marker."""
    row = con.execute("SELECT w.*, i.key FROM worklogs w JOIN work_items i ON i.id = w.work_item_id WHERE w.id = ?",
                      (worklog_id,)).fetchone()
    con.execute("DELETE FROM worklogs WHERE work_item_id = ? AND jira_worklog_id = ? AND id <> ? AND origin = 'jira'",
                (row["work_item_id"], jira_worklog_id, worklog_id))
    con.execute("UPDATE worklogs SET state = 'posted', jira_worklog_id = ?, posted_at = ?, error = NULL WHERE id = ?",
                (jira_worklog_id, posted_at, worklog_id))
    emit(con, "odin", "worklog.posted", "worklogs", worklog_id, row["key"],
         {"origin": row["origin"], "seconds": row["seconds"], "proposal_id": row["proposal_id"],
          "calendar_event_id": row["calendar_event_id"]}, run_id)


def finish_post(con: sqlite3.Connection, worklog_id: int, jira_worklog_id: str,
                posted_at: Optional[str] = None) -> None:
    """Jira created the worklog: record its id."""
    jira_worklog_id = str(jira_worklog_id)
    with transaction(con):
        row = con.execute("SELECT state, jira_worklog_id FROM worklogs WHERE id = ?", (worklog_id,)).fetchone()
        if row is None:
            raise MuninnError(f"No worklog {worklog_id} in Muninn.")
        if row["state"] == "posted":
            if row["jira_worklog_id"] == jira_worklog_id:
                return  # a sync already settled it
            raise MuninnError(f"Worklog {worklog_id} is already posted as Jira worklog {row['jira_worklog_id']}.")
        if row["state"] not in ("sending", "failed"):
            raise MuninnError(f"Worklog {worklog_id} is {row['state']}, not waiting for Jira.")
        _settle(con, worklog_id, jira_worklog_id, posted_at or utcnow())


def fail_post(con: sqlite3.Connection, worklog_id: int, error: str) -> None:
    """Jira definitely refused the worklog (a 4xx answer). Don't call this after a timeout.

    The error text is kept, so anything in it that looks like a credential is masked first.
    """
    error = scrub(str(error)) or "failed"
    with transaction(con):
        row = con.execute("UPDATE worklogs SET state = 'failed', error = ? WHERE id = ? AND state = 'sending' "
                          "RETURNING work_item_id, origin, proposal_id, calendar_event_id", (error[:2000], worklog_id)).fetchone()
        if row is None:
            return
        key = con.execute("SELECT key FROM work_items WHERE id = ?", (row["work_item_id"],)).fetchone()[0]
        emit(con, "odin", "worklog.failed", "worklogs", worklog_id, key,
             {"origin": row["origin"], "proposal_id": row["proposal_id"],
              "calendar_event_id": row["calendar_event_id"], "error": error[:500]})


def stuck_posts(con: sqlite3.Connection, older_than_seconds: int = 120) -> List[Dict[str, Any]]:
    """'sending' rows whose outcome is unknown. Search the issue's worklogs in Jira for each marker,
    then call resolve_stuck(). Keep older_than_seconds well above Odin's HTTP timeout, so a post
    still waiting on Jira is never mistaken for a lost one."""
    rows = con.execute("SELECT w.id, w.comment, w.started_at, w.seconds, w.origin, w.created_at, i.key "
                       "FROM worklogs w JOIN work_items i ON i.id = w.work_item_id "
                       "WHERE w.state = 'sending' AND w.created_at <= ? ORDER BY w.created_at",
                       (ago(older_than_seconds),)).fetchall()
    return [{"worklog_id": r["id"], "key": r["key"], "marker": marker_in(r["comment"]),
             "started_at": r["started_at"], "seconds": r["seconds"], "origin": r["origin"],
             "sent_at": r["created_at"]} for r in rows]


def resolve_stuck(con: sqlite3.Connection, worklog_id: int, jira_worklog_id: Optional[str] = None,
                  posted_at: Optional[str] = None, *, searched: bool = False) -> None:
    """Settle a stuck post: Jira's id if its marker was found, otherwise it failed and is offered again.

    Marking a post failed makes its time due again, so a post that did reach Jira would be posted
    twice. Pass searched=True only after searching the issue's worklogs for the marker and not
    finding it; without it, a missing id is refused rather than taken as "not there".
    """
    if jira_worklog_id:
        finish_post(con, worklog_id, jira_worklog_id, posted_at)
    elif not searched:
        raise MuninnError("Search the issue's worklogs in Jira for this post's marker first. Call "
                          "resolve_stuck(..., searched=True) only when it isn't there; marking a post that "
                          "did reach Jira as failed would post its time twice.")
    else:
        fail_post(con, worklog_id, "Not found in Jira after an interrupted post")


# --------------------------------------------------------------------------
# Moving Odin's history in
# --------------------------------------------------------------------------

def adopt_meeting_worklog(con: sqlite3.Connection, worklog_id: int, calendar_event_id: int) -> bool:
    """Mark a worklog found in Jira as the meeting time Odin logged before it used Muninn."""
    with transaction(con):
        row = con.execute("UPDATE worklogs SET origin = 'meeting', calendar_event_id = ? WHERE id = ? "
                          "AND origin IN ('jira', 'manual') RETURNING id", (calendar_event_id, worklog_id)).fetchone()
        if row:
            key = con.execute("SELECT i.key FROM worklogs w JOIN work_items i ON i.id = w.work_item_id "
                              "WHERE w.id = ?", (worklog_id,)).fetchone()[0]
            con.execute("UPDATE calendar_events SET logged_as_key = coalesce(logged_as_key, ?) WHERE id = ?",
                        (key, calendar_event_id))
    return row is not None


def classify_meeting_worklogs(con: sqlite3.Connection, comment_like: str) -> int:
    """Find Odin's earlier meeting worklogs among those pulled from Jira.

    A worklog is marked a meeting only when its comment matches comment_like
    (a LIKE pattern for how Odin wrote meeting comments, such as 'Meeting:%'),
    it starts at the same second as a busy meeting and lasts as long (within a
    minute), and that pairing is the only one for both of them. Meetings that
    already have a logged worklog are skipped. Reading development time as a
    meeting would make Baldur post it again, so anything unsure stays as it is.
    """
    if not comment_like or not comment_like.strip("%_ "):
        raise ValueError("classify_meeting_worklogs needs the pattern Odin's meeting comments follow, "
                         "such as 'Meeting:%'")
    pairs = con.execute(
        "SELECT w.id AS wid, e.id AS eid FROM worklogs w JOIN v_busy_meetings e "
        "ON e.starts_at = w.started_at AND abs((julianday(e.ends_at) - julianday(e.starts_at)) * 86400 - w.seconds) < 60 "
        "WHERE w.origin = 'jira' AND w.state = 'posted' AND w.comment LIKE ? "
        "AND NOT EXISTS (SELECT 1 FROM worklogs m WHERE m.calendar_event_id = e.id AND m.state IN ('sending', 'posted'))",
        (comment_like,)).fetchall()
    per_worklog = Counter(p["wid"] for p in pairs)
    per_event = Counter(p["eid"] for p in pairs)
    n = 0
    for p in pairs:
        if per_worklog[p["wid"]] == 1 and per_event[p["eid"]] == 1 and adopt_meeting_worklog(con, p["wid"], p["eid"]):
            n += 1
    return n
