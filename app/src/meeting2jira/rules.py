"""Decide, per meeting, whether to create a sub-task and under which parent.

Position in the flow
    sync.py calls Router.decide once per meeting and does what it says. All the judgement lives
    here; sync.py only executes. Nothing in this module touches Jira or the network.

Order of evaluation
  1. filters         hard exclusions: cancelled, all-day, not ended yet, declined, private,
                     shown as free, too short/long, subject or organizer text, category
  2. tour of duty    a meeting wholly outside working hours may be skipped or rerouted
  3. rules           first match wins: route to a parent, or skip
  4. default_parent

Two ordering decisions worth knowing
    * Filters run cheapest-first, so a meeting that is both declined and contains "OOO" reports
      "declined". Tests asserting a skip reason must disable whatever would fire earlier.
    * Tour-of-duty `route`/`skip` is applied *before* rules, because where time worked outside a
      tour gets recorded is a timekeeping decision and a subject match should not override it.

Tour of duty
    Comparisons are minutes-since-local-midnight, deliberately not datetime arithmetic. That keeps
    it a pure wall-clock question ("was this during my working hours?") and needs no timezone
    database, which matters because Windows has none without the tzdata package. Overnight tours
    are handled by also considering the previous day's window.

    A meeting that merely overruns the end of a tour is `partial` and treated as inside it. Work
    that ran late is still work and must not be silently dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Sequence

from .config import TEXT_FILTER_KEYS, ConfigError, parse_hhmm
from .models import Meeting

MATCH_KEYS = {"subject_regex", "organizer_regex", "location_regex", "category", "is_teams"}

MINUTES_PER_DAY = 24 * 60
# Monday is 0, to line up with datetime.weekday().
WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

INSIDE = "inside"
PARTIAL = "partial"
OUTSIDE = "outside"
UNKNOWN = "unknown"      # tour of duty not configured


class TourOfDuty:
    """Classifies a meeting against the user's scheduled working hours.

    Everything is computed as minutes since local midnight rather than with datetime arithmetic.
    That keeps the comparison a pure wall-clock question ("was this during my working hours?"),
    which is what a tour of duty actually means, and avoids DST and tzinfo subtleties on a machine
    with no tz database available.
    """

    def __init__(self, cfg: Dict[str, Any]):
        tod = cfg.get("tour_of_duty") or {}
        self.enabled = bool(tod.get("enabled"))
        self.action = str(tod.get("outside_action") or "label")
        self.label = str(tod.get("outside_label") or "outside-tod")
        self.parent = tod.get("outside_parent")
        self.grace = int(tod.get("grace_minutes") or 0)
        self.start = parse_hhmm(tod.get("start") or "00:00")
        self.end = parse_hhmm(tod.get("end") or "00:00")
        self.days = {WEEKDAY_NAMES.index(str(d).strip().lower()[:3])
                     for d in (tod.get("days") or []) if str(d).strip()[:3].lower() in WEEKDAY_NAMES}

    def _windows(self, weekday: int):
        """Working windows relevant to a meeting starting on `weekday`, in minutes-of-day.

        Yields (start, end) where end may exceed 1440 for an overnight tour. The previous day's
        window is included, shifted negative, so an 05:00 meeting on a 22:00-06:00 tour is still
        recognised as inside it.
        """
        end = self.end if self.end > self.start else self.end + MINUTES_PER_DAY
        if weekday in self.days:
            yield self.start - self.grace, end + self.grace
        previous = (weekday - 1) % 7
        if previous in self.days and end > MINUTES_PER_DAY:
            yield self.start - self.grace - MINUTES_PER_DAY, end + self.grace - MINUTES_PER_DAY

    @staticmethod
    def _span(m: Meeting):
        """The meeting as (start, end) minutes-of-day, with end past 1440 if it crossed midnight."""
        start = m.start_local.hour * 60 + m.start_local.minute
        end = m.end_local.hour * 60 + m.end_local.minute
        if end < start or (end == start and m.minutes > 0):
            end += MINUTES_PER_DAY
        return start, end

    def overlap_minutes(self, m: Meeting) -> int:
        start, end = self._span(m)
        total = 0
        for window_start, window_end in self._windows(m.start_local.weekday()):
            total += max(0, min(end, window_end) - max(start, window_start))
        return total

    def classify(self, m: Meeting) -> str:
        if not self.enabled:
            return UNKNOWN
        overlap = self.overlap_minutes(m)
        if overlap <= 0:
            return OUTSIDE
        start, end = self._span(m)
        return INSIDE if overlap >= (end - start) else PARTIAL

    def minutes_outside(self, m: Meeting) -> int:
        if not self.enabled:
            return 0
        start, end = self._span(m)
        return max(0, (end - start) - self.overlap_minutes(m))


@dataclass
class Decision:
    action: str                   # "create" or "skip"
    parent: Optional[str] = None
    reason: str = ""
    tod_status: str = UNKNOWN     # inside | partial | outside | unknown


class _Rule:
    def __init__(self, raw: Dict[str, Any], index: int):
        self.name = raw.get("name") or f"rules[{index}]"
        match = raw.get("match") or {}
        unknown = set(match) - MATCH_KEYS
        if unknown:
            raise ConfigError(f"{self.name}: unknown match keys {sorted(unknown)}; allowed: {sorted(MATCH_KEYS)}")
        if not match:
            raise ConfigError(f"{self.name}: 'match' needs at least one condition")
        try:
            self.subject = re.compile(match["subject_regex"]) if "subject_regex" in match else None
            self.organizer = re.compile(match["organizer_regex"]) if "organizer_regex" in match else None
            self.location = re.compile(match["location_regex"]) if "location_regex" in match else None
        except re.error as exc:
            raise ConfigError(f"{self.name}: bad regex: {exc}") from None
        cats = match.get("category")
        cat_list = [cats] if isinstance(cats, str) else (cats or [])
        self.categories = {str(c).lower() for c in cat_list} or None
        self.is_teams = match.get("is_teams")
        self.skip = raw.get("skip") is True
        self.parent = raw.get("parent")

    def matches(self, m: Meeting) -> bool:
        if self.subject and not self.subject.search(m.subject):
            return False
        if self.organizer and not self.organizer.search(m.organizer or ""):
            return False
        if self.location and not self.location.search(m.location or ""):
            return False
        if self.categories and not (self.categories & {c.lower() for c in m.categories}):
            return False
        if self.is_teams is not None and bool(self.is_teams) != m.is_teams:
            return False
        return True


def _first_contained(text: str, needles: Sequence[str]) -> Optional[str]:
    """Return the first needle that appears in text (both already case-folded), else None."""
    low = (text or "").lower()
    for needle in needles:
        if needle and needle in low:
            return needle
    return None


class Router:
    def __init__(self, cfg: Dict[str, Any]):
        self.f = cfg["filters"]
        self.default_parent = cfg["jira"]["default_parent"]
        self.skip_patterns = [re.compile(p) for p in self.f.get("skip_subject_patterns") or []]
        self.rules = [_Rule(r, i) for i, r in enumerate(cfg.get("rules") or [])]
        self.tod = TourOfDuty(cfg)
        # Lower-cased once here so the per-meeting check stays a plain substring test.
        self.text_filters = {
            key: [str(s).strip().lower() for s in (self.f.get(key) or []) if str(s).strip()]
            for key in TEXT_FILTER_KEYS
        }

    def parents(self) -> set:
        found = {self.default_parent} | {r.parent for r in self.rules if not r.skip and r.parent}
        if self.tod.enabled and self.tod.action == "route" and self.tod.parent:
            found.add(self.tod.parent)   # so `check` verifies the comp-time issue too
        return found

    def filter_reason(self, m: Meeting, now: datetime) -> Optional[str]:
        f = self.f
        if f["skip_cancelled"] and m.is_cancelled:
            return "cancelled"
        if f["skip_all_day"] and m.all_day:
            return "all-day"
        if f["only_ended"] and m.end_utc > now:
            return "not ended yet"
        if f["meetings_only"] and not m.is_meeting:
            return "appointment (no attendees)"
        if f["teams_only"] and not m.is_teams:
            return "not a Teams meeting"
        if f["skip_declined"] and m.response == "declined":
            return "declined"
        if f["skip_not_responded"] and m.response in ("none", "not_responded"):
            return "not responded"
        if f["skip_tentative"] and m.response == "tentative":
            return "tentative"
        if f["skip_free"] and m.busy_status == "free":
            return "shown as free"
        if f["skip_private"] and m.is_private:
            return "private"
        if m.minutes < int(f["min_minutes"] or 0):
            return f"under {f['min_minutes']} min"
        if f["max_minutes"] and m.minutes > int(f["max_minutes"]):
            return f"over {f['max_minutes']} min"

        hit = _first_contained(m.subject, self.text_filters["skip_subject_contains"])
        if hit:
            return f"subject contains {hit!r}"
        hit = _first_contained(m.organizer or "", self.text_filters["skip_organizer_contains"])
        if hit:
            return f"organizer contains {hit!r}"
        hit = _first_contained(m.location or "", self.text_filters["skip_location_contains"])
        if hit:
            return f"location contains {hit!r}"
        excluded = self.text_filters["skip_categories"]
        if excluded:
            for category in m.categories:
                if category.strip().lower() in excluded:
                    return f"category {category.strip()!r}"

        for pattern in self.skip_patterns:
            if pattern.search(m.subject):
                return f"subject matches /{pattern.pattern}/"
        return None

    def decide(self, m: Meeting, now: datetime) -> Decision:
        reason = self.filter_reason(m, now)
        if reason:
            return Decision("skip", reason=reason)

        status = self.tod.classify(m)
        # A wholly-outside meeting is handled before the rules deliberately: where time worked
        # outside your tour gets recorded is a timekeeping decision, and a subject-matching rule
        # should not quietly send it to a project issue instead. "partial" is treated as inside,
        # so work that ran past the end of the tour is never dropped.
        if status == OUTSIDE:
            if self.tod.action == "skip":
                return Decision("skip", reason="outside tour of duty", tod_status=status)
            if self.tod.action == "route":
                return Decision("create", parent=self.tod.parent,
                                reason="outside tour of duty", tod_status=status)

        for rule in self.rules:
            if rule.matches(m):
                if rule.skip:
                    return Decision("skip", reason=f"rule '{rule.name}'", tod_status=status)
                return Decision("create", parent=rule.parent, reason=f"rule '{rule.name}'",
                                tod_status=status)
        return Decision("create", parent=self.default_parent, reason="default parent",
                        tod_status=status)
