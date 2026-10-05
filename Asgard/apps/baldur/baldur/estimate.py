"""The estimator: commits, checkouts and meetings in; per-ticket minutes per day out.

Pure functions on plain data, so every rule can be tested without git or a
database. Every step can only keep a number or lower it: caps, the day cap,
the meeting weight and the final rounding all go down.

    1. Drop duplicates (a cherry-pick, a rebase) and future-dated commits.
    2. Build sessions on one timeline across all repositories.
    3. Start each session at the checkout onto its first commit's branch, or
       a lead-in before that commit; end it at its last commit; clamp it.
       Sessions a squash copy links share one clamp.
    4. Split sessions at local midnight.
    5. Weigh minutes that overlap meetings by the day's policy.
    6. Scale the day down to its cap.
    7. Split each session's minutes across tickets by commit count.
    8. Sum per ticket per day and round down.
"""
from __future__ import annotations

import bisect
import datetime as dt
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

UTC = dt.timezone.utc
FUTURE_SLACK = dt.timedelta(minutes=5)
_EPS = 1e-9


@dataclass(frozen=True)
class Commit:
    id: int
    sha: str
    at: dt.datetime            # author time, aware
    keys: Tuple[str, ...] = ()  # empty: untracked
    repo: str = ""             # which repository (the store passes its Muninn id); checkouts match on it
    subject: str = ""
    patch_id: Optional[str] = None
    branch: Optional[str] = None   # the branch it was made on (commits.branch_hint)
    repo_name: str = ""        # for people; defaults to repo
    origin: str = ""           # the project, across clones: duplicates are only looked for within one

    @property
    def label(self) -> str:
        return self.repo_name or self.repo


@dataclass(frozen=True)
class Checkout:
    """A reflog entry 'checkout: moving from A to B': you switched to branch B."""
    at: dt.datetime
    repo: str = ""
    branch: Optional[str] = None


@dataclass(frozen=True)
class Meeting:
    id: str
    start: dt.datetime
    end: dt.datetime
    title: str = ""


@dataclass(frozen=True)
class Params:
    idle_gap_minutes: int = 120
    lead_in_minutes: int = 30
    min_session_minutes: int = 15
    max_session_minutes: int = 240
    max_daily_dev_minutes: int = 480
    policy: str = "ambient"
    ambient_weight: float = 0.5
    concurrent_fraction: float = 0.5
    round_to_minutes: int = 15
    tour_minutes: int = 480

    @classmethod
    def from_settings(cls, settings) -> "Params":
        v = settings.values
        return cls(v["idle_gap_minutes"], v["lead_in_minutes"], v["min_session_minutes"], v["max_session_minutes"],
                   v["max_daily_dev_minutes"], v["policy"], float(v["ambient_weight"]),
                   float(v["concurrent_fraction"]), v["round_to_minutes"], settings.tour_minutes())


@dataclass
class Session:
    start: dt.datetime
    end: dt.datetime
    commits: List[Commit]
    start_basis: str           # 'reflog' or 'lead_in'
    link_scale: float = 1.0    # below 1 when a squash copy links it to a neighbour (share_limits)


@dataclass
class Part:
    """A session's share of one local day: the unit stored as a work_sessions row."""
    session_no: int
    local_date: dt.date
    start: dt.datetime
    end: dt.datetime
    commits: List[Commit]      # commits that set this part's split across tickets
    start_basis: str
    borrowed: bool = False     # no commits of its own (a lead-in before midnight): uses the session's
    link_scale: float = 1.0    # its session's share of a limit it shares with linked sessions
    policy: str = "ambient"
    focused: float = 0.0       # minutes outside meetings
    ambient: float = 0.0       # minutes overlapping meetings
    counted: float = 0.0       # after the meeting weight and the day cap
    shares: Dict[Optional[str], float] = field(default_factory=dict)

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


@dataclass
class Day:
    local_date: dt.date
    policy: str
    has_calendar: bool
    meeting_minutes: float
    cap: float
    weighted: float = 0.0      # after the meeting weight, before the cap
    counted: float = 0.0
    scale: float = 1.0
    flags: List[str] = field(default_factory=list)


@dataclass
class Proposal:
    local_date: dt.date
    key: Optional[str]
    minutes_raw: float
    minutes_proposed: int
    first_started_at: dt.datetime
    parts: List[Part]
    commits: List[Commit]


@dataclass
class Estimate:
    days: "OrderedDict[dt.date, Day]"
    parts: List[Part]
    proposals: List[Proposal]
    meetings: List[Meeting]
    excluded: List[Tuple[Commit, str]]

    def proposals_for(self, day: dt.date) -> List[Proposal]:
        return [p for p in self.proposals if p.local_date == day]

    def parts_on(self, day: dt.date) -> List[Part]:
        return [p for p in self.parts if p.local_date == day]


# --------------------------------------------------------------------------
# Time helpers (local time is this computer's)
# --------------------------------------------------------------------------

def local_midnight(day: dt.date) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day).astimezone()


def local_day(at: dt.datetime) -> dt.date:
    return at.astimezone().date()


def minutes_between(a: dt.datetime, b: dt.datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 60)


def merge(intervals: Iterable[Tuple[dt.datetime, dt.datetime]]) -> List[Tuple[dt.datetime, dt.datetime]]:
    """Union of intervals, so two overlapping meetings never count twice."""
    out: List[Tuple[dt.datetime, dt.datetime]] = []
    for start, end in sorted(i for i in intervals if i[1] > i[0]):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def overlap_minutes(start: dt.datetime, end: dt.datetime, merged: Sequence[Tuple[dt.datetime, dt.datetime]]) -> float:
    total = 0.0
    for a, b in merged:
        if b <= start:
            continue
        if a >= end:
            break
        total += minutes_between(max(a, start), min(b, end))
    return total


def round_down(minutes: float, step: int) -> int:
    return int(math.floor(minutes / step + _EPS)) * step if minutes > 0 else 0


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

def clean(commits: Sequence[Commit], now: dt.datetime, prior: Iterable[Tuple[str, str]] = ()
          ) -> Tuple[List[Commit], List[Tuple[Commit, str]]]:
    """Each change once, and nothing from the future.

    A SHA counts once anywhere. Within one project (origin), so does a
    patch-id (a cherry-pick or rebase) and a time-and-subject pair (an amend
    or a rebase that changed the diff); the same edit made in two different
    repositories is two pieces of work. prior holds (origin, patch_id) pairs
    of earlier commits outside the range being estimated, so the answer
    doesn't depend on which days you asked for.
    """
    kept: List[Commit] = []
    dropped: List[Tuple[Commit, str]] = []
    seen_sha: Set[str] = set()
    seen_patch: Set[Tuple[str, str]] = set(prior)
    seen_text: Set[Tuple[str, dt.datetime, str]] = set()
    for c in sorted(commits, key=lambda c: (c.at, c.sha)):
        patch = (c.origin, c.patch_id) if c.patch_id else None
        text = (c.origin, c.at, c.subject)
        if c.at > now + FUTURE_SLACK:
            dropped.append((c, "dated in the future; check this computer's clock or the commit"))
        elif c.sha in seen_sha:
            dropped.append((c, "the same commit in another clone"))
        elif patch and patch in seen_patch:
            dropped.append((c, "the same change as an earlier commit (cherry-pick or rebase)"))
        elif c.subject and text in seen_text:
            dropped.append((c, "the same time and message as an earlier commit (amend or rebase)"))
        else:
            kept.append(c)
        seen_sha.add(c.sha)
        if patch:
            seen_patch.add(patch)
        seen_text.add(text)
    return kept, dropped


Onto = Dict[Tuple[str, str], List[dt.datetime]]


def _place(group: Sequence[Commit], onto: Onto, prev_end: Optional[dt.datetime], p: Params
           ) -> Tuple[dt.datetime, str]:
    """The start of a session of these commits, and what set it (see build_sessions)."""
    gap = dt.timedelta(minutes=p.idle_gap_minutes)
    head = group[0]
    first, last = head.at, group[-1].at
    floor = first - gap if prev_end is None else max(first - gap, prev_end)
    marks = [m for m in onto.get((head.repo, head.branch or ""), ()) if floor <= m <= first]
    if marks:
        start, basis = max(marks), "reflog"
    else:
        start, basis = first - dt.timedelta(minutes=p.lead_in_minutes), "lead_in"
        if prev_end is not None and start < prev_end:
            start = prev_end
    length = minutes_between(start, last)
    if length < p.min_session_minutes:
        start = last - dt.timedelta(minutes=p.min_session_minutes)
        if prev_end is not None and start < prev_end:
            start = prev_end
    elif length > p.max_session_minutes:
        start = last - dt.timedelta(minutes=p.max_session_minutes)
    if basis == "reflog" and start != max(marks):
        basis = "lead_in"           # a clamp moved it, so the checkout no longer sets the start
    return start, basis


def build_sessions(commits: Sequence[Commit], checkouts: Sequence[Checkout], p: Params,
                   copies: Sequence[dt.datetime] = ()) -> List[Session]:
    """Sessions on one timeline: a gap longer than idle_gap_minutes starts a new one.

    A session starts when you checked out the branch its first commit was
    made on, if that was no more than the idle gap earlier; otherwise
    lead_in_minutes before the first commit. When you checked the branch out
    more than once, the latest checkout counts, which keeps the session
    short. Reflog alone never makes a session. The end is the last commit.
    A session is then clamped: too short grows back from its end, too long
    keeps the stretch closest to its commits' end.

    copies are the times of squash copies of work already counted (see
    share_limits): never work, but sessions they link share one limit.
    """
    gap = dt.timedelta(minutes=p.idle_gap_minutes)
    groups: List[List[Commit]] = []
    for c in sorted(commits, key=lambda c: (c.at, c.sha)):
        if groups and c.at - groups[-1][-1].at <= gap:
            groups[-1].append(c)
        else:
            groups.append([c])
    onto: Onto = {}
    for k in checkouts:
        if k.branch:
            onto.setdefault((k.repo, k.branch), []).append(k.at)
    sessions: List[Session] = []
    prev_end: Optional[dt.datetime] = None
    for group in groups:
        start, basis = _place(group, onto, prev_end, p)
        sessions.append(Session(start, group[-1].at, group, basis))
        prev_end = group[-1].at
    share_limits(sessions, copies, onto, p)
    return sessions


def share_limits(sessions: List[Session], copies: Sequence[dt.datetime], onto: Onto, p: Params) -> None:
    """Sessions a copy links can't add up to more than one session would have.

    A squash merge (on GitHub, or a local git merge --squash) copies commits
    that are already counted, so it is never work and never adds a minute.
    It may not even be your time (someone else can press merge), so it is
    only ever used to lower a number: when copies bridge two sessions, every
    step no longer than the idle gap, the sessions are one stretch, and
    together they keep no more minutes than the single session they'd have
    formed with the copy counted (at most max_session_minutes). Without this,
    skipping a copy between two stretches of work splits one clamped session
    into two and raises the day.

    Every session in the stretch is scaled by the same factor, as the day cap
    scales a day: the spans stay what the evidence says, each session's split
    across tickets stays the one its commits give, and no ticket in the
    stretch can rise.
    """
    if len(sessions) < 2 or not copies:
        return
    gap = dt.timedelta(minutes=p.idle_gap_minutes)
    marks = sorted(copies)
    stretches: List[List[int]] = [[0]]
    for n in range(1, len(sessions)):
        a, b = sessions[n - 1].end, sessions[n].commits[0].at
        between = marks[bisect.bisect_left(marks, a):bisect.bisect_right(marks, b)]
        chain = [a, *between, b]
        if between and all(y - x <= gap for x, y in zip(chain, chain[1:])):
            stretches[-1].append(n)
        else:
            stretches.append([n])
    for stretch in stretches:
        if len(stretch) < 2:
            continue
        members = [sessions[n] for n in stretch]
        prev_end = sessions[stretch[0] - 1].end if stretch[0] else None
        as_one, _ = _place([c for s in members for c in s.commits], onto, prev_end, p)
        budget = (members[-1].end - as_one).total_seconds()
        total = sum((s.end - s.start).total_seconds() for s in members)
        if total > budget:
            for s in members:
                s.link_scale = budget / total


def split_days(sessions: Sequence[Session]) -> List[Part]:
    """Cut sessions at local midnight; each part counts toward its own day."""
    parts: List[Part] = []
    for n, s in enumerate(sessions):
        cursor = s.start
        while cursor < s.end or (cursor == s.end and not any(p.session_no == n for p in parts)):
            day = local_day(cursor)
            boundary = local_midnight(day + dt.timedelta(days=1))
            end = min(s.end, boundary)
            last_part = end == s.end
            own = [c for c in s.commits if cursor <= c.at < end or (last_part and c.at == end)]
            parts.append(Part(n, day, cursor, end, own or list(s.commits), s.start_basis, borrowed=not own,
                              link_scale=s.link_scale))
            if last_part:
                break
            cursor = end
    return parts


def day_policy(p: Params, has_calendar: bool) -> str:
    return p.policy if has_calendar and p.policy != "independent" else "independent"


def estimate(commits: Sequence[Commit], checkouts: Sequence[Checkout], meetings: Sequence[Meeting],
             days: Sequence[dt.date], p: Params, *, calendar: Optional[Tuple[dt.date, dt.date]] = None,
             calendar_synced: Optional[dt.datetime] = None, reflog_since: Optional[dt.datetime] = None,
             prior_patches: Iterable[Tuple[str, str]] = (), now: Optional[dt.datetime] = None,
             copies: Sequence[dt.datetime] = ()) -> Estimate:
    """The whole pipeline for a range of local days.

    Pass every commit that could share a session with one in the range (the
    store extends its window until it finds a quiet gap), so a session that
    crosses midnight is built whole. calendar is the first and last local day
    the calendar sync covers: other days count as having no calendar data,
    because Baldur can't tell an unsynced day from a day without meetings.
    calendar_synced is when that sync ran: work after it on its own day may
    overlap meetings Baldur hasn't heard of, and the day says so. reflog_since
    is the oldest reflog entry known; days before it get a flag. prior_patches
    are (origin, patch_id) pairs of earlier commits outside the range. copies
    are the times of squash copies of your commits (see share_limits).
    """
    now = now or dt.datetime.now(UTC)
    wanted = set(days)
    kept, excluded = clean(commits, now, prior_patches)
    sessions = build_sessions(kept, checkouts, p, copies)
    parts = [x for x in split_days(sessions) if x.local_date in wanted]
    linked_days = {x.local_date for x in parts if x.link_scale < 1}
    merged = merge((m.start, m.end) for m in meetings)

    result_days: "OrderedDict[dt.date, Day]" = OrderedDict()
    for d in sorted(wanted):
        start, end = local_midnight(d), local_midnight(d + dt.timedelta(days=1))
        has_calendar = calendar is not None and calendar[0] <= d <= calendar[1]
        policy = day_policy(p, has_calendar)
        meeting_minutes = overlap_minutes(start, end, merged) if has_calendar else 0.0
        if policy == "overlap":
            cap = max(0.0, p.max_daily_dev_minutes - meeting_minutes * (1 - p.concurrent_fraction))
        else:
            cap = float(p.max_daily_dev_minutes)
        result_days[d] = Day(d, policy, has_calendar, meeting_minutes, cap)

    for part in parts:
        day = result_days[part.local_date]
        part.policy = day.policy
        part.ambient = overlap_minutes(part.start, part.end, merged) if day.has_calendar else 0.0
        part.focused = max(0.0, part.minutes - part.ambient)
        weight = p.ambient_weight if day.policy == "ambient" else 1.0
        part.counted = (part.focused + weight * part.ambient) * part.link_scale
        day.weighted += part.counted

    for day in result_days.values():
        if day.weighted > day.cap + _EPS:
            day.scale = day.cap / day.weighted if day.weighted else 0.0
            day.flags.append(f"Scaled down to the day cap of {fmt(day.cap)}")
        day.counted = min(day.weighted, day.cap)
    for part in parts:
        part.counted *= result_days[part.local_date].scale
        part.shares = attribute(part)

    proposals = _proposals(parts, p)
    reflog_day = local_day(reflog_since) if reflog_since else None
    for day in result_days.values():
        on_day = [x for x in parts if x.local_date == day.local_date]
        dev = sum(x.minutes_proposed for x in proposals if x.local_date == day.local_date)
        if not day.has_calendar and on_day:
            day.flags.append("No calendar data for this day, so meetings aren't taken into account")
        elif calendar_synced and local_day(calendar_synced) == day.local_date \
                and any(x.end > calendar_synced for x in on_day):
            day.flags.append(f"The calendar was last synced at {calendar_synced.astimezone():%H:%M}; "
                             "meetings after that aren't known yet")
        if day.local_date in linked_days:
            day.flags.append(f"A squash merge links sessions on this day, so they share one "
                             f"{fmt(p.max_session_minutes)} session limit")
        if day.policy == "independent" and on_day and day.meeting_minutes + dev > p.tour_minutes:
            day.flags.append(f"Meetings ({fmt(day.meeting_minutes)}) plus development ({fmt(dev)}) "
                             f"exceed your tour of duty ({fmt(p.tour_minutes)})")
        if reflog_day and day.local_date < reflog_day and any(x.start_basis == "lead_in" for x in on_day):
            day.flags.append(f"Git's reflog doesn't reach back to this day, so sessions start "
                             f"{p.lead_in_minutes}m before their first commit")
        if day.local_date.weekday() >= 5 and on_day:
            day.flags.append("A weekend day")
    for c, why in excluded:
        if local_day(c.at) in result_days and why.startswith("dated in the future"):
            result_days[local_day(c.at)].flags.append(f"Commit {c.sha[:10]} is {why}")
    return Estimate(result_days, parts, proposals, list(meetings), excluded)


def attribute(part: Part) -> Dict[Optional[str], float]:
    """Split a part's minutes across tickets by commit count; a commit naming two tickets splits evenly.

    Commit count, not lines changed: a vendored library or a reformat would
    dwarf an afternoon of debugging in line counts.
    """
    weights: Dict[Optional[str], float] = {}
    for c in part.commits:
        targets: Sequence[Optional[str]] = c.keys or (None,)
        for key in targets:
            weights[key] = weights.get(key, 0.0) + 1.0 / len(targets)
    total = sum(weights.values())
    return {k: part.counted * w / total for k, w in weights.items()} if total else {}


def _proposals(parts: Sequence[Part], p: Params) -> List[Proposal]:
    grouped: "OrderedDict[Tuple[dt.date, Optional[str]], List[Part]]" = OrderedDict()
    for part in sorted(parts, key=lambda x: x.start):
        for key, minutes in part.shares.items():
            if minutes > 0 or part.counted == 0:
                grouped.setdefault((part.local_date, key), []).append(part)
    out = []
    for (day, key), group in grouped.items():
        raw = sum(x.shares.get(key, 0.0) for x in group)
        commits: List[Commit] = []
        for x in group:
            for c in x.commits:
                if (key in c.keys if key else not c.keys) and c not in commits:
                    commits.append(c)
        proposed = round_down(raw, p.round_to_minutes)
        # round_down forgives float noise (89.99999999 is 90), so raw never reads as less than the result.
        out.append(Proposal(day, key, max(raw, float(proposed)), proposed, min(x.start for x in group),
                            group, commits))
    out.sort(key=lambda x: (x.local_date, x.key is None, x.key or ""))
    return out


def fmt(minutes: float) -> str:
    """135 -> '2h15m', 30 -> '30m', 0 -> '0m'."""
    m = int(math.floor(minutes + _EPS))
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"
