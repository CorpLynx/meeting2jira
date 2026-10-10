"""The normalized Meeting record that every calendar source produces.

Position in the flow
    sources.py builds these; rules.py filters and routes them; sync.py renders and pushes them.
    Adding a new calendar source means producing Meeting objects (or schema-v1 JSON that becomes
    them) and nothing downstream changes.

Why it matters
    This is the seam that keeps the Outlook COM path, the CSV path, and any future Graph source
    interchangeable. Times are always stored as aware UTC; `start_local`/`end_local` derive the
    local wall-clock view on demand.

Dedupe identity (changing either formula re-creates every past meeting)
    * `key`          source-specific and stable per occurrence.
                     COM uses "GlobalAppointmentID|start_utc"; CSV uses "csv:" + content_hash.
    * `content_hash` sha256(normalized subject | start | end)[:32]. Source-independent, which is
                     what stops a COM run and a CSV run from duplicating each other.

Also here
    * parse_utc        tolerates the trailing "Z" that Python < 3.11 cannot parse natively
    * unwrap_ps_array  undoes Windows PowerShell 5.1's {"value":[...],"Count":n} array wrapping
    * iso_utc          the single timestamp format written to state and to exports
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


_COMPACT_OFFSET = re.compile(r"([+-]\d\d)(\d\d)$")


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into aware UTC. Before Python 3.11 fromisoformat reads neither
    '...Z' nor Jira's '+0000' offsets, so both are rewritten as '+00:00' first."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = _COMPACT_OFFSET.sub(r"\1:\2", text)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def unwrap_ps_array(value: Any) -> Any:
    """Windows PowerShell 5.1 can serialize arrays as {"value": [...], "Count": n}; undo that."""
    if isinstance(value, dict) and "value" in value and isinstance(value["value"], list):
        return value["value"]
    return value


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Meeting:
    source: str
    key: str                       # stable id for dedupe (source-specific)
    subject: str
    start_utc: datetime
    end_utc: datetime
    all_day: bool = False
    is_meeting: bool = True        # False for personal appointments with no attendees
    is_cancelled: bool = False
    response: str = "unknown"      # organizer|accepted|tentative|declined|none|not_responded|unknown
    busy_status: str = "unknown"   # free|tentative|busy|oof|elsewhere|unknown
    is_private: bool = False
    location: str = ""
    categories: List[str] = field(default_factory=list)
    organizer: Optional[str] = None
    is_teams: bool = False
    # The calendar's own id for the meeting (the part of `key` before its "|") and whether it belongs
    # to a series. Optional in the export; None means the source didn't say. Together they let Odin
    # tell a one-off meeting that moved from a new one (sync.py, store.find_moved).
    global_id: Optional[str] = None
    is_recurring: Optional[bool] = None

    @property
    def minutes(self) -> int:
        return max(0, int(round((self.end_utc - self.start_utc).total_seconds() / 60)))

    @property
    def start_local(self) -> datetime:
        return self.start_utc.astimezone()

    @property
    def end_local(self) -> datetime:
        return self.end_utc.astimezone()

    @property
    def content_hash(self) -> str:
        """Source-independent fingerprint, so COM and CSV runs don't duplicate each other."""
        subject = " ".join(self.subject.split()).lower()
        basis = f"{subject}|{iso_utc(self.start_utc)}|{iso_utc(self.end_utc)}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]

    @classmethod
    def from_dict(cls, d: Dict[str, Any], source: str) -> "Meeting":
        categories = unwrap_ps_array(d.get("categories")) or []
        if isinstance(categories, str):
            categories = [c.strip() for c in categories.split(",") if c.strip()]
        return cls(
            source=source,
            key=str(d["key"]),
            subject=str(d.get("subject") or "(no subject)"),
            start_utc=parse_utc(d["start_utc"]),
            end_utc=parse_utc(d["end_utc"]),
            all_day=bool(d.get("all_day", False)),
            is_meeting=bool(d.get("is_meeting", True)),
            is_cancelled=bool(d.get("is_cancelled", False)),
            response=str(d.get("response") or "unknown"),
            busy_status=str(d.get("busy_status") or "unknown"),
            is_private=bool(d.get("is_private", False)),
            location=str(d.get("location") or ""),
            categories=[str(c) for c in categories],
            organizer=d.get("organizer") or None,
            is_teams=bool(d.get("is_teams", False)),
            global_id=(str(d["global_id"]).strip() or None) if d.get("global_id") is not None else None,
            # Only a real boolean counts: anything else (absent, "false" as text) means "didn't say".
            is_recurring=d["is_recurring"] if isinstance(d.get("is_recurring"), bool) else None,
        )
