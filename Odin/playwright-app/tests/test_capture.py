"""Tests for the browser layer, against a local fake OWA rather than a live mailbox.

This is the half that was previously untested. Everything here runs offline: no mailbox, no login, no
internet. It drives a real browser through Playwright against tests/fake_owa.py, so the logic that
actually decides whether an export works - which request counts as the calendar API, which headers
get replayed, how the window is rewritten, whether paging is followed, what happens when the session
has expired - is exercised for real.

Skipped when Playwright is not installed, so the rest of the suite still runs anywhere:

    pip install -r requirements.txt && playwright install chromium
    python -m unittest discover -s tests -v
"""
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from owa import capture
from tests.fake_owa import FakeOwa

try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except ImportError:
    HAVE_PLAYWRIGHT = False

START = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
END = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)


@unittest.skipUnless(HAVE_PLAYWRIGHT, "playwright not installed")
class CaptureAgainstFakeOwaTests(unittest.TestCase):
    """The happy path and the interesting failures, end to end through a real browser."""

    @classmethod
    def setUpClass(cls):
        cls._playwright = sync_playwright().start()

    @classmethod
    def tearDownClass(cls):
        cls._playwright.stop()

    def setUp(self):
        self.profile = Path(tempfile.mkdtemp())
        self.fake = FakeOwa()
        self.fake.start()
        # Bundled Chromium, not msedge: the tests must not depend on which browsers a dev machine has.
        self.context = capture.open_session(
            self._playwright, str(self.profile), headless=True, channel="chromium",
            timeout_ms=20_000)

    def tearDown(self):
        self.context.close()
        self.fake.stop()
        shutil.rmtree(self.profile, ignore_errors=True)

    def test_discovers_the_endpoint_and_replays_its_auth_headers(self):
        """The whole design rests on this: learn the URL and credentials by watching the page."""
        endpoint, seed = capture.observe_calendar_endpoint(
            self.context, timeout_ms=15_000, calendar_url=self.fake.url)

        self.assertIn("calendarView", endpoint.url)
        self.assertEqual(endpoint.method, "GET")
        self.assertEqual(len(seed), 3)
        # The Authorization header the page used must be captured, or the direct call gets a 401.
        lowered = {k.lower(): v for k, v in endpoint.headers.items()}
        self.assertEqual(lowered.get("authorization"), "Bearer FAKE-TOKEN-FROM-PAGE")
        self.assertEqual(lowered.get("x-owa-canary"), "canary-value")

    def test_fetch_window_rewrites_the_dates_and_asks_for_utc(self):
        """The point of the direct call: our window, not whatever the view happened to show."""
        endpoint, _ = capture.observe_calendar_endpoint(
            self.context, timeout_ms=15_000, calendar_url=self.fake.url)
        self.fake.requests.clear()

        events = capture.fetch_window(self.context, endpoint, START, END)

        self.assertEqual(len(events), 3)
        _method, path, raw_headers = self.fake.calendar_requests()[-1]
        # HTTP header names are case-insensitive and Playwright sends them lower-cased, so compare
        # case-insensitively or this asserts something about the client rather than about behaviour.
        headers = {k.lower(): v for k, v in raw_headers.items()}
        self.assertIn("2026-09-21T04%3A00%3A00Z", path.replace(":", "%3A"))
        self.assertIn("startDateTime", path)
        # Asking for UTC is what lets mapping.py refuse to guess a timezone.
        self.assertEqual(headers.get("prefer"), 'outlook.timezone="UTC"')
        # And the replayed credential must survive into the direct call.
        self.assertEqual(headers.get("authorization"), "Bearer FAKE-TOKEN-FROM-PAGE")

    def test_follows_paging_to_the_end(self):
        """A month of meetings arrives in pages; stopping at the first loses most of them."""
        self.fake.pages = 4
        endpoint, _ = capture.observe_calendar_endpoint(
            self.context, timeout_ms=15_000, calendar_url=self.fake.url)

        events = capture.fetch_window(self.context, endpoint, START, END)

        self.assertEqual(len(events), 12)          # 4 pages x 3
        keys = [e["id"] for e in events]
        self.assertEqual(len(keys), len(set(keys)), "paging produced duplicates")

    def test_max_pages_is_a_backstop_not_a_silent_truncation(self):
        self.fake.pages = 50
        endpoint, _ = capture.observe_calendar_endpoint(
            self.context, timeout_ms=15_000, calendar_url=self.fake.url)
        events = capture.fetch_window(self.context, endpoint, START, END, max_pages=3)
        self.assertEqual(len(events), 9)

    def test_expired_session_is_reported_as_sign_in_not_as_a_missing_endpoint(self):
        """The most common real failure. The message has to name the fix, which is --login."""
        self.fake.mode = "signin"
        with self.assertRaises(capture.CaptureError) as ctx:
            capture.observe_calendar_endpoint(self.context, timeout_ms=5_000,
                                              calendar_url=self.fake.url)
        message = str(ctx.exception)
        # Assert the sign-in message specifically, not merely that "--login" appears. Both failure
        # messages mention --login, so the looser assertion passed even when the diagnosis had
        # regressed to the generic one - which sends the user off to --debug-endpoints for nothing.
        self.assertIn("asking for sign-in", message)
        self.assertNotIn("--debug-endpoints", message)

    def test_html_instead_of_json_does_not_look_like_an_empty_calendar(self):
        """An SSO page served in place of the API must not be mistaken for 'no meetings'."""
        endpoint, _ = capture.observe_calendar_endpoint(
            self.context, timeout_ms=15_000, calendar_url=self.fake.url)
        self.fake.mode = "html_intercept"
        with self.assertRaises(capture.CaptureError):
            capture.fetch_window(self.context, endpoint, START, END)

    def test_server_error_on_the_first_page_is_raised_with_advice(self):
        endpoint, _ = capture.observe_calendar_endpoint(
            self.context, timeout_ms=15_000, calendar_url=self.fake.url)
        self.fake.mode = "server_error"
        with self.assertRaises(capture.CaptureError) as ctx:
            capture.fetch_window(self.context, endpoint, START, END)
        self.assertIn("500", str(ctx.exception))
        self.assertIn("--login", str(ctx.exception))

    def test_post_endpoint_is_refused_rather_than_silently_getting_nothing(self):
        """Issuing a GET against a POST-shaped endpoint would look like an empty calendar."""
        endpoint, _ = capture.observe_calendar_endpoint(
            self.context, timeout_ms=15_000, calendar_url=self.fake.url)
        post_endpoint = capture.ObservedEndpoint(endpoint.url, "POST", endpoint.headers, "{}")
        with self.assertRaises(capture.CaptureError) as ctx:
            capture.fetch_window(self.context, post_endpoint, START, END)
        self.assertIn("POST", str(ctx.exception))
        self.assertIn("--raw-out", str(ctx.exception))

    def test_empty_calendar_is_not_an_error(self):
        endpoint, _ = capture.observe_calendar_endpoint(
            self.context, timeout_ms=15_000, calendar_url=self.fake.url)
        self.fake.mode = "empty"
        self.assertEqual(capture.fetch_window(self.context, endpoint, START, END), [])

    def test_picks_the_calendar_not_the_telemetry_beacon(self):
        """Three decoys with /events in the path are fetched before the calendar.

        Picking one of them makes the export succeed with nonsense, which is worse than failing.
        The decoys are layered so no single defence covers them all: activities/events defeats URL
        matching (integer start/end, caught by the shape check), and insights/events defeats the
        shape check (subject plus an ISO start, caught only by strong-over-weak URL tiering).

        The assertions below check the decoys were really *reached*. An earlier version of this test
        passed for the wrong reason: the only decoy was /owa/telemetry/events, which the route
        handler aborts before the browser issues it, so the matcher was never exercised at all.
        """
        endpoint, seed = capture.observe_calendar_endpoint(
            self.context, timeout_ms=15_000, calendar_url=self.fake.url)

        reached = [path for _, path, _ in self.fake.requests]
        self.assertTrue(any("insights/events" in p for p in reached),
                        "the decoy that defeats the shape check never reached the matcher")
        self.assertTrue(any("activities/events" in p for p in reached),
                        "the decoy that defeats URL matching never reached the matcher")

        self.assertIn("calendarView", endpoint.url)
        for decoy in ("telemetry", "insights", "activities"):
            self.assertNotIn(decoy, endpoint.url)
        self.assertEqual(len(seed), 3)          # the calendar's 3, not a decoy's

    def test_renamed_endpoint_falls_back_without_picking_up_a_decoy(self):
        """When Microsoft renames the endpoint, the shape check is the only defence left.

        With the calendar served from a merely weakly-recognisable path, the strong tier matches
        nothing, so a decoy and the real calendar are both weak candidates. Whichever arrives first
        wins on URL alone - and the decoys arrive first. Only the shape check separates them, and it
        has to work here or the export silently fills up with someone else's records.
        """
        self.fake.calendar_path = "/owa/gcv/calendar"   # weak: matches /calendar, not the strong tier
        endpoint, seed = capture.observe_calendar_endpoint(
            self.context, timeout_ms=15_000, calendar_url=self.fake.url)
        self.assertIn("/owa/gcv/calendar", endpoint.url)
        self.assertEqual(len(seed), 3)

    def test_telemetry_beacon_is_not_even_sent(self):
        """Separate property from discovery: beacon hosts are aborted, so nothing is reported out."""
        capture.observe_calendar_endpoint(self.context, timeout_ms=15_000,
                                          calendar_url=self.fake.url)
        reached = [path for _, path, _ in self.fake.requests]
        self.assertFalse(any("telemetry" in p for p in reached))

    def test_ignores_the_other_json_the_page_fetches(self):
        """Presence, mail folders and user config all return JSON and none are the calendar."""
        endpoint, _ = capture.observe_calendar_endpoint(
            self.context, timeout_ms=15_000, calendar_url=self.fake.url)
        for decoy in ("GetPresence", "mailFolders", "findMeetingTimes", "GetUserConfiguration"):
            self.assertNotIn(decoy, endpoint.url)
        # And the page really did make them, so this is not passing by accident.
        self.assertGreaterEqual(len(self.fake.json_requests()), 5)

    def test_resource_blocking_prevents_the_heavy_assets(self):
        """Measurable, not just "the page loaded": stylesheets, images and fonts must not be fetched."""
        capture.observe_calendar_endpoint(self.context, timeout_ms=15_000,
                                         calendar_url=self.fake.url)
        fetched = [r[1] for r in self.fake.asset_requests()]
        self.assertEqual([a for a in fetched if a.endswith((".css", ".png", ".woff2"))], [])

    def test_discovery_does_not_leak_a_page(self):
        """A page left open holds a renderer process for the rest of the run."""
        before = len(self.context.pages)
        capture.observe_calendar_endpoint(self.context, timeout_ms=15_000,
                                         calendar_url=self.fake.url)
        self.assertEqual(len(self.context.pages), before)

    def test_slow_endpoint_still_discovered_within_the_timeout(self):
        """The budget must be spent against the clock, not decremented per wakeup.

        An earlier version charged a full slice each time wait_for_event returned - which it does as
        soon as *any* candidate arrives, decoys included - and so abandoned discovery seconds into a
        long timeout.
        """
        self.fake.latency_ms = 150
        endpoint, seed = capture.observe_calendar_endpoint(
            self.context, timeout_ms=15_000, calendar_url=self.fake.url)
        self.assertIn("calendarView", endpoint.url)
        self.assertEqual(len(seed), 3)

    def test_paging_loop_is_broken_not_followed_forever(self):
        """A nextLink pointing back at a fetched page would otherwise spin indefinitely."""
        endpoint, _ = capture.observe_calendar_endpoint(
            self.context, timeout_ms=15_000, calendar_url=self.fake.url)
        self.fake.pages = 2
        self.fake.self_referential_next = True
        events = capture.fetch_window(self.context, endpoint, START, END, max_pages=50)
        self.assertLessEqual(len(events), 6)

    def test_transient_failure_is_retried_once(self):
        """One retry covers the common case - a blip on an agency proxy - without masking a real fault."""
        endpoint, _ = capture.observe_calendar_endpoint(
            self.context, timeout_ms=15_000, calendar_url=self.fake.url)
        self.fake.fail_next_with = [503]        # first call fails, retry succeeds
        events = capture.fetch_window(self.context, endpoint, START, END)
        self.assertEqual(len(events), 3)

    def test_event_cap_stops_runaway_memory(self):
        self.fake.pages = 10
        endpoint, _ = capture.observe_calendar_endpoint(
            self.context, timeout_ms=15_000, calendar_url=self.fake.url)
        events = capture.fetch_window(self.context, endpoint, START, END, max_events=5)
        self.assertLessEqual(len(events), 6)    # stops on the page that crosses the cap

    def test_debug_endpoints_lists_what_the_page_asked_for(self):
        """The escape hatch when discovery fails has to actually show the candidate."""
        lines = capture.debug_endpoints(self.context, seconds=3, calendar_url=self.fake.url)
        self.assertTrue(any("calendarView" in line for line in lines))
        self.assertTrue(any("looks like the calendar API" in line for line in lines))
        # The telemetry beacon must not be flagged as the strong candidate.
        flagged = [line for line in lines if "looks like the calendar API" in line]
        self.assertFalse(any("telemetry" in line for line in flagged))


class ShapeDetectionTests(unittest.TestCase):
    """No browser needed: the predicate that decides what counts as calendar JSON."""

    def test_recognises_the_graph_and_ews_envelopes(self):
        graph = {"value": [{"start": {}, "end": {}, "subject": "x"}]}
        ews = {"Items": [{"Start": "...", "End": "...", "Subject": "x"}]}
        self.assertTrue(capture._looks_like_events(graph))
        self.assertTrue(capture._looks_like_events(ews))

    def test_rejects_unrelated_json(self):
        for payload in ({}, {"value": []}, {"value": [{"displayName": "a user"}]},
                        {"error": "nope"}, [], "a string", None,
                        # The telemetry/activities shape: start and end present, but as integers.
                        # This is the one that matters - it is why the check asks for a subject or a
                        # date-shaped start rather than merely for the key "start".
                        {"value": [{"start": 1727, "end": 1892, "name": "render"}]}):
            self.assertFalse(capture._looks_like_events(payload), payload)

    def test_extract_events_handles_each_envelope(self):
        self.assertEqual(len(capture.extract_events({"value": [{"a": 1}]})), 1)
        self.assertEqual(len(capture.extract_events({"Items": [{"a": 1}, {"b": 2}]})), 2)
        self.assertEqual(capture.extract_events({"nothing": 1}), [])
        self.assertEqual(capture.extract_events("not a dict"), [])


class WindowRewriteTests(unittest.TestCase):
    """No browser needed: turning the observed URL into the window we want."""

    def test_replaces_existing_date_parameters(self):
        url = ("https://example/api/v2.0/me/calendarView"
               "?startDateTime=2020-01-01T00:00:00Z&endDateTime=2020-01-02T00:00:00Z&$top=25")
        rewritten = capture._with_window(url, START, END)
        self.assertIn("2026-09-21T04%3A00%3A00Z", rewritten)
        self.assertNotIn("2020-01-01", rewritten)
        self.assertIn("%24top=1000", rewritten)      # fewer round trips

    def test_adds_them_when_absent(self):
        rewritten = capture._with_window("https://example/api/calendar", START, END)
        self.assertIn("startDateTime", rewritten)
        self.assertIn("endDateTime", rewritten)

    def test_preserves_unrelated_parameters(self):
        rewritten = capture._with_window("https://example/api/c?foo=bar&startDateTime=x", START, END)
        self.assertIn("foo=bar", rewritten)


if __name__ == "__main__":
    unittest.main()
