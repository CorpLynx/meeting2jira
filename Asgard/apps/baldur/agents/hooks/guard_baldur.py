#!/usr/bin/env python3
"""Agent hook (Kiro PreToolUse): keep an AI agent's hands off Baldur's decisions.

The person decides what time is logged (Asgard rules 4 and 5). An agent may record its own
estimates (ai record), read what Baldur has (ai list, ai show, report, days, keys SHA) and run
collection or an estimate; it may not:
- approve, reject or change a figure, or take AI-assisted figures (approve --ai);
- note real hours (actual), which calibration trusts as ground truth, or accept a calibration;
- change Baldur's settings, a commit's keys, which repositories count, the schedule or the
  GitHub token;
- touch Muninn's file directly (muninn.db), or run Asgard's database maintenance;
- switch Ysildir's tools on or off, connect AI clients to it (ysildir.cmd tools --on/--off, setup),
  or touch its switches file (ysildir.json): which tools an agent may use is the person's choice,
  approved by their ISSO.

Exit code 2 blocks the tool call and stderr tells the agent why; anything else lets it run. A
bug in this script never blocks anything. Standard library only, Python 3.9+.
"""
from __future__ import annotations

import json
import re
import shlex
import sys
from typing import Any, Iterator, List, Optional

COMMAND_KEYS = {"command", "cmd", "script", "commandLine", "command_line"}
# Baldur and Ysildir are started as baldur.cmd (or baldur on PATH), or as their cli.py from the
# install or the repo.
CLI_PY = re.compile(r"baldur[\\/]+cli\.py", re.I)
MUNINN_FILE = re.compile(r"muninn\.db", re.I)
SWITCHES_FILE = re.compile(r"ysildir\.json", re.I)
MUNINN_MAINTENANCE = re.compile(r"--muninn\s+(?:restore|repair|retention)", re.I)

DECISIONS = {"approve", "reject", "change", "actual", "schedule"}
MESSAGE = ("Blocked by Baldur's agent guard: {what} is the person's decision, not an agent's. "
           "Record your estimate with `baldur.cmd ai record ...` and tell the person what you found; "
           "they approve in Baldur. (Baldur agent guide: baldur.cmd ai guide)")
YSILDIR_MESSAGE = ("Blocked by Baldur's agent guard: {what} is the person's decision, not an agent's. Which "
                   "Ysildir tools an agent may use is theirs, approved by their ISSO; tell them what you would "
                   "need and why. (ysildir.cmd tools lists the switches; Ysildir's asgard_guide, topic tools, too.)")


def read_event() -> Any:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {"command": raw}


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


def _words(text: str) -> List[str]:
    try:
        return shlex.split(text, posix=False)
    except ValueError:
        return text.split()


def app_args(command: str, app: str = "baldur") -> List[List[str]]:
    """The arguments of each invocation of an Asgard app's command line (baldur, ysildir) in a shell command."""
    cli_py = re.compile(app + r"[\\/]+cli\.py", re.I)
    out = []
    for part in re.split(r"&&|\|\||[;&|\n]", command):
        words = [w.strip("\"'") for w in _words(part)]
        for n, word in enumerate(words):
            name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if name in (app, f"{app}.cmd", f"{app}.bat", f"{app}.exe") or \
                    (name == "cli.py" and cli_py.search(word.replace("\\", "/"))):
                out.append(words[n + 1:])
                break
    return out


def baldur_args(command: str) -> List[List[str]]:
    """The arguments of each Baldur invocation in a shell command line."""
    return app_args(command, "baldur")


def verdict(command: str) -> Optional[str]:
    """Why this command is blocked, or None when it may run."""
    if MUNINN_FILE.search(command):
        return MESSAGE.format(what="Reading or writing Muninn's file directly") + \
            " Muninn changes only through Baldur's commands or Ysildir."
    if MUNINN_MAINTENANCE.search(command):
        return MESSAGE.format(what="Muninn maintenance (restore, repair, retention)")
    if SWITCHES_FILE.search(command):
        return YSILDIR_MESSAGE.format(what="Ysildir's switches file (ysildir.json)")
    for args in app_args(command, "ysildir"):
        words = [a.lower() for a in args if not a.startswith("-")]
        flags = {a.split("=", 1)[0].lower() for a in args if a.startswith("-")}
        if words[:1] == ["setup"]:
            return YSILDIR_MESSAGE.format(what="Connecting AI clients to Ysildir (setup)")
        if words[:1] == ["tools"] and flags & {"--on", "--off"}:
            return YSILDIR_MESSAGE.format(what="Switching Ysildir's tools on or off")
    for args in baldur_args(command):
        words = [a for a in args if not a.startswith("-")]
        flags = {a.split("=", 1)[0] for a in args if a.startswith("-")}
        sub = words[0].lower() if words else ""
        if sub in DECISIONS:
            return MESSAGE.format(what=f"`{sub}`")
        if sub == "calibrate" and "--accept" in flags:
            return MESSAGE.format(what="Accepting a calibration")
        if sub == "setup" and args[1:]:
            return MESSAGE.format(what="Changing Baldur's setup")
        if sub == "repos" and flags & {"--on", "--off"}:
            return MESSAGE.format(what="Switching a repository on or off")
        if sub == "keys" and len(words) > 2:
            return MESSAGE.format(what="Setting a commit's Jira keys")
        if sub == "github" and len(words) > 1 and words[1].lower() == "token":
            return MESSAGE.format(what="The GitHub token")
    return None


def main() -> int:
    for command in commands(read_event()):
        why = verdict(command)
        if why:
            print(why, file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                    # never block on a bug in the guard itself
        print(f"Baldur agent guard error (not blocking): {exc}", file=sys.stderr)
        sys.exit(0)
