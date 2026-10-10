"""Ysildir's Baldur tools, and the only Ysildir module that imports Baldur.

Each tool is one call into Baldur, or into Muninn's Baldur rules, on a connection opened for that
call:
- writes (recording and withdrawing a report, the review pack, submitting a review) on a
  connection opened as baldur, so Muninn's guard holds them to Baldur's tables exactly as it holds
  Baldur's own window and CLI;
- reads (the reports, the day) on the read-only connection opened as ysildir, which can't write
  at all: baldur_day runs Baldur's own desk.load_day there, which proves it writes nothing.
No rule lives here: a refusal is Baldur's own message. Nothing here approves, rejects, changes or
posts time; each answer gives the person the command that does, for them to run.
"""
import contextlib
import json
import sqlite3
from typing import Iterator

from asgard import muninn
from asgard.muninn import baldur as rules
from baldur import assist, desk
from baldur import estimate as E
from baldur import settings as baldur_settings

from . import SCHEMA, Refused, day_range, parse_day, reader
from .models import (MAX_ITEMS, AgentReport, Day, DateFrom, DateTo, DayAI, DayTicket, DayView, EstimateRow,
                     Estimates, IncludeReport, IncludeWithdrawn, ModelName, OptionalDay, Recorded, ReportId,
                     ReviewChecked, ReviewPack, ReviewReply, Withdrawn)

BALDUR = "baldur.cmd"
REPORT_CHARS = 32 * 1024        # the day report holds commit subjects; longer is cut
PACK_BYTES = 60 * 1024          # a bigger pack goes through the clipboard tier instead


@contextlib.contextmanager
def as_baldur() -> Iterator[sqlite3.Connection]:
    """Muninn for one write, as baldur: the guard allows Baldur's tables and nothing else."""
    con = muninn.open_app("baldur", supported=SCHEMA)
    try:
        yield con
    finally:
        con.close()


def settings() -> baldur_settings.Settings:
    """Baldur's settings, read only: Ysildir never creates or changes baldur.json."""
    path = baldur_settings.settings_path()
    if not path.exists():
        raise Refused("Baldur isn't set up on this computer yet. Ask the person to open Baldur once.")
    found = baldur_settings.load(path, create=False)
    if found.broken:
        raise Refused(f"Baldur's settings have a problem ({found.warnings[0]}). Ask the person to open Baldur "
                      "to fix them.")
    return found


def _span(minutes: int, low: object) -> str:
    return f"{E.fmt(int(low))} to {E.fmt(minutes)}" if isinstance(low, int) else E.fmt(minutes)


# --------------------------------------------------------------------------
# The agent's own reports
# --------------------------------------------------------------------------

def baldur_record_estimate(report: AgentReport) -> Recorded:
    data = report.model_dump(by_alias=True, exclude_none=True)
    data["schema"] = rules.REPORT_SCHEMA
    with as_baldur() as con:
        done = rules.record_agent_estimate(con, data, via="mcp")
        row = con.execute("SELECT agent, minutes, minutes_low, work_item_key, local_date FROM agent_estimates "
                          "WHERE id = ?", (done.id,)).fetchone()
    what = (f"{row['agent']}, {_span(row['minutes'], row['minutes_low'])} on "
            f"{row['work_item_key'] or 'the tickets of its commits'} ({row['local_date']})")
    replaced = [f"r{n}" for n in done.replaced]
    if done.status == "duplicate":
        message = f"Already recorded as r{done.id}: {what}. Nothing changes until you approve the day."
    else:
        message = (f"Recorded r{done.id}: {what}" + (f"; it replaces {', '.join(replaced)}" if replaced else "") +
                   ". Baldur shows it beside the day; nothing changes until you approve.")
    return Recorded(id=f"r{done.id}", status=done.status, replaced=replaced, message=message)


def baldur_withdraw_estimate(id: ReportId) -> Withdrawn:     # noqa: A002 - the agent sees "id"
    number = int(id.lstrip("rR"))
    with as_baldur() as con:
        rules.withdraw_agent_estimate(con, number)
    return Withdrawn(id=f"r{number}", status="withdrawn",
                     message=f"Withdrew r{number}. It stays in Muninn, marked withdrawn, and no longer counts.")


def baldur_estimates(date_from: DateFrom = None, date_to: DateTo = None,
                     include_withdrawn: IncludeWithdrawn = False) -> Estimates:
    first, last = day_range(date_from, date_to, default_days=14)
    with reader() as con:
        rows = rules.list_agent_estimates(con, first.isoformat(), last.isoformat(),
                                          include_withdrawn=include_withdrawn, limit=MAX_ITEMS + 1)
    for r in rows:
        r["summary"] = rules.clean_line(r["summary"], 300)
    return Estimates(date_from=first.isoformat(), date_to=last.isoformat(), estimates=[EstimateRow(**r) for r in rows],
                     untrusted_fields=["estimates[].summary"])


# --------------------------------------------------------------------------
# The day, and the AI review at the MCP tier
# --------------------------------------------------------------------------

def baldur_day(date: OptionalDay = None, include_report: IncludeReport = False) -> DayView:
    found = settings()
    day = parse_day(date)
    with reader() as con:
        view = desk.load_day(con, found, day)
    ai = view.ai
    tickets = []
    for t in view.tickets:
        s = ai.tickets.get(t.key) if ai else None
        said = s is not None and (s.changed or bool(s.reason))
        tickets.append(DayTicket(
            key=t.key, estimate=t.estimate, open=t.open_id is not None, approved=t.approved, jira_holds=t.held,
            problem=t.problem, ai_assisted=s.figure if s else None,
            reason=rules.clean_line(s.reason, 200) if said and s.reason else None,
            confidence=s.confidence if said else None, evidence=list(s.evidence) if s else [],
            worklog_line=ai.posted_line(t.key) if ai and s is not None and s.changed else None))
    changed = bool(ai and ai.changed())
    untrusted = ["tickets[].reason", "tickets[].worklog_line", "flags[]", "ai.source", "ai.flags[]"]
    report = None
    if include_report:
        report = view.report if len(view.report) <= REPORT_CHARS else view.report[:REPORT_CHARS] + "\n[cut]"
        untrusted.append("report")
    return DayView(
        day=day.isoformat(), tickets=tickets, untracked=view.untracked,
        flags=[rules.clean_line(f, 300) for f in view.flags],
        ai=DayAI(id=ai.digest(), method=ai.method, source=rules.clean_line(ai.source, 120),
                 reports=[f"r{n}" for n in ai.reports], flags=[rules.clean_line(f, 300) for f in ai.flags])
        if ai else None,
        take=f"{BALDUR} approve --date {day.isoformat()} --ai {ai.digest()}" if changed and ai else None,
        keep=f"{BALDUR} approve --date {day.isoformat()}" if changed else None,
        report=report, untrusted_fields=untrusted)


def baldur_review_pack(date: OptionalDay = None) -> ReviewPack:
    found = settings()
    day = parse_day(date)
    with as_baldur() as con:
        try:
            pack = assist.day_pack(con, found, day)       # obeys review_mode, then stores the day's estimate
        except assist.AssistError as exc:
            raise Refused(str(exc)) from None
    size = len(json.dumps(pack, separators=(",", ":")).encode("utf-8"))
    if size > PACK_BYTES:
        raise Refused(f"{day.isoformat()}'s evidence is too large for one answer ({size // 1024} KB). Ask the "
                      f"person to use the clipboard instead: {BALDUR} ai pack {day.isoformat()} --out FILE")
    return ReviewPack(day=day.isoformat(), pack=pack, prompt=assist.review_prompt(), reply_to="baldur_submit_review",
                      untrusted_fields=["pack.commits[].subject", "pack.agent_reports[].summary"])


def baldur_submit_review(date: Day, reply: ReviewReply, model: ModelName = None) -> ReviewChecked:
    found = settings()
    day = parse_day(date)
    with as_baldur() as con:
        try:
            checked = assist.apply_reply(con, found, day, reply.model_dump(), tier="mcp", model=model)
        except (assist.AssistError, rules.ReviewRejected) as exc:
            raise Refused(f"The reply wasn't used: {exc} Baldur's estimate stands as it is. Show the person this "
                          "message.") from None
        latest = assist.suggestions(con, found, day)    # the figures as the person will see and take them
    changed = [k for k in checked.figures if checked.figures[k] != checked.baseline[k]]
    taking = latest if latest is not None and latest.method == "review" and latest.changed() else None
    take = f"{BALDUR} approve --date {day.isoformat()} --ai {taking.digest()}" if changed and taking else None
    lines = {t.key: taking.posted_line(t.key) or "" for t in taking.changed()} if taking else {}
    if changed:
        moves = ", ".join(f"{k} {E.fmt(checked.baseline[k])} to {E.fmt(checked.figures[k])}" for k in changed)
        message = (f"Checked and stored the review: {moves}. Nothing changes until you approve"
                   + (f"; to take it: {take}" if take else "."))
    else:
        message = "Checked and stored the review: it changes nothing, so Baldur's estimate stands."
    return ReviewChecked(day=day.isoformat(), baseline=dict(checked.baseline), figures=dict(checked.figures),
                         flags=list(checked.flags), take=take, worklog_lines=lines, message=message,
                         untrusted_fields=["flags[]", "worklog_lines"])
