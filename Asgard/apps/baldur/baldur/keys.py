"""Jira keys in branch names and commit messages.

A key is PROJECT-123 whose project is in your project_keys setting, so
UTF-8, SHA-256 and ISO-8601 never count. Branch names are matched without
regard to case (feature/proj-123-retry), and keys come back upper-case.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Tuple

_KEY_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]+)-([1-9][0-9]{0,8})(?![0-9])")

# How Baldur knew, best first; the first source with any key wins.
METHODS = ("reflog", "branch", "pr", "message")


def find_keys(text: Optional[str], projects: Iterable[str], *, any_case: bool = False) -> List[str]:
    """Keys in text, in order of appearance, without repeats."""
    if not text:
        return []
    allowed = {p.upper() for p in projects}
    found: List[str] = []
    for m in _KEY_RE.finditer(text):
        project, number = m.group(1), m.group(2)
        if not any_case and project != project.upper():
            continue
        key = f"{project.upper()}-{int(number)}"
        if project.upper() in allowed and key not in found:
            found.append(key)
    return found


def branch_keys(branch: Optional[str], projects: Iterable[str]) -> List[str]:
    """Keys in a branch name; remote prefixes such as origin/ don't matter."""
    if not branch:
        return []
    name = branch
    for prefix in ("refs/heads/", "refs/remotes/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return find_keys(name, projects, any_case=True)


def choose(projects: Sequence[str], reflog_branch: Optional[str] = None, branches: Sequence[str] = (),
           pr_branches: Sequence[str] = (), message: Optional[str] = None) -> Tuple[List[str], Optional[str]]:
    """(keys, method) from the first source that has any; ([], None) when none does."""
    keys = branch_keys(reflog_branch, projects)
    if keys:
        return keys, "reflog"
    for method, names in (("branch", branches), ("pr", pr_branches)):
        found: List[str] = []
        for name in names:
            for key in branch_keys(name, projects):
                if key not in found:
                    found.append(key)
        if found:
            return found, method
    keys = find_keys(message, projects)
    if keys:
        return keys, "message"
    return [], None
