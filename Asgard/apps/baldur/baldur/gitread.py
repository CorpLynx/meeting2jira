"""Reading git repositories: metadata only, never code.

Diffs pass through one pipe from `git log -p` to `git patch-id`, which turns
each change into a fingerprint; Baldur keeps the fingerprint, not the diff.

Commits are read from branches, remote branches, tags and HEAD, never from
`--all`, which would include stash entries and notes. Times are read as Unix
seconds, so a commit with a malformed time zone can't stop a collection.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

CREATE_NO_WINDOW = 0x08000000
SKIP_DIRS = {"node_modules", ".venv", "venv", "env", "__pycache__", ".tox", "dist", "build", "target",
             ".gradle", ".idea", ".vs", "bin", "obj", "site-packages"}
_TS = "%Y-%m-%dT%H:%M:%SZ"
_GITHUB_RE = re.compile(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^/:]+)[:/](?:\d+/)?([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE)
_URL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/]+@)?([^/:]+)(?::\d+)?[:/](.+?)(?:\.git)?/*$", re.IGNORECASE)
_REFLOG_TIME_RE = re.compile(r"@\{(\d+)\}$")
_COMMIT_ACTIONS = ("commit", "cherry-pick", "revert", "merge", "am")
_STEP_RE = re.compile(r"\((pick|reword|edit|squash|fixup)\)")
_RETURN_RE = re.compile(r"\((finish|abort)\): returning to (?:refs/heads/)?(\S+)")
_RENAME_RE = re.compile(r"^Branch: renamed (?:refs/heads/)?(\S+) to (?:refs/heads/)?(\S+)")
_LOG_FORMAT = "%x1e%H%x1f%P%x1f%an%x1f%ae%x1f%at%x1f%cn%x1f%ce%x1f%ct%x1f%s%x1f%b%x1d"
_COAUTHOR_RE = re.compile(r"^[ \t]*co-authored-by:[ \t]*(.*?)[ \t]*<([^>\n]*)>[ \t]*$", re.IGNORECASE | re.MULTILINE)
_PR_SUFFIX_RE = re.compile(r"\(#\d+\)\s*$")
_GITHUB_COMMITTERS = ("github", "github enterprise")
_SQUASHED_RE = re.compile(r"^commit ([0-9a-f]{40}|[0-9a-f]{64})$", re.MULTILINE)
_version: Optional[Tuple[int, int]] = None


class GitError(RuntimeError):
    """git failed or isn't installed. The message says which, in plain words."""


def git_path() -> Optional[str]:
    return shutil.which("git")


def _flags() -> int:
    return CREATE_NO_WINDOW if os.name == "nt" else 0


def run_git(repo: Optional[Path], *args: str, timeout: int = 300, check: bool = True) -> str:
    exe = git_path()
    if not exe:
        raise GitError("git isn't installed, or isn't on your PATH.")
    cmd = [exe, "-c", "core.quotepath=off", "-c", "i18n.logOutputEncoding=utf-8", "-c", "color.ui=never"]
    if repo is not None:
        cmd += ["-C", str(repo)]
    cmd += list(args)
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_flags())
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} took longer than {timeout} seconds in {repo}") from exc
    except OSError as exc:
        raise GitError(f"git couldn't start: {exc}") from exc
    if check and done.returncode != 0:
        message = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GitError(f"git {args[0]} failed in {repo}: {message[-1] if message else done.returncode}")
    return done.stdout.decode("utf-8", "replace")


def git_version() -> Tuple[int, int]:
    global _version
    if _version is None:
        m = re.search(r"(\d+)\.(\d+)", run_git(None, "--version"))
        _version = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    return _version


def epoch_ts(seconds: str) -> Optional[str]:
    """Unix seconds as Muninn's UTC text; None for anything that isn't a sane time."""
    try:
        return dt.datetime.fromtimestamp(int(seconds), tz=dt.timezone.utc).strftime(_TS)
    except (ValueError, OverflowError, OSError):
        return None


def _since_args(since: Optional[str]) -> List[str]:
    # --since stops walking at the first older commit, so a commit under one with a skewed clock
    # would be missed; --since-as-filter (git 2.39) checks every commit. Older git: filter here.
    if since and git_version() >= (2, 39):
        return [f"--since-as-filter={since}"]
    return []


def _revs(repo: Path) -> List[str]:
    revs = ["--branches", "--remotes", "--tags"]
    if run_git(repo, "rev-parse", "-q", "--verify", "HEAD^{commit}", check=False).strip():
        revs.append("HEAD")
    return revs


# --------------------------------------------------------------------------
# Finding repositories
# --------------------------------------------------------------------------

def discover(roots: Iterable[str], max_depth: int = 3) -> List[Path]:
    """Repositories under the roots, at most max_depth folders down; never the whole disk."""
    found: List[Path] = []
    seen = set()

    def walk(folder: Path, depth: int) -> None:
        try:
            if (folder / ".git").exists():
                real = folder.resolve()
                if real not in seen:
                    seen.add(real)
                    found.append(folder)
                return
            if depth >= max_depth:
                return
            children = sorted(p for p in folder.iterdir() if p.is_dir())
        except OSError:
            return
        for child in children:
            if child.name.startswith(".") or child.name.lower() in SKIP_DIRS or child.is_symlink():
                continue
            walk(child, depth + 1)

    for root in roots:
        path = Path(os.path.expandvars(os.path.expanduser(root)))
        if path.is_dir():
            walk(path, 0)
    return found


def main_folder(path: Path) -> Path:
    """The repository's main working folder: a linked worktree maps to the folder it was added from."""
    out = run_git(path, "rev-parse", "--git-common-dir", check=False).strip()
    if not out:
        return path
    common = Path(out)
    if not common.is_absolute():
        common = path / common
    try:
        common = common.resolve()
    except OSError:
        return path
    return common.parent if common.name == ".git" and common.parent.is_dir() else path


def worktrees(repo: Path) -> List[Tuple[Path, Optional[str]]]:
    """(folder, branch checked out there now) for every working folder, main first.

    Bare and missing ones are left out; the branch is None for a detached HEAD.
    """
    found: List[Tuple[Path, Optional[str]]] = []
    for block in run_git(repo, "worktree", "list", "--porcelain", check=False).split("\n\n"):
        lines = block.strip().splitlines()
        if not lines or not lines[0].startswith("worktree "):
            continue
        path = Path(lines[0][len("worktree "):])
        if any(x == "bare" or x.startswith("prunable") for x in lines[1:]) or not path.is_dir():
            continue
        branch = next((x[len("branch refs/heads/"):] for x in lines[1:] if x.startswith("branch refs/heads/")), None)
        found.append((path, branch))
    return found or [(repo, None)]


@dataclass
class RepoInfo:
    path: Path
    name: str
    remote_url: Optional[str]
    default_branch: Optional[str]
    github_repo: Optional[str]


def parse_remote(url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(host, owner/name) from a git remote URL, or (None, None)."""
    if not url:
        return None, None
    m = _GITHUB_RE.match(url.strip())
    if not m:
        return None, None
    return m.group(1).lower(), f"{m.group(2)}/{m.group(3)}"


def remote_identity(url: Optional[str]) -> Optional[str]:
    """host/path, lower-case: the same project whichever clone or URL form (https, ssh) it came from."""
    if not url or not url.strip():
        return None
    m = _URL_RE.match(url.strip())
    return f"{m.group(1)}/{m.group(2)}".lower() if m else url.strip().lower()


def is_github_remote(host: Optional[str], github_host: Optional[str]) -> bool:
    """Whether a remote on host belongs to your GitHub (settings.github_host()); never when GitHub is off.

    Only the configured host counts: with GitHub Enterprise Server set, a
    github.com remote is not one of your GitHub repositories. github.com is also
    reached as ssh.github.com (SSH over port 443).
    """
    if not host or not github_host:
        return False
    if github_host == "github.com":
        return host in ("github.com", "www.github.com", "ssh.github.com")
    return host == github_host


def repo_info(path: Path, github_host: Optional[str] = None) -> RepoInfo:
    remotes = run_git(path, "remote", check=False).split()
    remote_name = "origin" if "origin" in remotes else (remotes[0] if remotes else None)
    remote = run_git(path, "config", "--get", f"remote.{remote_name}.url", check=False).strip() or None \
        if remote_name else None
    host, full = parse_remote(remote)
    default = None
    if remote_name:
        head = run_git(path, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote_name}/HEAD",
                       check=False).strip()
        if head:
            default = head.split("/", 1)[1] if "/" in head else head
    if default is None:
        for name in ("main", "master", "develop"):
            if run_git(path, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False).strip():
                default = name
                break
    github = full if is_github_remote(host, github_host) else None
    name = full.split("/", 1)[1] if full else path.name
    return RepoInfo(path, name, remote, default, github)


# --------------------------------------------------------------------------
# Commits
# --------------------------------------------------------------------------

@dataclass
class CommitRec:
    sha: str
    parents: List[str]
    author_name: str
    author_email: str
    authored_at: str
    committer_name: str
    committer_email: str
    committed_at: str
    subject: str
    body: str
    files_changed: int = 0
    additions: int = 0
    deletions: int = 0
    patch_id: Optional[str] = None

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def made_by_github(self) -> bool:
        """GitHub wrote it: a pull request merged on the web, or a file edited there.

        GitHub commits as "GitHub", Enterprise Server as "GitHub Enterprise", from
        whatever no-reply address the server is set to, so the name decides. The
        0.3.0 test (a name starting "github" from a noreply@ address) still
        applies, so nothing skipped before is counted now.
        """
        name = self.committer_name.strip().lower()
        return name in _GITHUB_COMMITTERS or (name.startswith("github")
                                               and self.committer_email.lower().startswith("noreply@"))

    @property
    def is_github_squash(self) -> bool:
        """A pull request squash-merged on GitHub ("Fix retry (#123)"): a copy of commits already made."""
        return self.made_by_github and bool(_PR_SUFFIX_RE.search(self.subject))

    @property
    def squashed(self) -> List[str]:
        """The commits a local `git merge --squash` commit lists in its message."""
        if not self.subject.startswith("Squashed commit of the following"):
            return []
        return _SQUASHED_RE.findall(self.body)


def _author_args(emails: Iterable[str]) -> List[str]:
    emails = sorted(set(emails))
    return (["--regexp-ignore-case", "--fixed-strings"] if emails else []) + [f"--author={e}" for e in emails]


def log_commits(repo: Path, emails: Set[str], since: Optional[str] = None) -> List[CommitRec]:
    """Your commits, newest first, with line counts. emails are lower-case; the author must be one exactly."""
    if not emails:
        return []
    args = ["log", *_revs(repo), "--no-merges", "--date-order", f"--format={_LOG_FORMAT}", "--numstat"]
    args += _author_args(emails) + _since_args(since)
    return [c for c in _parse_log(run_git(repo, *args))
            if c.author_email.lower() in emails and (not since or c.committed_at >= since)]


def coauthors(body: str) -> List[Tuple[str, str]]:
    """(name, email) from each Co-authored-by trailer."""
    return [(m.group(1), m.group(2)) for m in _COAUTHOR_RE.finditer(body or "")]


def log_coauthored(repo: Path, emails: Set[str], since: Optional[str] = None) -> List[CommitRec]:
    """Commits someone else authored that name one of your emails in a Co-authored-by trailer."""
    if not emails:
        return []
    args = ["log", *_revs(repo), "--no-merges", "--date-order", f"--format={_LOG_FORMAT}", "--numstat",
            "--regexp-ignore-case", "--fixed-strings"] + [f"--grep={e}" for e in sorted(emails)] + _since_args(since)
    return [c for c in _parse_log(run_git(repo, *args))
            if c.author_email.lower() not in emails and (not since or c.committed_at >= since)
            and any(e.strip().lower() in emails for _, e in coauthors(c.body))]


def _parse_log(out: str) -> List[CommitRec]:
    commits = []
    for chunk in out.split("\x1e")[1:]:
        head, _, stats = chunk.partition("\x1d")
        parts = head.split("\x1f")
        if len(parts) < 10:
            continue
        sha, parents, an, ae, at, cn, ce, ct, subject, body = parts[:10]
        authored, committed = epoch_ts(at), epoch_ts(ct)
        if authored is None or committed is None:
            continue
        rec = CommitRec(sha, parents.split(), an, ae, authored, cn, ce, committed, subject, body.strip())
        for line in stats.strip().splitlines():
            cols = line.split("\t")
            if len(cols) >= 3:
                rec.files_changed += 1
                rec.additions += int(cols[0]) if cols[0].isdigit() else 0
                rec.deletions += int(cols[1]) if cols[1].isdigit() else 0
        commits.append(rec)
    return commits


def patch_ids(repo: Path, shas: Sequence[str], timeout: int = 600) -> Dict[str, str]:
    """sha -> git patch-id for these commits, so a cherry-picked or rebased change is counted once."""
    if not shas:
        return {}
    exe = git_path()
    if not exe:
        raise GitError("git isn't installed, or isn't on your PATH.")
    pipe = subprocess.PIPE
    log = subprocess.Popen([exe, "-C", str(repo), "-c", "color.ui=never", "log", "--no-walk=unsorted", "--stdin",
                            "-p", "--no-ext-diff", "--format=commit %H"],
                           stdin=pipe, stdout=pipe, stderr=subprocess.DEVNULL, creationflags=_flags())
    pid = subprocess.Popen([exe, "-C", str(repo), "patch-id", "--stable"], stdin=log.stdout, stdout=pipe,
                           stderr=subprocess.DEVNULL, creationflags=_flags())
    if log.stdout:
        log.stdout.close()          # patch-id owns the read end now
    try:
        if log.stdin:
            log.stdin.write(("\n".join(shas) + "\n").encode("ascii"))
            log.stdin.close()
        out, _ = pid.communicate(timeout=timeout)
        log.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        for proc in (log, pid):
            proc.kill()
        raise GitError(f"git patch-id didn't finish in {repo}: {exc}") from exc
    result = {}
    for line in out.decode("utf-8", "replace").splitlines():
        cols = line.split()
        if len(cols) == 2:
            result[cols[1]] = cols[0]
    return result


# --------------------------------------------------------------------------
# Reflog and branches
# --------------------------------------------------------------------------

@dataclass
class ReflogRec:
    at: str
    sha: str
    action: str
    message: str


def head_reflog(worktree: Path) -> List[ReflogRec]:
    """A working folder's HEAD reflog, oldest first. Git keeps 90 days of it by default."""
    out = run_git(worktree, "log", "-g", "--date=unix", "--format=%H%x1f%gd%x1f%gs", "HEAD", check=False)
    entries = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        m = _REFLOG_TIME_RE.search(parts[1])
        at = epoch_ts(m.group(1)) if m else None
        if at is None:
            continue
        message = parts[2]
        entries.append(ReflogRec(at, parts[0], message.split(":", 1)[0].strip(), message))
    entries.reverse()
    return entries


def _is_branch(name: str) -> bool:
    return bool(name) and name != "HEAD" and not re.fullmatch(r"[0-9a-f]{7,64}", name)


def reflog_branches(entries: Sequence[ReflogRec], now_on: Optional[str] = None) -> Dict[str, str]:
    """sha -> the branch you were on when you made that commit, replayed from one HEAD reflog.

    Rebases (plain, interactive or from `pull --rebase`) make new commits on a
    detached HEAD; they belong to the branch the rebase returns to. The replay
    starts on the branch the first checkout moved away from; a reflog with no
    checkout at all (a new worktree, a fresh clone) is on now_on, the branch
    checked out there now.
    """
    first = next((e.message for e in entries if e.message.startswith("checkout: moving from ")), None)
    if first is not None and " to " in first:
        start = first[len("checkout: moving from "):].rsplit(" to ", 1)[0].strip()
        current: Optional[str] = start if _is_branch(start) else None
    else:
        current = now_on
    pending: List[str] = []
    made: Dict[str, str] = {}
    for e in entries:
        msg = e.message
        returned = _RETURN_RE.search(msg)
        renamed = _RENAME_RE.match(msg)
        if msg.startswith("checkout: moving from ") and " to " in msg:
            target = msg.rsplit(" to ", 1)[1].strip()
            current = target if _is_branch(target) else None
        elif returned:
            if returned.group(1) == "finish":
                for sha in pending:
                    made[sha] = returned.group(2)
            pending = []
            current = returned.group(2)
        elif _STEP_RE.search(e.action):
            pending.append(e.sha)
        elif renamed:
            if current == renamed.group(1):
                current = renamed.group(2)
        elif e.action.split(" ")[0] in _COMMIT_ACTIONS and current:
            made.setdefault(e.sha, current)
    return made


def branch_membership(repo: Path, default_branch: Optional[str], mine: Set[str],
                      since: Optional[str] = None) -> Dict[str, List[str]]:
    """sha -> the branch it was made for: of the branches it's on but the default branch isn't
    (`git log main..branch`), the smallest. Stacked branches contain the commits of the branches
    under them, so the smallest is the most specific."""
    if not default_branch or not mine:
        return {}
    base = None
    for candidate in (f"refs/remotes/origin/{default_branch}", f"refs/heads/{default_branch}"):
        if run_git(repo, "rev-parse", "--verify", "--quiet", candidate, check=False).strip():
            base = candidate
            break
    if base is None:
        return {}
    since_epoch = dt.datetime.strptime(since, _TS).replace(tzinfo=dt.timezone.utc).timestamp() if since else None
    # for-each-ref writes a byte as %1f; %x1f is git log's spelling and would come out as text.
    refs = run_git(repo, "for-each-ref", "--format=%(refname)%1f%(committerdate:unix)",
                   "refs/heads", "refs/remotes").splitlines()
    options: Dict[str, List[Tuple[int, str]]] = {}
    for line in refs:
        ref, _, when = line.partition("\x1f")
        short = ref.split("/", 2)[2] if ref.startswith("refs/") and ref.count("/") >= 2 else ref
        if ref.endswith("/HEAD") or short in (default_branch, f"origin/{default_branch}"):
            continue
        if since_epoch is not None and when.strip().isdigit() and int(when) < since_epoch:
            continue
        name = short.split("/", 1)[1] if ref.startswith("refs/remotes/") and "/" in short else short
        shas = run_git(repo, "rev-list", "--no-merges", f"{base}..{ref}", check=False).split()
        for sha in shas:
            if sha in mine:
                options.setdefault(sha, []).append((len(shas), name))
    members: Dict[str, List[str]] = {}
    for sha, found in options.items():
        smallest = min(size for size, _ in found)
        members[sha] = sorted({name for size, name in found if size == smallest})
    return members


def configured_identity() -> Tuple[Optional[str], Optional[str]]:
    """(email, name) from git config, to offer on first run.

    Reads the way git itself does (system, global and any [include] files),
    not --global alone, which skips includes. An email set per folder with
    includeIf only applies inside those repositories; add it with --email.
    """
    email = run_git(None, "config", "--includes", "--get", "user.email", check=False).strip() or None
    name = run_git(None, "config", "--includes", "--get", "user.name", check=False).strip() or None
    return email, name
