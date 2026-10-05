"""Estimate time spent on code changes, per Jira issue, from local git history.

Position in the flow
    A second source of worklogs, alongside meetings. Meetings CREATE sub-tasks and log the meeting's
    length; git work does not create anything - it logs time against the issue that is already named
    in the branch. So this module reads repositories and produces proposals; `__main__` renders them
    and, only when explicitly told to, pushes worklogs through the existing JiraClient.

THE IMPORTANT CAVEAT, read this before trusting any number it produces
    A meeting worklog is a measurement: the calendar says the meeting ran 13:00-13:30, so 30 minutes
    is a fact. A git worklog is an INFERENCE. Commit timestamps say when work was *recorded*, not
    when it started, how long the thinking took, or whether the author was doing something else
    between two commits.

    product.md requires that worklogs reflect time actually spent. Estimated git time does not meet
    that standard on its own, which is why:

      * the default mode is a PROPOSAL the human reviews, never an automatic push;
      * pushing requires an explicit opt-in flag, every time;
      * every proposal shows the evidence (sessions, commit counts, gaps) so it can be corrected
        rather than taken on faith;
      * the estimate is deliberately conservative, and all three caps round DOWN.

    Treat the output as a first draft of a timesheet, not a timesheet.

The rule the user asked for
    No Jira issue key in the branch name means the work is not tracked. Those commits are counted and
    reported under "untracked" so the time is visible, but they are never logged anywhere.

Why stdlib only
    Same rule as the rest of app/: no pip on the target machine. git is invoked as a subprocess with
    an argument list (never a shell string), which is also what keeps a repository path containing
    spaces - or a branch name containing a quote - from becoming a command-injection problem.
"""
from __future__ import annotations

import logging
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

# A Jira issue key: project key, hyphen, number. Uppercase by Jira convention.
# Anchored with a non-word guard so `PROJ-12` inside `feature/PROJ-12-add-thing` matches but the
# `12` in `release-2024-12` does not, and `ABC-1` inside `XABC-1` does not.
# The trailing guard rejects a letter or digit, not just a digit: `PROJ-1X` is not issue 1 of
# project PROJ, it is a string that happens to contain one. `-` and `_` must still be allowed,
# because `feature/PROJ-123-add-filter` and `ABC1-45_hotfix` are the shapes branches actually take.
ISSUE_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Z][A-Z0-9_]{1,9}-[1-9][0-9]{0,6})(?![0-9A-Za-z])")

# Branch names that are never feature work, so their lack of an issue key is not worth reporting.
BORING_BRANCHES = frozenset({"main", "master", "develop", "dev", "trunk", "HEAD"})

_GIT_RECORD_SEP = "\x1e"      # ASCII record separator: cannot appear in a commit subject
_GIT_FIELD_SEP = "\x1f"       # ASCII unit separator


class GitWorkError(Exception):
    """Something about the git scan is misconfigured. The message says what to change."""


class Commit:
    """One commit by the configured author, with the issue key it is attributable to."""

    def __init__(self, repo: str, sha: str, when: datetime, subject: str,
                 branches: Sequence[str] = (), insertions: int = 0, deletions: int = 0):
        self.repo = repo
        self.sha = sha
        self.when = when
        self.subject = subject
        self.branches = list(branches)
        self.insertions = insertions
        self.deletions = deletions

    @property
    def issue(self) -> Optional[str]:
        """The issue this commit counts towards.

        Branch name first, because that is the convention the user actually follows and it survives
        a commit message that forgot the key. The commit subject is the fallback, which covers a
        detached HEAD, a squashed merge, and work committed straight onto a branch that was renamed.
        """
        for branch in self.branches:
            found = first_issue_key(branch)
            if found:
                return found
        return first_issue_key(self.subject)

    def __repr__(self) -> str:
        return "<Commit {} {} {}>".format(self.sha[:8], self.when.isoformat(), self.issue or "-")


class Session:
    """A contiguous stretch of work, inferred from a run of commits with no long gap."""

    def __init__(self, commits: List[Commit]):
        if not commits:
            raise ValueError("a session needs at least one commit")
        self.commits = sorted(commits, key=lambda c: c.when)

    @property
    def start(self) -> datetime:
        return self.commits[0].when

    @property
    def end(self) -> datetime:
        return self.commits[-1].when

    @property
    def span_minutes(self) -> int:
        """Wall-clock minutes between first and last commit. Zero for a single commit."""
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def day(self) -> date:
        """The local day this session is attributed to: the day it started.

        A session that crosses midnight counts entirely towards the day it began, which matches how
        a person reports "I worked Tuesday evening" and keeps the daily cap meaningful.
        """
        return self.start.astimezone().date()

    def issue_weights(self) -> Dict[str, int]:
        """Commit counts per issue key within this session, untracked commits under the empty key.

        Commit count, not lines changed. Lines changed looks more precise and is worse: a vendored
        dependency, a generated file or a bulk reformat dwarfs an afternoon of careful debugging.
        Commit count is a blunt instrument that is at least not actively misleading.
        """
        weights: Dict[str, int] = {}
        for commit in self.commits:
            key = commit.issue or ""
            weights[key] = weights.get(key, 0) + 1
        return weights

    def __repr__(self) -> str:
        return "<Session {} +{}m {} commits>".format(
            self.start.isoformat(), self.span_minutes, len(self.commits))


def first_issue_key(text: str) -> Optional[str]:
    """The first Jira issue key in a string, or None.

    Case-sensitive on purpose. `feature/proj-12-thing` is not matched, because lowercasing would make
    any hyphenated word followed by digits look like an issue key (`release-2`, `python-3`), and a
    false positive here logs someone else's hours against a real issue.
    """
    match = ISSUE_KEY_RE.search(text or "")
    return match.group(1) if match else None


def _git(repo: Path, *args: str, timeout: int = 30) -> str:
    """Run a git command in repo and return stdout.

    An argument list, never a shell string: repository paths contain spaces and branch names can
    contain almost anything.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo)] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except FileNotFoundError:
        raise GitWorkError(
            "git was not found on PATH. Install git, or remove the gitwork repository roots from "
            "your config.") from None
    except subprocess.TimeoutExpired:
        raise GitWorkError(
            "git timed out after {}s in {}. A very large repository or a stale network remote is "
            "the usual cause.".format(timeout, repo)) from None
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise GitWorkError("git {} failed in {}: {}".format(args[0], repo, detail or "no detail"))
    return completed.stdout.decode("utf-8", "replace")


def discover_repos(roots: Iterable[str], max_depth: int = 3) -> List[Path]:
    """Find git working trees under each root, without walking the whole disk.

    Bounded depth on purpose: pointed at a home directory with no limit this would descend into
    every node_modules and virtualenv on the machine. A repository is not searched below itself,
    because a submodule's commits belong to the submodule's own history.
    """
    found: List[Path] = []
    for root in roots:
        base = Path(root).expanduser()
        if not base.is_dir():
            log.warning("gitwork root %s does not exist; skipping.", base)
            continue
        if (base / ".git").exists():
            found.append(base)
            continue
        stack: List[Tuple[Path, int]] = [(base, 0)]
        while stack:
            current, depth = stack.pop()
            if depth > max_depth:
                continue
            try:
                children = sorted(p for p in current.iterdir() if p.is_dir())
            except OSError:
                continue          # permissions; not worth failing the whole scan
            for child in children:
                if child.name in (".git",):
                    continue
                if (child / ".git").exists():
                    found.append(child)
                    continue      # do not descend into a repository
                if child.name.startswith(".") or child.name in (
                        "node_modules", "__pycache__", "venv", ".venv", "site-packages"):
                    continue
                stack.append((child, depth + 1))
    return sorted(set(found))


def collect_commits(repo: Path, since: datetime, until: datetime,
                    authors: Sequence[str]) -> List[Commit]:
    """Your own commits in repo between since and until, with the branches that contain them.

    `authors` must not be empty. Logging time from every contributor's commits would attribute the
    whole team's work to one timesheet, so an empty author filter is rejected rather than treated as
    "everyone" - the same reasoning as the calendar side being own-calendar-only.
    """
    if not authors:
        raise GitWorkError(
            "gitwork.authors is empty. Set it to your git author name or e-mail (several are "
            "allowed), or no commits can be attributed to you.")

    fmt = _GIT_FIELD_SEP.join(["%H", "%aI", "%s"]) + _GIT_RECORD_SEP
    args = ["log", "--all", "--no-merges",
            "--since={}".format(since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            "--until={}".format(until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            "--pretty=format:{}".format(fmt)]
    for author in authors:
        args.append("--author={}".format(author))

    raw = _git(repo, *args)
    commits: List[Commit] = []
    for record in raw.split(_GIT_RECORD_SEP):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split(_GIT_FIELD_SEP)
        if len(parts) < 3:
            continue
        sha, when_raw, subject = parts[0].strip(), parts[1].strip(), parts[2]
        try:
            when = _parse_git_date(when_raw)
        except ValueError:
            log.debug("Skipping commit %s: unparseable date %r", sha[:8], when_raw)
            continue
        commits.append(Commit(repo=repo.name, sha=sha, when=when, subject=subject))

    _attach_branches(repo, commits)
    return sorted(commits, key=lambda c: c.when)


def _parse_git_date(text: str) -> datetime:
    """Parse git's strict-ISO author date into an aware datetime.

    Python 3.8's fromisoformat does not accept a trailing Z, and git emits +00:00 offsets, so both
    forms are normalized here rather than assumed.
    """
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _attach_branches(repo: Path, commits: List[Commit]) -> None:
    """Record which feature branches each commit belongs to, so the branch can supply the issue key.

    "Belongs to" means **added by that branch**, not merely reachable from it. That distinction is
    the whole correctness of this function, and getting it wrong is not subtle:
    `git rev-list <branch>` lists every ancestor, so every commit on main from the beginning of the
    repository is reachable from every feature branch cut off it. Attaching branches that way
    attributes the entire project history to whichever feature branch happens to be checked out.

    So each branch is listed with the trunk refs excluded (`rev-list branch --not main master ...`),
    which leaves only the commits that branch introduced.

    Known limitation: once a branch is merged, its commits become reachable from trunk and are
    excluded here, so they lose their branch-derived key and fall back to the commit subject. For
    the normal scan window - the last day or few days of active work - the branch is still
    unmerged, which is the case that matters. A merged branch's time should already have been
    proposed while the work was happening.

    One `git branch --contains` per commit would be O(n) subprocesses, so this inverts it: list each
    branch's own commits once, then map back by SHA.
    """
    by_sha: Dict[str, Commit] = {c.sha: c for c in commits}
    if not by_sha:
        return
    try:
        branch_lines = _git(repo, "for-each-ref", "--format=%(refname:short)",
                            "refs/heads").splitlines()
    except GitWorkError as exc:
        log.debug("Could not list branches in %s: %s", repo, exc)
        return

    branches = [b.strip() for b in branch_lines if b.strip()]
    trunks = [b for b in branches if b in BORING_BRANCHES]

    for branch in branches:
        # Only branches that could carry an issue key are worth the walk.
        if branch in BORING_BRANCHES or not first_issue_key(branch):
            continue
        args = ["rev-list", "--no-merges", "-n", "2000", branch]
        for trunk in trunks:
            if trunk != branch:
                args.extend(["--not", trunk])
        try:
            shas = _git(repo, *args).split()
        except GitWorkError:
            continue
        for sha in shas:
            commit = by_sha.get(sha)
            if commit is not None and branch not in commit.branches:
                commit.branches.append(branch)


def build_sessions(commits: Sequence[Commit], idle_gap_minutes: int = 120) -> List[Session]:
    """Group commits into work sessions, splitting wherever the gap exceeds idle_gap_minutes.

    The gap is the whole model. Two commits four minutes apart are obviously one stretch of work;
    two commits six hours apart are obviously not. Everything in between is a guess, and the default
    of two hours is chosen to under-report rather than over-report: a genuine continuous session with
    a long silent stretch gets split into two shorter ones, which loses time rather than inventing
    it. Erring the other way would inflate a timesheet.
    """
    if idle_gap_minutes <= 0:
        raise GitWorkError("gitwork.idle_gap_minutes must be greater than zero.")
    ordered = sorted(commits, key=lambda c: c.when)
    sessions: List[Session] = []
    bucket: List[Commit] = []
    for commit in ordered:
        if bucket and (commit.when - bucket[-1].when) > timedelta(minutes=idle_gap_minutes):
            sessions.append(Session(bucket))
            bucket = []
        bucket.append(commit)
    if bucket:
        sessions.append(Session(bucket))
    return sessions


def session_minutes(session: Session, lead_in_minutes: int = 30, min_minutes: int = 15,
                    max_minutes: int = 240) -> int:
    """Minutes to credit a single session.

    span + lead_in, then clamped.

    The lead-in exists because the first commit of a session is the *end* of the first piece of
    work, not its beginning: nobody commits the instant they sit down. Without it, a session of two
    commits twenty minutes apart scores twenty minutes for what was plainly longer, and a lone
    commit scores zero.

    max_minutes catches the pathological case of one commit at 09:00 and one at 17:00 with nothing
    between, which is not an eight-hour session - it is two sessions that the gap threshold failed
    to split because the author happened to commit at both ends of the day.
    """
    if min_minutes > max_minutes:
        raise GitWorkError("gitwork.min_session_minutes cannot exceed gitwork.max_session_minutes.")
    return max(min_minutes, min(session.span_minutes + lead_in_minutes, max_minutes))


def attribute(session: Session, minutes: int) -> Dict[str, int]:
    """Split a session's minutes across the issues its commits touch.

    Proportional to commit count, with the remainder going to the issue with the most commits so the
    parts always sum to the whole - minutes must not evaporate in integer division.

    Untracked commits (no issue key anywhere) hold their share under the empty key. That share is
    reported but never logged, which is the "no Jira code, no tracking" rule: the time stays visible
    so the user can see what fell outside the system, without anything being written to Jira.
    """
    weights = session.issue_weights()
    total_weight = sum(weights.values())
    if not total_weight:
        return {}
    shares: Dict[str, int] = {}
    for key, weight in weights.items():
        shares[key] = (minutes * weight) // total_weight
    shortfall = minutes - sum(shares.values())
    if shortfall:
        biggest = max(weights, key=lambda k: (weights[k], k))
        shares[biggest] = shares.get(biggest, 0) + shortfall
    return shares


class DayProposal:
    """What this module believes was worked on a single local day."""

    def __init__(self, day: date):
        self.day = day
        self.per_issue: Dict[str, int] = {}
        self.untracked_minutes = 0
        self.sessions: List[Session] = []
        self.capped_from: Optional[int] = None    # pre-cap total, when the daily cap bit

    @property
    def tracked_minutes(self) -> int:
        return sum(self.per_issue.values())

    @property
    def total_minutes(self) -> int:
        return self.tracked_minutes + self.untracked_minutes

    def __repr__(self) -> str:
        return "<DayProposal {} tracked={}m untracked={}m>".format(
            self.day, self.tracked_minutes, self.untracked_minutes)


def propose(commits: Sequence[Commit], idle_gap_minutes: int = 120, lead_in_minutes: int = 30,
            min_session_minutes: int = 15, max_session_minutes: int = 240,
            max_daily_minutes: int = 480,
            meeting_minutes_by_day: Optional[Dict[date, int]] = None) -> List[DayProposal]:
    """Turn commits into per-day, per-issue minute proposals.

    The daily cap is the part that matters most for a timesheet, and it accounts for meetings. Odin
    already logs meeting time from the calendar; if git estimates are added on top with no ceiling,
    a day with five hours of meetings and a busy evening of commits produces a twelve-hour day that
    nobody worked. So the cap is `max_daily_minutes` MINUS the meeting minutes already logged for
    that day, and tracked issues are scaled down proportionally to fit.

    Scaling down rather than truncating the last session, because truncation would silently zero
    whichever issue happened to sort last.
    """
    meetings = meeting_minutes_by_day or {}
    by_day: Dict[date, DayProposal] = {}

    for session in build_sessions(commits, idle_gap_minutes=idle_gap_minutes):
        minutes = session_minutes(session, lead_in_minutes=lead_in_minutes,
                                  min_minutes=min_session_minutes,
                                  max_minutes=max_session_minutes)
        proposal = by_day.setdefault(session.day, DayProposal(session.day))
        proposal.sessions.append(session)
        for key, share in attribute(session, minutes).items():
            if key:
                proposal.per_issue[key] = proposal.per_issue.get(key, 0) + share
            else:
                proposal.untracked_minutes += share

    for proposal in by_day.values():
        allowance = max_daily_minutes - meetings.get(proposal.day, 0)
        allowance = max(0, allowance)
        if proposal.tracked_minutes > allowance:
            proposal.capped_from = proposal.tracked_minutes
            proposal.per_issue = _scale_to(proposal.per_issue, allowance)

    return [by_day[d] for d in sorted(by_day)]


def _scale_to(per_issue: Dict[str, int], allowance: int) -> Dict[str, int]:
    """Scale minutes down to fit allowance, preserving the total exactly.

    Rounds each share down, then hands any remainder to the largest share. Rounding down is
    deliberate: when the estimate has to be trimmed, under-reporting is the safe direction.
    """
    total = sum(per_issue.values())
    if total <= 0 or allowance <= 0:
        return {key: 0 for key in per_issue}
    scaled = {key: (minutes * allowance) // total for key, minutes in per_issue.items()}
    shortfall = allowance - sum(scaled.values())
    if shortfall and scaled:
        biggest = max(per_issue, key=lambda k: (per_issue[k], k))
        scaled[biggest] = scaled.get(biggest, 0) + shortfall
    return scaled


def window(days_back: int, now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """Midnight local `days_back` days ago through now, as aware datetimes.

    Same shape as the calendar window, and safe to widen for the same reason: re-running cannot
    double-log, because the state table records minutes already logged per issue per day and only
    the difference is ever sent.
    """
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone()
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=max(0, days_back)), now


def render(proposals: Sequence[DayProposal], show_evidence: bool = True) -> List[str]:
    """Human-readable proposal. This is the default output, and the thing the user checks.

    Shows the evidence - sessions, commit counts, where a cap bit - because the numbers are
    estimates and an estimate the user cannot interrogate is one they cannot correct.
    """
    lines: List[str] = []
    tracked_total = 0
    untracked_total = 0

    for proposal in proposals:
        lines.append("{}  ({} session(s))".format(proposal.day.isoformat(),
                                                  len(proposal.sessions)))
        for key in sorted(proposal.per_issue):
            minutes = proposal.per_issue[key]
            lines.append("   {:<16} {}".format(key, _hhmm(minutes)))
            tracked_total += minutes
        if proposal.untracked_minutes:
            lines.append("   {:<16} {}   NOT TRACKED - no Jira key in branch or subject".format(
                "(untracked)", _hhmm(proposal.untracked_minutes)))
            untracked_total += proposal.untracked_minutes
        if proposal.capped_from is not None:
            lines.append("   capped: {} -> {} to stay inside the daily limit "
                         "(meetings already logged count against it)".format(
                             _hhmm(proposal.capped_from), _hhmm(proposal.tracked_minutes)))
        if show_evidence:
            for session in proposal.sessions:
                keys = ", ".join(sorted(k or "(untracked)" for k in session.issue_weights()))
                lines.append("     {}-{}  {} commit(s)  [{}]".format(
                    session.start.astimezone().strftime("%H:%M"),
                    session.end.astimezone().strftime("%H:%M"),
                    len(session.commits), keys))
        lines.append("")

    lines.append("Tracked {} across {} day(s).".format(_hhmm(tracked_total), len(proposals)))
    if untracked_total:
        lines.append("Untracked {} - branch names had no Jira issue key, so none of it will be "
                     "logged.".format(_hhmm(untracked_total)))
    lines.append("")
    lines.append("These are ESTIMATES inferred from commit times, not measured time. Review them "
                 "before logging anything.")
    return lines


def _hhmm(minutes: int) -> str:
    return "{}h{:02d}m".format(minutes // 60, minutes % 60)
