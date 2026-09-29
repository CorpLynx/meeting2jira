"""Turn calendar JSON captured from OWA into meeting2jira's schema-v1 export format.

Position in the flow
    capture.py obtains raw event JSON from the browser session; this module converts it and
    export_owa.py writes the result. The output is consumed by the existing, unmodified pipeline:
    `python -m meeting2jira push --input <file>`.

Why this file imports nothing but the standard library
    It is the part worth testing, and it is the part most likely to be wrong, because it encodes
    assumptions about fields OWA returns. Keeping Playwright out of it means the whole mapping is
    testable on any machine with no browser, no network, and no login - which is how the tests in
    tests/test_mapping.py run.

The contract it must honour
    schema_version 1, exactly as powershell/Export-OutlookMeetings.ps1 produces it. In particular
    `key` must be stable for the same occurrence across runs, or the dedupe in state.db cannot do
    its job. See the repo's README "Export schema".
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = 1
SOURCE = "owa-playwright"

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


def parse_graph_datetime(node: Any) -> datetime:
    """Parse a Graph dateTimeTimeZone node into an aware UTC datetime.

    Graph returns {"dateTime": "2026-09-21T14:00:00.0000000", "timeZone": "UTC"}. The request asks
    for UTC via the Prefer header, but the timeZone field is honoured when present so a tenant or
    endpoint that ignores the header cannot silently shift every meeting by the offset.
    """
    if isinstance(node, str):
        raw, zone = node, "UTC"
    elif isinstance(node, dict):
        raw, zone = _text(node.get("dateTime")), _text(node.get("timeZone")) or "UTC"
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
    series = _text(event.get("iCalUId")) or _text(event.get("uid")) or _text(event.get("id"))
    if not series:
        raise MappingError("event has no iCalUId or id to build a stable key from")
    return f"owa:{series}|{iso_utc(start)}"


def map_event(event: Dict[str, Any], include_organizer: bool = False) -> Dict[str, Any]:
    """One OWA/Graph event -> one schema-v1 meetings[] entry."""
    if not isinstance(event, dict):
        raise MappingError(f"expected an object, got {type(event).__name__}")

    start = parse_graph_datetime(event.get("start"))
    end = parse_graph_datetime(event.get("end"))
    if end < start:
        raise MappingError(f"end {iso_utc(end)} precedes start {iso_utc(start)}")

    subject = _text(event.get("subject")) or "(no subject)"

    # A meeting has other people in it; a personal appointment does not. Graph omits `attendees`
    # on some projections, so treat a missing key as unknown rather than as "no attendees", or
    # every meeting would be filtered out as an appointment.
    attendees = event.get("attendees")
    if attendees is None:
        is_meeting = True
    else:
        is_meeting = len([a for a in attendees if a]) > 0

    response = _RESPONSE.get(
        _text((event.get("responseStatus") or {}).get("response")).lower(), "unknown")
    show_as = _SHOW_AS.get(_text(event.get("showAs")).lower(), "unknown")

    location = _text((event.get("location") or {}).get("displayName"))
    if not location:
        location = _text(event.get("location") if isinstance(event.get("location"), str) else "")

    # Graph states this outright, unlike the COM path which has to match on the location string.
    provider = _text(event.get("onlineMeetingProvider")).lower()
    is_teams = bool(event.get("isOnlineMeeting")) and provider in ("teamsforbusiness", "")
    if not is_teams and "microsoft teams" in location.lower():
        is_teams = True

    organizer = None
    if include_organizer:
        node = (event.get("organizer") or {}).get("emailAddress") or {}
        organizer = _text(node.get("name")) or _text(node.get("address")) or None

    categories = [str(c).strip() for c in (event.get("categories") or []) if str(c).strip()]

    return {
        "key": occurrence_key(event, start),
        "subject": subject,
        "start_utc": iso_utc(start),
        "end_utc": iso_utc(end),
        "all_day": bool(event.get("isAllDay")),
        "is_meeting": is_meeting,
        "is_cancelled": bool(event.get("isCancelled")) or bool(_CANCELLED_SUBJECT.match(subject)),
        "response": response,
        "busy_status": show_as,
        "is_private": _text(event.get("sensitivity")).lower() in _PRIVATE,
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
