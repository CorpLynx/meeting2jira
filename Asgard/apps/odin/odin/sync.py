"""The push loop: decide -> dedupe -> render -> create sub-task -> record -> worklog -> transition.

Position in the flow
    The centre of Odin's meeting push. `run()` takes Meetings from sources.py, asks rules.Router
    what to do with each, checks Muninn's meeting_subtasks for prior work (store.py), renders the
    templates, and drives jira.py. Everything it needs is passed in, so the whole pipeline is
    testable offline with a fake client and a temporary Muninn.

Order of operations, and why
    1. Sort by start time and drop within-run duplicates (same key or same content hash).
    2. Router decides: skip with a reason, or create under a parent.
    3. store.find_subtask short-circuits anything already pushed (and, before the one-time import,
       anything state.db or the journal knows).
    4. Honour max_creates_per_run, counting created + recovered + planned.
    5. Look in Jira for this meeting's label, then create, then record in Muninn *immediately*,
       then fetch the new sub-task into Muninn, then log the meeting's time, then transition. A
       failure in the last three is a warning, not an error, because the sub-task already exists
       and is recorded; its worklog is retried by the next run (posting.retry_meetings).

The failure case that shapes this file
    Jira can make a sub-task that Odin never records: the answer is lost (a proxy timeout, a 5xx,
    an answer cut off), the run is stopped or killed between the create and the record, or the
    disk is full. Retrying blindly duplicates it, and so would the next run. So every sub-task
    carries a deterministic `m2j-<hash>` label, and before each create `find_created_issue` looks
    for one you made with an exact JQL lookup (again right after an ambiguous failure). Exactly one
    match is adopted; several, or a failed search, are reported for a person and nothing is
    created. If Muninn can't take a record, it goes to the journal and the run stops creating
    (store.record_subtask). With jira.dedupe_label off none of this can look, so a run stopped
    mid-create may make that sub-task again.

Returns a RunResult, never raises for per-item problems: one bad meeting must not abandon the rest.
Only a configuration fault (a bad template) aborts the run.
"""
from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from asgard import muninn
from asgard.muninn import odin as mo

from . import collect, posting, store
from .config import ConfigError
from .jira import JiraClient, JiraError
from .models import Meeting
from .rules import OUTSIDE, Router

log = logging.getLogger(__name__)

TEMPLATE_FIELDS = ("subject", "start_local", "end_local", "start_utc", "end_utc", "minutes", "hours",
                   "organizer", "location", "categories", "response", "source", "parent",
                   "tod_status", "minutes_outside_tod")


@dataclass
class RunResult:
    created: List[str] = field(default_factory=list)
    planned: int = 0
    existing: int = 0
    capped: int = 0
    skipped: Counter = field(default_factory=Counter)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    recovered: List[str] = field(default_factory=list)   # found in Jira after an ambiguous failure, or made by
                                                         # an earlier run that couldn't record it
    worklogs_retried: int = 0
    posts: posting.PostResult = field(default_factory=posting.PostResult)


def template_context(m: Meeting, parent: str, tod_status: str = "unknown",
                     minutes_outside_tod: int = 0) -> Dict[str, Any]:
    return {
        "tod_status": tod_status,
        "minutes_outside_tod": minutes_outside_tod,
        "subject": m.subject,
        "start_local": m.start_local,
        "end_local": m.end_local,
        "start_utc": m.start_utc,
        "end_utc": m.end_utc,
        "minutes": m.minutes,
        "hours": f"{m.minutes / 60:.2f}",
        "organizer": m.organizer or "",
        "location": m.location or "",
        "categories": ", ".join(m.categories),
        "response": m.response,
        "source": m.source,
        "parent": parent,
    }


def render(template: str, ctx: Dict[str, Any]) -> str:
    try:
        return template.format_map(ctx)
    except KeyError as exc:
        raise ConfigError(f"Template {template!r} uses unknown field {exc}; available: {', '.join(TEMPLATE_FIELDS)}") from None
    except (ValueError, IndexError) as exc:
        raise ConfigError(f"Template {template!r} is invalid: {exc}") from None


def clean_summary(text: str) -> str:
    return " ".join(text.split())[:255]   # Jira summaries: single line, max 255 chars


def dedupe_label(m: Meeting) -> str:
    """A deterministic, collision-resistant marker derived from the meeting itself.

    Same meeting -> same label, on every run and from either source, which is what lets an
    ambiguous create be resolved with an exact JQL lookup instead of summary matching.
    """
    return f"m2j-{m.content_hash[:10]}"


def build_fields(cfg: Dict[str, Any], parent: str, summary: str, description: str,
                 me: Optional[Dict[str, Any]], marker: Optional[str] = None,
                 extra_labels: Optional[List[str]] = None) -> Dict[str, Any]:
    j = cfg["jira"]
    fields: Dict[str, Any] = {
        "project": {"key": parent.rsplit("-", 1)[0]},
        "parent": {"key": parent},
        "issuetype": {"name": j["subtask_type"]},
        "summary": summary,
        "description": description,
    }
    labels = list(j.get("labels") or [])
    labels.extend(extra_labels or [])
    if marker:
        labels.append(marker)
    if labels:
        fields["labels"] = labels
    if me:
        fields["assignee"] = {"name": me["name"]}   # Data Center identifies users by name
    fields.update(j.get("extra_fields") or {})
    return fields


def find_created_issue(client: JiraClient, marker: str) -> Tuple[Optional[str], Optional[str]]:
    """The sub-task carrying this meeting's label that you created, if Jira has one.

    Returns (key, None) for exactly one, (None, None) for none (safe to create), and (None, why)
    when it can't tell: several issues carry the label, or the search failed. Searched across every
    project (a rule may have moved the meeting's parent since), and only what you created, because
    a colleague's Odin gives the same meeting the same label. Raises nothing.
    """
    jql = f'labels = "{marker}" AND creator = currentUser() ORDER BY created DESC'
    try:
        keys = client.search_issue_keys(jql)
    except JiraError as exc:
        return None, f"couldn't look in Jira for {marker} first, so nothing was created: {exc}"
    if len(keys) > 1:
        return None, f"{marker} is on several of your issues ({', '.join(keys)}); none was recorded or created"
    return (keys[0] if keys else None), None


def run(meetings: Iterable[Meeting], cfg: Dict[str, Any], con: sqlite3.Connection, client: Optional[JiraClient],
        *, data_dir: Path, dry_run: bool = False, now: Optional[datetime] = None,
        event_ids: Optional[Dict[str, int]] = None, ctx: Optional[mo.JiraContext] = None,
        me: Optional[Dict[str, Any]] = None,
        seen_before: Optional[Callable[[Meeting], Optional[str]]] = None) -> RunResult:
    """Push meetings to Jira as sub-tasks.

    con is Muninn (read-only for a dry run). event_ids maps each meeting's key to its row in
    calendar_events (store.store_calendar). ctx is Muninn's Jira context, needed to store each new
    sub-task. seen_before answers for history not yet in Muninn (state.db before its import).
    """
    if not dry_run and (client is None or ctx is None):
        raise ValueError("A Jira client and Muninn's Jira context are required unless dry_run is set")
    now = now or datetime.now(timezone.utc)
    j = cfg["jira"]
    templates = cfg["templates"]
    router = Router(cfg)
    cap = int(j.get("max_creates_per_run") or 0)
    result = RunResult()
    event_ids = event_ids or {}
    journal = store.Journal(data_dir)

    if client and not dry_run and j.get("assign_to_me") and me is None:
        me = client.myself()
    assignee = me if j.get("assign_to_me") else None

    with contextlib.ExitStack() as stack:
        # New sub-tasks are stored in Muninn under one sync run (none for a dry run, which writes nothing).
        subtasks = None if dry_run else stack.enter_context(
            muninn.Run(con, store.APP, ctx.source_id, "meeting-subtasks"))
        _push(meetings, cfg, con, client, result, router, cap, templates, now, dry_run, event_ids, journal,
              subtasks, ctx, assignee, seen_before, data_dir)

    if result.capped:
        log.warning("Stopped at jira.max_creates_per_run=%d; %d more meeting(s) left for the next run "
                    "(or pass --max).", cap, result.capped)
    return result


def _push(meetings: Iterable[Meeting], cfg: Dict[str, Any], con: sqlite3.Connection, client: Optional[JiraClient],
          result: RunResult, router: Router, cap: int, templates: Dict[str, str], now: datetime, dry_run: bool,
          event_ids: Dict[str, int], journal: store.Journal, subtasks: Optional[muninn.Run],
          ctx: Optional[mo.JiraContext], assignee: Optional[Dict[str, Any]],
          seen_before: Optional[Callable[[Meeting], Optional[str]]], data_dir: Path) -> None:
    j = cfg["jira"]
    seen = set()
    for m in sorted(meetings, key=lambda x: x.start_utc):
        if m.key in seen or m.content_hash in seen:
            continue
        seen.update((m.key, m.content_hash))
        decision = router.decide(m, now)
        # Mark out-of-tour meetings in the console so a dry run shows them at a glance.
        flag = "*" if decision.tod_status == OUTSIDE else " "
        label = f"{m.start_local:%Y-%m-%d %H:%M}{flag}{m.minutes:>4}m  {m.subject}"
        if decision.action == "skip":
            result.skipped[decision.reason] += 1
            # A private item's subject is withheld from the log as well as from Jira. Logs persist
            # in the user's profile, and skip_private is meant to keep these out of scope
            # entirely, not merely out of the issue tracker. The time and reason still show, so a
            # dry run remains reviewable.
            shown = label
            if decision.reason == "private":
                shown = (f"{m.start_local:%Y-%m-%d %H:%M}{flag}{m.minutes:>4}m  "
                         "<private item, subject withheld>")
            log.info("  SKIP     %-14s %s  [%s]", "", shown, decision.reason)
            continue

        existing = store.find_subtask(con, m)
        known = existing["issue_key"] if existing else None
        if existing is not None and existing["calendar_event_id"] is None and not dry_run and m.key in event_ids:
            store.link_event(con, existing["id"], event_ids[m.key], existing["issue_key"])
        if known is None:
            held = journal.holds(m)
            known = held.issue_key if held else (seen_before(m) if seen_before else None)
        if known:
            result.existing += 1
            log.info("  EXISTS   %-14s %s", known, label)
            continue

        # Recovered issues count against the cap too: they are sub-tasks this run is responsible
        # for, and each one costs a search.
        if cap and (len(result.created) + len(result.recovered) + result.planned) >= cap:
            result.capped += 1
            continue

        why_not = store.unrecordable(m)
        if why_not:
            result.skipped[why_not] += 1
            log.info("  SKIP     %-14s %s  [%s]", "", label, why_not)
            continue

        parent = decision.parent or j["default_parent"]
        tctx = template_context(m, parent, decision.tod_status, router.tod.minutes_outside(m))
        summary = clean_summary(render(templates["summary"], tctx))
        description = render(templates["description"], tctx)
        wants_worklog = bool(j.get("log_work"))
        worklog_comment = render(templates["worklog_comment"], tctx) if wants_worklog else None

        if dry_run:
            result.planned += 1
            log.info("  WOULD    %-14s %s  -> \"%s\"", parent, label, summary)
            continue

        marker = dedupe_label(m) if j.get("dedupe_label") else None
        tod_labels = ([router.tod.label] if decision.tod_status == OUTSIDE
                      and router.tod.action == "label" else [])
        issue_key, why = find_created_issue(client, marker) if marker else (None, None)
        if why:
            result.errors.append(f"{label}: {why}")
            log.error("  FAILED   %-14s %s  %s", parent, label, why)
            continue
        recovered = issue_key is not None
        if recovered:
            result.recovered.append(issue_key)
            log.warning("  FOUND    %-14s %s  (an earlier run made it but couldn't record it)", issue_key, label)
        else:
            try:
                issue_key = client.create_issue(
                    build_fields(cfg, parent, summary, description, assignee, marker, tod_labels))
            except JiraError as exc:
                # An ambiguous failure may still have created the sub-task. Look for the marker
                # before giving up; the next run looks again before creating.
                if exc.ambiguous and marker:
                    issue_key, why = find_created_issue(client, marker)
                if issue_key is None:
                    detail = str(exc)
                    if exc.ambiguous:
                        detail += (" [it may exist: the next run looks for its label before creating]" if marker
                                   else " [ambiguous: verify in Jira before the next run]")
                    if why:
                        detail += f"; {why}"
                    result.errors.append(f"{label}: {detail}")
                    log.error("  FAILED   %-14s %s  %s", parent, label, detail)
                    continue
                recovered = True
                result.recovered.append(issue_key)
                log.warning("  RECOVERED %-13s %s  (create reported failure but the issue exists)",
                            issue_key, label)

        # Record first, with the rendered worklog comment, so a worklog failure can be retried on
        # a later run without re-deriving it from the calendar.
        record = store.SubtaskRecord.for_meeting(m, issue_key, parent, summary, event_ids.get(m.key),
                                                 wants_worklog, worklog_comment)
        kept = store.record_subtask(con, record, data_dir)
        if kept:
            msg = f"{issue_key}: created, but {kept}. This run stopped creating."
            result.errors.append(msg)
            log.error("  FAILED   %-14s %s", issue_key, msg)
            if not recovered:
                result.created.append(issue_key)
            return
        if not recovered:
            result.created.append(issue_key)
            log.info("  CREATED  %-14s %s  (under %s)", issue_key, label, parent)

        # Muninn needs the sub-task itself before its time can be logged against it.
        try:
            collect.collect_issue(subtasks, issue_key, ctx, client)
        except (JiraError, sqlite3.DatabaseError, muninn.MuninnError) as exc:
            msg = f"{issue_key}: couldn't read it back from Jira ({exc}); its worklog is retried next run"
            result.warnings.append(msg)
            log.warning("           %s", msg)

        if wants_worklog:
            row = con.execute("SELECT * FROM meeting_subtasks WHERE meeting_key = ?", (m.key,)).fetchone()
            posts = result.posts          # a refusal stays in posts.failed: the run reports it as an error
            before = (len(posts.unknown), len(posts.skipped))
            if row is not None:
                # A sub-task an earlier run made may have had its time logged by hand since: read
                # its worklogs first, as a retry does.
                posting.log_meeting(con, client, row, posts, run=subtasks if recovered else None,
                                    ctx=ctx if recovered else None)
            for lines, n in zip((posts.unknown, posts.skipped), before):
                result.warnings.extend(f"worklog {line}" for line in lines[n:])

        if j.get("transition_to"):
            try:
                if not client.transition(issue_key, j["transition_to"]):
                    msg = f"{issue_key}: no transition named '{j['transition_to']}' available"
                    result.warnings.append(msg)
                    log.warning("           %s", msg)
            except JiraError as exc:
                result.warnings.append(f"{issue_key}: transition failed: {exc}")
                log.warning("           transition failed for %s: %s", issue_key, exc)
