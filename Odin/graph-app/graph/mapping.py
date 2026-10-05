"""Turn Microsoft Graph calendar JSON into meeting2jira's schema-v1 export format.

*** THIS FILE IS A DUPLICATE of playwright-app/owa/mapping.py. ***

    Only two lines may differ: SOURCE and KEY_PREFIX. Everything else must stay byte-identical, and
    tests/test_mapping_drift.py fails the build if it does not. Fix bugs in BOTH copies.

    Why duplicate rather than share: each top-level folder has to be independently copyable - "copy
    this folder and it runs" - so neither may import from the other or from a third package. The
    repo already resolved this tension the same way for the Python-discovery logic, which exists in
    five copies with a drift guardrail (test_resolve_python_copies_are_identical). This is that
    pattern, not a new exception to the rule.

Position in the flow
    client.py fetches raw event JSON from Graph; this module converts it and export_graph.py writes
    the result. The output is consumed by the existing, unmodified pipeline:
    `python -m meeting2jira push --input <file>`.

Why it maps Graph and OWA with one body of code
    They are the same data model. OWA's own calendar API is an Outlook REST endpoint, and Microsoft
    documents the two as differing mainly in property casing - Graph camelCase, Outlook PascalCase -
    with everything else being the same resource types. `_field` reads either, so one mapping serves
    both. Graph is the easier of the two here: camelCase is this file's native case.

Why this file imports nothing but the standard library
    It is the part worth testing and the part most likely to be wrong, because it encodes
    assumptions about the fields Graph returns. Keeping msal and the HTTP client out of it means the
    whole mapping is testable with no network, no token and no mailbox - which is how the tests run.

The contract it must honour
    schema_version 1, exactly as app/src/windows/Export-OutlookMeetings.ps1 produces it. In
    particular `key` must be stable for the same occurrence across runs, or the dedupe in state.db
    cannot do its job. See the repo's README "Export schema".
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = 1

# These two constants are the ONLY intended difference between this file and its duplicate at
# graph-app/graph/mapping.py. Everything else must stay byte-identical, which
# graph-app/tests/test_mapping_drift.py enforces by normalizing exactly these two lines and
# comparing the rest. Keeping them as named constants is what makes "did the copy drift?" a
# mechanical question instead of a judgement call.
SOURCE = "graph-msal"
KEY_PREFIX = "graph"

# Graph/OWA responseStatus.response -> the vocabulary rules.py filters on.
_RESPONSE = {
    "none": "none",
    "organizer": "organizer",
    "tentativelyaccepted": "tentative",
    "accepted": "accepted",
    "declined": "declined",
    "notresponded": "not_responded",
}

# Graph/OWA showAs -> the vocabulary rules.py filters on.
_SHOW_AS = {
    "free": "free",
    "tentative": "tentative",
    "busy": "busy",
    "oof": "oof",
    "workingelsewhere": "elsewhere",
    "unknown": "unknown",
}

# sensitivity values that mean "keep this out of Jira"
_PRIVATE = {"private", "confidential"}

_CANCELLED_SUBJECT = re.compile(r"^\s*cancell?ed\s*:", re.IGNORECASE)


class MappingError(ValueError):
    """Raised when an event cannot be mapped. The caller skips the item and keeps going."""


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _field(node: Any, *names: str) -> Any:
    """Look up a field without caring how it is cased. Returns None if absent.

    This is not defensive programming for its own sake - it is the difference between working and
    silently exporting nothing. The same calendar data is served in two casings, and which one you
    get depends on which endpoint the web client happens to call:

        Microsoft Graph   graph.microsoft.com/v1.0    camelCase   subject, isAllDay, showAs
        Outlook endpoint  outlook.office.com/api      PascalCase  Subject, IsAllDay, ShowAs

    Microsoft states this directly in "Compare Microsoft Graph and Outlook endpoints": Graph uses
    camelCase, the Outlook endpoint uses PascalCase, and translating between them is just a case
    conversion. Because this exporter *discovers* its endpoint by watching the browser rather than
    choosing it, it does not get to assume which one it will be handed.

    The failure mode if this were camelCase-only and the tenant served PascalCase is not a crash:
    `start` would be missing, every event would be skipped as unmappable, and the run would look
    like an empty calendar. The worse variant is a mixed or partial projection, where subject and
    start map but Sensitivity and ResponseStatus do not - which would push private and declined
    meetings into Jira while reporting complete success.

    Reference: https://learn.microsoft.com/en-us/outlook/rest/compare-graph
    """
    if not isinstance(node, dict):
        return None
    lowered = {key.lower(): value for key, value in node.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _node(value: Any) -> Dict[str, Any]:
    """A dict to read sub-fields from, whatever came back."""
    return value if isinstance(value, dict) else {}


def parse_graph_datetime(node: Any) -> datetime:
    """Parse a Graph dateTimeTimeZone node into an aware UTC datetime.

    Graph returns {"dateTime": "2026-09-21T14:00:00.0000000", "timeZone": "UTC"}. The request asks
    for UTC via the Prefer header, but the timeZone field is honoured when present so a tenant or
    endpoint that ignores the header cannot silently shift every meeting by the offset.
    """
    if isinstance(node, str):
        raw, zone = node, "UTC"
    elif isinstance(node, dict):
        raw = _text(_field(node, "dateTime"))
        zone = _text(_field(node, "timeZone")) or "UTC"
    else:
        raise MappingError(f"unrecognised date node {node!r}")

    if not raw:
        raise MappingError("missing dateTime")

    text = raw.strip()
    # Graph pads to seven fractional digits, which datetime.fromisoformat rejects before 3.11.
    text = re.sub(r"\.(\d{1,6})\d*$", r".\1", text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MappingError(f"unparseable dateTime {raw!r}: {exc}") from None

    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)

    # Naive. Only "UTC" is safe to assume; anything else would need a tz database, which Windows
    # does not have without the tzdata package. Fail loudly rather than guess an offset.
    if zone.upper() not in ("UTC", "GMT", "COORDINATED UNIVERSAL TIME"):
        raise MappingError(
            f"event time {raw!r} is in {zone!r}, not UTC. Request the calendar with "
            'Prefer: outlook.timezone="UTC" so no timezone conversion is needed here.'
        )
    return parsed.replace(tzinfo=timezone.utc)


def iso_utc(value: datetime) -> str:
    """The single timestamp format the export schema uses."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def occurrence_key(event: Dict[str, Any], start: datetime) -> str:
    """A stable per-occurrence identity, which is what state.db dedupes on.

    iCalUId is preferred: it is stable for the same occurrence across clients and across runs, and
    it is shared with the COM path's notion of series identity. The series id is combined with the
    start time so each instance of a recurring meeting is distinct - the mistake that otherwise
    makes a daily standup appear once and never again.
    """
    # Each is tried in turn, and a key that is present but blank falls through to the next - an
    # empty iCalUId is no more usable than a missing one.
    series = (_text(_field(event, "iCalUId")) or _text(_field(event, "uid"))
              or _text(_field(event, "id")))
    if not series:
        raise MappingError("event has no iCalUId or id to build a stable key from")
    return f"{KEY_PREFIX}:{series}|{iso_utc(start)}"


def map_event(event: Dict[str, Any], include_organizer: bool = False) -> Dict[str, Any]:
    """One OWA/Graph event -> one schema-v1 meetings[] entry."""
    if not isinstance(event, dict):
        raise MappingError(f"expected an object, got {type(event).__name__}")

    start = parse_graph_datetime(_field(event, "start"))
    end = parse_graph_datetime(_field(event, "end"))
    if end < start:
        raise MappingError(f"end {iso_utc(end)} precedes start {iso_utc(start)}")

    subject = _text(_field(event, "subject")) or "(no subject)"

    # A meeting has other people in it; a personal appointment does not. Graph omits `attendees`
    # on some projections, so treat a missing key as unknown rather than as "no attendees", or
    # every meeting would be filtered out as an appointment.
    attendees = _field(event, "attendees")
    if attendees is None:
        is_meeting = True
    else:
        is_meeting = len([a for a in attendees if a]) > 0

    response = _RESPONSE.get(
        _text(_field(_node(_field(event, "responseStatus")), "response")).lower(), "unknown")
    show_as = _SHOW_AS.get(_text(_field(event, "showAs")).lower(), "unknown")

    raw_location = _field(event, "location")
    location = _text(_field(_node(raw_location), "displayName"))
    if not location and isinstance(raw_location, str):
        location = _text(raw_location)

    # Graph states this outright, unlike the COM path which has to match on the location string.
    provider = _text(_field(event, "onlineMeetingProvider")).lower()
    is_teams = bool(_field(event, "isOnlineMeeting")) and provider in ("teamsforbusiness", "")
    if not is_teams and "microsoft teams" in location.lower():
        is_teams = True

    organizer = None
    if include_organizer:
        node = _node(_field(_node(_field(event, "organizer")), "emailAddress"))
        organizer = _text(_field(node, "name")) or _text(_field(node, "address")) or None

    categories = [str(c).strip() for c in (_field(event, "categories") or []) if str(c).strip()]

    return {
        "key": occurrence_key(event, start),
        "subject": subject,
        "start_utc": iso_utc(start),
        "end_utc": iso_utc(end),
        "all_day": bool(_field(event, "isAllDay")),
        "is_meeting": is_meeting,
        "is_cancelled": (bool(_field(event, "isCancelled"))
                         or bool(_CANCELLED_SUBJECT.match(subject))),
        "response": response,
        "busy_status": show_as,
        "is_private": _text(_field(event, "sensitivity")).lower() in _PRIVATE,
        "location": location,
        "categories": categories,
        "organizer": organizer,
        "is_teams": is_teams,
    }


def build_export(events: Iterable[Dict[str, Any]], range_start: datetime, range_end: datetime,
                 include_organizer: bool = False,
                 on_skip: Optional[Any] = None) -> Dict[str, Any]:
    """Map every event and wrap them in the schema-v1 envelope.

    A single unmappable event is skipped with a reason rather than aborting the export: one odd
    item on the calendar must not cost the user the whole day's meetings. on_skip, if given, is
    called with (index, reason).
    """
    mapped: List[Dict[str, Any]] = []
    seen = set()
    for index, event in enumerate(events):
        try:
            item = map_event(event, include_organizer=include_organizer)
        except MappingError as exc:
            if on_skip:
                on_skip(index, str(exc))
            continue
        # The same occurrence can appear twice when paging overlaps a series expansion.
        if item["key"] in seen:
            continue
        seen.add(item["key"])
        mapped.append(item)

    mapped.sort(key=lambda m: m["start_utc"])
    return {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "exported_at": iso_utc(datetime.now(timezone.utc)),
        "range_start": iso_utc(range_start),
        "range_end": iso_utc(range_end),
        "meetings": mapped,
    }


def window(days_back: int, now: Optional[datetime] = None):
    """The scan window: midnight local `days_back` days ago through now, as aware UTC.

    Deliberately generous. Re-running over the same window cannot create duplicates - state.db
    dedupes on key or content hash - so a wider window is free and is the correct answer to
    "did I miss a meeting that ran late".
    """
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone()
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight - timedelta(days=max(0, days_back))
    return start.astimezone(timezone.utc), now
