"""JiraClient against a throwaway local HTTP server (stdlib only, no network)."""
import json
import os
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "apps" / "odin"
for folder in (ROOT, APP):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from odin.jira import JiraClient, JiraError  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    calls = []            # (method, path, headers, body)
    fail_next_with = []   # queue of status codes to return before succeeding
    special = {}          # (method, path) -> function(handler) answering instead
    listing = []          # issues /search pages through, as {"key", "fields": {"updated"}}
    after_page = None     # function(start) run after each search page is served

    def log_message(self, *args):
        pass

    def _reply(self, status, payload=None, content_type="application/json"):
        body = b"" if payload is None else (payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.send_response(status)
        if body:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        _Handler.calls.append((self.command, self.path, dict(self.headers), body))
        answer = _Handler.special.get((self.command, self.path.split("?")[0]))
        if answer:
            return answer(self)
        if _Handler.fail_next_with:
            status = _Handler.fail_next_with.pop(0)
            return self._reply(status, {"errorMessages": ["try later"]})
        if self.path == "/rest/api/2/myself":
            return self._reply(200, {"name": "jdoe", "displayName": "Jordan Doe"})
        if self.path == "/rest/api/2/issue" and self.command == "POST":
            return self._reply(201, {"id": "10001", "key": "PROJ-501"})
        if self.path.startswith("/rest/api/2/search") and _Handler.listing:
            query = parse_qs(urlsplit(self.path).query)
            start, size = int(query["startAt"][0]), int(query["maxResults"][0])
            page = [json.loads(json.dumps(i)) for i in _Handler.listing[start:start + size]]
            self._reply(200, {"total": len(_Handler.listing), "startAt": start, "issues": page})
            if _Handler.after_page:
                _Handler.after_page(start)
            return None
        if self.path.startswith("/rest/api/2/search"):
            return self._reply(200, {"total": 1, "issues": [{"key": "PROJ-501"}]})
        if self.path.endswith("/worklog"):
            return self._reply(201, {"id": "1"})
        if self.path.endswith("/transitions") and self.command == "GET":
            return self._reply(200, {"transitions": [{"id": "31", "name": "Close", "to": {"name": "Done"}}]})
        if self.path.endswith("/transitions") and self.command == "POST":
            return self._reply(204)
        if self.path == "/rest/api/2/issue/NOPE-1?fields=summary,issuetype,project,status":
            return self._reply(404, {"errorMessages": ["Issue does not exist"]})
        if self.path == "/sso":
            return self._reply(200, b"<html>login</html>", "text/html")
        return self._reply(404, {"errorMessages": ["unexpected path " + self.path]})

    do_GET = do_POST = _handle


class _ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"   # never proxy the local mock
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()       # the listening socket too, or Windows keeps it open

    def setUp(self):
        _Handler.calls.clear()
        _Handler.fail_next_with.clear()
        _Handler.special.clear()
        _Handler.listing = []
        _Handler.after_page = None
        self.client = JiraClient(self.base, "secret-pat")


class JiraClientTests(_ServerCase):
    def test_bearer_auth_create_worklog_transition(self):
        self.assertEqual(self.client.myself()["name"], "jdoe")
        key = self.client.create_issue({"summary": "x"})
        self.assertEqual(key, "PROJ-501")
        # The returned worklog id is what lets state tell "already logged" from "never logged".
        worklog_id = self.client.add_worklog(key, 1800, datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc),
                                             "Meeting: x")
        self.assertEqual(worklog_id, "1")
        self.assertTrue(self.client.transition(key, "done"))   # matches the target status name

        method, path, headers, body = _Handler.calls[0]
        self.assertEqual(headers.get("Authorization"), "Bearer secret-pat")
        worklog = next(c for c in _Handler.calls if c[1].endswith("/worklog"))
        self.assertEqual(worklog[3]["started"], "2026-09-21T14:00:00.000+0000")
        self.assertEqual(worklog[3]["timeSpentSeconds"], 1800)
        self.assertEqual(_Handler.calls[-1][3], {"transition": {"id": "31"}})

    def test_authorization_is_always_a_bearer_token(self):
        """Data Center only: there is no basic-auth path to fall back to."""
        client = JiraClient(self.base, "tok")
        client.myself()
        self.assertEqual(_Handler.calls[0][2]["Authorization"], "Bearer tok")

    def test_error_detail_and_hint(self):
        with self.assertRaises(JiraError) as ctx:
            self.client.get_issue("NOPE-1")
        self.assertEqual(ctx.exception.status, 404)
        self.assertIn("Issue does not exist", str(ctx.exception))

    def test_retries_on_503(self):
        _Handler.fail_next_with[:] = [503, 503]
        with mock.patch("odin.jira.time.sleep") as sleep:
            self.assertEqual(self.client.myself()["name"], "jdoe")
        self.assertEqual(sleep.call_count, 2)

    def test_post_is_not_retried_on_ambiguous_5xx(self):
        _Handler.fail_next_with[:] = [504]
        with mock.patch("odin.jira.time.sleep") as sleep, self.assertRaises(JiraError) as ctx:
            self.client.create_issue({"summary": "x"})
        self.assertEqual(ctx.exception.status, 504)
        self.assertEqual(sleep.call_count, 0)
        self.assertEqual(sum(1 for c in _Handler.calls if c[0] == "POST"), 1)

    def test_ambiguity_is_flagged_only_where_the_write_may_have_landed(self):
        # 504 on a POST: Jira may have created the issue before the gateway gave up.
        _Handler.fail_next_with[:] = [504]
        with mock.patch("odin.jira.time.sleep"), self.assertRaises(JiraError) as ctx:
            self.client.create_issue({"summary": "x"})
        self.assertTrue(ctx.exception.ambiguous)

        # 400 on a POST: rejected outright, nothing was created.
        _Handler.fail_next_with[:] = [400]
        with self.assertRaises(JiraError) as ctx:
            self.client.create_issue({"summary": "x"})
        self.assertFalse(ctx.exception.ambiguous)

        # A GET changes nothing, so it is never ambiguous however it fails.
        _Handler.fail_next_with[:] = [504] * 5
        with mock.patch("odin.jira.time.sleep"), self.assertRaises(JiraError) as ctx:
            self.client.myself()
        self.assertFalse(ctx.exception.ambiguous)

    def test_search_issue_keys(self):
        keys = self.client.search_issue_keys('labels = "m2j-abc123"')
        self.assertEqual(keys, ["PROJ-501"])
        method, path, _headers, _body = _Handler.calls[-1]
        self.assertEqual(method, "GET")          # safe to retry, unlike POST /search
        self.assertIn("jql=", path)
        self.assertIn("m2j-abc123", path)

    def test_post_is_retried_on_429(self):
        _Handler.fail_next_with[:] = [429]
        with mock.patch("odin.jira.time.sleep"):
            self.assertEqual(self.client.create_issue({"summary": "x"}), "PROJ-501")

    def test_html_response_is_flagged(self):
        with self.assertRaises(JiraError) as ctx:
            self.client.request("GET", "/sso")
        self.assertIn("instead of JSON", str(ctx.exception))


class ReviewFindingTests(_ServerCase):
    """Review 2026-10-10: every failure is a JiraError, ambiguous wherever a write may have landed;
    paging can't skip an issue; the token never follows a redirect off Jira."""

    def test_a_write_whose_answer_is_cut_off_is_ambiguous(self):
        def cut(handler):
            handler.send_response(201)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            handler.wfile.write(b'{"id": "1')         # and the connection closes
        _Handler.special[("POST", "/rest/api/2/issue")] = cut
        with self.assertRaises(JiraError) as ctx:
            self.client.create_issue({"summary": "x"})
        self.assertTrue(ctx.exception.ambiguous)
        self.assertIn("cut off", str(ctx.exception))

    def test_unreadable_json_is_a_jira_error_ambiguous_on_a_write(self):
        def garbled(handler):
            handler._reply(201, b'{"key": "PRO', "application/json")
        _Handler.special[("POST", "/rest/api/2/issue")] = garbled
        _Handler.special[("GET", "/rest/api/2/myself")] = garbled
        with self.assertRaises(JiraError) as ctx:
            self.client.create_issue({"summary": "x"})
        self.assertTrue(ctx.exception.ambiguous)
        with self.assertRaises(JiraError) as ctx:
            self.client.myself()
        self.assertFalse(ctx.exception.ambiguous)

    def test_a_500_on_a_write_is_ambiguous(self):
        """A failing post-function answers 500 after the issue is committed."""
        _Handler.fail_next_with[:] = [500]
        with self.assertRaises(JiraError) as ctx:
            self.client.create_issue({"summary": "x"})
        self.assertTrue(ctx.exception.ambiguous)
        self.assertFalse(ctx.exception.refused)
        self.assertEqual(sum(1 for c in _Handler.calls if c[0] == "POST"), 1, "never retried")

    def test_a_create_answered_without_a_key_is_ambiguous(self):
        _Handler.special[("POST", "/rest/api/2/issue")] = lambda h: h._reply(201, {"id": "10001"})
        with self.assertRaises(JiraError) as ctx:
            self.client.create_issue({"summary": "x"})
        self.assertTrue(ctx.exception.ambiguous)

    def test_a_redirect_to_another_host_isnt_followed(self):
        def away(handler):
            handler.send_response(302)
            handler.send_header("Location", "http://sso.example.invalid/login?next=/rest")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
        _Handler.special[("GET", "/rest/api/2/myself")] = away
        with self.assertRaises(JiraError) as ctx:
            self.client.myself()
        self.assertIn("token wasn't sent there", str(ctx.exception))
        self.assertIn("sso.example.invalid", str(ctx.exception))

    def test_a_redirect_within_jira_is_followed_with_the_token(self):
        def moved(handler):
            handler.send_response(302)
            handler.send_header("Location", "/rest/api/2/serverInfo")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
        _Handler.special[("GET", "/rest/api/2/myself")] = moved
        _Handler.special[("GET", "/rest/api/2/serverInfo")] = lambda h: h._reply(200, {"version": "9.12.0"})
        self.assertEqual(self.client.myself(), {"version": "9.12.0"})
        self.assertEqual(_Handler.calls[-1][2].get("Authorization"), "Bearer secret-pat")

    @staticmethod
    def issues(n):
        return [{"key": f"P-{i}", "fields": {"updated": f"2026-10-01T10:{i:02d}:00.000+0000"}} for i in range(n)]

    def test_paging_doesnt_skip_an_issue_when_one_already_read_is_updated(self):
        """Without the overlap, the issue at the start of page two slides onto page one and is missed."""
        _Handler.listing = self.issues(25)

        def update_one(start):
            if start == 0:
                moved = _Handler.listing.pop(2)
                moved["fields"]["updated"] = "2026-10-01T11:00:00.000+0000"
                _Handler.listing.append(moved)
        _Handler.after_page = update_one
        seen = [i["key"] for page in self.client.search("x", page_size=10) for i in page]
        self.assertEqual(set(seen), {f"P-{i}" for i in range(25)})
        self.assertEqual(seen.count("P-2"), 2, "read again as it is now")
        self.assertEqual(len(seen), 26)

    def test_paging_stops_with_an_error_when_the_list_moved_too_far(self):
        _Handler.listing = self.issues(25)

        def update_many(start):
            if start == 0:
                for _ in range(8):
                    moved = _Handler.listing.pop(0)
                    moved["fields"]["updated"] = "2026-10-01T11:00:00.000+0000"
                    _Handler.listing.append(moved)
        _Handler.after_page = update_many
        pages = self.client.search("x", page_size=10)
        self.assertEqual(len(next(pages)), 10)
        with self.assertRaisesRegex(JiraError, "changed too much"):
            next(pages)

    def test_paging_reads_a_quiet_list_once(self):
        _Handler.listing = self.issues(23)
        seen = [i["key"] for page in self.client.search("x", page_size=10) for i in page]
        self.assertEqual(seen, [f"P-{i}" for i in range(23)])
        self.assertEqual(self.client.search("x", page_size=10, limit=12).__next__(), _Handler.listing[:10])


if __name__ == "__main__":
    unittest.main()
