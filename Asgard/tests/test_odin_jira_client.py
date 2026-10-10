"""JiraClient against a throwaway local HTTP server (stdlib only, no network)."""
import json
import os
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

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
        if _Handler.fail_next_with:
            status = _Handler.fail_next_with.pop(0)
            return self._reply(status, {"errorMessages": ["try later"]})
        if self.path == "/rest/api/2/myself":
            return self._reply(200, {"name": "jdoe", "displayName": "Jordan Doe"})
        if self.path == "/rest/api/2/issue" and self.command == "POST":
            return self._reply(201, {"id": "10001", "key": "PROJ-501"})
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


class JiraClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"   # never proxy the local mock
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _Handler.calls.clear()
        _Handler.fail_next_with.clear()
        self.client = JiraClient(self.base, "secret-pat")

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


if __name__ == "__main__":
    unittest.main()
