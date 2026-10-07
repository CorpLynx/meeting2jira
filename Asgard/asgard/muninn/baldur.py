"""Baldur's approval rules, kept next to the schema so every caller follows them.

A decided proposal never changes (a trigger enforces it). Approving a day
supersedes the day's earlier approval for the same ticket; changing an
approved number writes a new approved row and supersedes the old one, in
one transaction. approve_day() and reject_day() decide all of a day's
tickets in one transaction. Odin posts from the newest approval.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from .db import MuninnError, transaction, utcnow
from .sync import emit


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


def _approve(con: sqlite3.Connection, row: sqlite3.Row, minutes_final: Optional[int], at: Optional[str]) -> None:
    if row["status"] != "proposed":
        raise _not_open(con, row, "approve")
    if row["work_item_key"] is None:
        raise MuninnError("Untracked time can't be approved. Give its commits a Jira key first.")
    minutes = _minutes(row["minutes_proposed"] if minutes_final is None else minutes_final)
    _fits_in_day(con, row["local_date"], minutes, except_key=row["work_item_key"])
    con.execute("UPDATE day_proposals SET status = 'superseded' WHERE local_date = ? AND work_item_key = ? "
                "AND status = 'approved'", (row["local_date"], row["work_item_key"]))
    con.execute("UPDATE day_proposals SET status = 'approved', minutes_final = ?, decided_at = ? WHERE id = ?",
                (minutes, at or utcnow(), row["id"]))
    _emit_approved(con, int(row["id"]), row, minutes)


def approve(con: sqlite3.Connection, proposal_id: int, minutes_final: Optional[int] = None,
            at: Optional[str] = None) -> int:
    """Approve an open proposal, at its proposed minutes unless you give others."""
    with transaction(con):
        _approve(con, _proposal(con, proposal_id), minutes_final, at)
    return proposal_id


def _open_rows(con: sqlite3.Connection, local_date: str) -> List[sqlite3.Row]:
    return con.execute("SELECT * FROM day_proposals WHERE local_date = ? AND status = 'proposed' "
                       "AND work_item_key IS NOT NULL ORDER BY work_item_key", (local_date,)).fetchall()


def approve_day(con: sqlite3.Connection, local_date: str, minutes: Optional[Dict[str, int]] = None,
                at: Optional[str] = None) -> List[int]:
    """Approve every open proposal for a day's tickets together; minutes overrides some of them."""
    minutes = {k.strip().upper(): v for k, v in (minutes or {}).items()}
    with transaction(con):
        rows = _open_rows(con, local_date)
        # Keys compare without regard to case: rows written before schema v3 weren't checked.
        missing = sorted(set(minutes) - {r["work_item_key"].upper() for r in rows})
        if missing:
            raise MuninnError(f"No open proposal for {', '.join(missing)} on {local_date}.")
        for row in rows:
            _approve(con, row, minutes.get(row["work_item_key"].upper()), at)
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
