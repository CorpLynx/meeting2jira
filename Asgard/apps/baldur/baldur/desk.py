"""What Baldur's window shows and does, without the window, so all of it is testable with no display.

Every function takes the connection to use, because the window runs each call on a worker
thread with that thread's own connection (sqlite3 connections stay on the thread that opened
them), and nothing here holds Muninn's write lock while the screen waits for you.

The window's rules:
- What you see is a fresh estimate over Muninn as it is now (store.compute), the same numbers
  `cli.py days` and `cli.py report` show.
- Approving a day first stores that estimate (store.run, which writes only what's new), then
  approves the day's open proposals through muninn.baldur in one transaction. A figure you
  typed replaces the proposal's for that ticket only.
- An approved figure is changed with change_approval: a new approved row, the old one kept.
- Copy for timesheet copies approved figures; anything not yet approved is marked so.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from asgard import muninn
from asgard.muninn import baldur as approvals

from . import assist
from . import collect as collector
from . import estimate as E
from . import github, report, store
from .settings import Settings


class DeskError(RuntimeError):
    """Something you asked for can't be done; the message says why and what to do."""


def week_of(day: dt.date) -> dt.date:
    """The Monday of day's week."""
    return day - dt.timedelta(days=day.weekday())


def week_days(monday: dt.date) -> List[dt.date]:
    return [monday + dt.timedelta(days=n) for n in range(7)]


@dataclass
class DayRow:
    """One line of the week list."""
    day: dt.date
    dev: int                    # minutes on keyed tickets, as estimated now
    tickets: str
    review: str
    has_work: bool


@dataclass
class TicketRow:
    """One ticket on one day: the estimate, what you decided, what Jira holds."""
    key: str
    estimate: int               # minutes, from the fresh estimate
    open_id: Optional[int]      # the open proposal you'd approve
    approved_id: Optional[int]
    approved: Optional[int]     # minutes you approved
    held: Optional[int]         # minutes Jira holds that day (None: Odin hasn't looked it up)
    review: str                 # the report's review column
    problem: Optional[str] = None

    @property
    def figure(self) -> int:
        """The number this ticket stands at: your approval if you made one, otherwise the estimate."""
        return self.approved if self.approved is not None and self.open_id is None else self.estimate

    @property
    def approvable(self) -> bool:
        return self.estimate > 0 and not self.problem


@dataclass
class DayView:
    day: dt.date
    report: str
    tickets: List[TicketRow] = field(default_factory=list)
    untracked: int = 0
    flags: List[str] = field(default_factory=list)
    ai: Optional[assist.DaySuggestions] = None      # the AI-assisted figures, when there are any


def load_week(con: sqlite3.Connection, settings: Settings, monday: dt.date) -> List[DayRow]:
    days = week_days(monday)
    est = store.compute(con, settings, days[0], days[-1])
    todo = store.plan(con, settings, est, days[0], days[-1])
    rows = store.current_rows(con, days[0], days[-1])
    out = []
    for day in days:
        keyed = [x for x in est.proposals_for(day) if x.key and x.minutes_proposed]
        untracked = sum(x.minutes_proposed for x in est.proposals_for(day) if x.key is None)
        tickets = ", ".join(f"{x.key} {E.fmt(x.minutes_proposed)}" for x in keyed)
        if untracked:
            tickets = ", ".join(t for t in (tickets, f"untracked {E.fmt(untracked)}") if t)
        mine = {k: v for (d, k), v in rows.items() if d == day.isoformat() and k}
        status = []
        to_review = sum(1 for v in mine.values() if any(r["status"] == "proposed" for r in v))
        approved = sum(1 for v in mine.values() if any(r["status"] == "approved" for r in v))
        if to_review:
            status.append(f"{to_review} to review")
        if approved:
            status.append(f"{approved} approved")
        if todo.would_change(day) and keyed:
            status.append("new estimate" if not mine else "changed")
        has_work = bool(est.parts_on(day)) or bool(mine)
        out.append(DayRow(day, sum(x.minutes_proposed for x in keyed), tickets if has_work else "no commits",
                          ", ".join(status), has_work))
    return out


def load_day(con: sqlite3.Connection, settings: Settings, day: dt.date) -> DayView:
    est = store.compute(con, settings, day, day)
    todo = store.plan(con, settings, est, day, day)
    ai = assist.suggestions(con, settings, day)
    view = DayView(day, report.render_day(con, est, day, settings, plan=todo, ai=ai), flags=list(est.days[day].flags),
                   ai=ai)
    for t in report.tickets_for(con, est, day, todo):
        approved = t.approved
        view.tickets.append(TicketRow(
            t.key, t.estimate, int(t.open["id"]) if t.open is not None else None,
            int(approved["id"]) if approved is not None else None,
            int(approved["minutes_final"]) if approved is not None else None,
            t.held, report.review_text(con, t, False), t.problem))
    view.untracked = sum(x.minutes_proposed for x in est.proposals_for(day) if x.key is None)
    return view


def approve_day(con: sqlite3.Connection, settings: Settings, day: dt.date,
                figures: Optional[Dict[str, int]] = None, take_ai: Optional[str] = None) -> List[int]:
    """Store the day's estimate, then approve it; figures replace the proposals for those tickets.

    take_ai, the id of the AI-assisted figures the window showed (DayView.ai.digest()), approves at
    those figures instead (assist.approve_day), and only while they're still the ones shown; figures
    you typed still win for their tickets.
    """
    figures = {k.upper(): int(v) for k, v in (figures or {}).items()}
    for key, minutes in figures.items():
        if not 0 <= minutes <= 24 * 60:
            raise DeskError(f"{key}: {minutes} minutes doesn't fit in a day.")
    if take_ai is not None:
        try:
            return assist.approve_day(con, settings, day, figures, shown=take_ai)
        except assist.AssistError as exc:
            raise DeskError(str(exc)) from None
    store.run(con, settings, day, day)
    open_keys = {r["work_item_key"] for r in con.execute(
        "SELECT work_item_key FROM day_proposals WHERE local_date = ? AND status = 'proposed' "
        "AND work_item_key IS NOT NULL", (day.isoformat(),))}
    missing = sorted(set(figures) - open_keys)
    if missing:
        raise DeskError(f"{', '.join(missing)} has nothing to approve on {day.isoformat()}: it's already decided, "
                        "or its estimate rounds to nothing. Change an approved figure from its row instead.")
    if not open_keys:
        raise DeskError(f"Nothing to approve on {day.isoformat()}.")
    return approvals.approve_day(con, day.isoformat(), figures)


def reject_day(con: sqlite3.Connection, settings: Settings, day: dt.date) -> List[int]:
    store.run(con, settings, day, day)
    done = approvals.reject_day(con, day.isoformat())
    if not done:
        raise DeskError(f"Nothing to reject on {day.isoformat()}.")
    return done


def change_figure(con: sqlite3.Connection, approved_id: int, minutes: int) -> int:
    """A different figure for an approved ticket: a new approved row; Odin posts the difference."""
    if not 0 <= minutes <= 24 * 60:
        raise DeskError(f"{minutes} minutes doesn't fit in a day.")
    return approvals.change_approval(con, approved_id, minutes)


def estimate(con: sqlite3.Connection, settings: Settings, first: dt.date, last: dt.date) -> store.RunResult:
    return store.run(con, settings, first, last)


def collect(con: sqlite3.Connection, settings: Settings) -> collector.CollectResult:
    return collector.collect(con, settings)


@dataclass
class ReviewItem:
    """A pull request waiting on you: a review requested of you, or a review of yours to read."""
    kind: str                   # 'requested' or 'reviewed'
    repo: str
    number: int
    title: str
    who: str                    # its author, or the reviewer
    state: str                  # for 'reviewed': approved or changes_requested
    url: str
    draft: bool = False

    @property
    def label(self) -> str:
        return f"{self.repo}#{self.number}"


def review_items(con: sqlite3.Connection, days: int = 7) -> List[ReviewItem]:
    """Reviews requested of you (oldest first), then approvals and change requests on your
    pull requests from the last `days` days. Reads Muninn only; github.sync fills it."""
    out = [ReviewItem("requested", r["repo"], r["number"], r["title"], r["author"], "", r["url"], bool(r["is_draft"]))
           for r in con.execute("SELECT coalesce(r.github_repo, r.name) AS repo, p.number, p.title, p.author, p.url, "
                                "p.is_draft FROM pull_requests p JOIN repos r ON r.id = p.repo_id "
                                "WHERE p.state = 'open' AND p.review_requested = 1 ORDER BY p.created_at, p.id")]
    since = muninn.to_ts(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days))
    out += [ReviewItem("reviewed", r["repo"], r["number"], r["title"], r["reviewer"], r["state"], r["url"])
            for r in con.execute("SELECT coalesce(r.github_repo, r.name) AS repo, p.number, p.title, p.url, "
                                 "v.reviewer, v.state FROM pr_reviews v JOIN pull_requests p ON p.id = v.pr_id "
                                 "JOIN repos r ON r.id = p.repo_id WHERE p.is_mine = 1 AND v.is_mine = 0 "
                                 "AND v.state IN ('approved', 'changes_requested') AND v.submitted_at >= ? "
                                 "ORDER BY v.submitted_at DESC", (since,))]
    return out


def review_lines(con: sqlite3.Connection) -> List[str]:
    items = review_items(con)
    asked = [i for i in items if i.kind == "requested"]
    lines = [f"Reviews asked    {len(asked)}"]
    lines += [f"  {i.label:<24} {i.title[:44]:<44} {i.who}{' (draft)' if i.draft else ''}" for i in asked]
    done = [i for i in items if i.kind == "reviewed"]
    if done:
        lines.append(f"Your PRs, 7 days {len(done)} reviews")
        lines += [f"  {i.label:<24} {i.state.replace('_', ' '):<18} by {i.who}" for i in done]
    return lines


def sync_github(con: sqlite3.Connection, settings: Settings) -> Optional[github.SyncResult]:
    """One GitHub pass when GitHub is on and a token is saved; None otherwise (local git only)."""
    host = settings.github_host()
    token = github.load_token(host) if host else None
    if not token:
        return None
    return github.sync(con, settings, github.Client(settings.github_api, token))


def timesheet(view: DayView) -> str:
    """The day as plain text for a timesheet: tab-separated, so it pastes into a spreadsheet too."""
    lines = [f"{report.day_name(view.day)}  development time (Baldur estimate)"]
    total = 0
    pending = False
    for t in view.tickets:
        decided = t.approved is not None and t.open_id is None
        minutes = t.approved if decided else t.estimate
        if not minutes:
            continue
        total += minutes
        pending = pending or not decided
        lines.append(f"{t.key}\t{E.fmt(minutes)}\t{minutes / 60:.2f} h" + ("" if decided else "\tnot approved yet"))
    if total == 0:
        lines.append("No development time to report.")
    else:
        lines.append(f"Total\t{E.fmt(total)}\t{total / 60:.2f} h")
    if pending:
        lines.append("Lines marked 'not approved yet' are estimates you haven't approved.")
    return "\n".join(lines)
