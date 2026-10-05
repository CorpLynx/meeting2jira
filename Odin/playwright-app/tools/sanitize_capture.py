"""Turn a real `--raw-out` capture into a fixture that is safe to commit.

Why this matters more than any other test
    The mapping tests are only as good as the fixture they run against, and the shipped fixture was
    written from what Graph is *believed* to return. If the real endpoint uses different field names,
    every test passes and the export still produces nothing. Replacing that fixture with a real
    payload from your tenant is the single largest jump in confidence available.

    But a real capture is your calendar: subjects, attendees, email addresses, meeting bodies. This
    replaces all of it with synthetic values while preserving the *shape* - which field names exist,
    which are null, which are nested, what the enum spellings are. Shape is the whole point; content
    is a liability.

Usage
    python export_owa.py --days-back 7 --raw-out raw.json --out /dev/null
    python tools/sanitize_capture.py raw.json -o ../tests/fixtures/owa_real_capture.json
    python -m unittest discover -s tests      # now running against real field names

Always read the output before committing it. This is a best-effort scrubber, not a guarantee.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

# Replaced with synthetic values. Everything here is free text or an identifier that could carry
# personal or organisational information.
TEXT_FIELDS = {"subject", "bodyPreview", "body", "content", "displayName", "name", "address",
               "emailAddress", "locationUri", "uniqueId", "onlineMeetingUrl", "joinUrl",
               "conferenceId", "tollNumber", "tollFreeNumbers", "seriesMasterId", "changeKey",
               "webLink", "occurrenceId", "transactionId"}

# Kept verbatim: these decide behaviour, and changing them would defeat the purpose.
PRESERVE = {"isAllDay", "isCancelled", "sensitivity", "showAs", "response", "responseStatus",
            "type", "isOnlineMeeting", "onlineMeetingProvider", "isOrganizer", "isReminderOn",
            "categories", "start", "end", "dateTime", "timeZone", "recurrence", "attendees",
            "@odata.type", "@odata.etag", "hasAttachments", "importance", "status"}

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class Sanitizer:
    def __init__(self):
        self.counter = 0
        self.replaced = 0
        self.id_map: Dict[str, str] = {}

    def fake_id(self, original: str, prefix: str = "ID") -> str:
        """Stable per input, so two references to the same meeting stay consistent."""
        if original not in self.id_map:
            self.id_map[original] = f"{prefix}-{len(self.id_map) + 1:04d}"
        return self.id_map[original]

    def scrub_text(self, value: str, key: str) -> str:
        self.replaced += 1
        if key in ("address", "emailAddress"):
            return f"person{len(self.id_map) + 1}@example.gov"
        if key in ("name", "displayName"):
            # Keep the Teams marker: is_teams detection matches on it for the COM path.
            if "teams" in value.lower():
                return "Microsoft Teams Meeting"
            return f"Person {len(self.id_map) + 1}"
        if key == "subject":
            self.counter += 1
            return f"Meeting {self.counter}"
        return "[redacted]"

    def walk(self, node: Any, key: str = "") -> Any:
        if isinstance(node, dict):
            return {k: self.walk(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [self.walk(v, key) for v in node]
        if isinstance(node, str):
            if key in ("id", "iCalUId"):
                return self.fake_id(node, "UID" if key == "iCalUId" else "ID")
            if key in TEXT_FIELDS:
                return self.scrub_text(node, key)
            # Catch identifiers in fields not named above.
            if _EMAIL.search(node):
                self.replaced += 1
                return _EMAIL.sub("person@example.gov", node)
            if _GUID.search(node):
                self.replaced += 1
                return _GUID.sub("00000000-0000-0000-0000-000000000000", node)
        return node


def field_report(events: List[Dict[str, Any]]) -> List[str]:
    """What field names the real payload actually uses - the thing worth reviewing by eye."""
    seen: Dict[str, int] = {}
    for event in events:
        for key in event:
            seen[key] = seen.get(key, 0) + 1
    lines = []
    for key in sorted(seen):
        lines.append(f"  {key:28} present in {seen[key]}/{len(events)}")
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("raw", help="a file written by export_owa.py --raw-out")
    parser.add_argument("-o", "--out", required=True, help="where to write the sanitised fixture")
    parser.add_argument("--limit", type=int, default=25, help="keep at most this many events")
    args = parser.parse_args(argv)

    raw_path = Path(args.raw)
    events = json.loads(raw_path.read_text(encoding="utf-8-sig"))
    if isinstance(events, dict):
        events = events.get("value") or events.get("Items") or events.get("meetings") or []
    if not isinstance(events, list) or not events:
        print(f"{raw_path}: no events found. Was it written by --raw-out?", file=sys.stderr)
        return 2

    print(f"Read {len(events)} event(s) from {raw_path}\n")
    print("Field names in the real payload:")
    for line in field_report(events):
        print(line)

    sanitizer = Sanitizer()
    cleaned = [sanitizer.walk(event) for event in events[:args.limit]]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cleaned, indent=2, sort_keys=True), encoding="utf-8")

    print(f"\nWrote {len(cleaned)} sanitised event(s) -> {out_path}")
    print(f"Replaced {sanitizer.replaced} value(s); {len(sanitizer.id_map)} identifier(s) remapped.")
    print("\nBefore committing this file, read it. Check for anything recognisable that the")
    print("scrubber missed - an unusual field carrying a name, a URL with a tenant in it.")
    print("Then point the mapping tests at it and see whether they still pass: if they do not,")
    print("the real field names differ from the assumed ones, which is exactly what you wanted")
    print("to find out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
