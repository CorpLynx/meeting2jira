"""Ysildir's Muninn tools: read-only answers about the person's work.

Every query runs on the read-only connection opened as ysildir (reader()), where query_only is on
and Muninn's guard refuses every write. Metadata only: nothing here returns a Jira description,
a worklog comment, a commit body, meeting notes or a payload field that holds free text. Text
from outside Asgard (Jira summaries and statuses, commit subjects, pull request titles) is
cleaned to one capped line with credential-shaped text masked (clean_line), and each answer
names it in untrusted_fields.
"""
import datetime as dt
import json
import re
from typing import Any, Dict, List, Optional

from asgard.muninn import guard, keys, tables
from asgard.muninn.baldur import clean_line

from . import Refused, day_range, reader
from .models import (MAX_ITEMS, Catalog, CatalogEntry, DateFrom, DateTo, DayStatus, DayStatusRow, EventKinds, EventRow,
                     Events, Hit, Issue, JiraKey, Limit, Query, Search, SearchKinds, Since)

# The payload fields muninn_what_changed returns for each kind: only ones that hold no free text
# (the spec's allow-list). A kind that isn't here comes back without a payload.
PAYLOAD_FIELDS: Dict[str, tuple] = {
    "day_proposal.approved": ("local_date", "minutes"),
    "estimate_run.created": ("date_from", "date_to", "proposals", "withdrawn"),
    "agent_estimate.recorded": ("local_date", "minutes", "agent", "commits", "replaced"),
    "agent_estimate.withdrawn": (),
    "calibration.accepted": ("days", "from", "to", "error_before", "error_after", "replaced"),
    "work_item.created": ("status",),             # the summary is left out: it's free text
    "work_item.reopened": ("status",),
    "work_item.moved": ("from", "to"),
    "work_item.done": ("resolution", "resolved_at"),
    "work_item.deleted": (),
    "worklog.posted": ("origin", "seconds", "proposal_id"),
    "worklog.failed": ("origin", "proposal_id"),  # the error text is left out
}
CHANGED_FIELDS = "work_item.updated"              # returns the names of the changed fields only
SEARCH_KINDS = {"issues": 1, "commits": 2, "pull_requests": 3}   # search rowid & 15 (0001_initial.sql)
_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2})?(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$")


def _value(value: Any) -> Any:
    """A payload value Ysildir passes on: numbers, short clean text, or lists of numbers."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return clean_line(value, 80)
    if isinstance(value, list) and all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        return value[:50]
    return None


def payload_for(kind: str, text: str) -> Dict[str, Any]:
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    if kind == CHANGED_FIELDS:
        return {"changed": sorted(clean_line(k, 40) for k in data)}
    return {k: _value(data[k]) for k in PAYLOAD_FIELDS.get(kind, ()) if k in data}


# --------------------------------------------------------------------------
# The tools
# --------------------------------------------------------------------------

def muninn_catalog() -> Catalog:
    entries = []
    with reader() as con:
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
        found = con.execute("SELECT type, name FROM sqlite_schema WHERE type IN ('table', 'view') "
                            "AND name NOT LIKE 'sqlite%' ORDER BY type, name").fetchall()
        for kind, name in found:
            if name in guard.FTS_SHADOW:
                continue
            owners = sorted(guard.OWNERS.get(name, ()))
            if kind == "view":
                owner = "view"
            elif name in guard.SHARED:
                owner = "shared"
            elif name == "search":
                owner = "search index (kept by triggers)"
            else:
                owner = ", ".join(owners) or "?"
            rows = int(con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]) if kind == "table" else None
            entries.append(CatalogEntry(name=name, type=kind, owner=owner,
                                        meaning=tables.MEANINGS.get(name, "(no description yet)"), rows=rows))
    return Catalog(schema_version=version, entries=entries, rules=list(tables.RULES))


def _since(text: str) -> str:
    """since as Muninn's UTC time text: a time with its zone, or a day meaning its local midnight."""
    value = text.strip()
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            at = dt.datetime.combine(dt.date.fromisoformat(value), dt.time()).astimezone()
        elif _TIME.match(value):
            at = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        else:
            raise ValueError
    except ValueError:
        raise Refused("since must be a time with its zone, like 2026-10-08T04:00:00Z, or a day, like 2026-10-08.") \
            from None
    return at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def muninn_what_changed(since: Since, kinds: EventKinds = None, limit: Limit = 100) -> Events:
    start = _since(since)
    where, params = ["at >= ?"], [start]
    if kinds:
        where.append("(" + " OR ".join("kind = ? OR kind GLOB ?" for _ in kinds) + ")")
        for k in kinds:
            params += [k, k + ".*"]
    with reader() as con:
        rows = con.execute(f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY at, id LIMIT ?",
                           params + [limit + 1]).fetchall()
    events = [EventRow(id=r["id"], at=r["at"], app=r["app"], kind=r["kind"], entity_type=r["entity_type"],
                       entity_id=r["entity_id"], ref=clean_line(r["ref"], 80) if r["ref"] else None,
                       payload=payload_for(r["kind"], r["payload"])) for r in rows[:limit]]
    cut = len(rows) > limit
    return Events(since=start, events=events, truncated=cut, narrow=Events.NARROW if cut else None,
                  untrusted_fields=["events[].payload.status", "events[].payload.resolution"])


def muninn_day_status(date_from: DateFrom = None, date_to: DateTo = None) -> DayStatus:
    first, last = day_range(date_from, date_to, default_days=7)
    with reader() as con:
        rows = con.execute(
            "SELECT d.local_date, d.work_item_key, d.approved_minutes, d.logged_minutes, u.reason FROM v_day_status d "
            "LEFT JOIN v_unpostable_days u ON u.proposal_id = d.proposal_id "
            "WHERE d.local_date BETWEEN ? AND ? ORDER BY d.local_date, d.work_item_key LIMIT ?",
            (first.isoformat(), last.isoformat(), MAX_ITEMS + 1)).fetchall()
    return DayStatus(date_from=first.isoformat(), date_to=last.isoformat(), rows=[DayStatusRow(
        date=r["local_date"], key=r["work_item_key"], approved_minutes=r["approved_minutes"],
        logged_minutes=r["logged_minutes"], to_post=max(0, r["approved_minutes"] - r["logged_minutes"]),
        unpostable=r["reason"]) for r in rows])


def muninn_issue(key: JiraKey) -> Issue:
    try:
        asked = keys.normalize_key(key)
    except ValueError as exc:
        raise Refused(str(exc)) from None
    with reader() as con:
        row = con.execute("SELECT w.* FROM work_item_aliases a JOIN work_items w ON w.id = a.work_item_id "
                          "WHERE a.key = ? AND a.status <> 'not_found'", (asked,)).fetchone()
        if row is None:
            row = con.execute("SELECT * FROM work_items WHERE key = ? ORDER BY id DESC LIMIT 1", (asked,)).fetchone()
    if row is None:
        return Issue(found=False, asked=asked, message=f"Muninn has no issue {asked}. Odin collects Jira issues; it "
                                                       "may not have looked this one up yet.")
    return Issue(found=True, asked=asked, key=row["key"], summary=clean_line(row["summary"], 300),
                 status=clean_line(row["status"], 60), status_category=row["status_category"],
                 type=clean_line(row["issue_type"], 60), epic=row["epic_key"], parent=row["parent_key"],
                 resolution=clean_line(row["resolution"], 60) if row["resolution"] else None,
                 updated=row["updated_at"], deleted=row["deleted_at"] is not None,
                 untrusted_fields=["summary", "status", "type", "resolution"])


def match_expression(query: str) -> str:
    """The query as FTS5 sees it: each word quoted, so no word is read as an operator, a prefix or syntax."""
    words = [w for w in query.split() if any(ch.isalnum() for ch in w)][:12]
    if not words:
        raise Refused("Give at least one word to search for.")
    return " ".join('"' + w.replace('"', '""') + '"' for w in words)


def _ref(con: Any, kind: int, entity_id: int) -> Optional[Dict[str, Any]]:
    if kind == 1:
        r = con.execute("SELECT key AS ref, updated_at AS at FROM work_items WHERE id = ? AND deleted_at IS NULL",
                        (entity_id,)).fetchone()
    elif kind == 2:
        r = con.execute("SELECT sha AS ref, committed_at AS at FROM commits WHERE id = ? AND is_mine = 1",
                        (entity_id,)).fetchone()
    else:
        r = con.execute("SELECT coalesce(o.github_repo, o.name) || '#' || p.number AS ref, p.updated_at AS at "
                        "FROM pull_requests p JOIN repos o ON o.id = p.repo_id WHERE p.id = ?", (entity_id,)).fetchone()
    return dict(r) if r else None


def muninn_search(query: Query, kinds: SearchKinds = None, limit: Limit = 20) -> Search:
    expression = match_expression(query)
    codes = sorted({SEARCH_KINDS[k] for k in (kinds or SEARCH_KINDS)})
    names = {v: k for k, v in SEARCH_KINDS.items()}
    hits: List[Hit] = []
    cut = False
    with reader() as con:
        rows = con.execute(f"SELECT rowid, title FROM search WHERE search MATCH ? AND (rowid & 15) IN "
                           f"({', '.join('?' for _ in codes)}) ORDER BY bm25(search) LIMIT ?",
                           [expression] + codes + [4 * (limit + 1)]).fetchall()
        for r in rows:
            found = _ref(con, r["rowid"] & 15, r["rowid"] >> 4)
            if found is None:                     # another author's commit, or a deleted issue
                continue
            if len(hits) == limit:
                cut = True
                break
            hits.append(Hit(kind=names[r["rowid"] & 15], id=r["rowid"] >> 4, ref=clean_line(found["ref"], 120),
                            title=clean_line(r["title"], 200), at=found["at"]))
    return Search(query=expression, hits=hits, truncated=cut, narrow=Search.NARROW if cut else None,
                  untrusted_fields=["hits[].title"])
