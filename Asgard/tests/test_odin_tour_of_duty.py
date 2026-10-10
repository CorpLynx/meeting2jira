"""Tour of duty: classifying meetings against scheduled working hours.

Tour of duty deliberately does NOT decide which days get scanned. The scan window is safe to
widen because the state database makes re-runs idempotent, so "did I miss a late meeting" is a
question for -DaysBack, not for this. What this decides is how an out-of-tour meeting is treated.
"""
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "apps" / "odin"
for folder in (ROOT, APP):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from odin.config import ConfigError, build_config  # noqa: E402
from odin.models import Meeting  # noqa: E402
from odin.rules import INSIDE, OUTSIDE, PARTIAL, UNKNOWN, Router, TourOfDuty  # noqa: E402
from odin.state import State  # noqa: E402
from odin.sync import run  # noqa: E402

NOW = datetime(2099, 1, 1, tzinfo=timezone.utc)   # far future, so nothing is "not ended yet"

WEEKDAY_TOUR = {
    "enabled": True,
    "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
    "start": "07:00",
    "end": "15:30",
    "grace_minutes": 15,
    "outside_action": "label",
    "outside_label": "outside-tod",
}


def config(tour=None, **jira_overrides):
    jira = {"base_url": "https://j.example.gov", "default_parent": "PROJ-9", "assign_to_me": False}
    jira.update(jira_overrides)
    body = {"jira": jira}
    if tour is not None:
        body["tour_of_duty"] = tour
    return build_config(body)


def meeting_at(day, hhmm, minutes=60, subject="Status sync"):
    """A meeting whose LOCAL wall-clock start is `hhmm` on 2026-09-<day>."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    start = datetime(2026, 9, day, hour, minute).astimezone()
    return Meeting(source="outlook-com", key=f"GID-{day}-{hhmm}", subject=subject,
                   start_utc=start, end_utc=start + timedelta(minutes=minutes))


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tod = TourOfDuty(config(WEEKDAY_TOUR))

    def test_disabled_by_default(self):
        plain = TourOfDuty(config())
        self.assertFalse(plain.enabled)
        self.assertEqual(plain.classify(meeting_at(21, "22:00")), UNKNOWN)
        self.assertEqual(plain.minutes_outside(meeting_at(21, "22:00")), 0)

    def test_inside_partial_and_outside(self):
        # 2026-09-21 is a Monday.
        cases = [
            ("09:00", 60, INSIDE, 0),
            ("07:00", 510, INSIDE, 0),     # exactly the whole tour, 07:00-15:30
            ("06:50", 30, INSIDE, 0),      # early, but inside the 15-minute grace
            ("06:00", 30, OUTSIDE, 30),    # before the tour starts
            ("18:00", 60, OUTSIDE, 60),    # evening
            ("15:00", 60, PARTIAL, 15),    # ran past the end of the tour
        ]
        for hhmm, minutes, expected, outside in cases:
            m = meeting_at(21, hhmm, minutes)
            with self.subTest(start=hhmm, minutes=minutes):
                self.assertEqual(self.tod.classify(m), expected)
                self.assertEqual(self.tod.minutes_outside(m), outside)

    def test_non_working_day_is_outside(self):
        saturday = meeting_at(26, "10:00", 60)
        self.assertEqual(saturday.start_local.weekday(), 5)
        self.assertEqual(self.tod.classify(saturday), OUTSIDE)
        self.assertEqual(self.tod.minutes_outside(saturday), 60)

    def test_overnight_tour_spanning_midnight(self):
        night = TourOfDuty(config(dict(WEEKDAY_TOUR, start="22:00", end="06:00", grace_minutes=0)))
        # Monday 23:00 for 90 minutes runs into Tuesday: still inside the Monday tour.
        self.assertEqual(night.classify(meeting_at(21, "23:00", 90)), INSIDE)
        # Tuesday 05:00 belongs to the tour that began Monday night, not Tuesday's.
        self.assertEqual(night.classify(meeting_at(22, "05:00", 30)), INSIDE)
        self.assertEqual(night.classify(meeting_at(22, "12:00", 60)), OUTSIDE)
        self.assertEqual(night.classify(meeting_at(21, "22:00", 480)), INSIDE)

    def test_grace_of_zero_is_strict(self):
        strict = TourOfDuty(config(dict(WEEKDAY_TOUR, grace_minutes=0)))
        self.assertEqual(strict.classify(meeting_at(21, "06:50", 30)), PARTIAL)
        self.assertEqual(strict.minutes_outside(meeting_at(21, "06:50", 30)), 10)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _created(self, tour, meetings):
        recorded = []

        class Jira:
            def create_issue(self, fields):
                recorded.append(fields)
                return "PROJ-%d" % (500 + len(recorded))

            def add_worklog(self, *a):
                return "1"

            def transition(self, *a):
                return True

        with State(self.tmp / "state.db") as state:
            result = run(meetings, config(tour), state, Jira(), now=NOW)
        return result, recorded

    def test_include_treats_out_of_tour_like_any_other_meeting(self):
        tour = dict(WEEKDAY_TOUR, outside_action="include")
        result, created = self._created(tour, [meeting_at(21, "20:00")])
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["parent"]["key"], "PROJ-9")
        self.assertNotIn("outside-tod", created[0]["labels"])

    def test_label_marks_it_but_still_creates_it(self):
        result, created = self._created(WEEKDAY_TOUR, [meeting_at(21, "20:00")])
        self.assertEqual(len(created), 1)
        self.assertIn("outside-tod", created[0]["labels"])
        self.assertEqual(created[0]["parent"]["key"], "PROJ-9")

    def test_route_sends_it_to_the_comp_time_issue(self):
        tour = dict(WEEKDAY_TOUR, outside_action="route", outside_parent="OT-1")
        result, created = self._created(tour, [meeting_at(21, "20:00"), meeting_at(21, "09:00")])
        parents = sorted(f["parent"]["key"] for f in created)
        self.assertEqual(parents, ["OT-1", "PROJ-9"])

    def test_route_wins_over_a_matching_rule(self):
        """Where out-of-tour time is recorded is a timekeeping decision, not a subject match."""
        tour = dict(WEEKDAY_TOUR, outside_action="route", outside_parent="OT-1")
        cfg = config(tour)
        cfg["rules"] = [{"name": "status syncs", "match": {"subject_regex": "(?i)status"},
                         "parent": "PROJ-200"}]
        router = Router(cfg)
        evening = router.decide(meeting_at(21, "20:00"), NOW)
        workday = router.decide(meeting_at(21, "09:00"), NOW)
        self.assertEqual((evening.parent, evening.tod_status), ("OT-1", OUTSIDE))
        self.assertEqual((workday.parent, workday.tod_status), ("PROJ-200", INSIDE))

    def test_skip_excludes_it_with_a_clear_reason(self):
        tour = dict(WEEKDAY_TOUR, outside_action="skip")
        result, created = self._created(tour, [meeting_at(21, "20:00"), meeting_at(21, "09:00")])
        self.assertEqual(len(created), 1)
        self.assertEqual(result.skipped["outside tour of duty"], 1)

    def test_partial_meetings_are_never_dropped(self):
        """A meeting that ran past the end of the tour is still work."""
        tour = dict(WEEKDAY_TOUR, outside_action="skip")
        result, created = self._created(tour, [meeting_at(21, "15:00", 60)])
        self.assertEqual(len(created), 1)
        self.assertEqual(result.skipped["outside tour of duty"], 0)

    def test_route_parent_is_checked_for_existence(self):
        tour = dict(WEEKDAY_TOUR, outside_action="route", outside_parent="OT-1")
        self.assertIn("OT-1", Router(config(tour)).parents())

    def test_template_fields_are_available(self):
        cfg = config(WEEKDAY_TOUR)
        cfg["templates"]["description"] = "{tod_status} / {minutes_outside_tod} min outside"
        recorded = []

        class Jira:
            def create_issue(self, fields):
                recorded.append(fields)
                return "PROJ-501"

        with State(self.tmp / "state.db") as state:
            run([meeting_at(21, "15:00", 60)], cfg, state, Jira(), now=NOW)
        self.assertEqual(recorded[0]["description"], "partial / 15 min outside")


class ValidationTests(unittest.TestCase):
    def test_rejects_bad_settings(self):
        cases = {
            "bad start time": dict(WEEKDAY_TOUR, start="7am"),
            "hour out of range": dict(WEEKDAY_TOUR, start="25:00"),
            "bad day name": dict(WEEKDAY_TOUR, days=["Mon", "Funday"]),
            "days not a list": dict(WEEKDAY_TOUR, days="Mon"),
            "empty days while enabled": dict(WEEKDAY_TOUR, days=[]),
            "unknown action": dict(WEEKDAY_TOUR, outside_action="delete"),
            "route with no parent": dict(WEEKDAY_TOUR, outside_action="route"),
            "route with a bad parent": dict(WEEKDAY_TOUR, outside_action="route",
                                            outside_parent="not-a-key"),
            "label with a space": dict(WEEKDAY_TOUR, outside_label="outside tod"),
            "negative grace": dict(WEEKDAY_TOUR, grace_minutes=-5),
        }
        for name, tour in cases.items():
            with self.subTest(case=name), self.assertRaises(ConfigError):
                config(tour)

    def test_accepts_a_reasonable_tour(self):
        cfg = config(dict(WEEKDAY_TOUR, days=["monday", "TUE", "Wed"], start="06:30", end="17:00"))
        tod = TourOfDuty(cfg)
        self.assertEqual(tod.days, {0, 1, 2})


if __name__ == "__main__":
    unittest.main()
