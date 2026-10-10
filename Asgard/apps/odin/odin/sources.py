"""Readers that turn calendar exports into Meeting objects.

Position in the flow
    The front door. __main__.cmd_push calls one of these, then hands the resulting list to sync.py.
    Everything downstream sees only Meeting objects and cannot tell which path produced them.

Two sources today
  * load_export      - the versioned JSON written by powershell/Export-OutlookMeetings.ps1 (Path A)
  * load_outlook_csv - Outlook's File > Open & Export > Export to a file > CSV (Calendar) (Path B)

A future Microsoft Graph source only needs to produce the same JSON (see README "Export schema").

Tolerance is deliberate
    A single malformed row must not abandon a whole run, so both readers log a warning and skip the
    item rather than raising. A malformed *envelope* (wrong schema_version, missing columns) does
    raise, because that means the file is not what the user thinks it is.

Path B limitations, carried in the data
    The CSV has no response status, so `response` is "unknown" and declined meetings cannot be
    filtered on that path. It also contains full meeting bodies, which is why the README tells the
    user to delete it afterwards. `key` is "csv:" + content_hash, so a meeting already pushed via
    COM is recognised rather than duplicated.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Sequence

from .models import Meeting, unwrap_ps_array

log = logging.getLogger(__name__)

SUPPORTED_SCHEMA = 1
_CANCELLED_RE = re.compile(r"^\s*cancell?ed\s*:", re.IGNORECASE)
_TRUE = {"true", "yes", "1"}
# Outlook's "Show time as" column; same numbering as the OOM BusyStatus enum. Verify on your export.
_CSV_BUSY = {"0": "free", "1": "tentative", "2": "busy", "3": "oof", "4": "elsewhere"}
_CSV_REQUIRED = {"subject", "start date", "start time", "end date", "end time"}


def load_export(path) -> List[Meeting]:
    with open(path, encoding="utf-8-sig") as fh:   # PowerShell 5.1 may write a BOM
        doc = json.load(fh)
    version = doc.get("schema_version")
    if version != SUPPORTED_SCHEMA:
        raise ValueError(f"{path}: unsupported schema_version {version!r} (expected {SUPPORTED_SCHEMA})")
    source = str(doc.get("source") or "unknown")
    meetings: List[Meeting] = []
    for i, raw in enumerate(unwrap_ps_array(doc.get("meetings")) or []):
        try:
            meetings.append(Meeting.from_dict(raw, source))
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Skipping malformed item #%d in %s: %s", i, path, exc)
    return meetings


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):   # Outlook usually writes the ANSI code page
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _parse_local(date_s: str, time_s: str, formats: Sequence[str]) -> datetime:
    text = f"{date_s} {time_s}".strip()
    for fmt in formats:
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return naive.astimezone(timezone.utc)   # naive is interpreted as this machine's local time
    raise ValueError(f"unrecognized date/time {text!r}; add its format to csv.datetime_formats")


def load_outlook_csv(path, datetime_formats: Sequence[str]) -> List[Meeting]:
    text = _read_text(Path(path))
    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = {h.strip().lower() for h in (reader.fieldnames or []) if h}
    missing = _CSV_REQUIRED - headers
    if missing:
        raise ValueError(
            f"{path} is missing columns {sorted(missing)}. Is it an Outlook *calendar* CSV export "
            "with English column names?"
        )

    meetings: List[Meeting] = []
    for record_no, row in enumerate(reader, start=1):
        r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k and isinstance(v, str)}
        try:
            start = _parse_local(r["start date"], r["start time"], datetime_formats)
            end = _parse_local(r["end date"], r["end time"], datetime_formats)
        except ValueError as exc:
            log.warning("CSV record %d skipped: %s", record_no, exc)
            continue

        subject = r.get("subject") or "(no subject)"
        location = r.get("location", "")
        attendees = r.get("required attendees") or r.get("optional attendees")
        meeting = Meeting(
            source="outlook-csv",
            key="",
            subject=subject,
            start_utc=start,
            end_utc=end,
            all_day=r.get("all day event", "").lower() in _TRUE,
            is_meeting=bool(attendees),
            is_cancelled=bool(_CANCELLED_RE.match(subject)),
            response="unknown",          # the CSV export doesn't include your response
            busy_status=_CSV_BUSY.get(r.get("show time as", ""), "unknown"),
            is_private=r.get("private", "").lower() in _TRUE,
            location=location,
            categories=[c.strip() for c in re.split(r"[;,]", r.get("categories", "")) if c.strip()],
            organizer=r.get("meeting organizer") or None,
            is_teams="microsoft teams" in location.lower(),
        )
        meeting.key = "csv:" + meeting.content_hash
        meetings.append(meeting)
    return meetings
