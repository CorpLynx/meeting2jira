"""Tests for the OWA -> schema-v1 mapping.

These run anywhere: no Playwright, no browser, no network, no mailbox. That is the whole reason
mapping.py imports nothing but the standard library - the conversion is the part most likely to be
wrong, so it has to be cheap to test.

    cd playwright-app && python -m unittest discover -s tests -v
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from owa.mapping import (MappingError, build_export, iso_utc, map_event, occurrence_key,
                         parse_graph_datetime, window)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_sample():
    return json.loads((FIXTURES / "owa_events_sample.json").read_text(encoding="utf-8"))


def find(events, subject_fragment):
    for event in events:
        if subject_fragment.lower() in str(event.get("subject", "")).lower():
            return event
    raise AssertionError(f"no fixture event matching {subject_fragment!r}")


class DateTimeTests(unittest.TestCase):
    def test_graph_seven_digit_fraction(self):
        """Graph pads fractional seconds to 7 digits, which fromisoformat rejects before 3.11."""
        parsed = parse_graph_datetime({"dateTime": "2026-09-22T13:30:00.0000000", "timeZone": "UTC"})
        self.assertEqual(iso_utc(parsed), "2026-09-22T13:30:00Z")

    def test_accepts_offset_and_z_forms(self):
        self.assertEqual(iso_utc(parse_graph_datetime("2026-09-22T13:30:00Z")),
                         "2026-09-22T13:30:00Z")
        self.assertEqual(iso_utc(parse_graph_datetime("2026-09-22T09:30:00-04:00")),
                         "2026-09-22T13:30:00Z")

    def test_naive_non_utc_is_refused_rather_than_guessed(self):
        """Guessing an offset would silently shift every meeting. Windows has no tz database."""
        with self.assertRaises(MappingError) as ctx:
            parse_graph_datetime({"dateTime": "2026-09-23T09:00:00", "timeZone": "Eastern Standard Time"})
        self.assertIn("outlook.timezone", str(ctx.exception))

    def test_missing_datetime(self):
        with self.assertRaises(MappingError):
            parse_graph_datetime({"timeZone": "UTC"})


class KeyTests(unittest.TestCase):
    def test_recurring_instances_get_distinct_keys(self):
        """Both standup instances share an iCalUId; they must not collapse into one meeting.

        This is the classic recurring-meeting bug: key on the series and a daily standup appears
        once and never again.
        """
        events = load_sample()
        first, second = events[0], events[1]
        self.assertEqual(first["iCalUId"], second["iCalUId"])
        key_one = occurrence_key(first, parse_graph_datetime(first["start"]))
        key_two = occurrence_key(second, parse_graph_datetime(second["start"]))
        self.assertNotEqual(key_one, key_two)

    def test_key_is_stable_across_runs(self):
        event = load_sample()[0]
        start = parse_graph_datetime(event["start"])
        self.assertEqual(occurrence_key(event, start), occurrence_key(event, start))

    def test_key_survives_a_missing_icaluid(self):
        event = dict(load_sample()[0])
        del event["iCalUId"]
        key = occurrence_key(event, parse_graph_datetime(event["start"]))
        self.assertIn(event["id"], key)

    def test_no_identity_at_all_is_an_error(self):
        with self.assertRaises(MappingError):
            occurrence_key({}, datetime(2026, 9, 22, tzinfo=timezone.utc))

    def test_the_calendar_id_is_the_key_before_its_start(self):
        """Odin recognises a one-off meeting that moved by this id (export field global_id)."""
        for event in load_sample()[:2]:
            item = map_event(event)
            self.assertEqual(item["global_id"], "owa:" + event["iCalUId"])
            self.assertEqual(item["key"], item["global_id"] + "|" + item["start_utc"])

    def test_without_a_series_type_it_is_unknown_whether_it_recurs(self):
        """The captured samples have no `type`, so Odin treats these meetings as it always has."""
        event = load_sample()[0]
        self.assertNotIn("type", event)
        self.assertIsNone(map_event(event)["is_recurring"])
        self.assertIs(map_event(dict(event, Type="SingleInstance"))["is_recurring"], False)


class FieldMappingTests(unittest.TestCase):
    """The filters in rules.py depend on these exact values, so each is pinned."""

    def setUp(self):
        self.events = load_sample()

    def test_teams_detection_uses_the_explicit_flag(self):
        """Graph states this outright; the COM path can only match on the location string."""
        mapped = map_event(find(self.events, "Daily Standup"))
        self.assertTrue(mapped["is_teams"])

    def test_response_status_vocabulary(self):
        self.assertEqual(map_event(find(self.events, "Vendor demo"))["response"], "declined")
        self.assertEqual(map_event(find(self.events, "training"))["response"], "tentative")
        self.assertEqual(map_event(find(self.events, "Dentist"))["response"], "organizer")

    def test_busy_status_vocabulary(self):
        self.assertEqual(map_event(find(self.events, "Vendor demo"))["busy_status"], "free")
        self.assertEqual(map_event(find(self.events, "Dentist"))["busy_status"], "oof")

    def test_private_sensitivity(self):
        self.assertTrue(map_event(find(self.events, "Dentist"))["is_private"])
        self.assertFalse(map_event(find(self.events, "Daily Standup"))["is_private"])

    def test_appointment_with_no_attendees_is_not_a_meeting(self):
        self.assertFalse(map_event(find(self.events, "Dentist"))["is_meeting"])
        self.assertTrue(map_event(find(self.events, "Daily Standup"))["is_meeting"])

    def test_missing_attendees_key_is_treated_as_unknown_not_empty(self):
        """A projection that omits attendees must not make every meeting look like an appointment."""
        event = dict(find(self.events, "Daily Standup"))
        del event["attendees"]
        self.assertTrue(map_event(event)["is_meeting"])

    def test_cancelled_by_flag_and_by_subject(self):
        self.assertTrue(map_event(find(self.events, "Architecture review"))["is_cancelled"])
        event = dict(find(self.events, "Daily Standup"))
        event["isCancelled"] = False
        event["subject"] = "Cancelled: Daily Standup"
        self.assertTrue(map_event(event)["is_cancelled"])

    def test_all_day_and_categories(self):
        offsite = map_event(find(self.events, "offsite"))
        self.assertTrue(offsite["all_day"])
        self.assertEqual(map_event(find(self.events, "training"))["categories"], ["Training"])

    def test_organizer_is_withheld_unless_asked_for(self):
        standup = find(self.events, "Daily Standup")
        self.assertIsNone(map_event(standup)["organizer"])
        self.assertEqual(map_event(standup, include_organizer=True)["organizer"], "Alex Kim")

    def test_end_before_start_is_refused(self):
        event = dict(find(self.events, "Daily Standup"))
        event["end"] = {"dateTime": "2026-09-22T13:00:00.0000000", "timeZone": "UTC"}
        with self.assertRaises(MappingError):
            map_event(event)


class ExportEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.events = load_sample()
        self.start = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
        self.end = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)

    def test_envelope_matches_schema_v1(self):
        doc = build_export(self.events, self.start, self.end)
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(doc["source"], "owa-playwright")
        self.assertEqual(doc["range_start"], "2026-09-21T04:00:00Z")
        for field in ("exported_at", "range_end", "meetings"):
            self.assertIn(field, doc)

    def test_bad_items_are_skipped_not_fatal(self):
        """One odd calendar item must not cost the user the whole day's meetings."""
        skipped = []
        doc = build_export(self.events, self.start, self.end,
                           on_skip=lambda i, why: skipped.append(why))
        subjects = [m["subject"] for m in doc["meetings"]]
        self.assertNotIn("Malformed item with no end time", subjects)
        self.assertIn("Daily Standup", subjects)
        self.assertEqual(len(skipped), 2)   # the missing end time, and the non-UTC event

    def test_duplicate_occurrences_are_collapsed(self):
        doubled = self.events + self.events
        doc = build_export(doubled, self.start, self.end)
        keys = [m["key"] for m in doc["meetings"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_meetings_are_sorted_by_start(self):
        doc = build_export(self.events, self.start, self.end)
        starts = [m["start_utc"] for m in doc["meetings"]]
        self.assertEqual(starts, sorted(starts))

    def test_every_field_the_pipeline_reads_is_present(self):
        """Missing a key here surfaces as a confusing failure inside the push, not here."""
        required = {"key", "subject", "start_utc", "end_utc", "all_day", "is_meeting",
                    "is_cancelled", "response", "busy_status", "is_private", "location",
                    "categories", "organizer", "is_teams"}
        doc = build_export(self.events, self.start, self.end)
        self.assertTrue(doc["meetings"])
        for meeting in doc["meetings"]:
            self.assertEqual(required - set(meeting), set())


class WindowTests(unittest.TestCase):
    def test_days_back_covers_whole_local_days(self):
        now = datetime(2026, 9, 24, 16, 45, tzinfo=timezone.utc)
        start, end = window(1, now=now)
        self.assertEqual(end, now)
        self.assertLessEqual(start, now - timedelta(hours=16))

    def test_zero_days_back_is_today_only(self):
        now = datetime(2026, 9, 24, 16, 45, tzinfo=timezone.utc)
        start, _ = window(0, now=now)
        self.assertLessEqual(start, now)


if __name__ == "__main__":
    unittest.main()


def to_pascal(value):
    """Recase every key the way the Outlook endpoint does: first letter upper, rest untouched.

    subject -> Subject, isAllDay -> IsAllDay, dateTime -> DateTime. That is exactly the
    transformation Microsoft describes between the Graph and Outlook endpoints.
    """
    if isinstance(value, dict):
        return {key[:1].upper() + key[1:]: to_pascal(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_pascal(item) for item in value]
    return value


class PropertyCasingTests(unittest.TestCase):
    """The same calendar, served in either casing, must map to byte-identical output.

    Graph (graph.microsoft.com) returns camelCase; the Outlook endpoint (outlook.office.com/api)
    returns PascalCase, and Microsoft documents this as the notable difference when moving between
    them. This exporter discovers its endpoint by watching the browser instead of choosing it, so it
    does not get to assume which casing it will be handed.

    Found by research, not by a failing run: an independent project that reads the same OWA calendar
    (yusufaltunbicak/outlook-cli) reads Subject, Start.DateTime, ShowAs, Sensitivity and
    ResponseStatus.Response in PascalCase, against outlook.office.com/api/v2.0. The mapping here was
    camelCase-only, so on such a tenant every event would have been skipped as unmappable and the run
    would have reported an empty calendar.

    Reference: https://learn.microsoft.com/en-us/outlook/rest/compare-graph
    """

    def test_pascal_case_maps_identically_to_camel_case(self):
        events = load_sample()
        for event in events:
            camel = None
            try:
                camel = map_event(event, include_organizer=True)
            except MappingError:
                pass                     # the deliberately malformed fixture item
            pascal_event = to_pascal(event)
            if camel is None:
                with self.assertRaises(MappingError):
                    map_event(pascal_event, include_organizer=True)
                continue
            self.assertEqual(camel, map_event(pascal_event, include_organizer=True))

    def test_the_filter_fields_survive_recasing(self):
        """The fields whose loss would be silent rather than loud, asserted by name.

        If start went missing the event would be skipped and someone would notice. If Sensitivity or
        ResponseStatus went missing, a private or declined meeting would be pushed to Jira and the
        run would report success - so these are checked explicitly rather than only in aggregate.
        """
        events = load_sample()
        private = to_pascal(find(events, "Dentist"))        # sensitivity: private
        declined = to_pascal(find(events, "Vendor demo"))   # responseStatus.response: declined
        self.assertTrue(map_event(private)["is_private"])
        self.assertEqual(map_event(declined)["response"], "declined")

    def test_mixed_casing_in_one_payload(self):
        """A projection need not be uniform, so the lookup is per-field rather than per-payload."""
        event = {
            "Subject": "Mixed casing sync",
            "start": {"DateTime": "2026-09-22T13:00:00", "timeZone": "UTC"},
            "End": {"dateTime": "2026-09-22T13:30:00", "TimeZone": "UTC"},
            "iCalUId": "mixed-1",
            "Sensitivity": "Private",
            "showAs": "Busy",
        }
        mapped = map_event(event)
        self.assertEqual(mapped["subject"], "Mixed casing sync")
        self.assertEqual(mapped["start_utc"], "2026-09-22T13:00:00Z")
        self.assertEqual(mapped["end_utc"], "2026-09-22T13:30:00Z")
        self.assertTrue(mapped["is_private"])
        self.assertEqual(mapped["busy_status"], "busy")
