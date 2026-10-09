#!/usr/bin/env python3
"""Agent hook (Kiro PostToolUse): after a git commit, remind the agent to record its estimate.

Prints one line into the agent's context when a shell command made a commit, and nothing
otherwise, so it costs no tokens the rest of the time. The line names the command that records
the estimate (Baldur's agent guide, baldur-agent-2). Standard library only, Python 3.9+; a bug
in this script prints nothing and never fails the agent's command.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Iterator

COMMAND_KEYS = {"command", "cmd", "script", "commandLine", "command_line"}
COMMIT = re.compile(r"(?:^|[\s;&|(])git(?:\.exe)?(?:\s+-[cC]\s+\S+)*\s+commit\b(?![^;&|\n]*--(?:dry-run|help)\b)", re.I)
REMINDER = ("Baldur: you just committed. If this was work you did with the person, record your estimate of their "
            "time on it: baldur.cmd ai record --agent <your tool> --guide baldur-agent-2 --minutes <estimate> "
            "--commit <git rev-parse HEAD> --confidence <high|medium|low> --summary \"<one sentence, no code>\" "
            "--json   (full guide: baldur.cmd ai guide)")


def commands(event: Any) -> Iterator[str]:
    if isinstance(event, dict):
        for key, value in event.items():
            if key in COMMAND_KEYS and isinstance(value, str):
                yield value
            else:
                yield from commands(value)
    elif isinstance(event, list):
        for item in event:
            yield from commands(item)


def main() -> int:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        event = json.loads(raw) if raw.strip() else {}
    except ValueError:
        event = {"command": raw}
    if any(COMMIT.search(c) for c in commands(event)):
        print(REMINDER)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                           # a reminder is never worth failing a command over
        sys.exit(0)
