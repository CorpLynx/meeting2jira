"""The day report: what Baldur measured, what it estimated, and what it can't see.

The numbers come from a fresh estimate over what's in Muninn now; the
review column shows what you've decided and whether storing the estimate
again would change anything. Output is plain ASCII so it survives any
console or clipboard.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

from . import estimate as E
from . import store
from .settings import Settings

if TYPE_CHECKING:                       # assist imports this module's neighbours, not this module
    from . import assist

WIDTH = 72
HELD_BY = {"jira": "logged by hand", "baldur": "posted by Odin", "manual": "typed into Odin"}
INVISIBLE = ("  Not visible to Baldur: design talk, reading code, messages, planning.\n"
             "  If the day felt fuller than this, that is the gap.")


def line(label: str, value: str = "", note: str = "") -> str:
    text = f"  {label:<36}{value:>7}"
    return f"{text}   {note}".rstrip() if note else text.rstrip()


def day_name(day: dt.date) -> str:
    return day.strftime("%a ") + day.isoformat()


@dataclass
class Ticket:
    key: str
    estimate: int = 0                              # minutes from the fresh estimate, rounded down
    rows: List[sqlite3.Row] = field(default_factory=list)   # open, approved and rejected, oldest first
    held: Optional[int] = None                     # minutes Jira holds for it that day; None: not looked up yet
    held_by: List[str] = field(default_factory=list)
    jira_key: Optional[str] = None                 # the key Jira uses now, when the issue moved
    problem: Optional[str] = None                  # why Odin can't post it: not found, or deleted
    action: Optional[str] = None                   # what storing the estimate again would do

    def _last(self, status: str) -> Optional[sqlite3.Row]:
        found = [r for r in self.rows if r["status"] == status]
        return found[-1] if found else None

    @property
    def open(self) -> Optional[sqlite3.Row]:
        return self._last("proposed")

    @property
    def approved(self) -> Optional[sqlite3.Row]:
        return self._last("approved")

    @property
    def rejected(self) -> Optional[sqlite3.Row]:
        return self._last("rejected")

    @property
    def target(self) -> int:
        """What Jira should hold once you approve: the open proposal, your approval, or the estimate.

        A rejection without an approval means nothing; rejecting a newer proposal leaves an approval standing.
        """
        if self.open is not None:
            return int(self.open["minutes_proposed"])
        if self.approved is not None:
            return int(self.approved["minutes_final"])
        return 0 if self.rejected is not None else self.estimate


def jira_status(con: sqlite3.Connection, key: str, day: dt.date) -> Tuple[Optional[int], List[str], Optional[str], Optional[str]]:
    """(minutes Jira holds, how they got there, key Jira uses now, why it can't be posted).

    Minutes are None while Odin hasn't looked the key up; that isn't a problem yet.
    """
    alias = con.execute("SELECT a.status, a.work_item_id, w.key AS now_key, w.deleted_at FROM work_item_aliases a "
                        "LEFT JOIN work_items w ON w.id = a.work_item_id WHERE a.key = ?", (key,)).fetchone()
    if alias is None:
        return None, [], None, None
    if alias["status"] == "not_found":
        return 0, [], None, "not found in Jira"
    lo, hi = store.day_bounds(day, day)
    rows = con.execute("SELECT origin, sum(seconds) FROM worklogs WHERE work_item_id = ? AND state IN ('sending', 'posted') "
                       "AND origin <> 'meeting' AND started_at >= ? AND started_at < ? GROUP BY origin ORDER BY origin",
                       (alias["work_item_id"], lo, hi)).fetchall()
    held = (sum(int(r[1]) for r in rows) + 59) // 60
    moved = alias["now_key"] if alias["now_key"] and alias["now_key"] != key else None
    return held, [HELD_BY.get(r[0], r[0]) for r in rows], moved, ("deleted in Jira" if alias["deleted_at"] else None)


def tickets_for(con: sqlite3.Connection, est: E.Estimate, day: dt.date, plan: Optional[store.Plan]) -> List[Ticket]:
    found: Dict[str, Ticket] = {}
    for prop in est.proposals_for(day):
        if prop.key:
            found[prop.key] = Ticket(prop.key, prop.minutes_proposed)
    for (_, key), rows in store.current_rows(con, day, day).items():
        if key:
            found.setdefault(key, Ticket(key)).rows = rows
    for t in found.values():
        t.held, t.held_by, t.jira_key, t.problem = jira_status(con, t.key, day)
        t.action = plan.action_for(day, t.key) if plan else None
    return [found[k] for k in sorted(found)]


def due(con: sqlite3.Connection, t: Ticket) -> int:
    """Minutes Odin would post for a ticket: now, for an approval, or once you approve, otherwise.

    For an approval this is Odin's own answer (v_worklogs_to_post), which
    already knows about posts in flight and worklogs deleted in Jira.
    """
    if t.problem:
        return 0
    if t.open is not None:
        return max(0, int(t.open["minutes_proposed"]) - (t.held or 0))
    if t.approved is not None:
        if t.held is None:
            return int(t.approved["minutes_final"])      # Odin posts it once it has looked the key up
        row = con.execute("SELECT minutes_to_post FROM v_worklogs_to_post WHERE proposal_id = ?",
                          (t.approved["id"],)).fetchone()
        return int(row[0]) if row else 0
    if t.rejected is not None:
        return 0
    return max(0, t.estimate - (t.held or 0))


def _posting(con: sqlite3.Connection, t: Ticket, row: sqlite3.Row) -> str:
    if t.problem:
        return f"can't be posted: {t.problem}"
    if t.held is None:
        return "Odin posts it once it finds the ticket in Jira"
    states = {r[0]: r[1] for r in con.execute("SELECT state, error FROM worklogs WHERE proposal_id = ? "
                                               "ORDER BY id", (row["id"],))}
    if "posted" in states:
        return "posted"
    if "sending" in states:
        return "posting"
    if "failed" in states:
        return "post failed: " + (states["failed"] or "see Odin")[:60]
    if "deleted" in states:
        return "deleted in Jira"
    due = con.execute("SELECT minutes_to_post FROM v_worklogs_to_post WHERE proposal_id = ?", (row["id"],)).fetchone()
    return f"Odin will post {E.fmt(due[0])}" if due else "nothing to post"


def review_text(con: sqlite3.Connection, t: Ticket, dry_run: bool) -> str:
    stale = " (run estimate)" if t.action in ("insert", "withdraw") else ""
    if t.open is not None:
        text = f"#{t.open['id']} to review"
        if int(t.open["minutes_proposed"]) != t.estimate:
            text += f" at {E.fmt(t.open['minutes_proposed'])}, now {E.fmt(t.estimate)}"
        return text + stale
    if t.approved is not None:
        text = f"#{t.approved['id']} approved {E.fmt(t.approved['minutes_final'])}, {_posting(con, t, t.approved)}"
        extra = (t.held or 0) - int(t.approved["minutes_final"])
        if extra > 0:
            text += f"; Jira holds {E.fmt(extra)} more, which Baldur leaves alone"
        if t.action == "insert":
            text += f"; estimate now {E.fmt(t.estimate)}{stale}"
        return text
    if t.rejected is not None:
        return f"#{t.rejected['id']} rejected" + (f"; estimate now {E.fmt(t.estimate)}{stale}" if t.action == "insert" else "")
    if t.estimate <= 0:
        return "under the rounding step"
    return "dry run, not stored" if dry_run else "not proposed yet" + stale


def render_day(con: sqlite3.Connection, est: E.Estimate, day: dt.date, settings: Settings, *,
               plan: Optional[store.Plan] = None, dry_run: bool = False,
               ai: Optional["assist.DaySuggestions"] = None) -> str:
    text = _render_day(con, est, day, settings, plan=plan, dry_run=dry_run)
    return text + "\n\n" + render_ai(ai, day) if ai is not None else text


def _render_day(con: sqlite3.Connection, est: E.Estimate, day: dt.date, settings: Settings, *,
                plan: Optional[store.Plan] = None, dry_run: bool = False) -> str:
    p = E.Params.from_settings(settings)
    d = est.days[day]
    parts = est.parts_on(day)
    props = est.proposals_for(day)
    keyed = [x for x in props if x.key]
    untracked = next((x for x in props if x.key is None), None)
    tickets = tickets_for(con, est, day, plan)
    lo, hi = store.day_bounds(day, day)

    policy = store.policy_text(d, p)
    out = [f"{day_name(day)}{'policy: ' + policy:>{WIDTH - len(day_name(day))}}"]
    if not parts and not tickets:
        out.append("  No commits of yours this day.")
        if d.has_calendar and d.meeting_minutes:
            out.append(line("Meetings (calendar)", E.fmt(d.meeting_minutes)))
        return "\n".join(out)

    if d.has_calendar:
        out.append(line("Meetings (calendar)", E.fmt(d.meeting_minutes), "kept out of the estimate"))
    else:
        out.append(line("Meetings (calendar)", "-", "no calendar data for this day"))
    dev = sum(x.minutes_proposed for x in keyed)
    out.append(line("Development (estimated from git)", E.fmt(dev),
                    ", ".join(f"{x.key} {E.fmt(x.minutes_proposed)}" for x in keyed if x.minutes_proposed)))

    held = [t for t in tickets if t.held]
    looked_up = [t for t in tickets if t.held is not None and not t.problem]
    if tickets and not looked_up and not any(t.problem for t in tickets):
        out.append(line("Already in Jira for these tickets", "-", "Odin hasn't looked these tickets up yet"))
    else:
        note = ", ".join(f"{t.key} {E.fmt(t.held)} {' and '.join(t.held_by)}".rstrip() for t in held)
        out.append(line("Already in Jira for these tickets", E.fmt(sum(t.held or 0 for t in tickets)), note))
    # Tickets Odin hasn't looked up yet count as holding nothing: that's what it will find, or it will say so.
    owed = [(t, due(con, t)) for t in tickets]
    owed = [(t, m) for t, m in owed if m > 0]
    note = ", ".join(f"{t.key} {E.fmt(m)}" for t, m in owed)
    blocked = [t for t in tickets if t.problem and t.target]
    if blocked:
        note = "; ".join(x for x in (note, ", ".join(f"{t.key} {t.problem}" for t in blocked)) if x)
    label = "Odin will post once approved" if any(t.approved is None or t.open is not None for t, _ in owed) \
        else "Odin will post"
    out.append(line(label, E.fmt(sum(m for _, m in owed)), note))

    if untracked:
        n = len(untracked.commits)
        out.append(line("Untracked commits (no Jira key)", E.fmt(untracked.minutes_proposed),
                        f"{store.plural(n, 'commit')}; add a key with: cli.py keys SHA PROJ-1"))
    else:
        out.append(line("Untracked commits (no Jira key)", "0m"))
    reviews = con.execute("SELECT r.name || '#' || p.number FROM pr_reviews v JOIN pull_requests p ON p.id = v.pr_id "
                          "JOIN repos r ON r.id = p.repo_id WHERE v.is_mine = 1 AND v.submitted_at >= ? "
                          "AND v.submitted_at < ? ORDER BY v.submitted_at", (lo, hi)).fetchall()
    if reviews or settings.github_api:
        out.append(line("Pull request reviews (not logged)", str(len(reviews)),
                        ", ".join(sorted({r[0] for r in reviews}))))
    # Not yours, never estimated: co-authored. (Commits of an email you removed that an old estimate
    # used stay as its evidence; they're the ones in session_commits.)
    shared = con.execute("SELECT DISTINCT c.sha, r.name FROM commits c JOIN repos r ON r.id = c.repo_id "
                         "WHERE c.is_mine = 0 AND c.is_merge = 0 AND r.active = 1 "
                         "AND c.authored_at >= ? AND c.authored_at < ? "
                         "AND c.id NOT IN (SELECT commit_id FROM session_commits) ORDER BY c.authored_at",
                         (lo, hi)).fetchall()
    if shared:
        out.append(line("Co-authored commits (not counted)", str(len(shared)),
                        ", ".join(f"{r[1]} {r[0][:8]}" for r in shared[:4]) + (", ..." if len(shared) > 4 else "")))

    out.append("  " + "-" * 48)
    accounted = (d.meeting_minutes if d.has_calendar else 0) + dev + (untracked.minutes_proposed if untracked else 0)
    out.append(line("Accounted", E.fmt(accounted), "" if d.has_calendar else "meetings not known"))
    start, end = settings.values["tour_of_duty"]["start"], settings.values["tour_of_duty"]["end"]
    unpaid = int(settings.values["tour_of_duty"].get("unpaid_minutes", 0))
    out.append(line("Tour of duty", E.fmt(p.tour_minutes), f"{start}-{end}" + (f", {unpaid}m unpaid" if unpaid else "")))
    if accounted > p.tour_minutes:
        out.append(line("Overlap assumed", E.fmt(accounted - p.tour_minutes), "coding during calls"))
    elif accounted < p.tour_minutes:
        out.append(line("Not accounted for", E.fmt(p.tour_minutes - accounted), "see below"))
    out.append("")
    out.append(INVISIBLE)

    if d.flags:
        out.append("")
        out.extend(f"  ! {flag}" for flag in d.flags)

    if parts:
        out.append("")
        out.append(f"  {'Sessions':<22}{'length':>8}{'meetings':>10}{'counted':>9}   commits")
        for x in parts:
            span = f"{store.hhmm(x.start)}-{store.hhmm(x.end)}" + (" co" if x.start_basis == "reflog" else "")
            out.append(f"    {span:<20}{E.fmt(x.minutes):>8}{E.fmt(x.ambient):>10}{E.fmt(x.counted):>9}   "
                       f"{_commit_mix(x)}")
        if any(x.start_basis == "reflog" for x in parts):
            out.append("    co: started when you checked out the branch")

    if tickets:
        out.append("")
        out.append(f"  {'Ticket':<14}{'Estimate':>9}{'In Jira':>9}   Review")
        for t in tickets:
            name = t.key + (f" (now {t.jira_key})" if t.jira_key else "")
            in_jira = "?" if t.held is None else E.fmt(t.held)
            out.append(f"  {name:<14}{E.fmt(t.estimate):>9}{in_jira:>9}   {review_text(con, t, dry_run)}")
        if any(t.open is not None for t in tickets):
            out.append("")
            out.append(f"  Approve: cli.py approve --date {day.isoformat()}    one ticket: cli.py approve ID [--minutes 1h15m]")
    return "\n".join(out)


def render_ai(ai: "assist.DaySuggestions", day: dt.date) -> str:
    """The day's AI-assisted figures beside the estimate: what changes, why, and what to check."""
    out = [f"{day_name(day)}  AI-assisted figures, from {ai.source}"]
    changed = ai.changed()
    if ai.tickets:
        out.append(f"  {'Ticket':<14}{'Estimate':>9}{'AI-assisted':>13}   Why")
        for t in ai.tickets.values():
            why = t.reason if t.changed else "no change"
            out.append(f"  {t.key:<14}{E.fmt(t.baseline):>9}{E.fmt(t.figure):>13}   {why[:60]}")
    for flag in ai.flags:
        out.append(f"  ! {flag}")
    if changed:
        total = sum(t.figure for t in ai.tickets.values()) - sum(t.baseline for t in ai.tickets.values())
        out.append(f"  Day: {'unchanged' if total == 0 else E.fmt(-total) + ' lower'}; an AI figure never raises a day.")
        out.append("  If you take them, Odin's worklog comments will say:")
        for t in changed:
            out.append(f"    {t.key}: {ai.posted_line(t.key)}")
        out.append(f"  Take them: cli.py approve --date {day.isoformat()} --ai {ai.digest()}    "
                   "or set your own: --set KEY=TIME")
    elif ai.tickets:
        out.append("  The estimate stands: nothing here changes a figure.")
    return "\n".join(out)


def _commit_mix(part: E.Part) -> str:
    counts: Dict[str, float] = {}
    for c in part.commits:
        for key in c.keys or ("untracked",):
            counts[key] = counts.get(key, 0) + 1
    text = ", ".join(f"{k} x{int(n)}" for k, n in sorted(counts.items(), key=lambda kv: (kv[0] == "untracked", kv[0])))
    return text + (" (from the next day)" if part.borrowed else "")


def render_days(con: sqlite3.Connection, est: E.Estimate, days: Sequence[dt.date],
                plan: Optional[store.Plan] = None) -> str:
    """One line per day: development, tickets and where review stands."""
    rows = store.current_rows(con, min(days), max(days)) if days else {}
    out = [f"  {'Day':<16}{'Dev':>7}   {'Tickets':<38}Review"]
    for day in days:
        props = [x for x in est.proposals_for(day) if x.minutes_proposed or x.key is None]
        keyed = [x for x in props if x.key]
        dev = sum(x.minutes_proposed for x in keyed)
        tickets = ", ".join(f"{x.key} {E.fmt(x.minutes_proposed)}" for x in keyed if x.minutes_proposed)
        untracked = next((x for x in props if x.key is None and x.minutes_proposed), None)
        if untracked:
            tickets = ", ".join(t for t in (tickets, f"untracked {E.fmt(untracked.minutes_proposed)}") if t)
        mine = {k: v for (d, k), v in rows.items() if d == day.isoformat() and k}
        to_review = sum(1 for v in mine.values() if any(r["status"] == "proposed" for r in v))
        approved = sum(1 for v in mine.values() if any(r["status"] == "approved" for r in v))
        status = []
        if to_review:
            status.append(f"{to_review} to review")
        if approved:
            status.append(f"{approved} approved")
        if plan and plan.would_change(day):
            status.append("run estimate")
        if not est.parts_on(day) and not mine:
            out.append(f"  {day_name(day):<16}{'-':>7}   no commits")
            continue
        if len(tickets) > 37:
            tickets = tickets[:34] + "..."
        out.append(f"  {day_name(day):<16}{E.fmt(dev):>7}   {tickets:<38}{', '.join(status)}")
    return "\n".join(out)
