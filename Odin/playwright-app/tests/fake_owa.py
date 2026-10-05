"""A local stand-in for Outlook on the web, so the browser layer can actually be tested.

Without this, `capture.py` could only ever be exercised against a live mailbox - which means in
practice it was never exercised at all, and the discovery, header-replay, window-rewrite and paging
logic were all taken on faith.

It serves:
  * a page that fetches its calendar the way the real client does, from JavaScript with an
    Authorization header, so discovery has something realistic to observe
  * a paged calendar endpoint using @odata.nextLink
  * switchable failure modes, because the interesting tests are the unhappy ones

Standard library only, and binds to 127.0.0.1 on a random port.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

# The page deliberately calls its API from script rather than being server-rendered, so the capture
# code has to observe a real XHR the way it will in production.
# Deliberately noisy. The real client pulls scripts, styles, fonts and images, fires telemetry, and
# makes several unrelated JSON calls before it ever asks for the calendar. A fake that only makes the
# one interesting request proves almost nothing: it cannot show that resource blocking saves
# anything, and it cannot show that the search logic picks the right request out of a crowd.
PAGE = """<!doctype html>
<html><head><title>Fake OWA</title>
<link rel="stylesheet" href="/assets/owa.css">
<link rel="stylesheet" href="/assets/theme.css">
</head>
<body><div id="status">loading</div>
<img src="/assets/avatar.png" alt="">
<img src="/assets/logo.png" alt="">
<script src="/assets/vendor.js"></script>
<script>
  const J = {'Authorization': 'Bearer FAKE-TOKEN-FROM-PAGE',
             'X-OWA-CANARY': 'canary-value', 'Accept': 'application/json'};

  // Unrelated JSON the real client fetches on startup. Three are traps: each has "/events" in its
  // path and start/end in its payload, which is exactly what a loose match takes for the calendar.
  //
  // They are not interchangeable, and that is the point:
  //   /owa/telemetry/events        aborted by _BLOCKED_HOSTS, so it never reaches the matcher.
  //                                Tests resource blocking, and nothing about discovery.
  //   /api/v1.0/me/activities/events  reaches the matcher, start/end are integers. Only the shape
  //                                check rejects it.
  //   /api/v1.0/me/insights/events    reaches the matcher AND passes the shape check. Only the
  //                                strong/weak URL tiering rejects it.
  // All of them are fetched before the calendar, so a matcher that takes the first plausible hit
  // fails. That is a bug this fake found for real.
  const noise = [
    '/owa/service.svc?action=GetPresence',
    '/api/v1.0/me/mailFolders',
    '/owa/telemetry/events',
    '/api/v1.0/me/insights/events',
    '/api/v1.0/me/activities/events',
    '/api/v1.0/me/findMeetingTimes',
    '/owa/service.svc?action=GetUserConfiguration'
  ];

  Promise.all(noise.map(u => fetch(u, {headers: J}).then(r => r.json()).catch(() => null)))
    .then(() => fetch('__CALENDAR_PATH__?startDateTime=2026-01-01T00:00:00Z&endDateTime=2026-01-02T00:00:00Z&$top=25',
                      {headers: J}))
    .then(r => r.json())
    .then(d => {
      document.getElementById('status').textContent = 'loaded ' + (d.value || []).length;
    });
</script>
</body></html>
"""

# Roughly what a telemetry beacon returns: an array under "value" whose items carry start and end.
# It is not a calendar, but a shape check that only looks for start/end cannot tell the difference.
TELEMETRY = {"value": [{"start": 1727, "end": 1892, "name": "render", "durationMs": 165},
                       {"start": 1892, "end": 2050, "name": "hydrate", "durationMs": 158}]}

# Two traps that are NOT blocked, so they genuinely reach the discovery matcher, and each defeats a
# different single-layer defence. They are what proves the matcher works, rather than proving the
# route handler happened to abort the decoy first.
#
# INSIGHTS passes the shape check - it has a subject and an ISO start, because it is a list of
# documents shown around meetings. Only the strong/weak URL tiering keeps it from being chosen, and
# it is fetched before the calendar, so a matcher that took the first plausible hit would take this.
INSIGHTS = {"value": [{"subject": "Q4 deck", "start": {"dateTime": "2026-09-22T13:00:00", "timeZone": "UTC"},
                       "resourceVisualization": {"title": "deck.pptx"}}]}

# ACTIVITIES defeats URL matching instead: "/events" in the path, start and end present, but as
# integers. Only the shape check rejects it.
ACTIVITIES = {"value": [{"start": 1727, "end": 1892, "name": "render"},
                        {"start": 1892, "end": 2050, "name": "hydrate"}]}

PRESENCE = {"value": [{"id": "u1", "availability": "Busy", "activity": "InAMeeting"}]}
MAIL_FOLDERS = {"value": [{"id": "inbox", "displayName": "Inbox", "unreadItemCount": 12}]}
USER_CONFIG = {"UserOptions": {"TimeZone": "Eastern Standard Time", "WeekStartDay": "Sunday"}}
MEETING_TIMES = {"meetingTimeSuggestions": [{"confidence": 100.0}]}

SIGN_IN_PAGE = """<!doctype html>
<html><body><form><input type="password" name="passwd"></form></body></html>
"""


def _event(index: int, subject: str) -> dict:
    hour = 9 + (index % 8)
    return {
        "id": f"AAMkAG-fake-{index}",
        "iCalUId": f"040000008200E00074C5B7101A82E008000000{index:04d}",
        "subject": subject,
        "start": {"dateTime": f"2026-09-2{1 + index % 3}T{hour:02d}:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": f"2026-09-2{1 + index % 3}T{hour:02d}:30:00.0000000", "timeZone": "UTC"},
        "isAllDay": False,
        "isCancelled": False,
        "sensitivity": "normal",
        "showAs": "busy",
        "responseStatus": {"response": "accepted"},
        "location": {"displayName": "Microsoft Teams Meeting"},
        "categories": [],
        "isOnlineMeeting": True,
        "onlineMeetingProvider": "teamsForBusiness",
        "organizer": {"emailAddress": {"name": "Alex Kim", "address": "alex@example.gov"}},
        "attendees": [{"emailAddress": {"name": "Alex Kim"}, "type": "required"}],
    }


class FakeOwa:
    """Controls what the fake server does. Mutate the attributes between requests to steer a test."""

    def __init__(self):
        self.mode = "ok"            # ok | signin | html_intercept | server_error | post_only | empty
        self.pages = 1              # how many pages of events to serve
        self.events_per_page = 3
        self.requests: list = []    # (method, path, headers) for assertions
        self.latency_ms = 0         # per-response delay, to imitate a real network
        self.fail_next_with: list = []       # status codes to return before succeeding
        self.self_referential_next = False   # emit a nextLink pointing at the current page
        # Where the calendar lives. Overridable because Microsoft renames this endpoint
        # periodically, and the interesting case is a rename to something only *weakly* recognisable
        # - then the strong tier matches nothing and the shape check is the only thing standing
        # between the user and an export full of some decoy's records.
        self.calendar_path = "/api/v2.0/me/calendarView"
        self._server = None
        self._thread = None

    # ---- lifecycle -----------------------------------------------------------------------------
    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, content_type="application/json"):
                if fake.latency_ms:
                    time.sleep(fake.latency_ms / 1000.0)
                # dicts are serialised here; an earlier version called .encode() on them, which
                # killed the request at the socket level and looked exactly like capture.py failing
                # to discover the endpoint.
                if isinstance(body, bytes):
                    payload = body
                elif isinstance(body, str):
                    payload = body.encode("utf-8")
                else:
                    payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                fake.requests.append(("GET", self.path, dict(self.headers)))
                parts = urlsplit(self.path)

                if parts.path in ("/", "/calendar/view/workweek"):
                    if fake.mode == "signin":
                        return self._send(200, SIGN_IN_PAGE, "text/html")
                    return self._send(200, PAGE.replace("__CALENDAR_PATH__", fake.calendar_path),
                                      "text/html")

                # Static assets: what resource blocking is supposed to stop. Counted so a test can
                # assert the saving rather than just asserting the page loaded.
                if parts.path.startswith("/assets/"):
                    if parts.path.endswith(".css"):
                        return self._send(200, "body{margin:0}", "text/css")
                    if parts.path.endswith(".js"):
                        return self._send(200, "/* vendor */", "application/javascript")
                    if parts.path.endswith(".png"):
                        return self._send(200, b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048, "image/png")
                    if parts.path.endswith(".woff2"):
                        return self._send(200, b"wOF2" + b"\x00" * 4096, "font/woff2")
                    return self._send(404, {"error": "no such asset"})

                # The three traps. Each has "/events" in the path and start/end in the payload, so a
                # loose match picks any of them. See the comments on TELEMETRY / ACTIVITIES /
                # INSIGHTS: they defeat different single layers, on purpose.
                if parts.path == "/owa/telemetry/events":
                    return self._send(200, TELEMETRY)
                if parts.path == "/api/v1.0/me/insights/events":
                    return self._send(200, INSIGHTS)
                if parts.path == "/api/v1.0/me/activities/events":
                    return self._send(200, ACTIVITIES)
                if parts.path == "/api/v1.0/me/mailFolders":
                    return self._send(200, MAIL_FOLDERS)
                if parts.path == "/api/v1.0/me/findMeetingTimes":
                    return self._send(200, MEETING_TIMES)
                if "action=GetPresence" in parts.query:
                    return self._send(200, PRESENCE)
                if "action=GetUserConfiguration" in parts.query:
                    return self._send(200, USER_CONFIG)

                if parts.path.startswith(fake.calendar_path):
                    if fake.mode == "server_error":
                        return self._send(500, {"error": "boom"})
                    if fake.mode == "html_intercept":
                        # The SSO-page-instead-of-JSON case seen on federal networks.
                        return self._send(200, "<html>sign in</html>", "text/html")
                    if fake.mode == "post_only":
                        return self._send(405, {"error": "use POST"})

                    if fake.fail_next_with:
                        return self._send(fake.fail_next_with.pop(0), {"error": "try again"})

                    query = parse_qs(parts.query)
                    page = int((query.get("page") or ["1"])[0])
                    if fake.mode == "empty":
                        return self._send(200, {"value": []})

                    start_index = (page - 1) * fake.events_per_page
                    events = [_event(start_index + i, f"Fake meeting {start_index + i}")
                              for i in range(fake.events_per_page)]
                    body = {"value": events}
                    if fake.self_referential_next:
                        body["@odata.nextLink"] = f"http://127.0.0.1:{fake.port}{self.path}"
                    elif page < fake.pages:
                        body["@odata.nextLink"] = (
                            f"http://127.0.0.1:{fake.port}{parts.path}?page={page + 1}")
                    return self._send(200, body)

                return self._send(404, {"error": "not found"})

            def do_POST(self):
                fake.requests.append(("POST", self.path, dict(self.headers)))
                # The service.svc shape: a POST whose window lives in the body.
                return self._send(200, {"Items": [_event(99, "From a POST endpoint")]})

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    # ---- helpers -------------------------------------------------------------------------------
    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def calendar_requests(self):
        return [r for r in self.requests if "calendarView" in r[1]]

    def asset_requests(self):
        """Requests for things resource blocking should have prevented."""
        return [r for r in self.requests if r[1].startswith("/assets/")]

    def json_requests(self):
        """Every JSON-ish call, including the decoys the search logic has to ignore."""
        return [r for r in self.requests if not r[1].startswith("/assets/") and r[1] != "/"]

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
