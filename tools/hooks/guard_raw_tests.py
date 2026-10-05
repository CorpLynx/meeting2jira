#!/usr/bin/env python3
"""Kiro PreToolUse hook: block shell commands that run the test suite directly.

This repo's tests are stdlib unittest (`python -m unittest discover ...`); pytest is blocked too in
case it gets installed. Raw verbose output for the suite is thousands of tokens that the main model
then carries for the rest of the session. Exiting non-zero blocks the tool call, and stderr is
shown to the agent so it retries with the compact runner.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _hookio import event_commands, read_event  # noqa: E402

PYTEST_CALL = re.compile(
    r"""(?:^|[;&|(\n]\s*|\brun\s+|\s-m\s+)   # command start, 'uv/poetry/pipenv run', or 'python -m'
        (?:[^\s;&|'"]*[\\/])?                # optional path prefix such as .venv/Scripts/
        pytest(?:\.exe)?
        (?=$|[\s;&|)'"])""",
    re.X | re.I,
)
UNITTEST_CALL = re.compile(r"-m\s+unittest\b", re.I)
ALLOWED = re.compile(r"run_tests\.py|\bpip3?\b|\binstall\b|--version\b|--help\b", re.I)

MESSAGE = (
    "Blocked: don't run unittest/pytest directly, because the raw output floods the context.\n"
    "Use `python tools/run_tests.py [test_module[.Class.test]] [-k pattern] [--lf] [-x]` "
    "(compact summary; full log in .test-output/last-run.log),\n"
    "or delegate a full-suite run to the `test-runner` subagent."
)


def main() -> int:
    if os.environ.get("KIRO_ALLOW_RAW_TESTS") == "1":
        return 0
    for cmd in event_commands(read_event()):
        raw = PYTEST_CALL.search(cmd) or UNITTEST_CALL.search(cmd)
        if raw and not ALLOWED.search(cmd):
            print(MESSAGE, file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never block on a hook bug
        print(f"guard hook error: {exc}", file=sys.stderr)
        sys.exit(0)
