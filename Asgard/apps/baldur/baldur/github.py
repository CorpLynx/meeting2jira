"""GitHub (spec build step 4): your pull requests, their reviews, and the reviews requested of you.

Read-only, always: Baldur never comments, approves or merges. Without a token it does nothing,
and Baldur works from local git alone.

- The token lives in Windows Credential Manager (per user, no admin rights), under
  "Asgard Baldur GitHub <host>". BALDUR_GITHUB_TOKEN overrides it, for tests and other
  platforms. It is never printed, logged or written anywhere else.
- Every list request is conditional (If-None-Match with the ETag from the last answer, kept
  as the sync stream's cursor in Muninn), so an unchanged answer costs nothing.
- It fetches first and writes after, so Muninn's write lock is never held across a request.
- Pull requests in repositories you've cloned (repos.github_repo) are synced with their
  reviews. Every open pull request that requests your review, from the search
  `is:pr is:open review-requested:@me`, is synced too; one in a repository you haven't
  cloned gets a review-only repos row, alerted on and never estimated.
- Your pull requests' head branches name Jira keys for their commits (method 'pr'). That
  evidence ranks below the reflog and branch membership and above the commit message, and
  weaker evidence never replaces stronger (collect._store_keys).
"""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import re
import socket
import sqlite3
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from asgard import muninn

from . import keys as keyfinder
from .collect import _store_keys
from .settings import Settings

TOKEN_ENV = "BALDUR_GITHUB_TOKEN"
SEARCH = "is:pr is:open review-requested:@me"
_LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
_REVIEW_STATES = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes_requested", "COMMENTED": "commented",
                  "DISMISSED": "dismissed"}
MAX_PAGES = 10


class GitHubError(RuntimeError):
    """GitHub couldn't be read; the message says what to do. status is the HTTP status, if any."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# The token: Windows Credential Manager
# --------------------------------------------------------------------------

def token_target(host: str) -> str:
    return f"Asgard Baldur GitHub {host}"


if os.name == "nt":
    from ctypes import wintypes

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    _advapi.CredReadW.restype = wintypes.BOOL
    _advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    _advapi.CredWriteW.restype = wintypes.BOOL
    _advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi.CredDeleteW.restype = wintypes.BOOL
    _advapi.CredFree.argtypes = [ctypes.c_void_p]
    _advapi.CredFree.restype = None
    _GENERIC, _PERSIST_LOCAL_MACHINE, _NOT_FOUND = 1, 2, 1168


def load_token(host: str) -> Optional[str]:
    """Your token for host: BALDUR_GITHUB_TOKEN if set, else Credential Manager; None if there's none."""
    env = os.environ.get(TOKEN_ENV, "").strip()
    if env:
        return env
    if os.name != "nt":
        return None
    found = ctypes.POINTER(_CREDENTIAL)()
    if not _advapi.CredReadW(token_target(host), _GENERIC, 0, ctypes.byref(found)):
        error = ctypes.get_last_error()
        if error == _NOT_FOUND:
            return None
        raise GitHubError(f"Couldn't read the GitHub token from Credential Manager (error {error}).")
    try:
        cred = found.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return blob.decode("utf-8").strip() or None
    finally:
        _advapi.CredFree(found)


def save_token(host: str, token: str) -> None:
    token = token.strip()
    if not token or any(ch.isspace() for ch in token):
        raise GitHubError("That doesn't look like a GitHub token; paste the whole token, with no spaces.")
    if os.name != "nt":
        raise GitHubError(f"Tokens are kept in Windows Credential Manager. Elsewhere, set {TOKEN_ENV}.")
    data = token.encode("utf-8")
    blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    cred = _CREDENTIAL()
    cred.Type = _GENERIC
    cred.TargetName = token_target(host)
    cred.Comment = "Read-only GitHub token for Asgard's Baldur"
    cred.CredentialBlobSize = len(data)
    cred.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = _PERSIST_LOCAL_MACHINE
    cred.UserName = "baldur"
    if not _advapi.CredWriteW(ctypes.byref(cred), 0):
        raise GitHubError(f"Couldn't save the token in Credential Manager (error {ctypes.get_last_error()}).")


def delete_token(host: str) -> bool:
    if os.name != "nt":
        return False
    if _advapi.CredDeleteW(token_target(host), _GENERIC, 0):
        return True
    if ctypes.get_last_error() == _NOT_FOUND:
        return False
    raise GitHubError(f"Couldn't remove the token from Credential Manager (error {ctypes.get_last_error()}).")


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------

@dataclass
class Response:
    status: int
    data: Any = None
    etag: Optional[str] = None
    next_url: Optional[str] = None

    @property
    def unchanged(self) -> bool:
        return self.status == 304


class Client:
    """GET-only REST client for one GitHub (github.com, GHE.com or Enterprise Server)."""

    def __init__(self, api: str, token: str, timeout: float = 20.0) -> None:
        parts = urllib.parse.urlsplit(api)
        local = (parts.hostname or "") in ("127.0.0.1", "localhost", "::1")
        if parts.scheme != "https" and not (parts.scheme == "http" and local):
            raise GitHubError("github_api must be an https address.")
        self.api = api.rstrip("/")
        self.host = parts.hostname or ""
        self._token = token
        self.timeout = timeout
        self.requests = 0
        # Verified TLS, always, with the Windows certificate stores Python already trusts.
        self._context = ssl.create_default_context() if parts.scheme == "https" else None

    def url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        return f"{self.api}{path}{query}"

    def get(self, path_or_url: str, params: Optional[Dict[str, Any]] = None,
            etag: Optional[str] = None) -> Response:
        url = path_or_url if path_or_url.startswith(("http://", "https://")) else self.url(path_or_url, params)
        if not url.startswith(self.api):
            raise GitHubError("GitHub answered with a link to another server; Baldur won't follow it.")
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "Asgard-Baldur",
                   "Authorization": f"Bearer {self._token}"}
        if etag:
            headers["If-None-Match"] = etag
        request = urllib.request.Request(url, headers=headers, method="GET")
        shown = url.split("?", 1)[0][len(self.api):] or "/"
        self.requests += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._context) as answer:
                body = answer.read()
                link = answer.headers.get("Link") or ""
                nxt = _LINK_NEXT.search(link)
                return Response(answer.status, json.loads(body.decode("utf-8")) if body else None,
                                answer.headers.get("ETag"), nxt.group(1) if nxt else None)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return Response(304, None, exc.headers.get("ETag") or etag)
            raise self._explain(exc, shown) from None
        except ssl.SSLError as exc:
            raise GitHubError(f"Couldn't make a secure connection to {self.host} ({exc.reason or exc}). If your "
                              "browser reaches it, the server is probably not sending its intermediate "
                              "certificate; ask its administrators.") from None
        except (urllib.error.URLError, socket.timeout, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLError):
                raise GitHubError(f"Couldn't make a secure connection to {self.host} ({reason}). If your browser "
                                  "reaches it, the server is probably not sending its intermediate certificate; "
                                  "ask its administrators.") from None
            raise GitHubError(f"Couldn't reach {self.host} ({reason}). Check github_api in baldur.json and your "
                              "network.") from None
        except ValueError:
            raise GitHubError(f"{self.host} answered {shown} with something that isn't JSON.") from None

    def _explain(self, exc: urllib.error.HTTPError, shown: str) -> GitHubError:
        code = exc.code
        if code == 401:
            return GitHubError(f"{self.host} refused the token (401). It may have expired or been revoked: "
                               "create a new read-only token and run  baldur.cmd github token", code)
        if code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
            reset = exc.headers.get("X-RateLimit-Reset")
            when = (dt.datetime.fromtimestamp(int(reset)).strftime("%H:%M") if reset and reset.isdigit()
                    else "later")
            return GitHubError(f"{self.host}'s rate limit is used up; Baldur tries again after {when}.", code)
        if code in (403, 404):
            return GitHubError(f"{self.host} won't show {shown} to this token ({code}). A fine-grained token "
                               "reaches one organization and none of the enterprise's internal repositories; "
                               "a classic token with repo scope reaches them all.", code)
        return GitHubError(f"{self.host} answered {shown} with HTTP {code}.", code)

    def get_all(self, path: str, params: Optional[Dict[str, Any]] = None) -> List[Any]:
        """Every page of a list (up to MAX_PAGES), unconditionally."""
        out: List[Any] = []
        url: Optional[str] = self.url(path, params)
        pages = 0
        while url and pages < MAX_PAGES:
            got = self.get(url)
            out.extend(got.data or [])
            url, pages = got.next_url, pages + 1
        return out


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

def _ts(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return muninn.to_ts(dt.datetime.fromisoformat(value.replace("Z", "+00:00")))


@dataclass
class SyncResult:
    login: str = ""
    repos: int = 0
    pulls_changed: int = 0
    reviews_new: int = 0
    requested: int = 0
    keyed_commits: int = 0
    unchanged: int = 0
    requests: int = 0
    problems: List[str] = field(default_factory=list)


def source_for(con: sqlite3.Connection, client: Client) -> int:
    return muninn.ensure_source(con, "github", f"github {client.host}", client.api)


def _me(con: sqlite3.Connection, client: Client, source: int) -> str:
    with muninn.Run(con, "baldur", source, "github:user") as run:
        got = client.get("/user", etag=run.cursor)
        if got.unchanged:
            row = con.execute("SELECT value FROM identities WHERE kind = 'github_login' AND source_id = ?",
                              (source,)).fetchone()
            if row:
                return str(row[0])
            got = client.get("/user")
        login = str((got.data or {}).get("login") or "")
        if not login:
            raise GitHubError(f"{client.host} didn't say who the token belongs to.")
        with run.batch():
            muninn.add_identity(con, "github_login", login, source)
        run.set_cursor(got.etag or "")
    return login


def _pr_row(raw: Dict[str, Any], me: str, projects: Sequence[str]) -> Dict[str, Any]:
    head = str((raw.get("head") or {}).get("ref") or "")
    merged = _ts(raw.get("merged_at"))
    state = "merged" if merged else ("open" if raw.get("state") == "open" else "closed")
    found = keyfinder.branch_keys(head, projects) or keyfinder.find_keys(raw.get("title"), projects)
    requested = {str(u.get("login", "")).lower() for u in raw.get("requested_reviewers") or []}
    author = str((raw.get("user") or {}).get("login") or "")
    return {"number": int(raw["number"]), "title": str(raw.get("title") or "")[:500], "author": author,
            "is_mine": int(author.lower() == me.lower()), "head_ref": head, "work_item_key": found[0] if found else None,
            "state": state, "is_draft": int(bool(raw.get("draft"))),
            "requested": me.lower() in requested and state == "open",
            "created_at": _ts(raw.get("created_at")), "updated_at": _ts(raw.get("updated_at")),
            "merged_at": merged, "closed_at": _ts(raw.get("closed_at")),
            "additions": raw.get("additions"), "deletions": raw.get("deletions"),
            "url": str(raw.get("html_url") or "")}


def _upsert_pr(con: sqlite3.Connection, repo_id: int, row: Dict[str, Any], run: muninn.Run,
               requested: Optional[bool] = None) -> Tuple[int, bool]:
    """Store one pull request; returns (id, changed). requested None keeps what the review search said."""
    before = con.execute("SELECT * FROM pull_requests WHERE repo_id = ? AND number = ?",
                         (repo_id, row["number"])).fetchone()
    flag = int(row["requested"]) if requested is None else int(requested)
    if before is not None and requested is None:
        flag = before["review_requested"] if row["state"] == "open" else 0
    if before is None:
        new = con.execute(
            "INSERT INTO pull_requests (repo_id, number, title, author, is_mine, head_ref, work_item_key, state, "
            "is_draft, review_requested, created_at, updated_at, merged_at, closed_at, additions, deletions, url, "
            "first_seen_at, last_seen_at, run_id) VALUES (:repo, :number, :title, :author, :is_mine, :head_ref, "
            ":work_item_key, :state, :is_draft, :flag, :created_at, :updated_at, :merged_at, :closed_at, :additions, "
            ":deletions, :url, :now, :now, :run) RETURNING id",
            dict(row, repo=repo_id, flag=flag, now=run.now, run=run.id)).fetchone()[0]
        return int(new), True
    changed = before["updated_at"] != row["updated_at"] or before["state"] != row["state"] \
        or before["review_requested"] != flag
    # A review requested again after it was answered alerts again.
    renotify = flag == 1 and before["review_requested"] == 0
    con.execute(
        "UPDATE pull_requests SET title = :title, author = :author, is_mine = :is_mine, head_ref = :head_ref, "
        "work_item_key = :work_item_key, state = :state, is_draft = :is_draft, review_requested = :flag, "
        "notified_at = CASE WHEN :renotify THEN NULL ELSE notified_at END, updated_at = :updated_at, "
        "merged_at = :merged_at, closed_at = :closed_at, additions = coalesce(:additions, additions), "
        "deletions = coalesce(:deletions, deletions), url = :url, last_seen_at = :now, "
        "run_id = CASE WHEN :changed THEN :run ELSE run_id END WHERE id = :id",
        dict(row, flag=flag, renotify=int(renotify), now=run.now, run=run.id, changed=int(changed),
             id=before["id"]))
    return int(before["id"]), changed


def _upsert_reviews(con: sqlite3.Connection, pr_id: int, reviews: Sequence[Dict[str, Any]], me: str) -> int:
    added = 0
    for r in reviews:
        state = _REVIEW_STATES.get(str(r.get("state") or "").upper())
        submitted = _ts(r.get("submitted_at"))
        if state is None or submitted is None:      # PENDING: not submitted yet
            continue
        reviewer = str((r.get("user") or {}).get("login") or "")
        got = con.execute(
            "INSERT INTO pr_reviews (pr_id, github_id, reviewer, is_mine, state, submitted_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (pr_id, github_id) DO UPDATE SET state = excluded.state RETURNING (xmax IS NULL)"
            if False else
            "INSERT INTO pr_reviews (pr_id, github_id, reviewer, is_mine, state, submitted_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (pr_id, github_id) DO UPDATE SET state = excluded.state",
            (pr_id, str(r.get("id")), reviewer, int(reviewer.lower() == me.lower()), state, submitted))
        added += got.rowcount > 0 and 1 or 0
    return added


def _key_commits(con: sqlite3.Connection, shas: Sequence[str], head_ref: str, projects: Sequence[str]) -> int:
    """Give your commits in a pull request the keys its head branch names (method 'pr')."""
    found = keyfinder.branch_keys(head_ref, projects)
    if not found:
        return 0
    n = 0
    for sha in shas:
        for (cid,) in con.execute("SELECT id FROM commits WHERE sha = ? AND is_mine = 1 AND is_merge = 0", (sha,)):
            before = {tuple(r) for r in con.execute(
                "SELECT work_item_key, method FROM commit_work_items WHERE commit_id = ?", (cid,))}
            _store_keys(con, cid, found, "pr", projects)
            after = {tuple(r) for r in con.execute(
                "SELECT work_item_key, method FROM commit_work_items WHERE commit_id = ?", (cid,))}
            n += int(after != before)
    return n


def _repo_for(con: sqlite3.Connection, source: int, full: str) -> int:
    """The repos row for owner/name: your clone's, or a review-only one made now."""
    row = con.execute("SELECT id FROM repos WHERE github_repo = ?", (full,)).fetchone()
    if row:
        return int(row[0])
    return int(con.execute("INSERT INTO repos (source_id, name, github_repo) VALUES (?, ?, ?) RETURNING id",
                           (source, full.split("/", 1)[1], full)).fetchone()[0])


def sync(con: sqlite3.Connection, settings: Settings, client: Client) -> SyncResult:
    """One pass: your repositories' pull requests, their reviews, and the reviews requested of you."""
    projects = list(settings.project_keys)
    res = SyncResult()
    source = source_for(con, client)
    me = res.login = _me(con, client, source)

    clones = con.execute("SELECT id, github_repo FROM repos WHERE github_repo IS NOT NULL AND local_path IS NOT NULL "
                         "AND active = 1 ORDER BY github_repo").fetchall()
    for repo_id, full in clones:
        res.repos += 1
        try:
            _sync_repo(con, client, source, int(repo_id), str(full), me, projects, res)
        except GitHubError as exc:
            if exc.status not in (403, 404):
                raise
            res.problems.append(str(exc))
    _sync_requests(con, client, source, me, projects, res)
    res.requests = client.requests
    return res


def _sync_repo(con: sqlite3.Connection, client: Client, source: int, repo_id: int, full: str, me: str,
               projects: Sequence[str], res: SyncResult) -> None:
    with muninn.Run(con, "baldur", source, f"github:pulls {full}") as run:
        got = client.get(f"/repos/{full}/pulls", {"state": "all", "sort": "updated", "direction": "desc",
                                                  "per_page": 50}, etag=run.cursor)
        if got.unchanged:
            res.unchanged += 1
            return
        rows = [_pr_row(raw, me, projects) for raw in got.data or []]
        stored = {r["number"]: r["updated_at"] for r in con.execute(
            "SELECT number, updated_at FROM pull_requests WHERE repo_id = ?", (repo_id,))}
        fresh = [r for r in rows if stored.get(r["number"]) != r["updated_at"]]
        # Fetch everything first; write once at the end, so no lock is held across a request.
        extra: Dict[int, Tuple[List[Any], List[str]]] = {}
        for r in fresh:
            reviews = client.get_all(f"/repos/{full}/pulls/{r['number']}/reviews", {"per_page": 100})
            shas: List[str] = []
            if r["is_mine"] and keyfinder.branch_keys(r["head_ref"], projects):
                shas = [str(c.get("sha")) for c in client.get_all(f"/repos/{full}/pulls/{r['number']}/commits",
                                                                  {"per_page": 100})]
            extra[r["number"]] = (reviews, shas)
        with run.batch():
            for r in rows:
                pr_id, changed = _upsert_pr(con, repo_id, r, run)
                run.items_seen += 1
                if r["number"] in extra:
                    reviews, shas = extra[r["number"]]
                    res.reviews_new += _upsert_reviews(con, pr_id, reviews, me)
                    res.keyed_commits += _key_commits(con, shas, r["head_ref"], projects)
                if changed:
                    res.pulls_changed += 1
                    run.items_changed += 1
        run.set_cursor(got.etag or "")


def _sync_requests(con: sqlite3.Connection, client: Client, source: int, me: str, projects: Sequence[str],
                   res: SyncResult) -> None:
    with muninn.Run(con, "baldur", source, "github:review-requested") as run:
        got = client.get("/search/issues", {"q": SEARCH, "per_page": 100}, etag=run.cursor)
        if got.unchanged:
            res.unchanged += 1
            res.requested = int(con.execute("SELECT count(*) FROM pull_requests WHERE state = 'open' "
                                            "AND review_requested = 1").fetchone()[0])
            return
        wanted: List[Tuple[str, int]] = []
        for item in (got.data or {}).get("items") or []:
            m = re.search(r"/repos/([^/]+/[^/]+)$", str(item.get("repository_url") or ""))
            if m and item.get("number"):
                wanted.append((m.group(1), int(item["number"])))
        details = {(full, n): _pr_row(client.get(f"/repos/{full}/pulls/{n}").data or {}, me, projects)
                   for full, n in wanted}
        with run.batch():
            asked: Set[int] = set()
            for (full, _), row in details.items():
                if not row.get("number"):
                    continue
                pr_id, changed = _upsert_pr(con, _repo_for(con, source, full), row, run,
                                            requested=row["state"] == "open")
                asked.add(pr_id)
                run.items_seen += 1
                run.items_changed += int(changed)
            # Requests that are gone (answered, withdrawn or the PR closed) stop counting.
            for (pid,) in con.execute("SELECT id FROM pull_requests WHERE review_requested = 1").fetchall():
                if pid not in asked:
                    con.execute("UPDATE pull_requests SET review_requested = 0 WHERE id = ?", (pid,))
                    run.items_changed += 1
        run.set_cursor(got.etag or "")
        res.requested = len(asked)
