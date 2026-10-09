"""Calibration: measuring Baldur's dials against hours you noted, instead of guessing them.

    calibrate.note(con, day, minutes)            # the day's real development time (or one ticket's)
    fit = calibrate.fit(con, settings)           # the grid search; writes nothing
    calibrate.accept(con, settings, fit)         # new settings and an active calibration_runs row

The rules (Baldur spec, "Calibration"):
- Real hours are what you noted in time_actuals. Never Baldur's own numbers or your approvals: an
  approval usually starts from the estimate, so fitting to it would only confirm the settings.
- The search covers the three dials the spec names: idle gap 60-180 m, lead-in 0-60 m and the
  ambient weight 0.3-0.8 (your current value is always tried too). Every other setting stays.
- Only settings that estimate low on average may win (the mean of estimate minus actual is zero
  or less); among them the lowest mean absolute daily error wins. Ties go to the lower estimate,
  then to the value you already have, then to the smaller value. A dial the trial days can't tell
  apart therefore isn't moved on no evidence.
- A day counts only when you noted its total and Baldur has commits of yours on it. Work Baldur
  can't see (a day of design talk) is the same error under every setting, so it can't teach the
  dials anything, and it would let a setting that estimates high pass as one that runs low.
- Nothing changes until you accept, and accepting needs MIN_DAYS days. Past estimate runs keep
  the settings they recorded.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from asgard import muninn

from . import estimate as E
from . import settings as config
from . import store
from .settings import Settings

GAPS = (60, 75, 90, 105, 120, 135, 150, 165, 180)
LEAD_INS = (0, 15, 30, 45, 60)
WEIGHTS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8)
MIN_DAYS = 10                  # two working weeks, the shorter of the spec's trial lengths
DIALS = ("idle_gap_minutes", "lead_in_minutes", "ambient_weight")
DIAL_NAMES = {"idle_gap_minutes": "idle gap", "lead_in_minutes": "lead-in", "ambient_weight": "ambient weight"}
_EPS = 1e-6


class CalibrationError(ValueError):
    """Calibration can't do what was asked; the message says why and what to do."""


# --------------------------------------------------------------------------
# Real hours
# --------------------------------------------------------------------------

def note(con: sqlite3.Connection, day: dt.date, minutes: int, key: Optional[str] = None,
         text: Optional[str] = None, today: Optional[dt.date] = None) -> str:
    """Record real development minutes for a day (key None: the day's total) or one ticket.

    Returns 'added' or 'changed'. A day's ticket figures may not add up to more than its total.
    """
    today = today or dt.date.today()
    if day > today:
        raise CalibrationError(f"{day.isoformat()} hasn't happened yet; note real hours once the day is over.")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 1440:
        raise CalibrationError("Real time for a day is a whole number of minutes from 0 to 1440.")
    if key is not None:
        try:
            key = muninn.normalize_key(key)
        except ValueError as exc:
            raise CalibrationError(str(exc)) from None
    text = muninn.scrub((text or "").strip()) or None
    if text and len(text) > 200:
        raise CalibrationError("Keep the note under 200 characters.")
    with muninn.transaction(con):
        changed = con.execute("UPDATE time_actuals SET minutes = ?, note = ? WHERE on_date = ? AND work_item_key IS ?",
                              (minutes, text, day.isoformat(), key)).rowcount
        if not changed:
            con.execute("INSERT INTO time_actuals (on_date, minutes, work_item_key, note) VALUES (?, ?, ?, ?)",
                        (day.isoformat(), minutes, key, text))
        total, parts = con.execute(
            "SELECT max(CASE WHEN work_item_key IS NULL THEN minutes END), "
            "coalesce(sum(CASE WHEN work_item_key IS NOT NULL THEN minutes END), 0) "
            "FROM time_actuals WHERE on_date = ?", (day.isoformat(),)).fetchone()
        if total is not None and parts > total:
            raise CalibrationError(f"The tickets you noted for {day.isoformat()} add up to {E.fmt(parts)}, more than "
                                   f"the day's total of {E.fmt(total)}. Fix one of them.")
    return "changed" if changed else "added"


def forget(con: sqlite3.Connection, day: dt.date, key: Optional[str] = None) -> bool:
    """Remove a noted figure. Returns whether there was one."""
    if key is not None:
        try:
            key = muninn.normalize_key(key)
        except ValueError as exc:
            raise CalibrationError(str(exc)) from None
    with muninn.transaction(con):
        return con.execute("DELETE FROM time_actuals WHERE on_date = ? AND work_item_key IS ?",
                           (day.isoformat(), key)).rowcount > 0


def actuals(con: sqlite3.Connection, first: Optional[dt.date] = None,
            last: Optional[dt.date] = None) -> Dict[dt.date, Dict[Optional[str], int]]:
    """{day: {None: day total, key: ticket minutes}} for the days you noted, oldest first."""
    lo = first.isoformat() if first else "0000-01-01"
    hi = last.isoformat() if last else "9999-12-31"
    out: Dict[dt.date, Dict[Optional[str], int]] = {}
    for r in con.execute("SELECT on_date, work_item_key, minutes FROM time_actuals WHERE on_date BETWEEN ? AND ? "
                         "ORDER BY on_date, work_item_key IS NOT NULL, work_item_key", (lo, hi)):
        out.setdefault(dt.date.fromisoformat(r[0]), {})[r[1]] = int(r[2])
    return out


# --------------------------------------------------------------------------
# The fit
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Dials:
    idle_gap_minutes: int
    lead_in_minutes: int
    ambient_weight: float

    @classmethod
    def of(cls, settings: Settings) -> "Dials":
        v = settings.values
        return cls(int(v["idle_gap_minutes"]), int(v["lead_in_minutes"]), float(v["ambient_weight"]))

    def text(self) -> str:
        return f"gap {self.idle_gap_minutes}m, lead-in {self.lead_in_minutes}m, weight {self.ambient_weight:g}"


@dataclass
class Score:
    dials: Dials
    mae: float                         # mean absolute daily error, minutes
    bias: float                        # mean of (estimate - real), minutes; zero or less runs low
    estimates: Dict[dt.date, int]      # each counted day's estimated development, minutes
    ticket_mae: Optional[float] = None  # the same error over the tickets you noted, if any

    @property
    def runs_low(self) -> bool:
        return self.bias <= _EPS


@dataclass
class Fit:
    actual: Dict[dt.date, int]         # the counted days: your noted total for each
    left_out: List[dt.date]            # days you noted that Baldur has no commits for
    current: Score
    best: Optional[Score]              # None: no setting estimates low on average
    tried: int
    unmeasured: List[str] = field(default_factory=list)   # dials these days can't tell apart

    @property
    def days(self) -> List[dt.date]:
        return sorted(self.actual)

    @property
    def enough(self) -> bool:
        return len(self.actual) >= MIN_DAYS

    @property
    def changes(self) -> bool:
        return self.best is not None and self.best.dials != self.current.dials


def _values(standard: Sequence, current) -> List:
    return sorted(set(standard) | {current})


def _params(p: E.Params, d: Dials) -> E.Params:
    return dataclasses.replace(p, idle_gap_minutes=d.idle_gap_minutes, lead_in_minutes=d.lead_in_minutes,
                               ambient_weight=d.ambient_weight)


def _day_totals(est: E.Estimate) -> Tuple[Dict[dt.date, int], Dict[Tuple[dt.date, str], int]]:
    days: Dict[dt.date, int] = {}
    tickets: Dict[Tuple[dt.date, str], int] = {}
    for x in est.proposals:
        days[x.local_date] = days.get(x.local_date, 0) + x.minutes_proposed     # untracked counts: it's development
        if x.key:
            tickets[(x.local_date, x.key)] = x.minutes_proposed
    return days, tickets


def fit(con: sqlite3.Connection, settings: Settings, first: Optional[dt.date] = None,
        last: Optional[dt.date] = None, now: Optional[dt.datetime] = None) -> Fit:
    """Try every setting in the grid on the days you noted; the result says which fits best."""
    noted = actuals(con, first, last)
    totals = {day: figures[None] for day, figures in noted.items() if None in figures}
    if not totals:
        raise CalibrationError("No real hours noted yet. Note each day's development time first: "
                               "cli.py actual DATE TIME")
    current = Dials.of(settings)
    base = E.Params.from_settings(settings)
    grid = [Dials(g, ld, w) for g, ld, w in itertools.product(
        _values(GAPS, current.idle_gap_minutes), _values(LEAD_INS, current.lead_in_minutes),
        _values(WEIGHTS, current.ambient_weight))]
    widest = dataclasses.replace(base, idle_gap_minutes=max(d.idle_gap_minutes for d in grid),
                                 lead_in_minutes=max(d.lead_in_minutes for d in grid))
    inputs = store.load(con, min(totals), max(totals), widest, settings.project_keys)
    worked = {E.local_day(c.at) for c in inputs.commits}
    actual = {day: m for day, m in totals.items() if day in worked}
    left_out = sorted(day for day in totals if day not in worked)
    if not actual:
        raise CalibrationError("Baldur has no commits of yours on the days you noted, so there's nothing to "
                               "calibrate yet. Run cli.py collect, or note days you committed on.")
    renamed = store.current_keys(con)
    noted_tickets = {(day, renamed.get(k, k)): m for day, figures in noted.items() if day in actual
                     for k, m in figures.items() if k is not None}
    days = sorted(actual)

    def score(d: Dials) -> Score:
        est = E.estimate(inputs.commits, inputs.checkouts, inputs.meetings, days, _params(base, d),
                         calendar=inputs.calendar, calendar_synced=inputs.calendar_synced,
                         reflog_since=inputs.reflog_since, prior_patches=inputs.prior_patches, now=now,
                         copies=inputs.copies)
        per_day, per_ticket = _day_totals(est)
        errors = [per_day.get(day, 0) - actual[day] for day in days]
        tickets = [abs(per_ticket.get(k, 0) - m) for k, m in noted_tickets.items()]
        return Score(d, sum(abs(e) for e in errors) / len(errors), sum(errors) / len(errors),
                     {day: per_day.get(day, 0) for day in days}, sum(tickets) / len(tickets) if tickets else None)

    scores = [score(d) for d in grid]
    by_dials = {s.dials: s for s in scores}

    def order(s: Score) -> Tuple:
        # Lowest error; then the lower estimate; then the value you have; then the smaller value.
        prefer = tuple(x for name in DIALS for x in (getattr(s.dials, name) != getattr(current, name),
                                                      getattr(s.dials, name)))
        return (round(s.mae, 6), round(s.bias, 6)) + prefer

    low = sorted((s for s in scores if s.runs_low), key=order)
    best = low[0] if low else None
    unmeasured = []
    anchor = best.dials if best else current
    for name, values in zip(DIALS, (GAPS, LEAD_INS, WEIGHTS)):
        seen = {tuple(sorted(by_dials[dataclasses.replace(anchor, **{name: v})].estimates.items()))
                for v in _values(values, getattr(current, name))}
        if len(seen) == 1:
            unmeasured.append(name)
    return Fit(actual, left_out, by_dials[current], best, len(grid), unmeasured)


def accept(con: sqlite3.Connection, settings: Settings, result: Fit) -> int:
    """Make the best fit your settings, with an active calibration_runs row. Returns its id.

    baldur.json is written first, then Muninn in one transaction; if Muninn refuses, the file is
    put back, so the settings and the active calibration never disagree.
    """
    if not result.enough:
        raise CalibrationError(f"Calibration needs real hours for at least {MIN_DAYS} days with commits; you have "
                               f"{len(result.actual)}. Keep noting them: cli.py actual DATE TIME")
    if result.best is None:
        raise CalibrationError("No setting in the search estimates low on average against your notes, so none can "
                               "be accepted. Check the hours you noted (cli.py actuals).")
    if not result.changes:
        raise CalibrationError("Your current settings already fit best; there's nothing to change.")
    if settings.broken:
        raise CalibrationError("baldur.json has a mistake in it; fix it before accepting a calibration.")
    best = result.best
    path = config.settings_path()
    before = path.read_bytes() if path.exists() else None
    config.update({name: getattr(best.dials, name) for name in DIALS})
    try:
        with muninn.transaction(con):
            previous = con.execute("SELECT id FROM calibration_runs WHERE is_active = 1").fetchone()
            con.execute("UPDATE calibration_runs SET is_active = 0 WHERE is_active = 1")
            cid = int(con.execute(
                "INSERT INTO calibration_runs (gap_minutes, lead_in_minutes, ambient_weight, days_used, "
                "mean_abs_error_min, bias_min, is_active) VALUES (?, ?, ?, ?, ?, ?, 1) RETURNING id",
                (best.dials.idle_gap_minutes, best.dials.lead_in_minutes, best.dials.ambient_weight,
                 len(result.actual), round(best.mae, 3), min(0.0, round(best.bias, 3)))).fetchone()[0])
            muninn.emit(con, "baldur", "calibration.accepted", "calibration_runs", cid, None, {
                "days": len(result.actual), "from": result.current.dials.text(), "to": best.dials.text(),
                "error_before": round(result.current.mae, 1), "error_after": round(best.mae, 1),
                "replaced": int(previous[0]) if previous else None})
    except BaseException:
        _put_back(path, before)
        raise
    return cid


def _put_back(path, content: Optional[bytes]) -> None:
    """Restore baldur.json exactly as it was (or remove it, if it didn't exist)."""
    if content is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    tmp = path.with_suffix(".json.tmp")
    tmp.write_bytes(content)
    tmp.replace(path)


active = store.active_calibration      # the active calibration, while your settings still match it
