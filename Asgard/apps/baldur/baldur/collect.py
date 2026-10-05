"""The collector: your repositories into Muninn, as metadata only.

For each repository it reads git first, with no database lock held, then
writes in one short batch: the repo row, your commits, every working
folder's HEAD reflog, and each commit's Jira keys with how Baldur knows them.

- Yours means authored with one of your git emails (Muninn identities).
  Names don't count: two people can share one.
- Commits someone else authored are kept only when a Co-authored-by trailer
  names one of your emails, with is_mine = 0. The day report lists them;
  they never count toward an estimate.
- A linked worktree is the same repository as the folder it was added from,
  so its commits are stored once and its reflog joins the repository's.
- Copies aren't work: a pull request squash-merged on GitHub, or a local
  `git merge --squash` of commits already collected, is stored with
  is_merge = 1 and no keys. It never counts toward an estimate; its time
  only makes the sessions it links share one length limit.
- Each collection reads history_days of history, because commits can arrive
  late from another machine or a rebase; --full reads all of it.

Git expires reflog entries after 90 days, so collect at least weekly.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from asgard import muninn

from . import gitread, keys
from .settings import Settings


class CollectError(RuntimeError):
    """Collection can't start; the message says what to set up."""


@dataclass
class RepoResult:
    path: Path
    name: str
    commits_seen: int = 0
    commits_new: int = 0
    keyed: int = 0
    skipped_copies: int = 0
    coauthored: int = 0
    reflog_new: int = 0
    error: Optional[str] = None


@dataclass
class CollectResult:
    repos: List[RepoResult] = field(default_factory=list)

    @property
    def failed(self) -> List[RepoResult]:
        return [r for r in self.repos if r.error]


def my_emails(con: sqlite3.Connection) -> Set[str]:
    """Your git emails, lower-case. Only these decide which commits are yours."""
    return muninn.identities(con, "git_email")


def check_ready(con: sqlite3.Connection, settings: Settings) -> None:
    """Refuse to collect or estimate without knowing who you are and which Jira projects count."""
    if not my_emails(con):
        raise CollectError("Baldur doesn't know which commits are yours. Add your git email first: "
                           "cli.py setup --email you@agency.gov")
    if not settings.project_keys:
        raise CollectError("Set project_keys to the Jira projects you work in first: "
                           "cli.py setup --project PROJ")


def repo_folders(paths: Sequence[Path]) -> List[Path]:
    """Each repository once: linked worktrees fold into the folder they were added from."""
    out: List[Path] = []
    seen: Set[str] = set()
    for p in paths:
        try:
            main = gitread.main_folder(Path(p))
        except gitread.GitError:
            main = Path(p)
        key = str(main.resolve()).lower()
        if key not in seen:
            seen.add(key)
            out.append(main)
    return out


def collect(con: sqlite3.Connection, settings: Settings, only: Optional[Sequence[Path]] = None,
            full: bool = False) -> CollectResult:
    """Collect every repository under repo_roots (or just `only`)."""
    check_ready(con, settings)
    emails = my_emails(con)
    since = None if full else muninn.to_ts(dt.datetime.now(dt.timezone.utc)
                                           - dt.timedelta(days=int(settings.history_days)))
    source = muninn.ensure_source(con, "git", "local-git")
    found = list(only) if only else gitread.discover(settings.repo_roots, settings.max_depth)
    forget_other_githubs(con, settings.github_host())
    result = CollectResult()
    for folder in repo_folders(found):
        result.repos.append(_collect_repo(con, settings, source, folder, emails, since))
    return result


def forget_other_githubs(con: sqlite3.Connection, github_host: Optional[str]) -> int:
    """Clear github_repo on clones whose remote isn't on your GitHub (or all of them when it's off).

    github_repo is owner/name without a host, and unique, so a clone left holding a
    github.com name after you switch to Enterprise Server would stop the server's own
    clone of the same owner/name from getting it. Done before any repository is
    collected, so the result doesn't depend on the order folders are read in.
    Review-only rows (no folder) are left for the GitHub step.
    """
    stale = [r["id"] for r in con.execute(
        "SELECT id, remote_url FROM repos WHERE github_repo IS NOT NULL AND local_path IS NOT NULL")
        if not gitread.is_github_remote(gitread.parse_remote(r["remote_url"])[0], github_host)]
    if stale:
        with muninn.transaction(con):
            con.executemany("UPDATE repos SET github_repo = NULL WHERE id = ?", [(i,) for i in stale])
    return len(stale)


def _all_mine(con: sqlite3.Connection) -> Set[str]:
    """The SHAs of every commit of yours stored from any repository."""
    return {r[0] for r in con.execute("SELECT sha FROM commits WHERE is_mine = 1")}


def _stored(con: sqlite3.Connection, path: Path) -> Dict[str, Optional[str]]:
    """sha -> patch_id of the commits of yours already stored for this folder."""
    return {r[0]: r[1] for r in con.execute(
        "SELECT c.sha, c.patch_id FROM commits c JOIN repos r ON r.id = c.repo_id "
        "WHERE r.local_path = ? AND c.is_mine = 1", (str(path.resolve()),))}


def _collect_repo(con: sqlite3.Connection, settings: Settings, source: int, path: Path, emails: Set[str],
                  since: Optional[str]) -> RepoResult:
    res = RepoResult(path, path.name)
    try:
        with muninn.Run(con, "baldur", source, "commits:" + str(path.resolve()),
                        mode="full" if since is None else "incremental") as run:
            # Read everything from git first; no write lock is held while git runs.
            info = gitread.repo_info(path, settings.github_host())
            res.name = info.name
            mine = gitread.log_commits(path, emails, since)
            shared = gitread.log_coauthored(path, emails, since)
            stored = _stored(con, path)
            # A local squash is a copy when the commits it lists are yours anywhere: another clone may
            # hold the branch they were made on. Collection order can't matter, because store.load()
            # treats a SHA as a copy if any clone's row says so.
            known = _all_mine(con) | {c.sha for c in mine}
            copies = {c.sha for c in mine if c.is_github_squash or (c.squashed and set(c.squashed) <= known)}
            fingerprints = gitread.patch_ids(path, [c.sha for c in mine if c.sha not in copies
                                                    and not stored.get(c.sha)])
            reflogs = []
            made_on: Dict[str, str] = {}
            for n, (folder, now_on) in enumerate(gitread.worktrees(path)):
                entries = gitread.head_reflog(folder)
                reflogs.append(("HEAD" if n == 0 else f"HEAD of {folder.resolve()}", entries))
                for sha, branch in gitread.reflog_branches(entries, now_on).items():
                    made_on.setdefault(sha, branch)
            members = gitread.branch_membership(path, info.default_branch, {c.sha for c in mine}, since)
            with run.batch():
                repo_id = _upsert_repo(con, info, source, run.now)
                copies_new = 0
                for c in mine:
                    res.commits_seen += 1
                    run.items_seen += 1
                    if c.sha in copies:
                        # A copy of commits already counted: never work, and it has no keys, not even
                        # hand-set ones (they'd count it twice for Freya). Its time is kept only so the
                        # sessions it links share one length limit (estimate.share_limits).
                        res.skipped_copies += 1
                        c.patch_id = None
                        commit_id, new = _upsert_commit(con, repo_id, c, None, run, copy=True)
                        con.execute("DELETE FROM commit_work_items WHERE commit_id = ?", (commit_id,))
                        copies_new += int(new)
                        continue
                    c.patch_id = fingerprints.get(c.sha) or stored.get(c.sha)
                    branch = made_on.get(c.sha) or (members.get(c.sha) or [None])[0]
                    commit_id, new = _upsert_commit(con, repo_id, c, branch, run)
                    res.commits_new += int(new)
                    found, method = keys.choose(settings.project_keys, made_on.get(c.sha), members.get(c.sha, ()),
                                                (), f"{c.subject}\n{c.body}")
                    if _store_keys(con, commit_id, found, method, settings.project_keys):
                        res.keyed += 1
                for c in shared:
                    res.coauthored += 1
                    _upsert_commit(con, repo_id, c, made_on.get(c.sha), run, mine=False)
                for ref, entries in reflogs:
                    res.reflog_new += _store_reflog(con, repo_id, ref, entries, since)
                run.items_changed += res.commits_new + copies_new + res.reflog_new
            run.set_cursor(run.now)
    except (gitread.GitError, muninn.MuninnError, sqlite3.Error, OSError, ValueError, UnicodeError) as exc:
        res.error = str(exc) or type(exc).__name__
    return res


def _upsert_repo(con: sqlite3.Connection, info: gitread.RepoInfo, source: int, now: str) -> int:
    local = str(info.path.resolve())
    github = info.github_repo
    row = con.execute("SELECT id FROM repos WHERE local_path = ?", (local,)).fetchone()
    if github:
        other = con.execute("SELECT id, local_path FROM repos WHERE github_repo = ?", (github,)).fetchone()
        if other is not None and (row is None or other["id"] != row["id"]):
            if other["local_path"] is not None:
                github = None            # a second clone of the same GitHub repository keeps only its folder
            elif row is None:
                row = other              # a review-only row from the GitHub side gains its folder
            else:                        # both exist: fold the review-only row into this one
                con.execute("UPDATE pull_requests SET repo_id = ? WHERE repo_id = ?", (row["id"], other["id"]))
                con.execute("DELETE FROM repos WHERE id = ?", (other["id"],))
    if row:
        con.execute("UPDATE repos SET name = ?, local_path = ?, remote_url = ?, default_branch = ?, "
                    "github_repo = ?, source_id = ?, last_scanned_at = ? WHERE id = ?",
                    (info.name, local, info.remote_url, info.default_branch, github, source, now, row["id"]))
        return int(row["id"])
    return int(con.execute("INSERT INTO repos (source_id, name, github_repo, local_path, remote_url, default_branch, "
                           "last_scanned_at) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
                           (source, info.name, github, local, info.remote_url, info.default_branch, now)).fetchone()[0])


def _upsert_commit(con: sqlite3.Connection, repo_id: int, c: gitread.CommitRec, branch: Optional[str],
                   run: muninn.Run, mine: bool = True, copy: bool = False) -> Tuple[int, bool]:
    row = con.execute("SELECT id FROM commits WHERE repo_id = ? AND sha = ?", (repo_id, c.sha)).fetchone()
    merge = int(copy or c.is_merge)     # is_merge = 1: it combines commits already made, so it isn't work
    if row:
        # Whose it is follows your identities as they are now, and whether one of yours is a copy
        # follows the rules as they are now (0.3.0 stored some Enterprise Server squashes as work).
        con.execute("UPDATE commits SET patch_id = CASE WHEN ? THEN NULL ELSE coalesce(?, patch_id) END, "
                    "branch_hint = coalesce(branch_hint, ?), is_mine = ?, "
                    "is_merge = CASE WHEN ? THEN ? ELSE is_merge END WHERE id = ?",
                    (int(copy), c.patch_id, branch, int(mine), int(mine), merge, row[0]))
        return int(row[0]), False
    new_id = con.execute(
        "INSERT INTO commits (repo_id, sha, patch_id, author_name, author_email, authored_at, committed_at, subject, "
        "files_changed, additions, deletions, is_merge, is_mine, branch_hint, first_seen_at, run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (repo_id, c.sha, c.patch_id, c.author_name, c.author_email, c.authored_at, c.committed_at, c.subject,
         c.files_changed, c.additions, c.deletions, merge, int(mine), branch, run.now,
         run.id)).fetchone()[0]
    return int(new_id), True


def _store_keys(con: sqlite3.Connection, commit_id: int, found: List[str], method: Optional[str],
                projects: Sequence[str]) -> bool:
    """Keep the best evidence of a commit's keys; returns whether it has any.

    Keys found the same way or a better way replace the stored ones. Weaker or
    missing evidence never does: once the branch is deleted or the reflog
    expires, the keys saved earlier stand. Keys of projects you've removed
    from project_keys go, and keys you set by hand always stay.
    """
    if con.execute("SELECT 1 FROM commit_work_items WHERE commit_id = ? AND method = 'manual'",
                   (commit_id,)).fetchone():
        return True
    allowed = {p.upper() for p in projects}
    stored = con.execute("SELECT work_item_key, method FROM commit_work_items WHERE commit_id = ?",
                         (commit_id,)).fetchall()
    for key, _ in stored:
        if key.split("-", 1)[0] not in allowed:
            con.execute("DELETE FROM commit_work_items WHERE commit_id = ? AND work_item_key = ?", (commit_id, key))
    kept = [(k, m) for k, m in stored if k.split("-", 1)[0] in allowed]
    if not found or method is None:
        return bool(kept)
    best = min((keys.METHODS.index(m) for _, m in kept if m in keys.METHODS), default=len(keys.METHODS))
    if kept and keys.METHODS.index(method) > best:
        return True
    con.execute("DELETE FROM commit_work_items WHERE commit_id = ?", (commit_id,))
    for key in found:
        con.execute("INSERT INTO commit_work_items (commit_id, work_item_key, method) VALUES (?, ?, ?) "
                    "ON CONFLICT (commit_id, work_item_key) DO NOTHING", (commit_id, key, method))
    return True


def _store_reflog(con: sqlite3.Connection, repo_id: int, ref: str, entries: Sequence[gitread.ReflogRec],
                  since: Optional[str]) -> int:
    added = 0
    for e in entries:
        if since and e.at < since:
            continue
        row = con.execute("INSERT INTO reflog_entries (repo_id, ref, at, action, sha, message) "
                          "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (repo_id, ref, at, sha, action) DO NOTHING "
                          "RETURNING id", (repo_id, ref, e.at, e.action, e.sha, e.message[:500])).fetchone()
        added += int(row is not None)
    return added


def set_keys(con: sqlite3.Connection, commit_ids: Sequence[int], work_item_keys: Sequence[str]) -> None:
    """Give commits Jira keys by hand (method 'manual'), which collection never overrides.

    Pass every stored copy of a commit (one per clone). An empty list removes
    every key, but the next collection may find automatic ones again.
    """
    wanted = list(dict.fromkeys(k.strip().upper() for k in work_item_keys if k.strip()))
    with muninn.transaction(con):
        for cid in commit_ids:
            con.execute("DELETE FROM commit_work_items WHERE commit_id = ?", (cid,))
            for key in wanted:
                con.execute("INSERT INTO commit_work_items (commit_id, work_item_key, method) VALUES (?, ?, 'manual')",
                            (cid, key))


def refresh_ownership(con: sqlite3.Connection, removed: Sequence[str]) -> int:
    """After git emails are removed from your identities: their commits stop being yours.

    Commits nothing refers to are deleted; ones an earlier estimate stored
    sessions for stay, marked not yours, as that estimate's evidence. Call it
    inside the transaction that removed the identities. Returns how many
    commits stopped being yours.
    """
    emails = my_emails(con)
    gone = [(r[0], r[2]) for r in con.execute("SELECT id, author_email, is_merge FROM commits WHERE is_mine = 1")
            if r[1].lower() not in emails]
    for cid, _ in gone:
        con.execute("UPDATE commits SET is_mine = 0 WHERE id = ?", (cid,))
    for email in {e.strip().lower() for e in removed}:
        con.execute("DELETE FROM commits WHERE is_mine = 0 AND lower(author_email) = ? "
                    "AND id NOT IN (SELECT commit_id FROM session_commits)", (email,))
    return sum(1 for _, copy in gone if not copy)     # squash copies were never counted as commits


def identity_hint() -> Optional[str]:
    """Your email from git config, to offer when no identity is set."""
    try:
        return gitread.configured_identity()[0]
    except gitread.GitError:
        return None
