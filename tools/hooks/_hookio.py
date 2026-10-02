"""Shared helpers for Kiro hook scripts: read the JSON event from STDIN and pull out paths/commands.

Kiro passes event context as JSON on STDIN. Field names vary by trigger and version, so these
helpers search the payload for likely keys instead of relying on one exact schema.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent

PATH_KEYS = {
    "file_path",
    "filepath",
    "filePath",
    "path",
    "file",
    "files",
    "paths",
    "target_file",
    "targetFile",
    "filename",
}
COMMAND_KEYS = {"command", "cmd", "script", "commandLine", "command_line"}


def read_event() -> dict[str, Any]:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception:
        raw = ""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return data if isinstance(data, dict) else {"_value": data}


def _walk(obj: Any, keys: set[str]) -> Iterator[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys:
                if isinstance(v, str):
                    yield v
                elif isinstance(v, list):
                    yield from (x for x in v if isinstance(x, str))
            yield from _walk(v, keys)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item, keys)


def event_paths(event: dict[str, Any], suffixes: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for value in _walk(event, PATH_KEYS):
        if value.lower().endswith(suffixes):
            p = Path(value)
            if not p.is_absolute():
                p = ROOT / p
            if p.exists() and p not in found:
                found.append(p)
    return found


def event_commands(event: dict[str, Any]) -> list[str]:
    cmds = list(_walk(event, COMMAND_KEYS))
    if not cmds and "_raw" in event:
        cmds = [event["_raw"]]
    return cmds


def git_changed(suffixes: tuple[str, ...]) -> list[Path]:
    paths: list[Path] = []
    for args in (["diff", "--name-only", "HEAD"], ["ls-files", "--others", "--exclude-standard"]):
        try:
            out = subprocess.run(
                ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=15
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if out.returncode != 0:
            return []
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.lower().endswith(suffixes):
                p = ROOT / line
                if p.exists() and p not in paths:
                    paths.append(p)
    return paths


def rel(p: Path) -> str:
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)
