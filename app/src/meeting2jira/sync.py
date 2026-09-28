"""The push loop: decide -> dedupe -> render -> create sub-task -> worklog -> transition.

Position in the flow
    The centre of the program. `run()` takes Meetings from sources.py, asks rules.Router what to
    do with each, checks state.py for prior work, renders the templates, and drives jira.py.
    Everything it needs is passed in, so the whole pipeline is testable offline with a fake client.

Order of operations, and why
    1. Retry worklogs left unfinished by an earlier run, before anything new is created.
    2. Sort by start time and drop within-run duplicates (same key or same content hash).
    3. Router decides: skip with a reason, or create under a parent.
    4. state.find short-circuits anything already pushed.
    5. Honour max_creates_per_run, counting created + recovered + planned.
    6. Create, then record state *immediately*, then worklog, then transition. A failure in the
       last two is a warning, not an error, because the sub-task already exists and is recorded.

The failure case that shapes this file
    A create that fails ambiguously (proxy timeout, 502/503/504) may have been applied anyway.
    Retrying blindly duplicates the sub-task; giving up duplicates it on the next run. So every
    sub-task carries a deterministic `m2j-<hash>` label and `recover_created_issue` does an exact
    JQL lookup. Exactly one match is adopted; zero, several, or a failed search are reported for a
    human, never guessed at.

Returns a RunResult, never raises for per-item problems: one bad meeting must not abandon the rest.
Only a configuration fault (a bad template) aborts the run.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .config import ConfigError
from .jira import JiraClient, JiraError
from .models import Meeting, parse_utc
from .rules import OUTSIDE, Router
from .state import MAX_WORKLOG_ATTEMPTS, State

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
    recovered: List[str] = field(default_factory=list)   # created despite an ambiguous failure
    worklogs_retried: int = 0


def retry_pending_worklogs(state: State, client: JiraClient, result: RunResult) -> None:
    """Log work for sub-tasks created earlier whose worklog call failed.

    The sub-task exists and is recorded, so the only risk here is a duplicate worklog. That is
    guarded by worklog_logged, which is set the moment Jira accepts one, and by a bounded
    attempt count so a worklog Jira keeps rejecting eventually stops being retried.
    """
    for row in state.pending_worklogs():
        comment = row["worklog_comment"] or row["summary"]
        try:
            started = parse_utc(row["start_utc"])
            worklog_id = client.add_worklog(row["issue_key"], int(row["minutes"]) * 60, started, comment)
        except JiraError as exc:
            state.note_worklog_attempt(row["key"])
            attempts = int(row["worklog_attempts"]) + 1
            msg = f"{row['issue_key']}: worklog retry {attempts}/{MAX_WORKLOG_ATTEMPTS} failed: {exc}"
            result.warnings.append(msg)
            log.warning("           %s", msg)
            continue
        state.mark_worklog(row["key"], worklog_id)
        result.worklogs_retried += 1
        log.info("  WORKLOG  %-14s retried for %s (%dm)", row["issue_key"], row["start_utc"], row["minutes"])


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


def recover_created_issue(client: JiraClient, parent: str, marker: str) -> Optional[str]:
    """After an ambiguous create, find out whether the sub-task exists after all.

    Returns the issue key on an unambiguous single match. Returns None when the issue is absent
    (safe to create next run), when several issues carry the marker (a human should look), or
    when the search itself fails. Raises nothing: every outcome is "leave it for later".
    """
    project = parent.rsplit("-", 1)[0]
    jql = f'project = "{project}" AND labels = "{marker}" ORDER BY created DESC'
    try:
        keys = client.search_issue_keys(jql)
    except JiraError as exc:
        log.warning("           could not search for %s after an ambiguous create: %s", marker, exc)
        return None
    if len(keys) == 1:
        return keys[0]
    if len(keys) > 1:
        log.warning("           %s matches several issues (%s); not recording any of them",
                    marker, ", ".join(keys))
    return None


def run(meetings: Iterable[Meeting], cfg: Dict[str, Any], state: State, client: Optional[JiraClient],
        dry_run: bool = False, now: Optional[datetime] = None) -> RunResult:
    if not dry_run and client is None:
        raise ValueError("A Jira client is required unless dry_run is set")
    now = now or datetime.now(timezone.utc)
    j = cfg["jira"]
    templates = cfg["templates"]
    router = Router(cfg)
    cap = int(j.get("max_creates_per_run") or 0)
    result = RunResult()

    me = client.myself() if (client and not dry_run and j.get("assign_to_me")) else None

    # Clear last run's unfinished worklogs first: the sub-tasks already exist, so this is the
    # cheapest way to stop time quietly going unlogged.
    if client and not dry_run and j.get("log_work"):
        retry_pending_worklogs(state, client, result)

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

        existing = state.find(m)
        if existing:
            result.existing += 1
            log.info("  EXISTS   %-14s %s", existing["issue_key"], label)
            continue

        # Recovered issues count against the cap too: they are sub-tasks this run is responsible
        # for, and each one costs a create attempt plus a search.
        if cap and (len(result.created) + len(result.recovered) + result.planned) >= cap:
            result.capped += 1
            continue

        parent = decision.parent or j["default_parent"]
        ctx = template_context(m, parent, decision.tod_status, router.tod.minutes_outside(m))
        summary = clean_summary(render(templates["summary"], ctx))
        description = render(templates["description"], ctx)

        if dry_run:
            result.planned += 1
            log.info("  WOULD    %-14s %s  -> \"%s\"", parent, label, summary)
            continue

        marker = dedupe_label(m) if j.get("dedupe_label") else None
        tod_labels = ([router.tod.label] if decision.tod_status == OUTSIDE
                      and router.tod.action == "label" else [])
        recovered = False
        try:
            issue_key = client.create_issue(
                build_fields(cfg, parent, summary, description, me, marker, tod_labels))
        except JiraError as exc:
            # An ambiguous failure may still have created the sub-task. Creating it again next
            # run would duplicate it, so look for the marker before giving up.
            issue_key = None
            if exc.ambiguous and marker:
                issue_key = recover_created_issue(client, parent, marker)
            if issue_key is None:
                detail = f"{exc} [ambiguous: verify in Jira before the next run]" if exc.ambiguous else str(exc)
                result.errors.append(f"{label}: {detail}")
                log.error("  FAILED   %-14s %s  %s", parent, label, detail)
                continue
            recovered = True
            result.recovered.append(issue_key)
            log.warning("  RECOVERED %-13s %s  (create reported failure but the issue exists)",
                        issue_key, label)
        # Record first, and store the rendered worklog comment with it, so a worklog failure
        # can be retried on a later run without re-deriving it from the calendar.
        wants_worklog = bool(j.get("log_work"))
        worklog_comment = render(templates["worklog_comment"], ctx) if wants_worklog else None
        state.record(m, issue_key, parent, summary, worklog_comment, worklog_wanted=wants_worklog)
        if not recovered:
            result.created.append(issue_key)
            log.info("  CREATED  %-14s %s  (under %s)", issue_key, label, parent)

        if j.get("log_work"):
            try:
                # The worklog duration is the meeting's real length, from the calendar item.
                worklog_id = client.add_worklog(issue_key, m.minutes * 60, m.start_utc, worklog_comment)
                state.mark_worklog(m.key, worklog_id)
            except JiraError as exc:
                state.note_worklog_attempt(m.key)
                result.warnings.append(f"{issue_key}: worklog failed: {exc}")
                log.warning("           worklog failed for %s: %s (will retry next run)", issue_key, exc)

        if j.get("transition_to"):
            try:
                if not client.transition(issue_key, j["transition_to"]):
                    msg = f"{issue_key}: no transition named '{j['transition_to']}' available"
                    result.warnings.append(msg)
                    log.warning("           %s", msg)
            except JiraError as exc:
                result.warnings.append(f"{issue_key}: transition failed: {exc}")
                log.warning("           transition failed for %s: %s", issue_key, exc)

    if result.capped:
        log.warning("Stopped at jira.max_creates_per_run=%d; %d more meeting(s) left for the next run "
                    "(or pass --max).", cap, result.capped)
    return result
