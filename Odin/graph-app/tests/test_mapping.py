"""What Graph's mapping gives Odin to recognise a moved meeting: the calendar id and whether the
meeting is part of a series.

The rest of the mapping is tested in playwright-app/tests/test_mapping.py, on the copy that
test_mapping_drift.py proves is the same code.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fake_graph import _event  # noqa: E402
from graph import mapping  # noqa: E402


def with_type(kind):
    event = _event(1, "Design review")
    if kind is None:
        del event["type"]
    else:
        event["type"] = kind
    return event


class MovedMeetingFieldTests(unittest.TestCase):
    def test_a_one_off_meeting_has_its_calendar_id_and_isnt_recurring(self):
        event = with_type("singleInstance")
        item = mapping.map_event(event)
        self.assertEqual(item["global_id"], "graph:" + event["iCalUId"])
        self.assertTrue(item["key"].startswith(item["global_id"] + "|"))
        self.assertIs(item["is_recurring"], False)

    def test_an_occurrence_or_exception_of_a_series_is_recurring(self):
        for kind in ("occurrence", "exception", "seriesMaster"):
            self.assertIs(mapping.map_event(with_type(kind))["is_recurring"], True, kind)

    def test_no_type_or_one_it_doesnt_know_is_unknown(self):
        for kind in (None, "", "somethingNew"):
            self.assertIsNone(mapping.map_event(with_type(kind))["is_recurring"], kind)

    def test_moving_the_meeting_changes_its_key_but_not_its_calendar_id(self):
        before = with_type("singleInstance")
        after = dict(before, start={"dateTime": "2026-09-22T15:00:00.0000000", "timeZone": "UTC"},
                     end={"dateTime": "2026-09-22T15:30:00.0000000", "timeZone": "UTC"})
        one, two = mapping.map_event(before), mapping.map_event(after)
        self.assertNotEqual(one["key"], two["key"])
        self.assertEqual(one["global_id"], two["global_id"])

    def test_the_outlook_endpoints_pascal_case_type_reads_the_same(self):
        event = with_type(None)
        event["Type"] = "SingleInstance"
        self.assertIs(mapping.map_event(event)["is_recurring"], False)

    def test_the_client_asks_graph_for_the_type(self):
        from graph import client
        self.assertIn("type", client.SELECT_FIELDS)
        self.assertIn("iCalUId", client.SELECT_FIELDS)


if __name__ == "__main__":
    unittest.main()
