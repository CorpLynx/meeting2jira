"""A sub-task Jira made is recorded once, however the create went (HANDOFF backlog P1-A; review
2026-10-10 #2).

A POST that times out or returns a 5xx may have been applied by Jira anyway, and a run can stop
between the create and the record. Retrying blindly duplicates the sub-task; giving up duplicates it
on the *next* run. So each create is preceded, and an ambiguous one followed, by an exact lookup for
a deterministic marker label on issues you created.
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
    """Fails create with an ambiguous error (after it landed, if landed is set). The search finds
    nothing before the create and search_result after it, as when the create landed."""

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
        if self.search_raises and self.create_calls:
            raise JiraError("GET /rest/api/2/search -> HTTP 403: forbidden", 403)
        return list(self.search_result) if self.create_calls else []


class LandsThenStops(FakeJira):
    """The create lands in Jira, then what follows goes wrong: `then` is raised instead of the
    answer, and searches fail while `outage` is set (from the create on, with outage_after)."""

    def __init__(self, then, outage_after=False):
        super().__init__()
        self.then, self.outage, self.outage_after = then, False, outage_after

    def create_issue(self, fields):
        super().create_issue(fields)
        self.outage = self.outage or self.outage_after
        if self.then is not None:
            exc, self.then = self.then, None
            raise exc
        return self.issues[list(self.issues)[-1]]["key"]

    def search_issue_keys(self, jql, max_results=5):
        if self.outage:
            raise JiraError("GET /rest/api/2/search -> HTTP 504: gateway timeout", 504)
        return super().search_issue_keys(jql, max_results)


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
        self.assertIn("the next run looks for its label", result.errors[0])
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
        self.assertEqual(len(jira.searches), 1, "only the look before creating")
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


class LookBeforeCreatingTests(OdinTestCase):
    """Review 2026-10-10 #2: every way a sub-task can land in Jira without a record."""

    def setUp(self):
        super().setUp()
        self.meetings = one_meeting()

    def twice(self, jira, **cfg):
        first = self.push(self.meetings, config(**cfg), jira, now=NOW)
        jira.then, jira.outage, jira.outage_after = None, False, False
        second = self.push(self.meetings, config(**cfg), jira, now=NOW)
        return first, second

    def test_a_failed_search_after_an_ambiguous_create_doesnt_duplicate_it_next_run(self):
        jira = LandsThenStops(JiraError("POST /rest/api/2/issue -> HTTP 504: gateway timeout", 504, ambiguous=True),
                              outage_after=True)
        first, second = self.twice(jira)
        self.assertEqual(len(first.errors), 1)
        self.assertEqual(len(jira.created), 1, "made once")
        self.assertEqual(second.recovered, ["PROJ-901"])
        self.assertEqual([r["issue_key"] for r in store.recent(self.peek())], ["PROJ-901"])

    def test_a_create_whose_answer_was_cut_off_isnt_made_again(self):
        """An http.client error used to escape the run with nothing recorded."""
        jira = LandsThenStops(JiraError("POST /rest/api/2/issue failed: the answer was cut off (IncompleteRead)",
                                        ambiguous=True), outage_after=True)     # the search right after fails too
        first, second = self.twice(jira)
        self.assertEqual(first.created + first.recovered, [])
        self.assertEqual(len(jira.created), 1)
        self.assertEqual(second.recovered, ["PROJ-901"])

    def test_a_run_stopped_between_the_create_and_the_record_isnt_made_again(self):
        jira = LandsThenStops(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.push(self.meetings, config(), jira, now=NOW)
        self.assertEqual([r["issue_key"] for r in store.recent(self.peek())], [])
        second = self.push(self.meetings, config(), jira, now=NOW)
        self.assertEqual(len(jira.created), 1)
        self.assertEqual(second.recovered, ["PROJ-901"])

    def test_a_record_neither_muninn_nor_the_journal_could_take_is_found_next_run(self):
        """A full disk holds Muninn and the journal alike."""
        from unittest import mock
        jira = FakeJira()
        with mock.patch.object(store, "_insert", side_effect=__import__("sqlite3").OperationalError("disk full")), \
                mock.patch.object(store.Journal, "append", side_effect=OSError(28, "No space left on device")):
            first = self.push(self.meetings, config(), jira, now=NOW)
        self.assertTrue(any("next run finds it in Jira by its label" in e for e in first.errors), first.errors)
        second = self.push(self.meetings, config(), jira, now=NOW)
        self.assertEqual(len(jira.created), 1)
        self.assertEqual(second.recovered, ["PROJ-901"])

    def test_a_colleagues_sub_task_for_the_same_meeting_isnt_adopted(self):
        """Their Odin labels the meeting the same way; only what you created is yours."""
        jira = FakeJira()
        jira.add_issue("PROJ-500", "Meeting: Sprint Planning", parent="PROJ-9", subtask=True,
                       labels=[dedupe_label(self.meetings[0])], creator="colleague")
        result = self.push(self.meetings, config(), jira, now=NOW)
        self.assertEqual((result.created, result.recovered), (["PROJ-901"], []))

    def test_found_in_another_project_after_a_rule_moved_the_parent(self):
        jira = LandsThenStops(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.push(self.meetings, config(), jira, now=NOW)
        second = self.push(self.meetings, config(default_parent="OTHER-3"), jira, now=NOW)
        self.assertEqual((second.created, second.recovered), ([], ["PROJ-901"]))

    def test_when_jira_cant_be_searched_nothing_is_created(self):
        jira = LandsThenStops(None)
        jira.outage = True
        result = self.push(self.meetings, config(), jira, now=NOW)
        self.assertEqual(jira.created, [])
        self.assertIn("nothing was created", result.errors[0])

    def test_time_logged_by_hand_on_a_found_sub_task_isnt_logged_again(self):
        jira = LandsThenStops(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.push(self.meetings, config(log_work=True), jira, now=NOW)
        jira.add_jira_worklog("PROJ-901", 3600, self.meetings[0].start_utc, "logged by hand")
        self.push(self.meetings, config(log_work=True), jira, now=NOW)
        self.assertEqual(jira.posted, [], "the found sub-task's worklogs were read first")


class AmbiguityClassificationTests(unittest.TestCase):
    def test_which_failures_are_ambiguous(self):
        self.assertTrue(JiraError("x", 502, ambiguous=True).ambiguous)
        self.assertFalse(JiraError("x", 400).ambiguous)


if __name__ == "__main__":
    unittest.main()
