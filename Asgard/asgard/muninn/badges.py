"""Counts for the launcher's tiles, read from v_tile_badges.

Never raises: a missing, locked or newer database just means no badges.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .db import connect, default_path, latest_version, user_version


@dataclass(frozen=True)
class Badge:
    priority: int
    count: int
    label: str

    @property
    def text(self) -> str:
        """'2 reviews requested', or '1 review requested'."""
        label = self.label
        if self.count == 1:
            first, _, rest = label.partition(" ")
            if first.endswith("s") and not first.endswith("ss"):
                label = first[:-1] + (" " + rest if rest else "")
        return f"{self.count} {label}"


def tile_badges(path: Optional[Path] = None) -> Dict[str, List[Badge]]:
    """{app id: badges, most important first}. Empty when Muninn isn't there or can't be read."""
    db = Path(path) if path else default_path()
    if not db.exists():
        return {}
    try:
        con = connect(db, readonly=True, timeout=0.5)
    except sqlite3.Error:
        return {}
    try:
        version = user_version(con)
        if version < 1 or version > latest_version():
            return {}
        out: Dict[str, List[Badge]] = {}
        for r in con.execute("SELECT app, priority, n, label FROM v_tile_badges ORDER BY app, priority"):
            out.setdefault(r["app"], []).append(Badge(int(r["priority"]), int(r["n"]), str(r["label"])))
        return out
    except sqlite3.Error:
        return {}
    finally:
        con.close()
