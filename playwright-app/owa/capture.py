"""Obtain calendar events from Outlook on the web with Playwright.

The strategy, and why it is this one
    Scraping the rendered calendar is the obvious approach and the wrong one. The grid is a
    virtualised React view: it renders only visible rows, its class names are generated, and the
    fields the filters actually depend on - response status, sensitivity, categories, recurrence
    identity - are not in the DOM at all. A DOM scraper would silently lose skip_declined and
    skip_private, which is the worst possible failure for this tool.

    So the browser is used for exactly one thing: being an authenticated session. We observe the
    one request OWA makes to its own calendar API, learn the endpoint and the auth headers from it,
    and then call that endpoint ourselves for the precise window we want. That yields the same
    structured JSON the web client renders from, including every field the filters need.

Speed, in the order the wins actually matter
    1. Session reuse. A persistent profile means no interactive sign-in after the first run, which
       removes the only genuinely slow step (and all of the MFA prompts).
    2. Use installed Edge (channel="msedge") rather than downloading Chromium. On a managed machine
       this avoids a ~150MB download of an unsigned binary, and uses the browser IT already patches.
    3. Block images, media, fonts and stylesheets. The calendar's JSON arrives just the same, and
       the page stops fetching several MB of things we will never look at.
    4. One render, then direct API calls. After the endpoint is known we page through the window
       ourselves, so a 30-day export costs the same page load as a 1-day export.
    5. Headless once a session exists.

    Nothing here polls or sleeps on a fixed timer; it waits on events.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Only the user's own calendar. Shared, delegate and other people's calendars are out of scope.
CALENDAR_URL = "https://outlook.office.com/calendar/view/workweek"

# Requests whose URL looks like a calendar read. Kept broad on purpose: Microsoft has shipped
# several shapes of this endpoint, and the point is to observe whichever one this tenant uses
# rather than to hardcode a guess that breaks on the next change.
_CALENDAR_REQUEST = re.compile(
    r"(calendarview|calendarView|/events|GetCalendarView|FindItem|InstantSearch)", re.IGNORECASE)

# Headers worth replaying. Authorization is the important one; the OWA canary and routing hints
# matter for the service.svc shaped endpoints.
_REPLAY_HEADERS = ("authorization", "x-owa-canary", "x-anchormailbox", "x-routingparameter-sessionkey",
                   "x-owa-urlpostdata", "prefer", "client-request-id", "x-ms-client-request-id")

# Everything the calendar JSON does not depend on.
_BLOCKED_RESOURCES = {"image", "media", "font", "stylesheet"}


class CaptureError(RuntimeError):
    """Raised when events could not be obtained. The message says what to do next."""


class ObservedEndpoint:
    """What we learned by watching the web client talk to its own API."""

    def __init__(self, url: str, method: str, headers: Dict[str, str], body: Optional[str] = None):
        self.url = url
        self.method = method.upper()
        self.headers = headers
        self.body = body

    def __repr__(self) -> str:
        return f"<ObservedEndpoint {self.method} {self.url.split('?')[0]}>"


def _looks_like_events(payload: Any) -> bool:
    """Does this JSON body actually carry calendar events?

    Matching on shape rather than on URL, because the URL alone is not enough to tell a calendar
    read from any other OData call the client happens to make.
    """
    if not isinstance(payload, dict):
        return False
    for key in ("value", "Items", "items", "Events"):
        candidate = payload.get(key)
        if isinstance(candidate, list) and candidate:
            first = candidate[0]
            if isinstance(first, dict) and ("start" in first or "Start" in first):
                return True
    return False


def _install_resource_blocking(context) -> None:
    def route_handler(route):
        try:
            if route.request.resource_type in _BLOCKED_RESOURCES:
                route.abort()
            else:
                route.continue_()
        except Exception:      # noqa: BLE001 - a routing race must never fail the export
            try:
                route.continue_()
            except Exception:  # noqa: BLE001
                pass

    context.route("**/*", route_handler)


def open_session(playwright, profile_dir: str, headless: bool = True,
                 channel: str = "msedge", timeout_ms: int = 60_000,
                 block_resources: bool = True):
    """Open a browser context that reuses the persistent profile.

    A persistent profile is what makes this usable unattended: the first run signs in interactively
    and every later run reuses that session, so no password is ever handled by this code and
    Conditional Access sees the same browser it already trusted.

    block_resources must be False for interactive sign-in. Aborting stylesheets and images speeds up
    an unattended run, but it makes the sign-in page render as unstyled HTML, which is both alarming
    and hard to use for the human who has to click through MFA.
    """
    launch_kwargs = {
        "user_data_dir": profile_dir,
        "headless": headless,
        "args": ["--disable-extensions", "--disable-background-networking"],
    }
    try:
        context = playwright.chromium.launch_persistent_context(channel=channel, **launch_kwargs)
    except Exception as exc:  # noqa: BLE001 - Edge may not be present; fall back to bundled Chromium
        log.warning("Could not launch the installed %s (%s); falling back to bundled Chromium. "
                    "That needs `playwright install chromium` to have been run.", channel, exc)
        context = playwright.chromium.launch_persistent_context(**launch_kwargs)

    context.set_default_timeout(timeout_ms)
    if block_resources:
        _install_resource_blocking(context)
    return context


def observe_calendar_endpoint(context, timeout_ms: int = 60_000
                              ) -> Tuple[ObservedEndpoint, List[Dict[str, Any]]]:
    """Load the calendar once and capture the first request that returns events.

    Returns the endpoint we can reuse, plus the events that request happened to return - so even a
    run that only does this much already has usable data.
    """
    page = context.new_page()
    captured: Dict[str, Any] = {}

    def on_response(response):
        if captured:
            return
        if not _CALENDAR_REQUEST.search(response.url):
            return
        try:
            if "json" not in (response.headers.get("content-type") or "").lower():
                return
            payload = response.json()
        except Exception:      # noqa: BLE001 - partial or non-JSON bodies are simply not it
            return
        if not _looks_like_events(payload):
            return
        request = response.request
        headers = {}
        try:
            for name, value in request.all_headers().items():
                if name.lower() in _REPLAY_HEADERS:
                    headers[name] = value
        except Exception:      # noqa: BLE001
            pass
        captured["endpoint"] = ObservedEndpoint(request.url, request.method, headers,
                                                request.post_data)
        captured["payload"] = payload
        log.info("Observed the calendar API: %s %s", request.method, request.url.split("?")[0])

    context.on("response", on_response)
    try:
        page.goto(CALENDAR_URL, wait_until="domcontentloaded")

        # Block on the event rather than polling. A poll loop adds up to half its interval in
        # latency on every run and burns CPU while it waits; wait_for_event returns the instant a
        # matching response lands. The predicate is deliberately cheap - URL and content type only -
        # because it runs for every response on the page; the expensive shape check happens in the
        # handler above, which has already recorded the result by the time this returns.
        def is_candidate(response):
            return bool(_CALENDAR_REQUEST.search(response.url)) and \
                "json" in (response.headers.get("content-type") or "").lower()

        remaining = timeout_ms
        while not captured and remaining > 0:
            slice_ms = min(remaining, 15_000)
            try:
                page.wait_for_event("response", predicate=is_candidate, timeout=slice_ms)
            except Exception:  # noqa: BLE001 - a timeout here just means "nothing yet"
                # Sliced so a sign-in redirect is noticed promptly instead of after the full wait.
                if _looks_like_sign_in(page):
                    break
            remaining -= slice_ms
    finally:
        context.remove_listener("response", on_response)

    if not captured:
        if _looks_like_sign_in(page):
            raise CaptureError(
                "Outlook on the web is asking for sign-in. Run once with --login (which opens a "
                "visible browser), complete sign-in including MFA, then re-run normally: the "
                "session is kept in the profile directory and reused.")
        raise CaptureError(
            "Loaded the calendar but never saw a request returning events. Re-run with "
            "--login --debug-endpoints to print every URL the page requested, then pass the right "
            "one with --endpoint. Microsoft changes this endpoint periodically.")

    return captured["endpoint"], extract_events(captured["payload"])


def _looks_like_sign_in(page) -> bool:
    try:
        url = page.url or ""
        if "login.microsoftonline" in url or "adfs" in url.lower():
            return True
        return page.locator("input[type=password]").count() > 0
    except Exception:  # noqa: BLE001
        return False


def extract_events(payload: Any) -> List[Dict[str, Any]]:
    """Pull the event array out of whichever envelope this endpoint uses."""
    if not isinstance(payload, dict):
        return []
    for key in ("value", "Items", "items", "Events"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _with_window(url: str, start: datetime, end: datetime) -> str:
    """Rewrite the observed URL's date range to the window we actually want.

    The web client asks for whatever the visible view needs. We want our own range, which is what
    turns "render the calendar and take what it fetched" into a precise, single-request export.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    iso_start = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    iso_end = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    replaced = False
    for key in list(query):
        low = key.lower()
        if low in ("startdatetime", "start", "starttime"):
            query[key] = iso_start
            replaced = True
        elif low in ("enddatetime", "end", "endtime"):
            query[key] = iso_end
            replaced = True
    if not replaced:
        query["startDateTime"] = iso_start
        query["endDateTime"] = iso_end

    # Ask for as much as the endpoint will give per page, to minimise round trips.
    for key in list(query):
        if key.lower() in ("$top", "top"):
            query[key] = "1000"
    query.setdefault("$top", "1000")

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def fetch_window(context, endpoint: ObservedEndpoint, start: datetime, end: datetime,
                 max_pages: int = 20) -> List[Dict[str, Any]]:
    """Call the observed endpoint directly for our window, following paging.

    context.request shares the browser's cookies, so this is authenticated without re-handling any
    credential. Asking for UTC means mapping.py never has to convert a timezone.
    """
    if endpoint.method != "GET":
        # Some shapes of this endpoint (the service.svc / FindItem family) are POSTs whose window
        # lives inside an opaque request body. Rewriting that body reliably is not something to
        # guess at, and silently issuing a GET against a POST endpoint would fail in a way that
        # looks like "no meetings found". So say what happened and let the caller use the events
        # discovery already returned.
        raise CaptureError(
            f"The calendar API on this tenant is a {endpoint.method}, whose date range is inside the "
            "request body rather than the URL, so the exact-window fast path does not apply. The "
            "events seen while loading the calendar are still usable: re-run with --raw-out to keep "
            "them, and note the window will be whatever the web view showed rather than --days-back. "
            "Run --debug-endpoints to check whether a GET-shaped calendar endpoint is also available.")

    headers = dict(endpoint.headers)
    headers.setdefault("Accept", "application/json")
    # Removes an entire class of timezone bug: the response comes back already in UTC.
    headers["Prefer"] = 'outlook.timezone="UTC"'

    url = _with_window(endpoint.url, start, end)
    events: List[Dict[str, Any]] = []
    pages = 0

    while url and pages < max_pages:
        pages += 1
        response = context.request.get(url, headers=headers)
        if not response.ok:
            if pages == 1:
                raise CaptureError(
                    f"The calendar endpoint returned HTTP {response.status} when called directly. "
                    "The session may have expired: re-run with --login. If it persists, the "
                    "endpoint likely needs a header this build did not replay; use "
                    "--debug-endpoints to inspect.")
            log.warning("Paging stopped at page %d: HTTP %s", pages, response.status)
            break
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise CaptureError(f"Calendar endpoint returned non-JSON on page {pages}: {exc}") from None

        batch = extract_events(payload)
        events.extend(batch)
        url = payload.get("@odata.nextLink") or payload.get("NextLink") or None
        if url:
            log.debug("Following page %d (%d events so far)", pages + 1, len(events))

    if pages >= max_pages and url:
        log.warning("Stopped after %d pages; narrow the window if meetings appear to be missing.",
                    max_pages)
    return events


def debug_endpoints(context, seconds: int = 20) -> List[str]:
    """Print every JSON request the calendar page makes. The escape hatch when discovery fails."""
    page = context.new_page()
    seen: List[str] = []

    def on_response(response):
        content_type = (response.headers.get("content-type") or "").lower()
        if "json" in content_type:
            marker = "  <-- looks like events" if _CALENDAR_REQUEST.search(response.url) else ""
            seen.append(f"{response.status} {response.request.method} {response.url}{marker}")

    context.on("response", on_response)
    try:
        page.goto(CALENDAR_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(seconds * 1000)
    finally:
        context.remove_listener("response", on_response)
    return seen
