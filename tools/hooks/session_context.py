#!/usr/bin/env python3
"""Kiro SessionStart hook: print a few lines of orientation for a fresh session.

Stdout from a successful hook is added to the agent's context. This replaces the
`git status` / "what was I doing?" exploration a new session usually starts with.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _hookio import ROOT  # noqa: E402

MAX_CHANGED = 10
MAX_HANDOFF_LINES = 30


def git(*args: str) -> list[str] | None:
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.splitlines() if out.returncode == 0 else None


def age(path: Path) -> str:
    hours = (time.time() - path.stat().st_mtime) / 3600
    return f"{hours:.0f}h ago" if hours >= 1 else f"{hours * 60:.0f}m ago"


def main() -> int:
    lines: list[str] = []

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    status = git("status", "--porcelain")
    if branch is not None and status is not None:
        head = f"git: branch {branch[0] if branch else '?'}, {len(status)} uncommitted file(s)"
        lines.append(head)
        for entry in status[:MAX_CHANGED]:
            lines.append("  " + entry.strip())
        if len(status) > MAX_CHANGED:
            lines.append(f"  (+{len(status) - MAX_CHANGED} more)")

    summary = ROOT / ".test-output" / "last-summary.txt"
    if summary.exists():
        lines.append(
            "last test run: " + summary.read_text(encoding="utf-8", errors="replace").strip()
        )

    handoff = ROOT / ".kiro" / "session-handoff.md"
    if handoff.exists():
        body = handoff.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        lines.append(f"handoff note from a previous session ({age(handoff)}), continue from here:")
        lines.extend("  " + ln for ln in body[:MAX_HANDOFF_LINES])

    if lines:
        print("[session context]")
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"session hook error: {exc}", file=sys.stderr)
        sys.exit(0)
