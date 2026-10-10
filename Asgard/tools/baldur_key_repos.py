"""Give your unkeyed commits a Jira key by hand, so Baldur can be tried on repositories that have none.

Baldur counts a commit with no Jira key as "untracked work": it is estimated and shown, but it never
reaches a ticket, an approval or an AI review. For testing on personal repositories this assigns one
key per repository (or one key for all of them) the same way `cli.py keys SHA KEY` does, for every
commit of yours that has no key yet.

What it keeps to:
  * It only touches your own, non-merge commits that have no key at all. A commit that already has
    a key, from any source, is left alone, and so is every other clone's copy of it.
  * The keys are method 'manual', which collection never overrides, so a later `collect` keeps them.
  * Dry run unless you pass --apply. Run it again after new commits; it only handles the new ones.
  * It uses Baldur's own connection and functions, so it writes only what Baldur may write.

Not for real work: a key given this way is not evidence that the commit was for that ticket.

    python tools/baldur_key_repos.py PERS-1                       # every unkeyed commit -> PERS-1 (dry run)
    python tools/baldur_key_repos.py --repo site=PERS-1 --repo api=PERS-2 --apply
    python tools/baldur_key_repos.py PERS-9 --repo api=PERS-2 --apply   # PERS-9 for repos not named
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
ROOT = os.environ.get("ASGARD_APP") or str(HERE.parent)
for _folder in (str(Path(ROOT) / "apps" / "baldur"), ROOT):
    if _folder not in sys.path:
        sys.path.insert(0, _folder)

from asgard import muninn  # noqa: E402
from baldur import cli, collect  # noqa: E402


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    p.add_argument("default", nargs="?", metavar="KEY", help="the key for repositories not named by --repo")
    p.add_argument("--repo", action="append", default=[], metavar="NAME=KEY",
                   help="the key for one repository, by its name in Baldur (see: cli.py repos); repeat for more")
    p.add_argument("--apply", action="store_true", help="write the keys (the default is to show what would change)")
    return p.parse_args(argv)


def build_plan(args: argparse.Namespace) -> Tuple[Dict[str, str], Optional[str]]:
    """The key for each named repository (lower-case name), and the default; ValueError if one isn't a key."""
    by_repo: Dict[str, str] = {}
    for item in args.repo:
        name, sep, key = item.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"--repo {item!r} should look like NAME=KEY, such as site=PERS-1")
        by_repo[name.strip().lower()] = muninn.normalize_key(key)
    default = muninn.normalize_key(args.default) if args.default else None
    if not by_repo and default is None:
        raise ValueError("Give a KEY, or at least one --repo NAME=KEY, such as: PERS-1")
    return by_repo, default


def unkeyed(con: sqlite3.Connection) -> Dict[str, Dict[str, object]]:
    """Your non-merge commits with no key anywhere, by SHA: every stored copy's id, and the repository name.

    A SHA is skipped when any clone's copy of it already has a key.
    """
    rows = con.execute(
        "SELECT c.id, c.sha, r.name, (SELECT count(*) FROM commit_work_items w WHERE w.commit_id = c.id) AS n "
        "FROM commits c JOIN repos r ON r.id = c.repo_id WHERE c.is_mine = 1 AND c.is_merge = 0 "
        "ORDER BY c.authored_at, c.id").fetchall()
    out: Dict[str, Dict[str, object]] = {}
    keyed = set()
    for r in rows:
        if r["n"]:
            keyed.add(r["sha"])
        entry = out.setdefault(r["sha"], {"ids": [], "repo": r["name"]})
        entry["ids"].append(r["id"])      # type: ignore[union-attr]
    return {sha: e for sha, e in out.items() if sha not in keyed}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        by_repo, default = build_plan(args)
    except ValueError as exc:
        print(f"baldur_key_repos: {exc}", file=sys.stderr)
        return 2
    try:
        con = muninn.open_app("baldur", supported=cli.SCHEMA)
    except muninn.MuninnError as exc:
        print(f"baldur_key_repos: {exc}", file=sys.stderr)
        return 1
    try:
        todo = unkeyed(con)
        counts: Dict[Tuple[str, str], int] = {}
        left = 0
        assign: List[Tuple[List[int], str]] = []
        for entry in todo.values():
            repo = str(entry["repo"])
            key = by_repo.get(repo.lower(), default)
            if key is None:
                left += 1
                continue
            counts[(repo, key)] = counts.get((repo, key), 0) + 1
            assign.append((list(entry["ids"]), key))      # type: ignore[arg-type]
        unknown = sorted(set(by_repo) - {str(e["repo"]).lower() for e in todo.values()})
        for (repo, key), n in sorted(counts.items()):
            print(f"{repo}: {n} commit(s) -> {key}")
        if left:
            print(f"{left} unkeyed commit(s) in repositories with no --repo and no default key were left as they are.")
        for name in unknown:
            print(f"Note: no unkeyed commits of yours are left in a repository named {name!r} "
                  "(it may be already keyed, or the name may differ; names are in: cli.py repos).", file=sys.stderr)
        if not assign:
            print("Nothing to key.")
            return 0
        if not args.apply:
            print("Dry run: nothing was written. Add --apply to write these keys, then run: cli.py estimate")
            return 0
        for ids, key in assign:
            collect.set_keys(con, ids, [key])
        print(f"Keyed {len(assign)} commit(s). Run: cli.py estimate")
        return 0
    except sqlite3.Error as exc:
        print(f"baldur_key_repos: Muninn couldn't do that ({exc}). If another Asgard app is busy, try again.",
              file=sys.stderr)
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
