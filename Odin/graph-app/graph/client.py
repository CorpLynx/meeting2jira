"""Read /me/calendarView from Microsoft Graph over urllib.

Why urllib and not requests
    msal is the one dependency this folder takes, and it is taken because hand-rolled OAuth is
    genuinely risky. An HTTP GET is not risky. Adding `requests` would double the pip surface for no
    safety gain, and the project already has a working stdlib HTTP client in Asgard/apps/odin/odin/
    jira.py to match conventions against.

What this inherits from the OWA path
    The paging and retry logic here is the same shape as playwright-app/owa/capture.py, for the same
    reasons and because those reasons were learned the hard way: an unbounded nextLink loop, a
    truncation that reports nothing, and a retry that masks a real fault are all failures that look
    like success. A scheduled task makes every one of them expensive to notice.

Data minimization
    $select is explicit, so the response carries only the fields the filters actually use. Combined
    with Calendars.ReadBasic that is two independent reasons the meeting body never arrives - and
    unlike the permission, $select is visible in this file and provable by reading it.

    `attendees` is NOT selected by default. The mapping only ever counts attendees, to tell a meeting
    from a personal appointment, but counting still means the names come over the wire. The cost of
    leaving it out is that every item looks like a meeting; see with_attendee_count.
"""
from __future__ import annotations

import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

GRAPH_VERSION = "v1.0"

# Exactly what mapping.py reads, and nothing else.
SELECT_FIELDS = (
    "id", "iCalUId", "subject", "start", "end", "isAllDay", "isCancelled", "responseStatus",
    "showAs", "sensitivity", "location", "categories", "isOnlineMeeting", "onlineMeetingProvider",
    "organizer", "type", "seriesMasterId",
)

# Only requested when the caller opts in. See the module docstring.
ATTENDEE_FIELD = "attendees"

# Graph caps $top for calendarView well below this, but asking for more costs nothing and reduces
# round trips when the service allows it.
PAGE_SIZE = 500

# Bounds, so a pathological response cannot run forever or exhaust memory. Both announce themselves
# when they fire: a silently truncated export is worse than a failed one.
MAX_PAGES = 50
MAX_EVENTS = 20_000

RETRY_STATUS = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 3


class GraphError(Exception):
    """A Graph call failed. The message must say what to do next."""


class TokenExpired(GraphError):
    """401. The caller may re-authenticate once and retry."""


def _ssl_context(ca_bundle: str = "") -> ssl.SSLContext:
    """A verifying context, always.

    No switch disables verification here and none should ever be added. On Windows, Python already
    trusts the machine certificate store, which is why a TLS-inspecting agency proxy normally works
    with no configuration at all; ca_bundle exists for the cases where it does not.
    """
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)
    return ssl.create_default_context()


class GraphClient:
    def __init__(self, base: str, token: str, timeout: int = 30, ca_bundle: str = "",
                 opener: Optional[Any] = None):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._context = _ssl_context(ca_bundle)
        # Injectable so the tests can drive a local http.server without monkeypatching urllib
        # globally. Everything below this line is exercised offline because of it.
        self._opener = opener

    # -- plumbing --------------------------------------------------------------------------------
    def _open(self, request: "urllib.request.Request"):
        if self._opener is not None:
            return self._opener(request, timeout=self.timeout)
        return urllib.request.urlopen(request, timeout=self.timeout, context=self._context)

    def get_json(self, url: str) -> Dict[str, Any]:
        """GET with bounded retries. Returns the parsed body."""
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            request = urllib.request.Request(url, method="GET")
            request.add_header("Authorization", "Bearer {}".format(self.token))
            request.add_header("Accept", "application/json")
            # Removes an entire class of timezone bug: Graph converts to UTC server-side, so
            # mapping.py never has to know anything about time zones. Windows has no tz database
            # without the tzdata package, so this is not a convenience, it is the mechanism.
            request.add_header("Prefer", 'outlook.timezone="UTC"')

            try:
                response = self._open(request)
            except urllib.error.HTTPError as exc:
                body = _safe_read(exc)
                if exc.code == 401:
                    raise TokenExpired(
                        "Graph rejected the token (401). The access token has expired or been "
                        "revoked. Run `odin-graph login` and retry.") from None
                if exc.code == 403:
                    raise GraphError(
                        "Graph returned 403 Forbidden. The application is authenticated but lacks "
                        "the calendar permission, or admin consent was never granted. Ask IT to "
                        "confirm the delegated permission and consent.\n{}".format(
                            _graph_message(body))) from None
                if exc.code == 404:
                    raise GraphError(
                        "Graph returned 404 for {}. Check the cloud setting in graph.json: a GCC "
                        "High or DoD mailbox is not reachable from the commercial Graph "
                        "host.".format(url.split("?")[0])) from None
                if exc.code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                    delay = _retry_after(exc, attempt)
                    log.debug("Graph returned %s; retrying in %.1fs (attempt %d of %d).",
                              exc.code, delay, attempt, MAX_ATTEMPTS)
                    time.sleep(delay)
                    last_error = "HTTP {}".format(exc.code)
                    continue
                raise GraphError("Graph returned HTTP {}: {}".format(
                    exc.code, _graph_message(body))) from None
            except (urllib.error.URLError, socket.timeout, ssl.SSLError) as exc:
                # A proxy hiccup or a dropped VPN. Retry, then say so plainly.
                if attempt < MAX_ATTEMPTS:
                    delay = min(2 ** attempt, 8)
                    log.debug("Could not reach Graph (%s); retrying in %ds.", exc, delay)
                    time.sleep(delay)
                    last_error = str(exc)
                    continue
                raise GraphError(
                    "Could not reach Graph ({}). Check VPN and proxy. If TLS inspection is in use "
                    "and this is a certificate error, set ca_bundle in graph.json to your agency's "
                    "PEM chain.".format(exc)) from None

            with response:
                raw = response.read()
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError as exc:
                raise GraphError(
                    "Graph returned a non-JSON body ({}). A captive portal or proxy sign-in page "
                    "is the usual cause.".format(exc)) from None

        raise GraphError("Graph did not respond successfully after {} attempts ({}).".format(
            MAX_ATTEMPTS, last_error))

    # -- the one call we make --------------------------------------------------------------------
    def calendar_view_url(self, start: datetime, end: datetime,
                          with_attendee_count: bool = False) -> str:
        fields = list(SELECT_FIELDS)
        if with_attendee_count:
            fields.append(ATTENDEE_FIELD)
        query = [
            ("startDateTime", _iso(start)),
            ("endDateTime", _iso(end)),
            ("$select", ",".join(fields)),
            ("$orderby", "start/dateTime"),
            ("$top", str(PAGE_SIZE)),
        ]
        return "{}/{}/me/calendarView?{}".format(
            self.base, GRAPH_VERSION, urllib.parse.urlencode(query))

    def calendar_view(self, start: datetime, end: datetime,
                      with_attendee_count: bool = False) -> List[Dict[str, Any]]:
        """Every occurrence between start and end, following paging.

        calendarView is the right endpoint rather than /me/events because it expands recurring
        series into individual occurrences server-side. /me/events returns the series master, and
        a daily standup would then appear once and never again.
        """
        url = self.calendar_view_url(start, end, with_attendee_count=with_attendee_count)
        events: List[Dict[str, Any]] = []
        seen_urls = set()
        pages = 0

        while url:
            # A nextLink pointing at a page already fetched would spin forever. Rare, cheap to rule
            # out, and an infinite loop inside a scheduled task is expensive to discover.
            if url in seen_urls:
                log.warning("Paging stopped: Graph returned a link to a page already fetched.")
                break
            seen_urls.add(url)
            pages += 1

            payload = self.get_json(url)
            batch = [item for item in (payload.get("value") or []) if isinstance(item, dict)]
            events.extend(batch)
            log.debug("Page %d: %d event(s), %d so far.", pages, len(batch), len(events))

            if len(events) >= MAX_EVENTS:
                log.warning("Stopped at %d events (cap %d). Narrow the window.",
                            len(events), MAX_EVENTS)
                break
            url = payload.get("@odata.nextLink") or None
            if url and pages >= MAX_PAGES:
                log.warning("Stopped after %d pages. Narrow the window if meetings seem to be "
                            "missing.", MAX_PAGES)
                break
        return events

    def whoami(self) -> Dict[str, Any]:
        """Minimal identity check, for `check`. Confirms the token works before touching calendars."""
        return self.get_json("{}/{}/me?$select=displayName,userPrincipalName,mail".format(
            self.base, GRAPH_VERSION))


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_read(exc: "urllib.error.HTTPError") -> str:
    try:
        return exc.read().decode("utf-8", "replace")
    except Exception:                    # noqa: BLE001 - diagnosis must not raise
        return ""


def _graph_message(body: str) -> str:
    """Pull Graph's own error message out of its envelope; fall back to the raw body."""
    try:
        parsed = json.loads(body)
        error = parsed.get("error") or {}
        message = error.get("message") or ""
        code = error.get("code") or ""
        if message:
            return "{} {}".format(code, message).strip()
    except Exception:                    # noqa: BLE001
        pass
    return (body or "").strip()[:500]


def _retry_after(exc: "urllib.error.HTTPError", attempt: int) -> float:
    """Honour Retry-After when Graph throttles, because guessing is what gets you throttled harder."""
    header = ""
    try:
        header = exc.headers.get("Retry-After") or ""
    except Exception:                    # noqa: BLE001
        pass
    try:
        if header:
            return max(0.0, min(float(header), 60.0))
    except ValueError:
        pass
    return float(min(2 ** attempt, 8))
