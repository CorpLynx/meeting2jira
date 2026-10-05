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
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Only the user's own calendar. Shared, delegate and other people's calendars are out of scope.
CALENDAR_URL = "https://outlook.office.com/calendar/view/workweek"

# Two tiers, because one broad pattern is actively dangerous here. The client fires a telemetry
# beacon at /owa/telemetry/events whose payload is an array of records carrying start and end - so a
# single loose "URL contains /events, and the body has start" test picks the beacon instead of the
# calendar, and the export then succeeds with nonsense. A strong match is accepted immediately; a
# weak one is held as a fallback in case nothing better arrives.
_CALENDAR_STRONG = re.compile(
    r"(calendarview|getcalendarview|calendar/events|calendaritems|/me/events|finditem)",
    re.IGNORECASE)
_CALENDAR_WEAK = re.compile(r"(/events|/calendar|instantsearch)", re.IGNORECASE)

# Telemetry and analytics. Blocking these saves real bandwidth on a real tenant and keeps them out
# of the candidate pool entirely.
_BLOCKED_HOSTS = ("browser.events.data.microsoft.com", "js.monitor.azure.com", "dc.services.visualstudio.com",
                  "aria.microsoft.com", "/telemetry/", "clarity.ms", "nexus.officeapps.live.com")

# Bodies larger than this are not parsed during discovery. A calendar page of events is tens of KB;
# anything vastly bigger is a bundle or a blob, and parsing it wastes time on every response.
_MAX_DISCOVERY_BODY = 4 * 1024 * 1024

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


def _event_like(item: Any) -> bool:
    """Is this object plausibly a calendar event, as opposed to some other record with a start?

    The distinction that matters: a telemetry record has start and end as numbers, while a calendar
    event has either a subject or a start that is a date. Testing only for the presence of "start"
    cannot tell them apart, and picking the wrong one produces an export full of nonsense.
    """
    if not isinstance(item, dict):
        return False
    if any(k in item for k in ("subject", "Subject")):
        return True
    start = item.get("start", item.get("Start"))
    if isinstance(start, dict):
        return any(k in start for k in ("dateTime", "DateTime"))
    if isinstance(start, str):
        # An ISO-ish date, e.g. 2026-09-22T13:30:00Z
        return len(start) >= 10 and start[4:5] == "-" and start[7:8] == "-"
    return False


# Fields a calendar event carries that other dated records do not. Used only to rank weak candidates
# against one another - never to accept or reject one - so an endpoint that omits some of these is
# still usable. Graph's "insights" list is the case that forced this: it is a list of documents shown
# around meetings, so it has both a subject and a dateTime start and passes every shape check, yet it
# has none of the fields below.
_MEETING_FIELDS = ("end", "isallday", "showas", "responsestatus", "organizer", "sensitivity",
                   "icaluid", "iscancelled", "location", "isonlinemeeting", "seriesmasterid",
                   "recurrence", "categories", "weblink")


def _events_score(payload: Any) -> int:
    """How calendar-like is this body? Higher wins when only weak candidates are available."""
    if not isinstance(payload, dict):
        return 0
    for key in ("value", "Items", "items", "Events"):
        candidate = payload.get(key)
        if isinstance(candidate, list) and candidate and isinstance(candidate[0], dict):
            keys = {k.lower() for k in candidate[0]}
            return sum(1 for field in _MEETING_FIELDS if field in keys)
    return 0


def _looks_like_events(payload: Any) -> bool:
    """Does this JSON body actually carry calendar events?

    Shape as well as URL, because neither alone is sufficient: the URL cannot distinguish a calendar
    read from a telemetry beacon, and the shape cannot distinguish it from any other dated record.
    """
    if not isinstance(payload, dict):
        return False
    for key in ("value", "Items", "items", "Events"):
        candidate = payload.get(key)
        if isinstance(candidate, list) and candidate and _event_like(candidate[0]):
            return True
    return False


def _install_resource_blocking(context) -> None:
    def route_handler(route):
        try:
            request_url = route.request.url
            if route.request.resource_type in _BLOCKED_RESOURCES or \
                    any(host in request_url for host in _BLOCKED_HOSTS):
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


def observe_calendar_endpoint(context, timeout_ms: int = 60_000, calendar_url: Optional[str] = None
                              ) -> Tuple[ObservedEndpoint, List[Dict[str, Any]]]:
    """Load the calendar once and capture the first request that returns events.

    Returns the endpoint we can reuse, plus the events that request happened to return - so even a
    run that only does this much already has usable data.
    """
    # Injectable so the browser layer can be exercised against a local fake OWA. Without this the
    # discovery, header-replay, window-rewrite and paging logic could only ever be tested against
    # a live mailbox, which means in practice it was not tested at all.
    calendar_url = calendar_url or CALENDAR_URL
    page = context.new_page()
    captured: Dict[str, Any] = {}

    fallback: Dict[str, Any] = {}

    def on_response(response):
        if captured:
            return
        strong = bool(_CALENDAR_STRONG.search(response.url))
        if not strong and not _CALENDAR_WEAK.search(response.url):
            return
        try:
            if "json" not in (response.headers.get("content-type") or "").lower():
                return
            # Skip parsing anything implausibly large; it is a bundle, not a page of meetings.
            length = response.headers.get("content-length")
            if length and int(length) > _MAX_DISCOVERY_BODY:
                return
            payload = response.json()
        except Exception:      # noqa: BLE001 - partial or non-JSON bodies are simply not it
            return
        if not _looks_like_events(payload):
            return

        # Weak candidates compete on how calendar-like their payload is, rather than on arrival
        # order. Keeping the *first* weak hit looks equivalent and is not: the decoys are fetched
        # during page startup and the calendar is fetched last, so first-wins reliably picks a decoy
        # the moment the real endpoint is renamed to something the strong tier no longer matches.
        score = 0 if strong else _events_score(payload)
        if not strong and fallback and score <= fallback.get("score", -1):
            return

        request = response.request
        headers = {}
        try:
            for name, value in request.all_headers().items():
                if name.lower() in _REPLAY_HEADERS:
                    headers[name] = value
        except Exception:      # noqa: BLE001
            pass
        found = {"endpoint": ObservedEndpoint(request.url, request.method, headers,
                                             request.post_data),
                 "payload": payload}
        if strong:
            captured.update(found)
            log.info("Observed the calendar API: %s %s", request.method, request.url.split("?")[0])
        else:
            fallback.clear()
            fallback.update(found)
            fallback["score"] = score
            log.debug("Holding a weak candidate in reserve (score %d): %s",
                      score, request.url.split("?")[0])

    context.on("response", on_response)
    at_sign_in = False
    try:
        page.goto(calendar_url, wait_until="domcontentloaded")

        # Block on the event rather than polling. A poll loop adds up to half its interval in
        # latency on every run and burns CPU while it waits; wait_for_event returns the instant a
        # matching response lands. The predicate is deliberately cheap - URL and content type only -
        # because it runs for every response on the page; the expensive shape check happens in the
        # handler above, which has already recorded the result by the time this returns.
        def is_candidate(response):
            if not (_CALENDAR_STRONG.search(response.url) or _CALENDAR_WEAK.search(response.url)):
                return False
            return "json" in (response.headers.get("content-type") or "").lower()

        # A real deadline, measured. wait_for_event returns as soon as *any* candidate response
        # arrives - including a decoy that then fails the shape check - so charging the full slice
        # against the budget each time would abandon discovery seconds into a 60-second timeout.
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while not captured:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            try:
                # Capped per wait so a sign-in redirect is noticed promptly rather than at the end.
                page.wait_for_event("response", predicate=is_candidate,
                                    timeout=min(remaining_ms, 5_000))
            except Exception:  # noqa: BLE001 - a timeout here just means "nothing yet"
                if _looks_like_sign_in(page):
                    at_sign_in = True
                    break
                if fallback:
                    # A full quiet slice has passed with no stronger match, so the page has
                    # finished asking. That quiet period is also the grace window in which better
                    # weak candidates get a chance to outrank an early decoy.
                    break
    finally:
        context.remove_listener("response", on_response)
        # Ask the page what it is *before* closing it. A closed page answers nothing: querying it
        # for a password field raises, _looks_like_sign_in swallows that and returns False, and the
        # expired-session diagnosis silently degrades into the generic "never saw events" message -
        # sending the user to --debug-endpoints when all they needed was --login.
        if not captured and not at_sign_in:
            at_sign_in = _looks_like_sign_in(page)
        # Closing the page frees its renderer process. Leaving it open held a browser tab alive for
        # the rest of the run, which on a long paged export is pure waste.
        try:
            page.close()
        except Exception:      # noqa: BLE001
            pass

    if not captured and fallback:
        captured.update(fallback)
        log.info("No strongly-matching calendar endpoint seen; using the best candidate: %s",
                 captured["endpoint"].url.split("?")[0])

    if not captured:
        if at_sign_in:
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
                 max_pages: int = 20, max_events: int = 20_000) -> List[Dict[str, Any]]:
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
    seen_urls = set()

    while url and pages < max_pages:
        # A server that returns a nextLink pointing at the current page would loop forever. Rare,
        # but cheap to rule out, and an infinite loop in a scheduled task is expensive to discover.
        if url in seen_urls:
            log.warning("Paging stopped: the endpoint returned a link to a page already fetched.")
            break
        seen_urls.add(url)
        pages += 1

        # GET is idempotent, so one retry is safe and covers the single most common real failure:
        # a transient blip on an agency network or proxy. Anything persistent still surfaces.
        response = None
        for attempt in (1, 2):
            try:
                response = context.request.get(url, headers=headers)
            except Exception as exc:  # noqa: BLE001 - connection reset, DNS, proxy hiccup
                if attempt == 2:
                    raise CaptureError(
                        f"The calendar endpoint could not be reached on page {pages}: {exc}. Check "
                        "VPN and proxy, then re-run.") from None
                log.debug("Page %d attempt %d failed (%s); retrying once.", pages, attempt, exc)
                continue
            if response.status in (429, 502, 503, 504) and attempt == 1:
                log.debug("Page %d returned %s; retrying once.", pages, response.status)
                continue
            break

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
        if len(events) >= max_events:
            log.warning("Stopped at %d events. Narrow the window, or raise max_events.", len(events))
            break
        url = payload.get("@odata.nextLink") or payload.get("NextLink") or None
        if url:
            log.debug("Following page %d (%d events so far)", pages + 1, len(events))

    if pages >= max_pages and url:
        log.warning("Stopped after %d pages; narrow the window if meetings appear to be missing.",
                    max_pages)
    return events


def debug_endpoints(context, seconds: int = 20, calendar_url: Optional[str] = None) -> List[str]:
    """Print every JSON request the calendar page makes. The escape hatch when discovery fails."""
    calendar_url = calendar_url or CALENDAR_URL
    page = context.new_page()
    seen: List[str] = []

    def on_response(response):
        content_type = (response.headers.get("content-type") or "").lower()
        if "json" in content_type:
            if _CALENDAR_STRONG.search(response.url):
                marker = "  <-- looks like the calendar API"
            elif _CALENDAR_WEAK.search(response.url):
                marker = "  <-- possible, but weakly matched"
            else:
                marker = ""
            seen.append(f"{response.status} {response.request.method} {response.url}{marker}")

    context.on("response", on_response)
    try:
        page.goto(calendar_url, wait_until="domcontentloaded")
        page.wait_for_timeout(seconds * 1000)
    finally:
        context.remove_listener("response", on_response)
        page.close()
    return seen
