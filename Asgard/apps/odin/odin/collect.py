"""Reading Jira into Muninn: your issues, the parents you track, keys other apps mention, your worklogs.

Position in the flow
    cli.py runs these after the meeting push, each as its own Muninn sync run, so Baldur, Freya and
    Ysildir see Jira as it is. Nothing here writes to Jira. Pages are fetched with no write lock
    held; each page is written in one short transaction.

Scope (Muninn design, "Work tables (Odin)")
    1. Yours: `assignee was currentUser()`, which sets is_mine and feeds Assigned to Me.
    2. The parents your meetings go under (default_parent, the rules, the tour-of-duty parent) and
       their children.
    3. Keys other apps mention that Muninn can't resolve yet (v_unknown_keys), one GET each,
       because one bad key fails a whole `key in (...)` query.
    4. Mentioned issues not yet done, refreshed in batches of 50 so their move to done reaches Freya.
    5. Issues no run has seen for a week, checked one at a time: only a 404 marks one deleted.

Worklogs
    Muninn's design reads /worklog/updated, which returns every worklog in the whole Jira; on a
    large Data Center that is most of the instance's history. Odin asks for the issues you logged
    time on instead (`worklogAuthor = currentUser()`, then each issue's worklog list), which is
    proportional to your own work, and reads /worklog/deleted for removals.

Cursors
    Each stream keeps Muninn's cursor (the newest `updated` it stored). A run asks for
    `updated >= cursor` (two minutes early, odin.jql_time) in updated order, so a run that stops at
    its limit (muninn.max_issues_per_run, never inside a burst of updates) carries on where it left
    off next time. The first run reaches back muninn.history_days (365).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from asgard import muninn
from asgard.muninn import odin as mo

from . import store
from .jira import JiraClient, JiraError

log = logging.getLogger(__name__)

LOOKUPS_PER_RUN = 100
DELETION_CHECKS_PER_RUN = 50
UNSEEN_DAYS = 7


@dataclass
class CollectResult:
    issues: int = 0                  # issues stored or refreshed
    changed: int = 0
    looked_up: int = 0
    not_found: int = 0
    deleted: int = 0
    worklogs: int = 0
    worklogs_removed: int = 0
    problems: List[str] = field(default_factory=list)
    worklogs_ok: bool = False        # the worklog sync finished without problems (posting needs it)

    def add(self, run: muninn.Run) -> None:
        self.problems.extend(f"{run.stream}: {p}" for p in run.problems)


def context(con: sqlite3.Connection, client: JiraClient, jira_sid: int) -> mo.JiraContext:
    """What Muninn needs to know about this Jira: its custom field ids and status categories."""
    ids = mo.field_ids(client.fields())
    categories = mo.status_categories(client.statuses())
    return mo.JiraContext.load(con, jira_sid, status_categories=categories, **ids)


def extra_fields(ctx: mo.JiraContext) -> List[str]:
    return [f for f in (ctx.epic_field, ctx.story_points_field, ctx.sprint_field) if f]


def collect_issue(run: muninn.Run, key: str, ctx: mo.JiraContext, client: JiraClient) -> Optional[mo.IssueResult]:
    """Fetch one issue by key and store it (or record that Jira doesn't have it)."""
    raw = client.issue(key, extra_fields(ctx), changelog=True)
    return mo.record_lookup(run, key, raw, ctx)


def _since(cursor: Optional[str], history_days: int, field_name: str = "updated") -> str:
    if cursor:
        return f"updated >= '{mo.jql_time(cursor)}'"
    return f"{field_name} >= -{int(history_days)}d"


class _Limit:
    """A per-run limit that never stops inside a burst of updates.

    The next run re-reads from two minutes before the cursor (odin.jql_time), so stopping in the
    middle of, say, 600 issues bulk-edited in the same minute would re-read the same ones every run
    and never get past them. Once the limit is reached, reading goes on until updates are more than
    GAP apart, so each run always ends past a burst. Issues at or before the cursor (the overlap
    re-read) don't count toward the limit.
    """
    GAP = timedelta(minutes=3)

    def __init__(self, limit: int, after: Optional[str] = None):
        self.limit, self.count, self.until, self.after = max(1, int(limit)), 0, None, after

    def stop_before(self, raw: Dict[str, Any]) -> bool:
        updated = mo.parse_time((raw.get("fields") or {}).get("updated"))
        if self.until is not None and updated is not None and muninn.from_ts(updated) > self.until:
            return True
        if self.after is not None and updated is not None and updated <= self.after:
            return False
        self.count += 1
        if self.until is None and self.count >= self.limit and updated is not None:
            self.until = muninn.from_ts(updated) + self.GAP
        return False


def _issues(pages: Iterable[List[Dict[str, Any]]], limit: int,
            after: Optional[str] = None) -> Iterable[List[Dict[str, Any]]]:
    """The search's pages, cut off by _Limit."""
    cap = _Limit(limit, after)
    for page in pages:
        kept = []
        for raw in page:
            if cap.stop_before(raw):
                if kept:
                    yield kept
                return
            kept.append(raw)
        if kept:
            yield kept


def _store_pages(run: muninn.Run, pages: Iterable[List[Dict[str, Any]]], ctx: mo.JiraContext,
                 mine: Optional[bool], result: CollectResult) -> List[Dict[str, Any]]:
    stored: List[Dict[str, Any]] = []
    for page in pages:
        with run.batch():
            for raw in page:
                try:
                    got = mo.upsert_issue(run, raw, ctx, mine=mine)
                except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as exc:
                    run.problem(f"{raw.get('key', '?')}: {exc}")
                    continue
                result.issues += 1
                result.changed += 1 if got.changed else 0
                stored.append(raw)
                run.advance_cursor(mo.parse_time((raw.get("fields") or {}).get("updated")))
    return stored


def sync_issues(con: sqlite3.Connection, client: JiraClient, ctx: mo.JiraContext, parents: Sequence[str],
                history_days: int, limit: int, result: CollectResult) -> None:
    """Your issues, then each tracked parent and its children."""
    extra = extra_fields(ctx)
    with muninn.Run(con, store.APP, ctx.source_id, "issues") as run:
        jql = f"assignee was currentUser() AND {_since(run.cursor, history_days)} ORDER BY updated ASC"
        try:
            _store_pages(run, _issues(client.search(jql, extra, changelog=True), limit, run.cursor), ctx, True, result)
        except JiraError as exc:
            run.problem(str(exc))
    result.add(run)

    for parent in sorted(set(parents)):
        stream = f"parent:{parent}"
        with muninn.Run(con, store.APP, ctx.source_id, stream) as run:
            try:
                if mo.issue_by_key(con, parent) is None or run.cursor is None:
                    collect_issue(run, parent, ctx, client)
                jql = f"parent = {parent} AND {_since(run.cursor, history_days)} ORDER BY updated ASC"
                _store_pages(run, _issues(client.search(jql, extra, changelog=True), limit, run.cursor), ctx, None, result)
            except JiraError as exc:
                run.problem(str(exc))
        result.add(run)


def lookup_keys(con: sqlite3.Connection, client: JiraClient, ctx: mo.JiraContext, result: CollectResult,
                limit: int = LOOKUPS_PER_RUN) -> None:
    """Keys Baldur and the others mention that Muninn can't resolve yet, one GET each."""
    keys = mo.unknown_keys(con, limit)
    if not keys:
        return
    with muninn.Run(con, store.APP, ctx.source_id, "lookups") as run:
        for key in keys:
            try:
                got = collect_issue(run, key, ctx, client)
            except JiraError as exc:
                run.problem(f"{key}: {exc}")
                continue
            result.looked_up += 1
            result.not_found += 1 if got is None else 0
    result.add(run)


def refresh_open(con: sqlite3.Connection, client: JiraClient, ctx: mo.JiraContext, result: CollectResult) -> None:
    """Mentioned issues that aren't yours and aren't done, so their move to done is seen."""
    batches = mo.refresh_batches(con, ctx.source_id)
    if not batches:
        return
    extra = extra_fields(ctx)
    with muninn.Run(con, store.APP, ctx.source_id, "refresh") as run:
        for batch in batches:
            try:
                _store_pages(run, client.search("key in (%s)" % ", ".join(batch), extra, changelog=True), ctx,
                             None, result)
            except JiraError as exc:
                if exc.status != 400:
                    run.problem(str(exc))
                    continue
                for key in batch:            # one key Jira no longer has fails the whole query
                    try:
                        raw = client.issue(key, extra, changelog=True)
                    except JiraError as one:
                        run.problem(f"{key}: {one}")
                        continue
                    if raw is not None:
                        mo.upsert_issue(run, raw, ctx)
                        result.issues += 1
                        continue
                    item = mo.issue_by_key(con, key)
                    if item is not None and mo.mark_issue_deleted(run, item["jira_id"]):
                        result.deleted += 1
    result.add(run)


def check_unseen(con: sqlite3.Connection, client: JiraClient, ctx: mo.JiraContext, result: CollectResult,
                 now: Optional[datetime] = None, limit: int = DELETION_CHECKS_PER_RUN) -> None:
    """Issues no run has seen for a week: still there (seen now), or deleted (a 404)."""
    since = muninn.to_ts((now or datetime.now(timezone.utc)) - timedelta(days=UNSEEN_DAYS))
    rows = mo.not_seen_since(con, ctx.source_id, since)[:limit]
    if not rows:
        return
    extra = extra_fields(ctx)
    with muninn.Run(con, store.APP, ctx.source_id, "unseen") as run:
        for row in rows:
            try:
                raw = client.issue(row["key"], extra, changelog=True)
            except JiraError as exc:
                run.problem(f"{row['key']}: {exc}")
                continue
            if raw is None:
                result.deleted += 1 if mo.mark_issue_deleted(run, row["jira_id"]) else 0
            else:
                mo.upsert_issue(run, raw, ctx)
    result.add(run)


def sync_worklogs(con: sqlite3.Connection, client: JiraClient, ctx: mo.JiraContext, history_days: int,
                  limit: int, result: CollectResult, now: Optional[datetime] = None) -> None:
    """Your worklogs, whoever made them: each issue you logged time on, then its worklog list."""
    extra = extra_fields(ctx)
    clean = True
    with muninn.Run(con, store.APP, ctx.source_id, "worklogs") as run:
        jql = f"worklogAuthor = currentUser() AND {_since(run.cursor, history_days, 'worklogDate')} ORDER BY updated ASC"
        try:
            for page in _issues(client.search(jql, extra, changelog=True), limit, run.cursor):
                for raw in page:
                    try:
                        _issue_worklogs(run, client, ctx, raw, result)
                    except JiraError as exc:
                        run.problem(f"{raw.get('key', '?')}: {exc}")
                        continue
                    run.advance_cursor(mo.parse_time((raw.get("fields") or {}).get("updated")))
        except JiraError as exc:
            run.problem(str(exc))
        clean = not run.problems
    result.add(run)
    clean = _deleted_worklogs(con, client, ctx, history_days, result, now) and clean
    result.worklogs_ok = clean


def _issue_worklogs(run: muninn.Run, client: JiraClient, ctx: mo.JiraContext, raw: Dict[str, Any],
                    result: CollectResult) -> None:
    worklogs = client.issue_worklogs(raw["key"])          # before any write: no lock across the call
    with run.batch():
        item = mo.upsert_issue(run, raw, ctx)
        on_issue = {str(w.get("id")) for w in worklogs}
        for w in worklogs:
            got = mo.upsert_worklog(run, w, ctx)
            if got.status in ("inserted", "updated", "reconciled"):
                result.worklogs += 1
        # Your posted worklogs that the issue no longer has were deleted in Jira.
        for (jira_wid,) in run.con.execute("SELECT jira_worklog_id FROM worklogs WHERE work_item_id = ? "
                                           "AND state = 'posted' AND jira_worklog_id IS NOT NULL", (item.id,)).fetchall():
            if jira_wid not in on_issue and mo.mark_worklog_deleted(run, jira_wid):
                result.worklogs_removed += 1


def _deleted_worklogs(con: sqlite3.Connection, client: JiraClient, ctx: mo.JiraContext, history_days: int,
                      result: CollectResult, now: Optional[datetime]) -> bool:
    """Apply Jira's list of deleted worklogs: removing your only worklog on an issue takes the issue
    out of `worklogAuthor = currentUser()`, so the per-issue check above would never see it go."""
    now = now or datetime.now(timezone.utc)
    with muninn.Run(con, store.APP, ctx.source_id, "worklogs:deleted") as run:
        since_ms = int(muninn.from_ts(run.cursor).timestamp() * 1000) - 120_000 if run.cursor else \
            int((now - timedelta(days=history_days)).timestamp() * 1000)
        try:
            for page, until_ms in client.deleted_worklogs(since_ms):
                ids = [str(v.get("worklogId")) for v in page if v.get("worklogId") is not None]
                held = [r[0] for r in con.execute(
                    "SELECT jira_worklog_id FROM worklogs w JOIN work_items i ON i.id = w.work_item_id "
                    "WHERE i.source_id = ? AND w.state = 'posted' AND w.jira_worklog_id IN "
                    "(SELECT value FROM json_each(?))", (ctx.source_id, json.dumps(ids))).fetchall()]
                for jira_wid in held:
                    result.worklogs_removed += 1 if mo.mark_worklog_deleted(run, jira_wid) else 0
                if until_ms:
                    run.advance_cursor(muninn.to_ts(datetime.fromtimestamp(until_ms / 1000, timezone.utc)))
        except JiraError as exc:
            if exc.status == 404:            # an older Jira without the endpoint: per-issue checks only
                log.debug("This Jira has no /worklog/deleted: %s", exc)
                return True
            run.problem(str(exc))
    result.add(run)
    return not run.problems
