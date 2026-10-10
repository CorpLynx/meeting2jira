"""Recovery from an ambiguous issue-create failure (HANDOFF backlog P1-A).

A POST that times out or returns 502/503/504 may have been applied by Jira anyway. Retrying
blindly duplicates the sub-task; giving up duplicates it on the *next* run. So the create is
followed by an exact lookup for a deterministic marker label.
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_jira import FIXTURES, FakeJira, OdinTestCase  # noqa: E402

from odin import store  # noqa: E402
from odin.config import build_config  # noqa: E402
from odin.jira import JiraError  # noqa: E402
from odin.sources import load_export  # noqa: E402
from odin.sync import dedupe_label  # noqa: E402

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def one_meeting():
    """A single ended, unfiltered meeting, so each test creates exactly one sub-task."""
    meetings = [m for m in load_export(FIXTURES / "sample_export.json")
                if m.subject == "Sprint Planning" and m.key.startswith("GID-SPRINT|")]
    assert len(meetings) == 1, meetings
    return meetings


def config(**jira_overrides):
    jira = {"base_url": "https://j.example.gov", "default_parent": "PROJ-9", "assign_to_me": False}
    jira.update(jira_overrides)
    return build_config({"jira": jira})


class AmbiguousCreateJira(FakeJira):
    """Fails create with an ambiguous error (after it landed, if landed is set), then answers the search."""

    def __init__(self, search_result, search_raises=False, landed=None):
        super().__init__()
        self.search_result = search_result
        self.search_raises = search_raises
        self.create_calls = 0
        for key in landed or ():
            self.add_issue(key, "Meeting: Sprint Planning", parent="PROJ-9", subtask=True)

    def create_issue(self, fields):
        self.create_calls += 1
        raise JiraError("POST /rest/api/2/issue failed: timed out", ambiguous=True)

    def search_issue_keys(self, jql, max_results=5):
        self.searches.append(jql)
        if self.search_raises:
            raise JiraError("GET /rest/api/2/search -> HTTP 403: forbidden", 403)
        return list(self.search_result)


class RecoveryTests(OdinTestCase):
    def setUp(self):
        super().setUp()
        self.meetings = one_meeting()
        self.marker = dedupe_label(self.meetings[0])

    def recorded(self):
        return [r["issue_key"] for r in store.recent(self.peek())]

    def test_marker_label_is_deterministic_and_attached_on_create(self):
        jira = FakeJira()
        self.push(self.meetings, config(), jira, now=NOW)
        self.assertIn(self.marker, jira.created[0]["labels"])
        self.assertTrue(self.marker.startswith("m2j-"))
        # Same meeting, same label, every time.
        self.assertEqual(self.marker, dedupe_label(one_meeting()[0]))

    def test_create_timed_out_but_issue_exists_is_recorded_without_duplicating(self):
        jira = AmbiguousCreateJira(search_result=["PROJ-777"], landed=["PROJ-777"])
        result = self.push(self.meetings, config(), jira, now=NOW)

        self.assertEqual(result.recovered, ["PROJ-777"])
        self.assertEqual(result.created, [])
        self.assertEqual(result.errors, [])
        self.assertEqual(jira.create_calls, 1)          # never retried blindly
        self.assertIn(f'labels = "{self.marker}"', jira.searches[0])

        # Recorded in Muninn, so the next run reports EXISTS instead of creating a duplicate.
        self.assertEqual(self.recorded(), ["PROJ-777"])
        again = self.push(self.meetings, config(), AmbiguousCreateJira([]), now=NOW)
        self.assertEqual(again.existing, 1)
        self.assertEqual(again.recovered, [])

    def test_create_timed_out_and_issue_absent_is_left_for_the_next_run(self):
        jira = AmbiguousCreateJira(search_result=[])
        result = self.push(self.meetings, config(), jira, now=NOW)

        self.assertEqual(result.recovered, [])
        self.assertEqual(len(result.errors), 1)
        self.assertIn("ambiguous", result.errors[0])
        self.assertEqual(self.recorded(), [])        # nothing recorded, so it retries later

    def test_ambiguous_search_result_is_reported_and_left_alone(self):
        jira = AmbiguousCreateJira(search_result=["PROJ-777", "PROJ-778"])
        result = self.push(self.meetings, config(), jira, now=NOW)

        self.assertEqual(result.recovered, [])
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(self.recorded(), [])        # a human decides which one is right

    def test_failed_search_is_reported_and_left_alone(self):
        jira = AmbiguousCreateJira(search_result=[], search_raises=True)
        result = self.push(self.meetings, config(), jira, now=NOW)

        self.assertEqual(result.recovered, [])
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(self.recorded(), [])

    def test_unambiguous_failure_does_not_trigger_a_search(self):
        """A 400 means Jira rejected the payload outright; there is nothing to recover."""

        class RejectingJira(AmbiguousCreateJira):
            def create_issue(self, fields):
                self.create_calls += 1
                raise JiraError("POST -> HTTP 400: missing required field", 400)

        jira = RejectingJira(search_result=["PROJ-777"])
        result = self.push(self.meetings, config(), jira, now=NOW)
        self.assertEqual(jira.searches, [])
        self.assertEqual(result.recovered, [])
        self.assertEqual(len(result.errors), 1)
        self.assertNotIn("ambiguous", result.errors[0])

    def test_recovery_is_skipped_when_the_marker_label_is_disabled(self):
        jira = AmbiguousCreateJira(search_result=["PROJ-777"])
        result = self.push(self.meetings, config(dedupe_label=False), jira, now=NOW)
        self.assertEqual(jira.searches, [])             # no marker means no exact lookup
        self.assertEqual(len(result.errors), 1)
        self.assertIn("ambiguous", result.errors[0])

    def test_recovered_issue_still_gets_its_worklog(self):
        jira = AmbiguousCreateJira(search_result=["PROJ-777"], landed=["PROJ-777"])
        self.push(self.meetings, config(log_work=True), jira, now=NOW)
        self.assertEqual(len(jira.posted), 1)
        self.assertEqual(jira.posted[0][0], "PROJ-777")
        self.assertEqual(jira.posted[0][1], 3600)   # the meeting's real duration
        self.assertEqual(store.recent(self.peek())[0]["worklog_state"], "posted")


class AmbiguityClassificationTests(unittest.TestCase):
    def test_which_failures_are_ambiguous(self):
        self.assertTrue(JiraError("x", 502, ambiguous=True).ambiguous)
        self.assertFalse(JiraError("x", 400).ambiguous)


if __name__ == "__main__":
    unittest.main()
