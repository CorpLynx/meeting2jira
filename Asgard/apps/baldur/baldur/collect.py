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
import json
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
    message_keys: Dict[int, List[str]] = field(default_factory=dict)   # keys in each commit's message
    error: Optional[str] = None


@dataclass
class CollectResult:
    repos: List[RepoResult] = field(default_factory=list)
    pr_keyed: int = 0                 # commits whose keys or copy status changed from stored pull requests

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
    # Pull requests synced before these commits arrived still key them: the order of collect and
    # the GitHub sync doesn't matter.
    found_in_messages: Dict[int, List[str]] = {}
    for r in result.repos:
        found_in_messages.update(r.message_keys)
    result.pr_keyed = apply_pr_evidence(con, settings.project_keys, found_in_messages)
    return result


# --------------------------------------------------------------------------
# Evidence from pull requests (method 'pr'), from what the GitHub sync stored
# --------------------------------------------------------------------------

@dataclass
class _Merge:
    """What a merged PR of yours tells about the commit GitHub made when merging it."""
    listed: Set[str]                  # the PR's own commits, as GitHub lists them
    listed_patches: Set[str]          # their patch ids, for the ones collected here


def pr_merges(con: sqlite3.Connection) -> Dict[str, _Merge]:
    """{merge_commit_sha of each merged PR of yours, when it isn't one of the PR's own commits: _Merge}."""
    out: Dict[str, _Merge] = {}
    for r in con.execute(
            "SELECT p.id, p.merge_commit_sha FROM pull_requests p WHERE p.is_mine = 1 AND p.state = 'merged' "
            "AND p.commits_listed = 1 AND p.merge_commit_sha IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "
            "pull_request_commits m WHERE m.pr_id = p.id AND m.sha = p.merge_commit_sha)"):
        listed = {x[0] for x in con.execute("SELECT sha FROM pull_request_commits WHERE pr_id = ?", (r[0],))}
        patches = {x[0] for x in con.execute(
            "SELECT DISTINCT c.patch_id FROM pull_request_commits m JOIN commits c ON c.sha = m.sha "
            "WHERE m.pr_id = ? AND c.is_mine = 1 AND c.patch_id IS NOT NULL", (r[0],))}
        known = con.execute("SELECT 1 FROM pull_request_commits m JOIN commits c ON c.sha = m.sha "
                            "WHERE m.pr_id = ? AND c.is_mine = 1 LIMIT 1", (r[0],)).fetchone() is not None
        merge = out.setdefault(str(r[1]), _Merge(set(), set()))
        merge.listed |= listed if known else set()
        merge.listed_patches |= patches
    return out


def is_squash_copy(merge: Optional[_Merge], patch_id: Optional[str]) -> bool:
    """Whether the commit GitHub made merging a PR is a copy of work already counted.

    A squash combines the PR's commits into one, so its patch matches none of theirs: a copy. A
    rebase-merge replays each commit, so the merge commit's patch matches one of them, and it then
    counts once with its original, like any cherry-pick. When none of the PR's own commits were
    collected here (the work was done on another machine), the merge commit is the only evidence of
    that work, so it counts.
    """
    if merge is None or not merge.listed:
        return False
    return patch_id is None or patch_id not in merge.listed_patches


_STATE_RANK = "CASE WHEN p.state = 'merged' THEN 0 WHEN p.state = 'open' AND p.is_draft = 0 THEN 1 " \
              "WHEN p.state = 'open' THEN 2 ELSE 3 END"
LIST_CAP = 250                      # GitHub lists at most this many commits of one PR


def _best_pr_head(con: sqlite3.Connection, shas: Sequence[str], repo_id: int) -> Optional[str]:
    """The head branch of the PR of yours a commit was made for, among those listing any of shas.

    Merged beats open, open beats draft, draft beats closed-unmerged: a PR you abandoned or haven't
    finished never takes a commit from the one that shipped it. Then a PR in the commit's own
    repository, then the smallest (a commit in a stacked PR and in the PR made for it, or in a
    feature PR and a release PR, belongs to the smaller one). Numbers are compared only within one
    repository. GitHub cuts a PR's commit list off at 250, so two such lists can't be told apart
    by size: that is no evidence, and the commit's message decides.
    """
    if not shas:
        return None
    marks = ",".join("?" * len(shas))
    rows = con.execute(
        f"WITH sizes AS (SELECT pr_id, count(*) AS n FROM pull_request_commits GROUP BY pr_id) "
        f"SELECT p.head_ref, {_STATE_RANK} AS rank, p.repo_id = ? AS here, z.n >= ? AS cut "
        f"FROM pull_request_commits m JOIN pull_requests p ON p.id = m.pr_id "
        f"JOIN sizes z ON z.pr_id = p.id WHERE m.sha IN ({marks}) AND p.is_mine = 1 "
        f"ORDER BY rank, here DESC, cut, z.n, p.repo_id, p.number, p.id LIMIT 2",
        (repo_id, LIST_CAP, *shas)).fetchall()
    if not rows:
        return None
    best = rows[0]
    if best["cut"] and len(rows) > 1 and (rows[1]["rank"], rows[1]["here"]) == (best["rank"], best["here"]):
        return None
    return str(best["head_ref"])


def apply_pr_evidence(con: sqlite3.Connection, projects: Sequence[str],
                      message_keys: Optional[Dict[int, List[str]]] = None) -> int:
    """Bring every commit's 'pr' keys and squash copies in line with the stored pull requests.

    Uses Muninn only (no network), so it runs after a GitHub sync and after a collection alike.
    - The commit GitHub made squash-merging your PR becomes a copy with no keys (is_squash_copy).
    - Your commits in your PRs, and their rebased copies (same patch), get the keys of the head
      branch of the PR they were made for (_best_pr_head). If that head names none, the PR is no
      evidence and the commit's message decides.
    - A commit no PR lists any more loses its 'pr' keys and falls back to its message.
    message_keys holds the keys collection just found in each commit's full message (the body
    isn't stored); without it, the subject is used. Reflog, branch and hand-set keys are never
    touched. Returns how many commits changed.
    """
    message_keys = message_keys or {}
    changed = 0
    with muninn.transaction(con):
        for sha, merge in pr_merges(con).items():
            for r in con.execute("SELECT id, patch_id FROM commits WHERE sha = ? AND is_mine = 1 AND is_merge = 0",
                                 (sha,)).fetchall():
                if is_squash_copy(merge, r["patch_id"]):
                    con.execute("UPDATE commits SET is_merge = 1, patch_id = NULL WHERE id = ?", (r["id"],))
                    con.execute("DELETE FROM commit_work_items WHERE commit_id = ?", (r["id"],))
                    changed += 1
        # Repositories with PRs of yours whose commits weren't read yet: their 'pr' keys can't be judged.
        unread = {r[0] for r in con.execute("SELECT DISTINCT repo_id FROM pull_requests WHERE is_mine = 1 "
                                            "AND commits_listed = 0")}
        listed = ("SELECT m.sha FROM pull_request_commits m JOIN pull_requests p ON p.id = m.pr_id "
                  "WHERE p.is_mine = 1")
        candidates = con.execute(
            "SELECT c.id, c.sha, c.subject, c.repo_id, c.patch_id, c.message_keys FROM commits c WHERE c.is_mine = 1 "
            f"AND c.is_merge = 0 AND (c.sha IN ({listed}) OR (c.patch_id IS NOT NULL AND c.patch_id IN ("
            f"SELECT x.patch_id FROM commits x WHERE x.patch_id IS NOT NULL AND x.sha IN ({listed}))) "
            "OR EXISTS (SELECT 1 FROM commit_work_items w WHERE w.commit_id = c.id AND w.method = 'pr'))").fetchall()
        for c in candidates:
            stored = {(r[0], r[1]) for r in con.execute(
                "SELECT work_item_key, method FROM commit_work_items WHERE commit_id = ?", (c["id"],))}
            if any(m in ("manual", "reflog", "branch") for _, m in stored):
                continue
            shas = [c["sha"]]
            if c["patch_id"]:
                shas += [r[0] for r in con.execute("SELECT DISTINCT sha FROM commits WHERE patch_id = ? AND sha <> ?",
                                                   (c["patch_id"], c["sha"]))]
            head = _best_pr_head(con, shas, c["repo_id"])
            found = keys.branch_keys(head, projects) if head else []
            if found:
                want = {(k, "pr") for k in found}
                if stored != want:
                    con.execute("DELETE FROM commit_work_items WHERE commit_id = ?", (c["id"],))
                    con.executemany("INSERT INTO commit_work_items (commit_id, work_item_key, method) "
                                    "VALUES (?, ?, 'pr')", [(c["id"], k) for k in found])
                    changed += 1
            elif any(m == "pr" for _, m in stored) and (head is not None or c["repo_id"] not in unread):
                fallback = message_keys.get(c["id"])
                if fallback is None and c["message_keys"]:
                    allowed = {p.upper() for p in projects}
                    fallback = [k for k in json.loads(c["message_keys"]) if k.split("-", 1)[0] in allowed]
                if fallback is None:
                    fallback = keys.find_keys(c["subject"], projects)
                con.execute("DELETE FROM commit_work_items WHERE commit_id = ?", (c["id"],))
                con.executemany("INSERT INTO commit_work_items (commit_id, work_item_key, method) "
                                "VALUES (?, ?, 'message')", [(c["id"], k) for k in fallback])
                changed += 1
    return changed


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
            # What GitHub made when it merged your pull requests: a squash is a copy (see is_squash_copy).
            merges = pr_merges(con)
            copies |= {c.sha for c in mine if c.sha in merges and c.sha not in copies
                       and is_squash_copy(merges[c.sha], fingerprints.get(c.sha) or stored.get(c.sha))}
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
                    in_message = keys.message_keys(f"{c.subject}\n{c.body}")
                    con.execute("UPDATE commits SET message_keys = ? WHERE id = ? AND message_keys IS NOT ?",
                                (json.dumps(in_message), commit_id, json.dumps(in_message)))
                    res.message_keys[commit_id] = keys.find_keys(f"{c.subject}\n{c.body}", settings.project_keys)
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
        res.error = muninn.scrub(str(exc)) or type(exc).__name__     # git errors can echo a remote URL
    return res


def _upsert_repo(con: sqlite3.Connection, info: gitread.RepoInfo, source: int, now: str) -> int:
    local = str(info.path.resolve())
    # GitHub's owner/name is case-insensitive: CSB/Asgard and csb/asgard are one repository.
    github = info.github_repo.lower() if info.github_repo else None
    remote = muninn.redact_url(info.remote_url)        # a token typed into the remote URL isn't stored
    row = con.execute("SELECT id FROM repos WHERE local_path = ?", (local,)).fetchone()
    if github:
        other = con.execute("SELECT id, local_path FROM repos WHERE lower(github_repo) = ?", (github,)).fetchone()
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
                    (info.name, local, remote, info.default_branch, github, source, now, row["id"]))
        return int(row["id"])
    return int(con.execute("INSERT INTO repos (source_id, name, github_repo, local_path, remote_url, default_branch, "
                           "last_scanned_at) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
                           (source, info.name, github, local, remote, info.default_branch, now)).fetchone()[0])


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
