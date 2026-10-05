"""Estimating days from what's in Muninn, and saving the proposals you review.

    result = store.run(con, settings, date_from, date_to)

reads your commits, branch checkouts and busy meetings, runs the estimator,
and writes in the same transaction, so two estimates can't interleave:

- A proposal with nothing new to say isn't stored: its basis_hash matches an
  open, approved or rejected row for that day and ticket, or its number is
  the one you last decided on, or the one Jira will hold after your approval.
- A new proposal supersedes the open one for its day and ticket. Open rows
  for tickets with no time left that day are superseded without a successor.
- Approved and rejected rows are never touched. Changing an approval means
  approving a newer proposal (muninn.baldur.approve supersedes the old one).
- The estimate_runs row, with the sessions of each day it proposed for, is
  stored only when the run proposes something; it is the audit trail of
  those proposals.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from asgard import muninn

from . import MODEL_VERSION, gitread
from . import estimate as E
from .collect import CollectError, check_ready
from .settings import Settings

_CHECKOUT_RE = re.compile(r"^checkout: moving from .+ to (\S+)\s*$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
# Your work: not a squash copy in any clone. A clone collected before a copy was recognised (or
# one that can't see the commits a local squash lists) may still hold the copy as work.
_MINE = ("FROM commits c JOIN repos r ON r.id = c.repo_id "
         "WHERE c.is_mine = 1 AND c.is_merge = 0 AND r.active = 1 "
         "AND c.sha NOT IN (SELECT x.sha FROM commits x WHERE x.is_mine = 1 AND x.is_merge = 1)")
# Your squash copies (is_merge = 1): never work, but they link sessions (estimate.share_limits).
_COPIES = ("FROM commits c JOIN repos r ON r.id = c.repo_id "
           "WHERE c.is_mine = 1 AND c.is_merge = 1 AND r.active = 1")
# Both: what decides how far a range must reach to hold every session whole.
_PRESENT = ("FROM commits c JOIN repos r ON r.id = c.repo_id "
            "WHERE c.is_mine = 1 AND r.active = 1")


# --------------------------------------------------------------------------
# Reading the evidence
# --------------------------------------------------------------------------

@dataclass
class Inputs:
    commits: List[E.Commit]
    checkouts: List[E.Checkout]
    meetings: List[E.Meeting]
    calendar: Optional[Tuple[dt.date, dt.date]]
    calendar_synced: Optional[dt.datetime]
    reflog_since: Optional[dt.datetime]
    prior_patches: Set[Tuple[str, str]]
    copies: List[dt.datetime] = field(default_factory=list)


# How sure Baldur is of a commit's keys, best first; a commit stored by two clones uses its best copy.
_RANK = {"manual": 0, "reflog": 1, "branch": 2, "pr": 3, "message": 4}


def day_bounds(first: dt.date, last: dt.date) -> Tuple[str, str]:
    """Local midnight before first and after last, as Muninn UTC text."""
    return (muninn.to_ts(E.local_midnight(first)),
            muninn.to_ts(E.local_midnight(last + dt.timedelta(days=1))))


def days_between(first: dt.date, last: dt.date) -> List[dt.date]:
    return [first + dt.timedelta(days=n) for n in range((last - first).days + 1)]


def _shift(ts: str, minutes: float) -> str:
    return muninn.to_ts(muninn.from_ts(ts) + dt.timedelta(minutes=minutes))


def reach(p: E.Params) -> int:
    """How far a session can reach past its own commits: a gap, a lead-in or the minimum length."""
    return max(p.idle_gap_minutes, p.lead_in_minutes, p.min_session_minutes)


def commit_window(con: sqlite3.Connection, lo: str, hi: str, minutes: int) -> Tuple[str, str]:
    """Widen [lo, hi) until each end meets a quiet stretch longer than `minutes`.

    A session is a chain of commits no more than the idle gap apart, and its
    start can reach back a lead-in, so stopping at a fixed margin could cut a
    session in two, or miss one whose lead-in crosses into the range. Squash
    copies count as commits here, because sessions they link share a limit.
    """
    while True:
        row = con.execute(f"SELECT min(c.authored_at) {_PRESENT} AND c.authored_at < ? AND c.authored_at >= ?",
                          (lo, _shift(lo, -minutes))).fetchone()
        if row[0] is None:
            break
        lo = row[0]
    while True:
        row = con.execute(f"SELECT max(c.authored_at) {_PRESENT} AND c.authored_at >= ? AND c.authored_at <= ?",
                          (hi, _shift(hi, minutes))).fetchone()
        if row[0] is None:
            break
        hi = _shift(row[0], 1 / 60)   # one second past it; the bound is exclusive
    return lo, hi


def checkout_branch(message: Optional[str]) -> Optional[str]:
    """The branch a reflog 'checkout: moving from A to B' entry switched to; None for a detached HEAD."""
    m = _CHECKOUT_RE.match(message or "")
    if not m or m.group(1) == "HEAD" or _SHA_RE.match(m.group(1)):
        return None
    return m.group(1)


def calendar_coverage(con: sqlite3.Connection) -> Tuple[Optional[Tuple[dt.date, dt.date]], Optional[dt.datetime]]:
    """(the local days Odin's calendar sync covers, when it last synced): its first event through its last sync."""
    first = con.execute("SELECT min(starts_at) FROM calendar_events").fetchone()[0]
    if first is None:
        return None, None
    last = con.execute("SELECT max(coalesce(finished_at, started_at)) FROM sync_runs "
                       "WHERE stream = 'calendar' AND status IN ('ok', 'partial')").fetchone()[0]
    last = last or con.execute("SELECT max(last_seen_at) FROM calendar_events").fetchone()[0]
    synced = muninn.from_ts(last)
    return (E.local_day(muninn.from_ts(first)), E.local_day(synced)), synced


def current_keys(con: sqlite3.Connection) -> Dict[str, str]:
    """Every key Odin has resolved -> the key Jira uses for that issue now."""
    return {r[0]: r[1] for r in con.execute(
        "SELECT a.key, w.key FROM work_item_aliases a JOIN work_items w ON w.id = a.work_item_id "
        "WHERE a.status IN ('current', 'moved')")}


def origin_of(remote_url: Optional[str], repo_id: int) -> str:
    return gitread.remote_identity(remote_url) or f"repo:{repo_id}"


def load(con: sqlite3.Connection, first: dt.date, last: dt.date, p: E.Params,
         projects: Sequence[str] = ()) -> Inputs:
    """Everything the estimator needs for these local days.

    Keys go through Odin's aliases, so a moved issue's old and new keys are
    one ticket. Keys found automatically count only for projects in
    project_keys; keys you set by hand always count.
    """
    day_lo, day_hi = day_bounds(first, last)
    lo, hi = commit_window(con, day_lo, day_hi, reach(p))
    rows = con.execute(f"SELECT c.id, c.sha, c.authored_at, c.subject, c.patch_id, c.branch_hint, r.id AS repo_id, "
                       f"r.name AS repo, r.remote_url {_MINE} AND c.authored_at >= ? AND c.authored_at < ? "
                       f"ORDER BY c.authored_at, c.sha, c.id", (lo, hi)).fetchall()
    found: Dict[int, List[Tuple[str, str]]] = {}
    ids = json.dumps([r["id"] for r in rows])
    for cid, key, method in con.execute("SELECT commit_id, work_item_key, method FROM commit_work_items "
                                        "WHERE commit_id IN (SELECT value FROM json_each(?))", (ids,)):
        found.setdefault(cid, []).append((key, method))
    allowed = {k.upper() for k in projects}
    renamed = current_keys(con)

    def evidence(cid: int) -> Tuple[int, Tuple[str, ...]]:
        pairs = [(k, m) for k, m in found.get(cid, []) if m == "manual" or not allowed or k.split("-")[0] in allowed]
        if any(m == "manual" for _, m in pairs):
            pairs = [(k, m) for k, m in pairs if m == "manual"]
        rank = min((_RANK.get(m, 9) for _, m in pairs), default=9)
        return rank, tuple(sorted({renamed.get(k, k) for k, _ in pairs}))

    best: Dict[str, Tuple[int, int, sqlite3.Row, Tuple[str, ...]]] = {}
    for r in rows:                      # one copy per SHA: the one whose keys Baldur is surest of
        rank, ks = evidence(r["id"])
        if r["sha"] not in best or (rank, r["id"]) < best[r["sha"]][:2]:
            best[r["sha"]] = (rank, r["id"], r, ks)
    commits = [E.Commit(r["id"], r["sha"], muninn.from_ts(r["authored_at"]), ks, str(r["repo_id"]), r["subject"],
                        r["patch_id"], r["branch_hint"], r["repo"], origin_of(r["remote_url"], r["repo_id"]))
               for _, _, r, ks in sorted(best.values(), key=lambda b: (b[2]["authored_at"], b[2]["sha"]))]

    patches = json.dumps(sorted({c.patch_id for c in commits if c.patch_id}))
    prior = {(origin_of(r["remote_url"], r["repo_id"]), r["patch_id"]) for r in con.execute(
        f"SELECT DISTINCT c.patch_id, r.remote_url, r.id AS repo_id {_MINE} AND c.authored_at < ? "
        f"AND c.patch_id IN (SELECT value FROM json_each(?))", (lo, patches))}

    marks = con.execute("SELECT e.at, e.message, e.repo_id FROM reflog_entries e JOIN repos r ON r.id = e.repo_id "
                        "WHERE r.active = 1 AND e.action = 'checkout' AND e.at >= ? AND e.at < ?",
                        (_shift(lo, -reach(p)), hi)).fetchall()
    checkouts = [E.Checkout(muninn.from_ts(m["at"]), str(m["repo_id"]), checkout_branch(m["message"])) for m in marks]
    meetings = [E.Meeting(str(m["id"]), muninn.from_ts(m["starts_at"]), muninn.from_ts(m["ends_at"]), m["title"])
                for m in con.execute("SELECT id, title, starts_at, ends_at FROM v_busy_meetings "
                                     "WHERE starts_at < ? AND ends_at > ? ORDER BY starts_at", (day_hi, day_lo))]
    oldest = con.execute("SELECT min(at) FROM reflog_entries").fetchone()[0]
    calendar, synced = calendar_coverage(con)
    copies = [muninn.from_ts(r[0]) for r in con.execute(
        f"SELECT DISTINCT c.authored_at {_COPIES} AND c.authored_at >= ? AND c.authored_at < ? "
        f"ORDER BY c.authored_at", (lo, hi))]
    return Inputs(commits, checkouts, meetings, calendar, synced, muninn.from_ts(oldest) if oldest else None, prior,
                  copies)


def compute(con: sqlite3.Connection, settings: Settings, first: dt.date, last: dt.date,
            now: Optional[dt.datetime] = None) -> E.Estimate:
    """The estimate for these days from Muninn as it is now, without writing anything."""
    p = E.Params.from_settings(settings)
    inputs = load(con, first, last, p, settings.project_keys)
    return E.estimate(inputs.commits, inputs.checkouts, inputs.meetings, days_between(first, last), p,
                      calendar=inputs.calendar, calendar_synced=inputs.calendar_synced,
                      reflog_since=inputs.reflog_since, prior_patches=inputs.prior_patches, now=now,
                      copies=inputs.copies)


# --------------------------------------------------------------------------
# Basis text and hash
# --------------------------------------------------------------------------

def hhmm(at: dt.datetime) -> str:
    return at.astimezone().strftime("%H:%M")


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def policy_text(day: E.Day, p: E.Params) -> str:
    if day.policy == "ambient":
        return f"ambient {p.ambient_weight:g}"
    if day.policy == "overlap":
        return f"overlap {p.concurrent_fraction:g}"
    return "independent" if day.has_calendar or p.policy == "independent" else "independent (no calendar data)"


def meetings_in(parts: Sequence[E.Part], meetings: Sequence[E.Meeting]) -> List[str]:
    """Ids of the busy meetings that overlap these parts."""
    return sorted({m.id for m in meetings for x in parts if m.start < x.end and m.end > x.start},
                  key=lambda i: (len(i), i))


def basis_hash(prop: E.Proposal, day: E.Day, meetings: Sequence[E.Meeting], params_hash: str) -> str:
    """What this number came from: the model, the settings, and the sessions behind it.

    Each session contributes its span (which carries checkout evidence), the
    commits that split it (SHAs with their keys), and its meeting overlap; the
    day's scale carries the day cap. Evidence about other tickets that can't
    change this number doesn't change the hash.
    """
    parts = [{"start": muninn.to_ts(x.start), "end": muninn.to_ts(x.end), "basis": x.start_basis,
              "policy": x.policy, "ambient": round(x.ambient, 3),
              "commits": sorted([c.sha, *sorted(c.keys)] for c in x.commits)} for x in prop.parts]
    for entry, x in zip(parts, prop.parts):
        if x.link_scale < 1:            # only then, so hashes made before squash links existed still match
            entry["link_scale"] = round(x.link_scale, 6)
    payload = {"model": MODEL_VERSION, "params": params_hash, "date": prop.local_date.isoformat(), "key": prop.key,
               "scale": round(day.scale, 6), "parts": parts, "meetings": meetings_in(prop.parts, meetings)}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def basis_text(prop: E.Proposal, day: E.Day, p: E.Params, run_id: int) -> str:
    """The derivation, stored with the proposal; Odin posts it as the worklog comment."""
    who = prop.key or "untracked work"
    spans = []
    for x in sorted(prop.parts, key=lambda x: x.start):
        spans.append(f"{hhmm(x.start)}-{hhmm(x.end)}" + (" from checkout" if x.start_basis == "reflog" else ""))
    repos = sorted({c.label for c in prop.commits if c.label})
    where = f", {'repo' if len(repos) == 1 else 'repos'} {', '.join(repos)}" if repos else ""
    lines = [
        f"Baldur estimate: {E.fmt(prop.minutes_proposed)} for {who} on {prop.local_date.isoformat()}",
        f"Basis: {plural(len(prop.commits), 'commit')} in {plural(len(prop.parts), 'session')} "
        f"({', '.join(spans)}){where}",
        f"Model: {MODEL_VERSION}, policy {policy_text(day, p)}, gap {p.idle_gap_minutes}m, "
        f"lead-in {p.lead_in_minutes}m, rounded down to {p.round_to_minutes}m (run {run_id})",
        f"Meetings that day: {E.fmt(day.meeting_minutes)}" if day.has_calendar
        else "Meetings that day: not known (no calendar data)",
    ]
    linked = min((x.link_scale for x in prop.parts), default=1.0)
    if linked < 1:
        lines.append(f"Sessions a squash merge links scaled to {linked:.0%} to share one "
                     f"{E.fmt(p.max_session_minutes)} session limit")
    if day.scale < 1:
        lines.append(f"Day scaled down to its cap of {E.fmt(day.cap)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Deciding and writing
# --------------------------------------------------------------------------

@dataclass
class Change:
    """What a run did with one ticket's day."""
    local_date: dt.date
    key: Optional[str]
    minutes: int
    action: str                    # new, updated, unchanged, withdrawn
    proposal_id: Optional[int] = None
    replaced: List[int] = field(default_factory=list)


@dataclass
class RunResult:
    estimate: E.Estimate
    params: E.Params
    first: dt.date
    last: dt.date
    run_id: Optional[int] = None
    changes: List[Change] = field(default_factory=list)
    dry_run: bool = False


def decide(prop: E.Proposal, digest: str, rows: Sequence[sqlite3.Row]) -> Tuple[str, List[int]]:
    """('insert', 'keep' or 'skip', ids of open rows to supersede) for one day and ticket.

    rows are the day and ticket's open, approved and rejected proposals.
    """
    open_rows = [r for r in rows if r["status"] == "proposed"]
    if any(r["basis_hash"] == digest for r in open_rows):
        return "keep", []
    stale = [int(r["id"]) for r in open_rows]
    if prop.key is None:
        return ("insert" if prop.minutes_raw > 0 else "skip"), stale
    if prop.minutes_proposed <= 0:
        return "skip", stale            # rounds to nothing: no row to review
    decided = sorted((r for r in rows if r["status"] in ("approved", "rejected")),
                     key=lambda r: (r["decided_at"] or "", r["id"]))
    if any(r["basis_hash"] == digest for r in decided):
        return "skip", stale            # the same evidence you already decided on
    if decided and decided[-1]["minutes_proposed"] == prop.minutes_proposed:
        return "skip", stale            # the same number you last approved or rejected
    approved = [r for r in decided if r["status"] == "approved"]
    if approved and approved[-1]["minutes_final"] == prop.minutes_proposed:
        return "skip", stale            # Jira will hold this already
    return "insert", stale


def _rows_for(con: sqlite3.Connection, day: dt.date, key: Optional[str]) -> List[sqlite3.Row]:
    return con.execute("SELECT * FROM day_proposals WHERE local_date = ? AND work_item_key IS ? "
                       "AND status IN ('proposed', 'approved', 'rejected') ORDER BY id",
                       (day.isoformat(), key)).fetchall()


def _active_calibration(con: sqlite3.Connection) -> Optional[int]:
    row = con.execute("SELECT id FROM calibration_runs WHERE is_active = 1").fetchone()
    return int(row[0]) if row else None


def run(con: sqlite3.Connection, settings: Settings, first: dt.date, last: dt.date, *,
        dry_run: bool = False, now: Optional[dt.datetime] = None) -> RunResult:
    """Estimate first..last (local days) and store what's new. dry_run computes without writing."""
    if last < first:
        raise ValueError("The range ends before it starts.")
    if settings.broken:
        raise CollectError("baldur.json has a mistake in it, so Baldur won't estimate until it's fixed: "
                           + "; ".join(settings.warnings))
    check_ready(con, settings)
    p = E.Params.from_settings(settings)
    if dry_run:
        est = compute(con, settings, first, last, now)
        result = RunResult(est, p, first, last, dry_run=True)
        for prop in est.proposals:
            result.changes.append(Change(prop.local_date, prop.key, prop.minutes_proposed, "computed"))
        return result
    with muninn.transaction(con):
        est = compute(con, settings, first, last, now)
        result = RunResult(est, p, first, last)
        _save(con, settings, est, result)
    return result


@dataclass
class Plan:
    """What storing an estimate would do: per proposal, and for open rows left without one."""
    items: List[Tuple[E.Proposal, str, str, List[int]]]   # (proposal, basis_hash, action, open ids to supersede)
    leftovers: List[sqlite3.Row]

    def would_change(self, day: Optional[dt.date] = None) -> bool:
        items = [x for x in self.items if day is None or x[0].local_date == day]
        rows = [r for r in self.leftovers if day is None or r["local_date"] == day.isoformat()]
        return bool(rows) or any(action == "insert" or stale for _, _, action, stale in items)

    def action_for(self, day: dt.date, key: Optional[str]) -> Optional[str]:
        for prop, _, action, stale in self.items:
            if prop.local_date == day and prop.key == key:
                return "insert" if action == "insert" else ("withdraw" if stale else action)
        return None


def plan(con: sqlite3.Connection, settings: Settings, est: E.Estimate, first: dt.date, last: dt.date) -> Plan:
    params_hash = settings.params_hash()
    items = []
    for prop in est.proposals:
        digest = basis_hash(prop, est.days[prop.local_date], est.meetings, params_hash)
        action, stale = decide(prop, digest, _rows_for(con, prop.local_date, prop.key))
        items.append((prop, digest, action, stale))
    wanted = {(prop.local_date.isoformat(), prop.key) for prop in est.proposals}
    leftovers = [r for r in con.execute("SELECT id, local_date, work_item_key, minutes_proposed FROM day_proposals "
                                        "WHERE status = 'proposed' AND local_date BETWEEN ? AND ? ORDER BY id",
                                        (first.isoformat(), last.isoformat()))
                 if (r["local_date"], r["work_item_key"]) not in wanted]
    return Plan(items, leftovers)


def _save(con: sqlite3.Connection, settings: Settings, est: E.Estimate, result: RunResult) -> None:
    todo = plan(con, settings, est, result.first, result.last)
    first_iso, last_iso = result.first.isoformat(), result.last.isoformat()
    inserts = [x for x in todo.items if x[2] == "insert"]
    if inserts:
        result.run_id = int(con.execute(
            "INSERT INTO estimate_runs (date_from, date_to, model_version, params, params_hash, calibration_id) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (first_iso, last_iso, MODEL_VERSION, json.dumps(settings.estimate_params(), sort_keys=True),
             settings.params_hash(), _active_calibration(con))).fetchone()[0])
        for day in sorted({x[0].local_date for x in inserts}):
            for part in est.parts_on(day):
                _store_part(con, result.run_id, part)

    for prop, digest, action, stale in todo.items:
        for pid in stale:
            con.execute("UPDATE day_proposals SET status = 'superseded' WHERE id = ? AND status = 'proposed'", (pid,))
        change = Change(prop.local_date, prop.key, prop.minutes_proposed, "unchanged", replaced=stale)
        if action == "insert":
            day = est.days[prop.local_date]
            change.proposal_id = int(con.execute(
                "INSERT INTO day_proposals (estimate_run_id, local_date, work_item_key, minutes_raw, minutes_proposed, "
                "first_started_at, basis, basis_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (result.run_id, prop.local_date.isoformat(), prop.key, prop.minutes_raw, prop.minutes_proposed,
                 muninn.to_ts(prop.first_started_at), basis_text(prop, day, result.params, result.run_id),
                 digest)).fetchone()[0])
            change.action = "updated" if stale else "new"
        elif action == "keep":
            row = con.execute("SELECT id FROM day_proposals WHERE local_date = ? AND work_item_key IS ? "
                              "AND status = 'proposed'", (prop.local_date.isoformat(), prop.key)).fetchone()
            change.proposal_id = int(row[0]) if row else None
        elif stale:
            change.action = "withdrawn"
        result.changes.append(change)

    for r in todo.leftovers:
        con.execute("UPDATE day_proposals SET status = 'superseded' WHERE id = ?", (r["id"],))
        result.changes.append(Change(dt.date.fromisoformat(r["local_date"]), r["work_item_key"], 0, "withdrawn",
                                     replaced=[int(r["id"])]))

    if result.run_id is not None:
        muninn.emit(con, "baldur", "estimate_run.created", "estimate_runs", result.run_id, None,
                    {"date_from": first_iso, "date_to": last_iso,
                     "proposals": sum(1 for c in result.changes if c.action in ("new", "updated") and c.key),
                     "withdrawn": sum(1 for c in result.changes if c.action == "withdrawn")})


def _store_part(con: sqlite3.Connection, run_id: int, part: E.Part) -> None:
    sid = int(con.execute(
        "INSERT INTO work_sessions (estimate_run_id, local_date, started_at, ended_at, start_basis, policy, "
        "focused_minutes, ambient_minutes, counted_minutes, commit_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "RETURNING id",
        (run_id, part.local_date.isoformat(), muninn.to_ts(part.start), muninn.to_ts(part.end), part.start_basis,
         part.policy, part.focused, part.ambient, min(part.counted, part.focused + part.ambient),
         len(part.commits))).fetchone()[0])
    for c in part.commits:
        con.execute("INSERT INTO session_commits (session_id, commit_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                    (sid, c.id))
    for key, minutes in part.shares.items():
        con.execute("INSERT INTO session_allocations (session_id, work_item_key, minutes) VALUES (?, ?, ?)",
                    (sid, key, max(0.0, minutes)))


# --------------------------------------------------------------------------
# Reading proposals back
# --------------------------------------------------------------------------

def current_rows(con: sqlite3.Connection, first: dt.date, last: dt.date) -> Dict[Tuple[str, Optional[str]], List[sqlite3.Row]]:
    """Open, approved and rejected proposals per (day, key), oldest first."""
    out: Dict[Tuple[str, Optional[str]], List[sqlite3.Row]] = {}
    for r in con.execute("SELECT * FROM day_proposals WHERE local_date BETWEEN ? AND ? "
                         "AND status IN ('proposed', 'approved', 'rejected') ORDER BY id",
                         (first.isoformat(), last.isoformat())):
        out.setdefault((r["local_date"], r["work_item_key"]), []).append(r)
    return out
