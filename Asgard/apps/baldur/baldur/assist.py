"""Baldur's AI-assisted method: the manual engine's day, adjusted by what an AI could tell.

Baldur has two ways to estimate, side by side:
- The manual engine (estimate.py): commits, checkouts and meetings in; minutes out. No AI.
- The AI-assisted method (here). It never replaces the engine's numbers; it suggests figures
  beside them, and you take them (cli.py approve --date D --ai) or leave them.

Where an AI-assisted figure comes from, best first:
1. A checked AI review reply stored on the day's open proposals (muninn.baldur.store_review),
   while the evidence it saw is still the day's evidence (its pack hash matches).
2. Otherwise the day's agent estimates: what a coding agent that worked a change with you said
   your time on it was (muninn.baldur.record_agent_estimate). They're turned into adjustments
   here, in code, with no AI call at all.

Both go through muninn.baldur.check_review, so the same rules hold whatever the source: the
day's total never rises; a ticket gains only time moved from another ticket; every figure cites
evidence; untracked time is never suggested; results round down. An agent can't raise a day:
when it says a change took longer than git shows, the report says so and the number stays.

Metadata only (Asgard rule 6): a pack holds commit subjects, line counts, times and keys, never
code, and review_mode decides whether a pack may leave this computer at all.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from asgard import muninn
from asgard.muninn import baldur as rules

from . import estimate as E
from . import store
from .settings import Settings

PACK_SCHEMA = "baldur.review_pack/1"
PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "review.md"
GUIDE_FILE = Path(__file__).resolve().parent.parent / "prompts" / "agent-guide.md"
REPORT_WINDOW_DAYS = 7          # reports dated this close to a day are read with it
_RANK = {"high": 0, "medium": 1, "low": 2}


class AssistError(ValueError):
    """The AI-assisted method can't do what was asked; the message says why and what to do."""


def prompt_version(path: Path = PROMPT_FILE) -> str:
    """The version on the prompt's first line, <!-- baldur-review-N -->."""
    first = path.read_text(encoding="utf-8").splitlines()[0]
    return first.replace("<!--", "").replace("-->", "").strip()


def guide_version(path: Path = GUIDE_FILE) -> str:
    return prompt_version(path)


# --------------------------------------------------------------------------
# Agent reports
# --------------------------------------------------------------------------

@dataclass
class AgentReport:
    id: int
    agent: str
    model: Optional[str]
    key: Optional[str]
    local_date: dt.date
    minutes: int
    low: Optional[int]
    confidence: str
    summary: str
    cited: List[str]
    recorded_at: str

    @property
    def ref(self) -> str:
        return f"r{self.id}"

    @property
    def counted(self) -> int:
        """The figure Baldur uses: the low end of a range, since of two readings the smaller wins."""
        return self.low or self.minutes


def load_reports(con: sqlite3.Connection, first: dt.date, last: dt.date,
                 commits: Iterable[E.Commit] = ()) -> List[AgentReport]:
    """Recorded reports dated near these days, or citing any of these commits, oldest first."""
    lo = (first - dt.timedelta(days=REPORT_WINDOW_DAYS)).isoformat()
    hi = (last + dt.timedelta(days=REPORT_WINDOW_DAYS)).isoformat()
    prefixes = sorted({c.sha[:n] for c in commits for n in range(7, 13)} | {c.sha for c in commits})
    rows = con.execute(
        "SELECT * FROM agent_estimates WHERE status = 'recorded' AND (local_date BETWEEN ? AND ? OR id IN "
        "(SELECT estimate_id FROM agent_estimate_commits WHERE sha IN (SELECT value FROM json_each(?)))) ORDER BY id",
        (lo, hi, json.dumps(prefixes))).fetchall()
    cited: Dict[int, List[str]] = {}
    for eid, sha in con.execute("SELECT estimate_id, sha FROM agent_estimate_commits WHERE estimate_id IN "
                                "(SELECT value FROM json_each(?)) ORDER BY estimate_id, sha",
                                (json.dumps([r["id"] for r in rows]),)):
        cited.setdefault(int(eid), []).append(sha)
    return [AgentReport(int(r["id"]), r["agent"], r["model"], r["work_item_key"], dt.date.fromisoformat(r["local_date"]),
                        int(r["minutes"]), r["minutes_low"], r["confidence"], r["summary"], cited.get(int(r["id"]), []),
                        r["recorded_at"]) for r in rows]


def uncollected(con: sqlite3.Connection, shas: Iterable[str]) -> Set[str]:
    """The SHAs (or SHA prefixes) Muninn has no commit of yours for."""
    out = set()
    for sha in set(shas):
        lo, hi = sha, sha + "g"          # every hex string that starts with sha sorts between these
        if con.execute("SELECT 1 FROM commits WHERE is_mine = 1 AND sha >= ? AND sha < ? LIMIT 1",
                       (lo, hi)).fetchone() is None:
            out.add(sha)
    return out


def resolve(cited: Sequence[str], commits: Iterable[E.Commit]) -> Tuple[List[E.Commit], List[str], List[str]]:
    """(your commits a report cites, SHAs Baldur doesn't have, SHAs that match more than one commit)."""
    pool = list(commits)
    found: List[E.Commit] = []
    unknown: List[str] = []
    ambiguous: List[str] = []
    for sha in cited:
        hits = [c for c in pool if c.sha.startswith(sha)]
        if len(hits) == 1:
            if hits[0] not in found:
                found.append(hits[0])
        elif hits:
            ambiguous.append(sha)
        else:
            unknown.append(sha)
    return found, unknown, ambiguous


# --------------------------------------------------------------------------
# Suggestions
# --------------------------------------------------------------------------

@dataclass
class TicketSuggestion:
    key: str
    baseline: int            # the engine's figure (the open proposal)
    figure: int              # the AI-assisted figure
    reason: str = ""
    confidence: str = "low"
    evidence: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.figure != self.baseline


@dataclass
class DaySuggestions:
    day: dt.date
    method: str              # 'agent' (agent estimates, in code) or 'review' (a checked AI reply)
    source: str              # for people: "agent estimates (kiro, 2 reports)"
    tickets: "OrderedDict[str, TicketSuggestion]"
    flags: List[str] = field(default_factory=list)
    reports: List[int] = field(default_factory=list)
    pack_hash: Optional[str] = None

    def changed(self) -> List[TicketSuggestion]:
        return [t for t in self.tickets.values() if t.changed]

    def note(self, key: str) -> Dict[str, Any]:
        """What approving this ticket's AI-assisted figure records with the approval."""
        t = self.tickets[key]
        note = {"taken": True, "method": self.method, "source": self.source, "baseline": t.baseline,
                "suggested": t.figure, "reason": t.reason, "confidence": t.confidence, "evidence": t.evidence,
                "reports": self.reports, "pack_hash": self.pack_hash, "taken_at": muninn.utcnow()}
        return {k: v for k, v in note.items() if v not in (None, "", [])}


def _baseline(con: sqlite3.Connection, est: E.Estimate, plan: store.Plan, day: dt.date) -> "OrderedDict[str, int]":
    """The tickets an AI-assisted figure can change: open, or about to be stored as open, with time to move.

    A ticket you already decided is left out, so no time moves away from (or to) an approval.
    """
    open_keys = {r["work_item_key"] for r in rules.open_proposals(con, day.isoformat())}
    out: "OrderedDict[str, int]" = OrderedDict()
    for prop in est.proposals_for(day):
        if prop.key and prop.minutes_proposed > 0 and (prop.key in open_keys or plan.action_for(day, prop.key) == "insert"):
            out[prop.key] = prop.minutes_proposed
    return out


def _who(reports: Sequence[AgentReport]) -> str:
    agents = sorted({r.agent for r in reports})
    return f"agent estimates ({', '.join(agents)}, {len(reports)} report{'s' if len(reports) != 1 else ''})"


def agent_draft(est: E.Estimate, day: dt.date, baseline: Dict[str, int], reports: Sequence[AgentReport],
                commits: Sequence[E.Commit], step: int, uncollected: Iterable[str] = ()
                ) -> Tuple[Optional[Dict[str, Any]], List[str], List[AgentReport]]:
    """The day's agent estimates as a reply check_review can take: (reply or None, flags, reports used).

    Each report's figure (its low end, if it gave one) is shared across the commits it cites,
    so a report covering two days or two tickets splits like Baldur's own attribution. Where two
    reports cite one commit, the smaller share counts. For the tickets the reports cover, the
    target is the agents' figure for the covered commits plus the engine's share of any commits
    they don't cover. Targets are scaled down, never up, to fit the minutes the engine proposed
    for those tickets, so the day can't rise and a ticket can gain only what another gives up.
    uncollected are cited SHAs Muninn has no commit of yours for, which the day's flags name.
    """
    uncollected = set(uncollected)
    flags: List[str] = []
    props = {x.key: x for x in est.proposals_for(day) if x.key in baseline}
    on_day = {c.sha: c for x in est.proposals_for(day) for c in x.commits}
    share: Dict[str, float] = {}                      # sha -> minutes the agents give that commit
    covering: Dict[str, List[AgentReport]] = {}       # sha -> reports that cite it
    used: List[AgentReport] = []
    for r in reports:
        if not r.cited:
            if r.local_date == day:
                flags.append(f"{r.agent} reported {E.fmt(r.minutes)} on {r.key or 'no ticket'} ({r.ref}) with no "
                             "commits, so it isn't counted. If the time is real, approve a figure by hand.")
            continue
        found, unknown, ambiguous = resolve(r.cited, commits)
        if r.local_date == day:
            for sha in unknown:
                if sha in uncollected:
                    flags.append(f"{r.ref} cites {sha[:12]}, which Baldur hasn't collected as one of your commits "
                                 "(run cli.py collect).")
            for sha in ambiguous:
                flags.append(f"{r.ref} cites {sha}, which matches more than one commit; give more of the SHA.")
        if not found:
            continue
        each = r.counted / len(found)
        mine = [c for c in found if c.sha in on_day]
        if mine:
            used.append(r)
        for c in mine:
            share[c.sha] = min(share.get(c.sha, each), each)
            covering.setdefault(c.sha, []).append(r)
            if not c.keys:
                flags.append(f"{r.ref} covers {c.sha[:10]}, which has no Jira key; untracked time is never logged "
                             f"(cli.py keys {c.sha[:10]} {r.key or 'PROJ-1'}).")
            elif r.key and r.key not in c.keys:
                flags.append(f"{r.ref} names {r.key}, but {c.sha[:10]} counts toward {', '.join(c.keys)}.")
    target: Dict[str, float] = {}
    why: Dict[str, List[AgentReport]] = {}
    shas: Dict[str, List[str]] = {}
    for key, prop in props.items():
        mine = [c for c in prop.commits if key in c.keys]
        total = sum(1.0 / len(c.keys) for c in mine)
        covered = [c for c in mine if c.sha in share]
        if not covered or not total:
            continue
        weight = sum(1.0 / len(c.keys) for c in covered)
        target[key] = sum(share[c.sha] / len(c.keys) for c in covered) + prop.minutes_raw * (1 - weight / total)
        why[key] = sorted({r.id: r for c in covered for r in covering[c.sha]}.values(), key=lambda r: r.id)
        shas[key] = [c.sha for c in covered]
    if not target:
        return None, flags, used
    pool = sum(baseline[k] for k in target)
    wanted = sum(target.values())
    scale = min(1.0, pool / wanted) if wanted > 0 else 0.0
    if wanted > pool + step:
        flags.append(f"The agents put these changes at {E.fmt(wanted)}, more than the {E.fmt(pool)} Baldur can see "
                     "from git. An agent's estimate can't raise a day; if the time is real, approve it by hand.")
    adjustments = []
    for key, value in target.items():
        figure = E.round_down(value * scale, step)
        delta = figure - baseline[key]
        if delta == 0:
            continue
        agents = ", ".join(sorted({r.agent for r in why[key]}))
        if delta < 0:
            reason = f"{agents} put it at {E.fmt(value)}; commits gave {E.fmt(baseline[key])}"
        else:
            reason = f"{agents} put it at {E.fmt(value)}; moved from the other tickets"
        adjustments.append({"ticket": key, "minutes": delta, "evidence": [r.ref for r in why[key]] + shas[key],
                            "confidence": max((r.confidence for r in why[key]), key=lambda c: _RANK.get(c, 2)),
                            "reason": reason})
    reply = {"day": day.isoformat(), "adjustments": adjustments, "flags": []}
    return reply, flags, used


def _stored(con: sqlite3.Connection, day: dt.date, baseline: Dict[str, int],
            pack_hash: Optional[str]) -> Optional[DaySuggestions]:
    """A checked AI review stored on the day's open proposals, if it still matches the day's evidence."""
    rows = rules.open_proposals(con, day.isoformat())
    reviews = {r["work_item_key"]: rules.review_of(r) for r in rows}
    if not reviews or pack_hash is None:
        return None
    if any(rv.get("method") != "review" or rv.get("pack_hash") != pack_hash for rv in reviews.values()):
        return None
    if {k: rv.get("baseline") for k, rv in reviews.items()} != dict(baseline):
        return None
    first = next(iter(reviews.values()))
    tickets: "OrderedDict[str, TicketSuggestion]" = OrderedDict()
    for key, rv in sorted(reviews.items()):
        adj = rv.get("adjustments") or []
        tickets[key] = TicketSuggestion(key, int(rv["baseline"]), int(rv["suggested"]),
                                        "; ".join(a.get("reason", "") for a in adj if a.get("reason")),
                                        max((a.get("confidence", "low") for a in adj), key=lambda c: _RANK.get(c, 2),
                                            default="low"),
                                        [e for a in adj for e in a.get("evidence", [])])
    return DaySuggestions(day, "review", first.get("source") or "AI review", tickets, list(first.get("flags") or []),
                          [], pack_hash)


def suggestions(con: sqlite3.Connection, settings: Settings, day: dt.date,
                now: Optional[dt.datetime] = None) -> Optional[DaySuggestions]:
    """The day's AI-assisted figures, or None when there's nothing to suggest."""
    inputs, est = store.compute_with_inputs(con, settings, day, day, now)
    plan = store.plan(con, settings, est, day, day)
    baseline = _baseline(con, est, plan, day)
    reports = load_reports(con, day, day, inputs.commits)
    if settings.review_mode == "metadata" and baseline:
        try:
            pack = build_pack(con, settings, day, inputs, est, reports, baseline)
            found = _stored(con, day, baseline, pack["pack_hash"])
            if found:
                return found
        except AssistError:
            pass
    if not reports:
        return None
    reply, flags, used = agent_draft(est, day, baseline, reports, inputs.commits, settings.round_to_minutes,
                                     uncollected(con, [s for r in reports for s in r.cited]))
    tickets: "OrderedDict[str, TicketSuggestion]" = OrderedDict(
        (k, TicketSuggestion(k, m, m)) for k, m in baseline.items())
    if reply is not None:
        try:
            checked = rules.check_review(day.isoformat(), dict(baseline), {e for a in reply["adjustments"]
                                                                           for e in a["evidence"]},
                                         reply, step=settings.round_to_minutes)
        except rules.ReviewRejected as exc:
            # Can't happen with agent_draft as it is; if it ever does, the estimate stands and the day says why.
            flags.append(f"Baldur's check refused the agent figures ({exc}), so the estimate stands.")
            checked = None
        for a in checked.adjustments if checked else ():
            t = tickets[a.ticket]
            t.reason, t.confidence, t.evidence = a.reason, a.confidence, a.evidence
        for key, figure in checked.figures.items() if checked else ():
            tickets[key].figure = figure
    if reply is None and not flags:
        return None
    return DaySuggestions(day, "agent", _who(used or [r for r in reports if r.local_date == day]), tickets, flags,
                          [r.id for r in used])


# --------------------------------------------------------------------------
# AI review through the clipboard (or Ysildir): the evidence pack and the reply
# --------------------------------------------------------------------------

def _check_mode(settings: Settings) -> None:
    mode = settings.review_mode
    if mode == "off":
        raise AssistError("AI review is off. It sends commit subjects, times and ticket keys (no code) to your AI "
                          "tool, so turn it on once your ISSO agrees: cli.py setup --set review_mode=metadata")
    if mode == "content":
        raise AssistError("review_mode 'content' would send code to an AI tool, which Asgard doesn't do (rule 6: "
                          "metadata only). Use: cli.py setup --set review_mode=metadata")


def build_pack(con: sqlite3.Connection, settings: Settings, day: dt.date, inputs: store.Inputs, est: E.Estimate,
               reports: Sequence[AgentReport], baseline: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """The day's evidence for an AI review, metadata only. Its pack_hash names exactly this evidence."""
    _check_mode(settings)
    p = E.Params.from_settings(settings)
    rows = {r["work_item_key"]: r for r in rules.open_proposals(con, day.isoformat())}
    if baseline is None:
        baseline = {k: int(r["minutes_proposed"]) for k, r in rows.items()}
    if not baseline:
        raise AssistError(f"{day.isoformat()} has no open proposals to review. Run cli.py estimate first.")
    parts = sorted(est.parts_on(day), key=lambda x: x.start)
    sessions, commit_ids = [], []
    for n, x in enumerate(parts, 1):
        sessions.append({"id": f"s{n}", "start": store.hhmm(x.start), "end": store.hhmm(x.end),
                         "minutes": round(x.minutes, 1), "meeting_minutes": round(x.ambient, 1),
                         "counted": round(x.counted, 1), "commits": [c.sha for c in x.commits]})
        commit_ids.extend(c.id for c in x.commits if c.id not in commit_ids)
    stats = {r["id"]: r for r in con.execute("SELECT id, files_changed, additions, deletions FROM commits WHERE id IN "
                                             "(SELECT value FROM json_each(?))", (json.dumps(commit_ids),))}
    commits = []
    for x in parts:
        for c in x.commits:
            if any(item["sha"] == c.sha for item in commits):
                continue
            s = stats.get(c.id)
            commits.append({"sha": c.sha, "tickets": list(c.keys), "at": store.hhmm(c.at), "repo": c.label,
                            "subject": rules.clean_line(c.subject, 120),
                            "files": s["files_changed"] if s else None, "additions": s["additions"] if s else None,
                            "deletions": s["deletions"] if s else None})
    day_shas = {item["sha"] for item in commits}
    relevant = []
    for r in reports:
        found, _, _ = resolve(r.cited, inputs.commits)
        if r.local_date == day or any(c.sha in day_shas for c in found):
            relevant.append({"id": r.ref, "agent": r.agent, "ticket": r.key, "minutes": r.minutes,
                             "minutes_low": r.low, "confidence": r.confidence, "summary": r.summary,
                             "commits": list(r.cited)})
    d = est.days[day]
    pack = {"schema": PACK_SCHEMA, "day": day.isoformat(), "prompt_version": prompt_version(),
            "policy": store.policy_text(d, p), "round_to_minutes": p.round_to_minutes,
            "meeting_minutes": round(d.meeting_minutes, 1) if d.has_calendar else None,
            "baseline": [{"ticket": k, "minutes": m} for k, m in sorted(baseline.items())],
            "sessions": sessions, "commits": commits, "agent_reports": relevant}
    text = json.dumps(pack, sort_keys=True, separators=(",", ":"))
    pack["pack_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return pack


def evidence_ids(pack: Dict[str, Any]) -> Set[str]:
    """What an adjustment may cite: the pack's commit SHAs, session ids and report ids."""
    ids = {c["sha"] for c in pack["commits"]} | {s["id"] for s in pack["sessions"]}
    return ids | {r["id"] for r in pack["agent_reports"]}


def day_pack(con: sqlite3.Connection, settings: Settings, day: dt.date,
             now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    """Store the day's estimate (so its proposals are open and current), then build its pack."""
    _check_mode(settings)
    store.run(con, settings, day, day, now=now)
    inputs, est = store.compute_with_inputs(con, settings, day, day, now)
    return build_pack(con, settings, day, inputs, est, load_reports(con, day, day, inputs.commits))


def review_prompt() -> str:
    """The review prompt as a model reads it: review.md without its version line."""
    return "\n".join(PROMPT_FILE.read_text(encoding="utf-8").splitlines()[1:]).strip()


def clipboard_text(pack: Dict[str, Any]) -> str:
    """The prompt and the pack, ready to paste into an approved AI chat (the clipboard tier)."""
    return f"{review_prompt()}\n\nInput:\n{json.dumps(pack, indent=1)}\n"


def apply_reply(con: sqlite3.Connection, settings: Settings, day: dt.date, reply: Any, *, tier: str = "clipboard",
                model: Optional[str] = None, now: Optional[dt.datetime] = None) -> rules.CheckedReview:
    """Check an AI's reply against the day as it is now and store it on the open proposals.

    The reply must carry the pack_hash it was given: if the day changed since (new commits, a new
    agent report, a re-estimate), the reply is refused and a new pack is needed.
    """
    if tier not in ("clipboard", "mcp", "api"):
        raise AssistError("tier is clipboard, mcp or api.")
    _check_mode(settings)                  # first, so review off says so rather than "make a new pack"
    data = rules.parse_reply(reply)
    inputs, est = store.compute_with_inputs(con, settings, day, day, now)
    plan = store.plan(con, settings, est, day, day)
    if plan.would_change(day):
        raise AssistError(f"{day.isoformat()} has changed since it was estimated. Make a new pack: "
                          f"cli.py ai pack {day.isoformat()}")
    pack = build_pack(con, settings, day, inputs, est, load_reports(con, day, day, inputs.commits))
    if data.get("pack") != pack["pack_hash"]:
        raise AssistError(f"This reply is for different evidence than {day.isoformat()} has now (pack "
                          f"{data.get('pack')!r}, now {pack['pack_hash']}). Make a new pack: cli.py ai pack "
                          f"{day.isoformat()}")
    clean_model = rules.clean_line(model, 60) if model else None
    meta = {"method": "review", "tier": tier, "model": clean_model, "prompt_version": pack["prompt_version"],
            "pack_hash": pack["pack_hash"], "source": f"AI review ({tier}{', ' + clean_model if clean_model else ''})"}
    return rules.store_review(con, day.isoformat(), data, evidence=evidence_ids(pack), step=settings.round_to_minutes,
                              meta={k: v for k, v in meta.items() if v is not None})


# --------------------------------------------------------------------------
# Taking the figures
# --------------------------------------------------------------------------

def approve_day(con: sqlite3.Connection, settings: Settings, day: dt.date,
                figures: Optional[Dict[str, int]] = None, now: Optional[dt.datetime] = None) -> List[int]:
    """Approve the day at its AI-assisted figures; figures you give for a ticket win over them.

    The day's estimate is stored first (so the open proposals are current), exactly as approving
    without AI does. Each ticket whose AI-assisted figure you took records it with the approval,
    which Odin's worklog comment then mentions.
    """
    figures = {k.strip().upper(): int(v) for k, v in (figures or {}).items()}
    store.run(con, settings, day, day, now=now)
    found = suggestions(con, settings, day, now)
    if found is None or not found.changed():
        raise AssistError(f"No AI-assisted figures for {day.isoformat()}: no agent estimates or AI review change it. "
                          f"Approve the estimate instead: cli.py approve --date {day.isoformat()}")
    minutes = {t.key: t.figure for t in found.changed()}
    review = {k: found.note(k) for k in minutes if k not in figures}
    minutes.update(figures)
    return rules.approve_day(con, day.isoformat(), minutes, review=review)
