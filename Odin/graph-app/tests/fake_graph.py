"""A local stand-in for Microsoft Graph, for testing the client with no token and no mailbox.

Deliberately awkward in the ways Graph actually is, because a cooperative fake proves very little:

    * it throttles with 429 and a Retry-After header
    * it pages with @odata.nextLink
    * it can return the OData error envelope rather than a bare body
    * it can expire a token mid-run
    * it echoes back the query it received, so a test can assert what was *asked for* - which is how
      $select, $orderby and the Prefer header get verified rather than assumed

Runs on 127.0.0.1 over plain HTTP. That is the one unavoidable divergence from the real thing, and
it is why the TLS context in client.py is covered by inspection rather than by these tests.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit


def _event(index: int, subject: str) -> dict:
    hour = 9 + (index % 8)
    return {
        "id": "AAMkAG-graph-{}".format(index),
        "iCalUId": "040000008200E00074C5B7101A82E008000000{:04d}".format(index),
        "subject": subject,
        "start": {"dateTime": "2026-09-22T{:02d}:00:00.0000000".format(hour), "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-22T{:02d}:30:00.0000000".format(hour), "timeZone": "UTC"},
        "isAllDay": False,
        "isCancelled": False,
        "responseStatus": {"response": "accepted", "time": "2026-09-01T00:00:00Z"},
        "showAs": "busy",
        "sensitivity": "normal",
        "location": {"displayName": "Microsoft Teams Meeting"},
        "categories": [],
        "isOnlineMeeting": True,
        "onlineMeetingProvider": "teamsForBusiness",
        "organizer": {"emailAddress": {"name": "A Person", "address": "a@example.gov"}},
        "type": "singleInstance",
        "seriesMasterId": None,
    }


class FakeGraph:
    def __init__(self):
        self.mode = "ok"              # ok | throttle_once | expired | forbidden | odata_error | notjson
        self.pages = 1
        self.events_per_page = 3
        self.requests: list = []      # (path, headers) for assertions
        self.latency_ms = 0
        self._throttled = False
        self._server = None
        self._thread = None

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, content_type="application/json", headers=None):
                if fake.latency_ms:
                    time.sleep(fake.latency_ms / 1000.0)
                if isinstance(body, bytes):
                    payload = body
                elif isinstance(body, str):
                    payload = body.encode("utf-8")
                else:
                    payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                fake.requests.append((self.path, dict(self.headers)))
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)

                if not (self.headers.get("Authorization") or "").startswith("Bearer "):
                    return self._send(401, {"error": {"code": "InvalidAuthenticationToken",
                                                      "message": "Access token is empty."}})

                if fake.mode == "expired":
                    return self._send(401, {"error": {"code": "InvalidAuthenticationToken",
                                                      "message": "Access token has expired."}})
                if fake.mode == "forbidden":
                    return self._send(403, {"error": {"code": "ErrorAccessDenied",
                                                      "message": "Access is denied."}})
                if fake.mode == "odata_error":
                    return self._send(500, {"error": {"code": "InternalServerError",
                                                      "message": "Something went wrong."}})
                if fake.mode == "notjson":
                    return self._send(200, "<html>proxy sign-in</html>", "text/html")
                if fake.mode == "throttle_once" and not fake._throttled:
                    fake._throttled = True
                    return self._send(429, {"error": {"code": "TooManyRequests",
                                                      "message": "Throttled."}},
                                      headers={"Retry-After": "0"})

                if parts.path.endswith("/me"):
                    return self._send(200, {"displayName": "Test User",
                                            "userPrincipalName": "test@example.gov"})

                if parts.path.endswith("/me/calendarView"):
                    page = int((query.get("page") or ["1"])[0])
                    events = [_event(i + (page - 1) * fake.events_per_page,
                                     "Meeting {}".format(i + (page - 1) * fake.events_per_page))
                              for i in range(fake.events_per_page)]
                    body = {"value": events}
                    if page < fake.pages:
                        body["@odata.nextLink"] = "{}?page={}".format(
                            fake.url + "/v1.0/me/calendarView", page + 1)
                    return self._send(200, body)

                return self._send(404, {"error": {"code": "ResourceNotFound",
                                                  "message": parts.path}})

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:{}".format(self._server.server_port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    # -- assertions helpers ----------------------------------------------------------------------
    def calendar_requests(self):
        return [r for r in self.requests if "calendarView" in r[0]]
