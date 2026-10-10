"""Writing time to Jira: meeting worklogs, approved Baldur days, and posts nobody saw finish.

Position in the flow
    sync.py calls log_meeting() for each sub-task it creates; cli.py calls settle_stuck() before
    anything else that writes, retry_meetings() after the push, and post_approved() last, once the
    worklog sync has told Muninn what Jira already holds.

The protocol, which is a correctness matter
    Every worklog goes through a 'sending' row in Muninn, written and committed before Jira is
    called, whose comment ends with a marker such as [asgard:m-9f3c1a2b] (asgard.muninn.odin).
      * Jira answers with the new worklog's id: finish_post() records it.
      * Jira definitely refuses (a 4xx): fail_post() records why; the time is offered again.
      * Anything else (a timeout, a 5xx, a lost answer) leaves the row 'sending'. The next run's
        settle_stuck() searches that issue's worklogs for the marker: found means posted, not
        found means failed. So a crash or a timeout can never post the same time twice.

Baldur's approved days
    Odin posts only what you approved in Baldur, and only what Jira is missing: v_worklogs_to_post
    subtracts what Jira already holds, which is why the worklog sync must run first in the same
    run. posts_due() is capped per run (muninn.max_posts_per_run).
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from asgard import muninn
from asgard.muninn import odin as mo

from . import store
from .jira import JiraClient, JiraError

log = logging.getLogger(__name__)


@dataclass
class PostResult:
    posted: List[str] = field(default_factory=list)      # "PROJ-12 1h00m"
    failed: List[str] = field(default_factory=list)      # Jira refused; offered again later
    unknown: List[str] = field(default_factory=list)     # outcome unseen; settled next run
    skipped: List[str] = field(default_factory=list)     # nothing to send after all
    settled: int = 0                                     # stuck posts resolved at the start
    unsettled: int = 0                                   # stuck posts Jira couldn't be asked about
    planned: List[str] = field(default_factory=list)     # dry run: what would be posted


def _hm(seconds: int) -> str:
    minutes = int(round(seconds / 60))
    return f"{minutes // 60}h{minutes % 60:02d}m"


def send(con: sqlite3.Connection, client: JiraClient, post: mo.PendingPost) -> Tuple[str, str]:
    """Send one 'sending' row to Jira. Returns ('posted' | 'failed' | 'unknown', detail)."""
    try:
        worklog_id = client.add_worklog(post.key, post.seconds, post.started, post.comment)
    except JiraError as exc:
        if exc.refused:
            mo.fail_post(con, post.worklog_id, str(exc))
            return "failed", str(exc)
        return "unknown", f"{exc} (checked against Jira on the next run)"
    if not worklog_id:
        return "unknown", "Jira didn't say which worklog it made (checked on the next run)"
    try:
        mo.finish_post(con, post.worklog_id, worklog_id)
    except (sqlite3.DatabaseError, muninn.MuninnError) as exc:
        # Jira has it; the marker lets the next run's settle_stuck() record it.
        return "unknown", f"posted as {worklog_id}, but Muninn couldn't record it yet: {exc}"
    return "posted", str(worklog_id)


def _tally(result: PostResult, post: mo.PendingPost, status: str, detail: str) -> None:
    line = f"{post.key} {_hm(post.seconds)}"
    if status == "posted":
        result.posted.append(line)
        log.info("  LOGGED   %-14s %s", post.key, _hm(post.seconds))
    elif status == "failed":
        result.failed.append(f"{line}: {detail}")
        log.warning("  REFUSED  %-14s %s  %s", post.key, _hm(post.seconds), detail)
    else:
        result.unknown.append(f"{line}: {detail}")
        log.warning("  UNSURE   %-14s %s  %s", post.key, _hm(post.seconds), detail)


# --------------------------------------------------------------------------
# Posts nobody saw finish
# --------------------------------------------------------------------------

def settle_stuck(con: sqlite3.Connection, client: JiraClient, timeout: float = 30) -> PostResult:
    """Settle 'sending' rows left by a run that stopped or never heard back.

    Odin runs one at a time (store.RunLock), so a 'sending' row older than a few HTTP timeouts
    belongs to a run that is over. Each is looked up in its issue's worklogs by its marker.
    """
    result = PostResult()
    for stuck in mo.stuck_posts(con, older_than_seconds=max(120, int(4 * timeout))):
        if not stuck["marker"]:
            continue
        try:
            found = client.find_worklog(stuck["key"], stuck["marker"])
        except JiraError as exc:
            if exc.status != 404:          # 404: the issue is gone, so the worklog can't be there
                result.unsettled += 1
                log.warning("  could not check %s for an interrupted post: %s", stuck["key"], exc)
                continue
            found = None
        mo.resolve_stuck(con, stuck["worklog_id"], str(found["id"]) if found else None, searched=True)
        result.settled += 1
        log.info("  SETTLED  %-14s %s", stuck["key"],
                 f"posted as {found['id']}" if found else "not in Jira; offered again")
    return result


# --------------------------------------------------------------------------
# Meeting time
# --------------------------------------------------------------------------

def _meeting_post(con: sqlite3.Connection, ms: sqlite3.Row, key: str) -> Optional[mo.PendingPost]:
    """The 'sending' row for a sub-task's meeting time: on its calendar event when Muninn has it,
    else (history from state.db, or an event since removed) from the record itself."""
    comment = ms["worklog_comment"] or ms["summary"]
    event = ms["calendar_event_id"]
    if event is not None and con.execute("SELECT 1 FROM calendar_events WHERE id = ? AND deleted_at IS NULL",
                                         (event,)).fetchone():
        return mo.begin_meeting_post(con, event, key=key, comment=comment)
    return mo.begin_manual_post(con, key, ms["started_at"], max(60, int(ms["minutes"]) * 60), comment)


def log_meeting(con: sqlite3.Connection, client: JiraClient, ms: sqlite3.Row, result: PostResult,
                run: Optional[muninn.Run] = None, ctx: Optional[mo.JiraContext] = None) -> None:
    """Log a meeting sub-task's time in Jira, once.

    With run and ctx (a retry), the sub-task's worklogs are read from Jira first: time logged on it
    by hand, or by Odin before it used Muninn, counts, and nothing is sent.
    """
    item = mo.issue_by_key(con, ms["issue_key"])
    if item is None or item["deleted_at"] is not None:
        result.skipped.append(f"{ms['issue_key']}: not in Muninn yet; tried again next run")
        return
    if run is not None and ctx is not None:
        try:
            worklogs = client.issue_worklogs(item["key"])
        except JiraError as exc:
            result.skipped.append(f"{item['key']}: couldn't read its worklogs ({exc}); tried again next run")
            return
        with run.batch():
            for raw in worklogs:
                mo.upsert_worklog(run, raw, ctx)
        if not store.still_pending(con, ms["id"]):
            log.info("  LOGGED   %-14s already in Jira", item["key"])
            return
    try:
        post = _meeting_post(con, ms, item["key"])
    except muninn.MuninnError as exc:
        result.skipped.append(f"{item['key']}: {exc}")
        log.warning("           %s: %s", item["key"], exc)
        return
    if post is None:          # this meeting's time is already in Jira, perhaps from another calendar
        result.skipped.append(f"{item['key']}: this meeting's time is already logged")
        return
    status, detail = send(con, client, post)
    _tally(result, post, status, detail)


def retry_meetings(con: sqlite3.Connection, client: JiraClient, jira_sid: int, ctx: mo.JiraContext,
                   collect_issue: Any, result: PostResult) -> None:
    """Log the meeting time an earlier run couldn't, before anything new is created.

    collect_issue(run, key) fetches and stores a sub-task Muninn doesn't hold yet (collect.py).
    """
    pending = store.pending_meeting_worklogs(con)
    if not pending:
        return
    with muninn.Run(con, store.APP, jira_sid, "meeting-worklogs") as run:
        for ms in pending:
            if mo.issue_by_key(con, ms["issue_key"]) is None:
                try:
                    collect_issue(run, ms["issue_key"], ctx)
                except JiraError as exc:
                    run.problem(f"{ms['issue_key']}: {exc}")
                    result.skipped.append(f"{ms['issue_key']}: {exc}")
                    continue
            log_meeting(con, client, ms, result, run=run, ctx=ctx)


# --------------------------------------------------------------------------
# Approved Baldur days
# --------------------------------------------------------------------------

def post_approved(con: sqlite3.Connection, client: Optional[JiraClient], cap: int,
                  dry_run: bool = False) -> PostResult:
    """Post the time you approved in Baldur that Jira is missing, oldest day first."""
    result = PostResult()
    due = mo.posts_due(con)
    for row in due[:max(0, cap)]:
        line = f"{row['key']} {row['local_date']} {_hm(int(row['minutes_to_post']) * 60)}"
        if dry_run or client is None:
            result.planned.append(line)
            log.info("  WOULD    %-14s log %s for %s (approved in Baldur)", row["key"],
                     _hm(int(row["minutes_to_post"]) * 60), row["local_date"])
            continue
        post = mo.begin_post(con, row["proposal_id"])
        if post is None:
            result.skipped.append(f"{line}: no longer due")
            continue
        status, detail = send(con, client, post)
        _tally(result, post, status, detail)
    if len(due) > cap:
        log.warning("Posted %d approved day(s); %d more wait for the next run (muninn.max_posts_per_run).",
                    cap, len(due) - cap)
    return result
