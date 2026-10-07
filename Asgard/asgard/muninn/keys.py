"""Jira issue keys, the join between Asgard's apps.

Every app stores keys as text (PROJ-123), and Odin resolves them through work_item_aliases. A key
written in another case or with spaces would never resolve, and approvals for it could never be
posted, so keys are normalised before they are written. From schema v3 the database refuses a
malformed key in every cross-app key column.

The pattern is Jira Data Center's default, widened: the project part is a capital letter followed
by capitals, digits or underscores (Jira's default asks for two characters; an administrator can
allow one), then a hyphen and an issue number with no leading zero.
"""
from __future__ import annotations

import re
from typing import Optional

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$")

# The same rule in SQL, for the v3 triggers and integrity checks. {col} is the column expression.
SQL_IS_KEY = ("({col} GLOB '[A-Z]*-[1-9]*' AND {col} NOT GLOB '*[^A-Z0-9_-]*' "
              "AND {col} NOT GLOB '*-*-*' AND substr({col}, instr({col}, '-') + 1) NOT GLOB '*[^0-9]*')")


def is_key(text: Optional[str]) -> bool:
    """Whether text is a Jira key exactly as Muninn stores one."""
    return bool(text) and KEY_RE.fullmatch(text) is not None      # match() lets "ABC-1\n" through


def normalize_key(text: Optional[str]) -> str:
    """text as a stored key (trimmed, upper case), or ValueError saying what a key looks like."""
    key = (text or "").strip().upper()
    if not KEY_RE.fullmatch(key):
        raise ValueError(f"{text!r} isn't a Jira key like PROJ-123")
    return key
