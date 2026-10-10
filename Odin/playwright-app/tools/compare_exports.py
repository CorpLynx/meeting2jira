"""Diff an OWA export against a classic-Outlook COM export of the same window.

This is the highest-value test available, because you have a COM path that is known to work. Export
the same days both ways and compare: anything that disagrees is either a field this mapping gets
wrong or a genuine difference between what the two APIs expose.

    # on a machine with classic Outlook
    .\\Odin sync -DaysBack 7 -KeepExport -DryRun     # leaves a COM export behind
    python export_owa.py --days-back 7 --out owa.json
    python tools/compare_exports.py com_export.json owa.json

Matching is on content_hash - normalised subject plus start plus end - which is the same identity
the pipeline dedupes on. So "matched" here means the two exports would not create duplicate
sub-tasks, which is the property that actually matters.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

# The fields whose disagreement would change a filtering or routing decision. A mismatch in any of
# these means the two sources would behave differently, which is exactly what we want to surface.
DECISION_FIELDS = ("all_day", "is_meeting", "is_cancelled", "response", "busy_status",
                   "is_private", "is_teams", "categories")


def content_hash(meeting: Dict[str, Any]) -> str:
    """Reimplements models.Meeting.content_hash so this tool needs nothing from app/."""
    subject = " ".join(str(meeting.get("subject", "")).split()).lower()
    basis = f"{subject}|{meeting.get('start_utc')}|{meeting.get('end_utc')}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def load(path: Path) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    if doc.get("schema_version") != 1:
        raise SystemExit(f"{path}: schema_version {doc.get('schema_version')!r}, expected 1")
    by_hash = {}
    for meeting in doc.get("meetings") or []:
        by_hash[content_hash(meeting)] = meeting
    return str(doc.get("source") or "?"), by_hash


def describe(meeting: Dict[str, Any]) -> str:
    return f"{meeting.get('start_utc')}  {str(meeting.get('subject'))[:48]}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("left", help="first export (typically the COM one)")
    parser.add_argument("right", help="second export (typically the OWA one)")
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    args = parser.parse_args(argv)

    left_source, left = load(Path(args.left))
    right_source, right = load(Path(args.right))

    only_left = sorted(set(left) - set(right), key=lambda h: left[h].get("start_utc") or "")
    only_right = sorted(set(right) - set(left), key=lambda h: right[h].get("start_utc") or "")
    common = sorted(set(left) & set(right), key=lambda h: left[h].get("start_utc") or "")

    print(f"{args.left}  source={left_source}  {len(left)} meeting(s)")
    print(f"{args.right}  source={right_source}  {len(right)} meeting(s)")
    print(f"\nmatched {len(common)}, only in left {len(only_left)}, only in right {len(only_right)}")

    problems = 0

    if only_left:
        problems += len(only_left)
        print(f"\nMissing from {args.right} - the OWA path did not produce these:")
        for h in only_left:
            print(f"  - {describe(left[h])}")
        print("  Likely causes: the window did not match, paging stopped early, or the endpoint")
        print("  filtered them. Not harmless: these meetings would never reach Jira.")

    if only_right:
        print(f"\nExtra in {args.right} - present here but not in {args.left}:")
        for h in only_right:
            print(f"  + {describe(right[h])}")
        print("  Often benign (a wider window), but check the times: a timezone error shifts every")
        print("  meeting and makes all of them look 'extra' while the originals look 'missing'.")
        if only_left:
            print("  Both lists being non-empty and the same length is the signature of exactly that.")

    field_mismatches: List[str] = []
    for h in common:
        for field in DECISION_FIELDS:
            a, b = left[h].get(field), right[h].get(field)
            if a != b:
                field_mismatches.append(f"  {describe(left[h])}\n      {field}: {a!r} vs {b!r}")

    if field_mismatches:
        problems += len(field_mismatches)
        print("\nSame meeting, different decision-affecting fields:")
        for line in field_mismatches:
            print(line)
        print("  Each of these would change a filter or routing outcome between the two paths.")
    elif not args.quiet and common:
        print("\nEvery matched meeting agrees on all decision-affecting fields "
              f"({', '.join(DECISION_FIELDS)}).")

    if not problems:
        print("\nNo problems. The two exports would produce the same sub-tasks.")
        return 0
    print(f"\n{problems} problem(s) to look at.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
