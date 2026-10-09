"""Baldur's rules, kept next to the schema so every caller follows them.

Approvals. A decided proposal never changes (a trigger enforces it). Approving a day supersedes
the day's earlier approval for the same ticket; changing an approved number writes a new
approved row and supersedes the old one, in one transaction. approve_day() and reject_day()
decide all of a day's tickets in one transaction. Odin posts from the newest approval.

AI-assisted figures. An AI never decides a number; it can only suggest one, and every suggestion
is checked here before it is stored or taken (Baldur spec, "Optional AI review"):
- record_agent_estimate() stores what a coding agent says your time on a change was, as a fact
  with its commits. A newer report on the same change withdraws the older one.
- check_review() is the gate for any reply that adjusts a day: the day's total never rises, every
  adjustment names a ticket the day already has and cites evidence that was sent, no ticket goes
  below zero, at most 10 adjustments, and the results round down again.
- store_review() checks a reply against the day's open proposals as Muninn holds them (not as
  the caller says) and stores it on them; approving with review= records which figure you took.
The window, the CLI and Ysildir all call these, so none can skip a rule.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from .db import MuninnError, to_ts, transaction, utcnow
from .keys import normalize_key
from .redact import scrub
from .sync import emit

REPORT_SCHEMA = "baldur.agent_estimate/1"
MAX_ADJUSTMENTS = 10
CONFIDENCE = ("high", "medium", "low")


def _proposal(con: sqlite3.Connection, proposal_id: int) -> sqlite3.Row:
    row = con.execute("SELECT * FROM day_proposals WHERE id = ?", (proposal_id,)).fetchone()
    if row is None:
        raise MuninnError(f"No day proposal {proposal_id}.")
    return row


def _emit_approved(con: sqlite3.Connection, proposal_id: int, row: sqlite3.Row, minutes: int) -> None:
    emit(con, "baldur", "day_proposal.approved", "day_proposals", proposal_id, row["work_item_key"],
         {"local_date": row["local_date"], "minutes": minutes})


def _not_open(con: sqlite3.Connection, row: sqlite3.Row, verb: str) -> MuninnError:
    hint = ""
    if row["status"] == "superseded":
        newer = con.execute("SELECT id FROM day_proposals WHERE local_date = ? AND work_item_key IS ? "
                            "AND status = 'proposed'", (row["local_date"], row["work_item_key"])).fetchone()
        if newer:
            hint = f" Proposal {newer[0]} replaced it; {verb} that one."
    return MuninnError(f"Proposal {row['id']} is {row['status']}; only an open proposal can be {verb}d.{hint}")


def _minutes(value: Any) -> int:
    """An approved figure: whole minutes, 0 to 1440 (a day; the schema refuses more from v3)."""
    try:
        minutes = int(value)                  # a fraction is dropped: approvals only round down
    except (TypeError, ValueError, OverflowError) as exc:
        raise MuninnError(f"{value!r} isn't a number of minutes.") from exc
    if minutes < 0:
        raise MuninnError("Minutes can't be negative.")
    if minutes > 1440:
        raise MuninnError(f"{minutes} minutes is more than a day (1440).")
    return minutes


def _fits_in_day(con: sqlite3.Connection, local_date: str, minutes: int, except_key: Optional[str] = None,
                 except_id: Optional[int] = None) -> None:
    """All of a day's approvals together fit in the day (the schema refuses more from v3)."""
    others = con.execute("SELECT coalesce(sum(minutes_final), 0) FROM day_proposals WHERE local_date = ? "
                         "AND status = 'approved' AND work_item_key IS NOT ? AND id IS NOT ?",
                         (local_date, except_key, except_id)).fetchone()[0]
    if others + minutes > 1440:
        raise MuninnError(f"{local_date} already has {_hm(others)} approved, so {_hm(minutes)} more would be over "
                          "24 hours. Lower one of the day's figures first.")


def _hm(minutes: int) -> str:
    minutes = int(minutes)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _approve(con: sqlite3.Connection, row: sqlite3.Row, minutes_final: Optional[int], at: Optional[str],
             note: Optional[Dict[str, Any]] = None) -> None:
    if row["status"] != "proposed":
        raise _not_open(con, row, "approve")
    if row["work_item_key"] is None:
        raise MuninnError("Untracked time can't be approved. Give its commits a Jira key first.")
    minutes = _minutes(row["minutes_proposed"] if minutes_final is None else minutes_final)
    _fits_in_day(con, row["local_date"], minutes, except_key=row["work_item_key"])
    con.execute("UPDATE day_proposals SET status = 'superseded' WHERE local_date = ? AND work_item_key = ? "
                "AND status = 'approved'", (row["local_date"], row["work_item_key"]))
    con.execute("UPDATE day_proposals SET status = 'approved', minutes_final = ?, decided_at = ?, "
                "review = json_patch(review, ?) WHERE id = ?",
                (minutes, at or utcnow(), json.dumps(note or {}), row["id"]))
    _emit_approved(con, int(row["id"]), row, minutes)


def approve(con: sqlite3.Connection, proposal_id: int, minutes_final: Optional[int] = None,
            at: Optional[str] = None, review: Optional[Dict[str, Any]] = None) -> int:
    """Approve an open proposal, at its proposed minutes unless you give others.

    review, if given, is merged into the row's review record: which AI-assisted figure you took.
    """
    with transaction(con):
        _approve(con, _proposal(con, proposal_id), minutes_final, at, review)
    return proposal_id


def _open_rows(con: sqlite3.Connection, local_date: str) -> List[sqlite3.Row]:
    return con.execute("SELECT * FROM day_proposals WHERE local_date = ? AND status = 'proposed' "
                       "AND work_item_key IS NOT NULL ORDER BY work_item_key", (local_date,)).fetchall()


def open_proposals(con: sqlite3.Connection, local_date: str) -> List[sqlite3.Row]:
    """A day's open proposals for tickets, in key order (untracked time is never decided)."""
    return _open_rows(con, local_date)


def approve_day(con: sqlite3.Connection, local_date: str, minutes: Optional[Dict[str, int]] = None,
                at: Optional[str] = None, review: Optional[Dict[str, Dict[str, Any]]] = None) -> List[int]:
    """Approve every open proposal for a day's tickets together; minutes overrides some of them.

    review maps a ticket to what is recorded with its approval (an AI-assisted figure you took).
    """
    minutes = {k.strip().upper(): v for k, v in (minutes or {}).items()}
    review = {k.strip().upper(): v for k, v in (review or {}).items()}
    with transaction(con):
        rows = _open_rows(con, local_date)
        # Keys compare without regard to case: rows written before schema v3 weren't checked.
        missing = sorted((set(minutes) | set(review)) - {r["work_item_key"].upper() for r in rows})
        if missing:
            raise MuninnError(f"No open proposal for {', '.join(missing)} on {local_date}.")
        # The day's total is checked as it will end, not after each ticket: re-estimating can move
        # time between tickets, and the old approvals are superseded together first.
        figures = {r["id"]: _minutes(r["minutes_proposed"] if minutes.get(r["work_item_key"].upper()) is None
                                     else minutes[r["work_item_key"].upper()]) for r in rows}
        keys = [r["work_item_key"] for r in rows]
        if rows:
            marks = ",".join("?" * len(keys))
            kept = con.execute(f"SELECT coalesce(sum(minutes_final), 0) FROM day_proposals WHERE local_date = ? "
                               f"AND status = 'approved' AND work_item_key NOT IN ({marks})",
                               (local_date, *keys)).fetchone()[0]
            if kept + sum(figures.values()) > 1440:
                raise MuninnError(f"{local_date} would have {_hm(kept + sum(figures.values()))} approved, over 24 "
                                  "hours. Lower one of the day's figures first.")
            con.execute(f"UPDATE day_proposals SET status = 'superseded' WHERE local_date = ? AND status = 'approved' "
                        f"AND work_item_key IN ({marks})", (local_date, *keys))
        for row in rows:
            _approve(con, row, figures[row["id"]], at, review.get(row["work_item_key"].upper()))
    return [int(r["id"]) for r in rows]


def _reject(con: sqlite3.Connection, row: sqlite3.Row, at: Optional[str]) -> None:
    if row["status"] != "proposed":
        raise _not_open(con, row, "reject")
    if row["work_item_key"] is None:
        raise MuninnError("Untracked time is never logged, so there's nothing to reject.")
    con.execute("UPDATE day_proposals SET status = 'rejected', decided_at = ? WHERE id = ?",
                (at or utcnow(), row["id"]))


def reject(con: sqlite3.Connection, proposal_id: int, at: Optional[str] = None) -> None:
    with transaction(con):
        _reject(con, _proposal(con, proposal_id), at)


def reject_day(con: sqlite3.Connection, local_date: str, at: Optional[str] = None) -> List[int]:
    """Reject every open proposal for a day's tickets together."""
    with transaction(con):
        rows = _open_rows(con, local_date)
        for row in rows:
            _reject(con, row, at)
    return [int(r["id"]) for r in rows]


def change_approval(con: sqlite3.Connection, proposal_id: int, minutes_final: int,
                    at: Optional[str] = None) -> int:
    """Approve a different number for an approved day. Returns the new proposal's id."""
    with transaction(con):
        row = _proposal(con, proposal_id)
        if row["status"] != "approved":
            raise MuninnError(f"Proposal {proposal_id} is {row['status']}, not approved.")
        minutes_final = _minutes(minutes_final)
        _fits_in_day(con, row["local_date"], minutes_final, except_id=proposal_id)
        con.execute("UPDATE day_proposals SET status = 'superseded' WHERE id = ?", (proposal_id,))
        # upper(): a key stored before schema v3 checked keys comes back in capitals, so it resolves.
        new_id = con.execute(
            "INSERT INTO day_proposals (estimate_run_id, local_date, work_item_key, minutes_raw, minutes_proposed, "
            "minutes_final, first_started_at, basis, basis_hash, review, status, decided_at) "
            "SELECT estimate_run_id, local_date, upper(trim(work_item_key)), minutes_raw, minutes_proposed, ?, "
            "first_started_at, basis, basis_hash, review, 'approved', ? FROM day_proposals WHERE id = ? RETURNING id",
            (int(minutes_final), at or utcnow(), proposal_id)).fetchone()[0]
        _emit_approved(con, int(new_id), row, int(minutes_final))
    return int(new_id)


# --------------------------------------------------------------------------
# Agent estimates: what a coding agent says your time on a change was
# --------------------------------------------------------------------------

_AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._\-]{0,39}$")
_GUIDE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,39}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_FIELDS = {"schema", "agent", "model", "guide", "date", "key", "commits", "minutes", "minutes_low", "confidence",
           "summary", "started_at", "ended_at"}
# What makes a summary something other than one plain sentence: a code block, a diff, a patch hunk.
_CODE_RE = re.compile(r"```|^\s*(diff --git|@@ |\+\+\+ |--- )", re.MULTILINE)
FUTURE_SLACK = dt.timedelta(minutes=5)


@dataclass
class Recorded:
    id: int
    status: str                      # 'recorded', or 'duplicate' when the same report was stored before
    replaced: List[int] = field(default_factory=list)   # older reports on the same change, now withdrawn


def _text(report: Dict[str, Any], name: str) -> Optional[str]:
    value = report.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MuninnError(f"{name} must be text.")
    return value.strip() or None


def _when(value: Any, name: str) -> Optional[dt.datetime]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MuninnError(f"{name} must be a time like 2026-10-01T14:05:00Z.")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"          # fromisoformat takes Z only from Python 3.11
    try:
        at = dt.datetime.fromisoformat(text)
    except ValueError:
        raise MuninnError(f"{name} isn't a time like 2026-10-01T14:05:00Z.") from None
    if at.tzinfo is None:
        raise MuninnError(f"{name} needs its time zone, like 2026-10-01T14:05:00Z or 2026-10-01T10:05:00-04:00.")
    return at.astimezone(dt.timezone.utc).replace(microsecond=0)


def _whole_minutes(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MuninnError(f"{name} must be a whole number of minutes.")
    if not 1 <= value <= 1440:
        raise MuninnError(f"{name} must be from 1 to 1440 minutes (a day).")
    return value


def _summary(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MuninnError("summary is required: one sentence on what the change was.")
    if _CODE_RE.search(value) or value.count("\n") > 2:
        raise MuninnError("summary must be one plain sentence, with no code or diff: Muninn keeps metadata only.")
    text = " ".join(scrub(value).split())
    if len(text) > 300:
        raise MuninnError("summary must be under 300 characters: one sentence on what the change was.")
    return text


def _commits(value: Any) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        raise MuninnError("commits must be a list of commit SHAs.")
    out = []
    for s in value:
        sha = s.strip().lower()
        if not _SHA_RE.match(sha):
            raise MuninnError(f"{s[:70]!r} isn't a commit SHA (7 to 64 hex characters; the full one is best: "
                              "git rev-parse HEAD).")
        if sha not in out:
            out.append(sha)
    if len(out) > 50:
        raise MuninnError("A report covers at most 50 commits; record each day's work separately.")
    return out


def record_agent_estimate(con: sqlite3.Connection, report: Dict[str, Any], *, via: str = "cli",
                          now: Optional[dt.datetime] = None) -> Recorded:
    """Store one agent's estimate of your working time on a change (REPORT_SCHEMA).

    The report must be the agent's estimate of *your* time on the change that day: reading,
    prompting, reviewing and testing, not the agent's own running time. Recording it never
    changes a number; Baldur shows it beside the day and may use it to move or lower time.
    """
    if not isinstance(report, dict):
        raise MuninnError("A report is a JSON object; see Baldur's agent guide.")
    unknown = sorted(set(report) - _FIELDS)
    if unknown:
        raise MuninnError(f"Unknown fields {', '.join(unknown)}; a report has {', '.join(sorted(_FIELDS))}.")
    if report.get("schema", REPORT_SCHEMA) != REPORT_SCHEMA:
        raise MuninnError(f"schema must be {REPORT_SCHEMA!r}.")
    if via not in ("cli", "mcp", "window"):
        raise ValueError("via is 'cli', 'mcp' or 'window'")
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    agent = _text(report, "agent")
    if not agent or not _AGENT_RE.match(agent):
        raise MuninnError("agent must name the tool, like \"kiro\" or \"copilot\" (letters, digits, . _ -).")
    model = _text(report, "model")
    if model and (len(model) > 80 or not model.isprintable()):
        raise MuninnError("model must be the model's name, under 80 characters.")
    guide = _text(report, "guide")
    if guide and not _GUIDE_RE.match(guide):
        raise MuninnError("guide must be the agent guide's version, like \"baldur-agent-2\".")
    key = _text(report, "key")
    if key:
        try:
            key = normalize_key(key)
        except ValueError as exc:
            raise MuninnError(str(exc)) from None
    started, ended = _when(report.get("started_at"), "started_at"), _when(report.get("ended_at"), "ended_at")
    for name, at in (("started_at", started), ("ended_at", ended)):
        if at and at > now + FUTURE_SLACK:
            raise MuninnError(f"{name} is in the future; give the time the work happened.")
    if started and ended and ended < started:
        raise MuninnError("ended_at is before started_at.")
    if started and ended and ended - started > dt.timedelta(hours=24):
        raise MuninnError("A report covers at most one day of work; record each day separately.")
    date_text = _text(report, "date")
    if date_text:
        try:
            day = dt.date.fromisoformat(date_text)
        except ValueError:
            raise MuninnError("date must be the day the work happened, like 2026-10-01.") from None
    else:
        day = (ended or started or now).astimezone().date()
    if day > now.astimezone().date():
        raise MuninnError("date is in the future; give the day the work happened.")
    minutes = _whole_minutes(report.get("minutes"), "minutes")
    low = report.get("minutes_low")
    if low is not None:
        low = _whole_minutes(low, "minutes_low")
        if low > minutes:
            raise MuninnError("minutes_low is the low end of a range, so it can't be more than minutes.")
    confidence = (_text(report, "confidence") or "").lower()
    if confidence not in CONFIDENCE:
        raise MuninnError("confidence must be high, medium or low.")
    summary = _summary(report.get("summary"))
    commits = _commits(report.get("commits"))

    canonical = {"agent": agent.lower(), "date": day.isoformat(), "key": key, "minutes": minutes, "low": low,
                 "confidence": confidence, "summary": summary, "commits": sorted(commits),
                 "started": to_ts(started) if started else None, "ended": to_ts(ended) if ended else None}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode("utf-8")).hexdigest()[:32]
    with transaction(con):
        same = con.execute("SELECT id FROM agent_estimates WHERE report_hash = ?", (digest,)).fetchone()
        if same:
            return Recorded(int(same[0]), "duplicate")
        # A newer report from the same agent on exactly the same commits replaces the older one.
        replaced: List[int] = []
        if commits:
            for r in con.execute("SELECT e.id, (SELECT json_group_array(sha) FROM (SELECT sha FROM agent_estimate_commits "
                                 "WHERE estimate_id = e.id ORDER BY sha)) AS shas FROM agent_estimates e "
                                 "WHERE e.status = 'recorded' AND lower(e.agent) = ? AND e.local_date = ?",
                                 (agent.lower(), day.isoformat())):
                if json.loads(r["shas"]) == sorted(commits):
                    replaced.append(int(r["id"]))
        at = utcnow()
        for old in replaced:
            con.execute("UPDATE agent_estimates SET status = 'withdrawn', withdrawn_at = ? WHERE id = ?", (at, old))
        new_id = int(con.execute(
            "INSERT INTO agent_estimates (via, agent, model, guide_version, work_item_key, local_date, started_at, "
            "ended_at, minutes, minutes_low, confidence, summary, report_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (via, agent, model, guide, key, day.isoformat(), to_ts(started) if started else None,
             to_ts(ended) if ended else None, minutes, low, confidence, summary, digest)).fetchone()[0])
        con.executemany("INSERT INTO agent_estimate_commits (estimate_id, sha) VALUES (?, ?)",
                        [(new_id, s) for s in commits])
        emit(con, "baldur", "agent_estimate.recorded", "agent_estimates", new_id, key,
             {"local_date": day.isoformat(), "minutes": minutes, "agent": agent, "commits": len(commits),
              "replaced": replaced})
    return Recorded(new_id, "recorded", replaced)


def withdraw_agent_estimate(con: sqlite3.Connection, estimate_id: int) -> None:
    """Withdraw a report (it stays, marked withdrawn, for audit)."""
    with transaction(con):
        row = con.execute("SELECT status, work_item_key FROM agent_estimates WHERE id = ?", (estimate_id,)).fetchone()
        if row is None:
            raise MuninnError(f"No agent estimate {estimate_id}.")
        if row["status"] != "recorded":
            raise MuninnError(f"Agent estimate {estimate_id} is already withdrawn.")
        con.execute("UPDATE agent_estimates SET status = 'withdrawn', withdrawn_at = ? WHERE id = ?",
                    (utcnow(), estimate_id))
        emit(con, "baldur", "agent_estimate.withdrawn", "agent_estimates", estimate_id, row["work_item_key"], {})


def list_agent_estimates(con: sqlite3.Connection, first: str, last: str, *, include_withdrawn: bool = False,
                         limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Reports dated first to last (YYYY-MM-DD, both included), oldest first, as `ai list --json` gives them.

    Baldur's CLI and Ysildir both list through here, so the two can't disagree.
    """
    sql = ("SELECT e.*, (SELECT count(*) FROM agent_estimate_commits a WHERE a.estimate_id = e.id) AS n "
           "FROM agent_estimates e WHERE e.local_date BETWEEN ? AND ? " +
           ("" if include_withdrawn else "AND e.status = 'recorded' ") + "ORDER BY e.local_date, e.id")
    params: List[Any] = [first, last]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [{"id": f"r{r['id']}", "date": r["local_date"], "agent": r["agent"], "key": r["work_item_key"],
             "minutes": r["minutes"], "minutes_low": r["minutes_low"], "confidence": r["confidence"],
             "commits": r["n"], "summary": r["summary"], "status": r["status"]}
            for r in con.execute(sql, params)]


# --------------------------------------------------------------------------
# Checking a reply that adjusts a day
# --------------------------------------------------------------------------

class ReviewRejected(MuninnError):
    """A reply broke a rule, so none of it is used; the message says which."""


@dataclass
class Adjustment:
    ticket: str
    minutes: int                     # the change: negative lowers, positive moves time in from another ticket
    confidence: str
    evidence: List[str]
    reason: str


@dataclass
class CheckedReview:
    day: str
    baseline: Dict[str, int]         # each ticket's proposed minutes the reply was checked against
    figures: Dict[str, int]          # each ticket's figure after the adjustments, rounded down again
    adjustments: List[Adjustment]
    flags: List[str]

    def delta(self, ticket: str) -> int:
        return self.figures.get(ticket, 0) - self.baseline.get(ticket, 0)


def _round_down(minutes: float, step: int) -> int:
    return int(math.floor(minutes / step + 1e-9)) * step if minutes > 0 else 0


def clean_line(value: Any, limit: int) -> str:
    """Model text shown to a person: one line, no control characters, credential-shaped text masked."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(value or ""))
    text = " ".join((scrub(text) or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def parse_reply(text: Any) -> Dict[str, Any]:
    """The JSON object in a reply, also when a chat wraps it in ```json fences or a sentence."""
    if isinstance(text, dict):
        return text
    if not isinstance(text, str) or not text.strip():
        raise ReviewRejected("The reply is empty. Paste the whole JSON answer.")
    body = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", body, re.DOTALL)
    if fenced:
        body = fenced.group(1)
    elif not body.startswith("{"):
        start, end = body.find("{"), body.rfind("}")
        body = body[start:end + 1] if 0 <= start < end else body
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ReviewRejected(f"The reply isn't JSON ({exc.msg}). Ask for the JSON answer only, then paste it.") from None
    if not isinstance(data, dict):
        raise ReviewRejected("The reply must be one JSON object with day, adjustments and flags.")
    return data


def _cited(item: Any, evidence: Set[str]) -> bool:
    """Whether a cited id is in the evidence: exactly, or as a 7+ character prefix of one commit SHA.

    Session ids (s1) and report ids (r12) start with letters that aren't hex, so they can't be
    mistaken for a SHA prefix.
    """
    text = str(item).strip()
    if re.fullmatch(r"[0-9A-Fa-f]{7,64}", text):
        text = text.lower()
        if text in evidence:
            return True
        return sum(1 for e in evidence if e.startswith(text)) == 1
    return text in evidence


def check_review(day: str, baseline: Dict[str, int], evidence: Iterable[str], reply: Any, *,
                 step: int) -> CheckedReview:
    """Check a reply that adjusts one day; the whole reply is refused if any rule fails.

    baseline is each ticket's proposed minutes that day; evidence the commit SHAs, session ids
    and agent report ids that were sent. The rules, in the spec's words:
    1. The adjustments sum to zero or less: the day's total never rises.
    2. Every adjustment names a ticket already in the baseline, and none goes below zero.
    3. Every adjustment cites at least one commit SHA, session id or report id from the evidence.
    4. At most 10 adjustments; the result is rounded down again.
    """
    data = parse_reply(reply)
    evidence = {str(e) for e in evidence}
    if data.get("day") != day:
        raise ReviewRejected(f"The reply is for {data.get('day')!r}, not {day}.")
    items = data.get("adjustments", [])
    flags = data.get("flags", [])
    if not isinstance(items, list) or not isinstance(flags, list):
        raise ReviewRejected("adjustments and flags must be lists.")
    if len(items) > MAX_ADJUSTMENTS:
        raise ReviewRejected(f"The reply makes {len(items)} adjustments; at most {MAX_ADJUSTMENTS} are allowed.")
    keys = {k.upper(): k for k in baseline}
    adjustments: List[Adjustment] = []
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ReviewRejected(f"Adjustment {n} isn't an object.")
        ticket = str(item.get("ticket", "")).strip().upper()
        if ticket not in keys:
            raise ReviewRejected(f"Adjustment {n} names {ticket or 'no ticket'}, which isn't one of the day's tickets "
                                 f"({', '.join(sorted(baseline)) or 'none'}). A review can't add a ticket.")
        minutes = item.get("minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or abs(minutes) > 1440:
            raise ReviewRejected(f"Adjustment {n} ({ticket}) needs minutes as a whole number, like -15.")
        cites = item.get("evidence")
        if not isinstance(cites, list) or not cites:
            raise ReviewRejected(f"Adjustment {n} ({ticket}) cites no evidence; every adjustment must cite a commit "
                                 "SHA, session id or report id from the evidence it was given.")
        unknown = [clean_line(c, 70) for c in cites if not _cited(c, evidence)]
        if unknown:
            raise ReviewRejected(f"Adjustment {n} ({ticket}) cites {', '.join(unknown[:3])}, which wasn't in the "
                                 "evidence sent.")
        confidence = str(item.get("confidence", "low")).lower()
        if confidence not in CONFIDENCE:
            raise ReviewRejected(f"Adjustment {n} ({ticket}) has confidence {confidence!r}; use high, medium or low.")
        adjustments.append(Adjustment(keys[ticket], minutes, confidence, [str(c).strip() for c in cites],
                                      clean_line(item.get("reason", ""), 200)))
    total = sum(a.minutes for a in adjustments)
    if total > 0:
        raise ReviewRejected(f"The adjustments add {total} minutes; a review may move time or lower it, never raise "
                             "the day.")
    figures = dict(baseline)
    for a in adjustments:
        figures[a.ticket] = figures[a.ticket] + a.minutes
    below = sorted(k for k, m in figures.items() if m < 0)
    if below:
        raise ReviewRejected(f"The adjustments take {', '.join(below)} below zero.")
    figures = {k: _round_down(m, step) for k, m in figures.items()}
    if sum(figures.values()) > sum(baseline.values()):         # can't happen after the checks; kept as the rule
        raise ReviewRejected("The adjusted day would be more than the estimate.")
    return CheckedReview(day, dict(baseline), figures, adjustments,
                         [clean_line(f, 200) for f in flags[:10] if str(f).strip()])


def store_review(con: sqlite3.Connection, local_date: str, reply: Any, *, evidence: Iterable[str], step: int,
                 meta: Dict[str, Any]) -> CheckedReview:
    """Check a reply against the day's open proposals, as Muninn holds them, and store it on them.

    meta says where the reply came from: method ('review' or 'agent'), tier, model, prompt_version,
    pack_hash. A later estimate that replaces a proposal leaves the review behind with it, so a
    review never outlives the evidence it was checked against.
    """
    with transaction(con):
        rows = _open_rows(con, local_date)
        if not rows:
            raise ReviewRejected(f"{local_date} has no open proposals to review. Run cli.py estimate first.")
        baseline = {r["work_item_key"]: int(r["minutes_proposed"]) for r in rows}
        checked = check_review(local_date, baseline, evidence, reply, step=step)
        at = utcnow()
        for r in rows:
            key = r["work_item_key"]
            mine = [a for a in checked.adjustments if a.ticket == key]
            record = dict(meta, at=at, baseline=baseline[key], suggested=checked.figures[key],
                          adjustments=[a.__dict__ for a in mine], flags=checked.flags)
            con.execute("UPDATE day_proposals SET review = ? WHERE id = ? AND status = 'proposed'",
                        (json.dumps(record, sort_keys=True), r["id"]))
    return checked


def review_of(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        data = json.loads(row["review"] or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def review_line(review: Any, minutes_final: int) -> Optional[str]:
    """The worklog comment's 'Reviewed:' line, when the approved figure is an AI-assisted one you took."""
    if isinstance(review, str):
        try:
            review = json.loads(review)
        except ValueError:
            return None
    if not isinstance(review, dict) or not review.get("taken"):
        return None
    suggested, baseline = review.get("suggested"), review.get("baseline")
    if not isinstance(suggested, int) or not isinstance(baseline, int) or suggested != int(minutes_final):
        return None                    # changed by hand after taking it: the figure is yours alone
    who = review.get("source") or ("agent estimates" if review.get("method") == "agent" else "AI review")
    if suggested < baseline:
        what = f"lowered this from {_hm(baseline)} to {_hm(suggested)}"
    elif suggested > baseline:
        what = f"moved {_hm(suggested - baseline)} to this from the day's other tickets"
    else:
        what = f"kept {_hm(baseline)}"
    # A model's reason says why (the spec's example); an agent method's reason only repeats the numbers.
    reason = review.get("reason") if review.get("method") == "review" else None
    return f"Reviewed: {who} {what}" + (f' ("{reason}")' if reason else "")
