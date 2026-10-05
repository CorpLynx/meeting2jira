"""Opening Muninn: connection settings, versions, migrations and backups.

Only Asgard migrates (muninn.prepare() at launcher start). Other apps open
Muninn with muninn.open_app(), which checks the schema version and never
changes it.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, Union

from .. import paths

PathLike = Union[str, Path]

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
DB_NAME = "muninn.db"
MIN_SQLITE = (3, 37, 0)          # STRICT tables
DAILY_BACKUPS_KEPT = 7
MIGRATION_BACKUPS_KEPT = 3
FK_OFF_MARKER = "-- muninn: foreign_keys=off"

_TS = "%Y-%m-%dT%H:%M:%SZ"
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_TXN_RE = re.compile(r"^(BEGIN|COMMIT|END|ROLLBACK)\b", re.IGNORECASE)
_USER_VERSION_RE = re.compile(r"^PRAGMA\s+user_version\s*=\s*(\d+)$", re.IGNORECASE)
_LEADING_COMMENTS_RE = re.compile(r"^(\s*(--[^\n]*(?:\n|$)|/\*.*?\*/))*\s*", re.DOTALL)


class MuninnError(Exception):
    """Muninn can't be used as asked. The message is written for the person using the app."""


class NotReady(MuninnError):
    """Muninn hasn't been created yet; opening Asgard once creates it."""


class VersionError(MuninnError):
    """The database's schema version is outside the range the code understands."""


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(_TS)


def to_ts(value: dt.datetime) -> str:
    """A datetime as Muninn's UTC text. Naive datetimes are taken as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).strftime(_TS)


def from_ts(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, _TS).replace(tzinfo=dt.timezone.utc)


def ago(seconds: float) -> str:
    return to_ts(dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds))


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------

def default_path() -> Path:
    return paths.data_dir() / DB_NAME


def backup_dir() -> Path:
    return paths.data_dir() / "backups"


def sqlite_problems() -> List[str]:
    """What this Python's SQLite lacks for Muninn; empty when it has everything."""
    problems = []
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        problems.append(f"SQLite {sqlite3.sqlite_version} is older than 3.37, which STRICT tables need")
    con = sqlite3.connect(":memory:")
    try:
        for name, sql in (("FTS5", "CREATE VIRTUAL TABLE t USING fts5(x)"),
                          ("JSON functions", "SELECT json_valid('{}')")):
            try:
                con.execute(sql)
            except sqlite3.DatabaseError:
                problems.append(f"SQLite {sqlite3.sqlite_version} has no {name}")
    finally:
        con.close()
    return problems


def connect(path: Optional[PathLike] = None, *, readonly: bool = False,
            timeout: float = 5.0) -> sqlite3.Connection:
    """A connection with Muninn's settings. Transactions are explicit (see transaction())."""
    db = Path(path) if path else default_path()
    if not readonly:
        db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db), timeout=timeout, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    con.execute("PRAGMA synchronous = NORMAL")
    if readonly:
        con.execute("PRAGMA query_only = ON")
    return con


def user_version(con: sqlite3.Connection) -> int:
    return int(con.execute("PRAGMA user_version").fetchone()[0])


def ensure_wal(con: sqlite3.Connection, wait: float = 5.0) -> bool:
    """Switch the file to WAL (it persists). SQLite doesn't wait on busy for this, so retry."""
    deadline = time.monotonic() + wait
    while True:
        try:
            mode = con.execute("PRAGMA journal_mode").fetchone()[0].lower()
            if mode in ("wal", "memory"):
                return mode == "wal"
            return con.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() == "wal"
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if time.monotonic() > deadline:
                raise MuninnError("Muninn is busy in another app; close it and try again.") from exc
            time.sleep(0.05)


@contextmanager
def transaction(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK if the block raises.

    IMMEDIATE takes the write lock up front, so a busy database makes the
    write wait (up to busy_timeout) at the start instead of failing halfway.
    It refuses to nest: work that must be saved before a network call (such
    as a worklog's 'sending' row) would otherwise wait on the outer commit.
    """
    if con.in_transaction:
        raise MuninnError("This write must run on its own, outside any open transaction.")
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    if not con.in_transaction:
        raise MuninnError("SQLite rolled this change back on its own (disk full or I/O error); nothing was saved.")
    try:
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise


def open_app(app: str, *, supported: Tuple[int, int] = (1, 1),
             path: Optional[PathLike] = None, readonly: bool = False) -> sqlite3.Connection:
    """Open Muninn for an app that doesn't migrate (every app but Asgard).

    supported is the range of schema versions the app was written for.
    """
    db = Path(path) if path else default_path()
    if not db.exists():
        raise NotReady(f"Muninn isn't set up yet. Open Asgard once, then start {app} again.")
    problems = sqlite_problems()
    if problems:
        raise MuninnError(f"{app} can't use Muninn on this Python: " + "; ".join(problems) +
                          ". Run it with the same Python as Asgard, or Python 3.11 or newer.")
    con = connect(db, readonly=readonly)
    try:
        version = user_version(con)
        if version == 0:
            raise NotReady(f"Muninn isn't set up yet. Open Asgard once, then start {app} again.")
        low, high = supported
        if version < low:
            raise VersionError(f"Muninn is at version {version}, but {app} needs version {low} or newer. "
                               "Open Asgard to upgrade it.")
        if version > high:
            raise VersionError(f"Muninn is at version {version}, newer than {app} understands "
                               f"(up to {high}). Update {app}.")
    except BaseException:
        con.close()
        raise
    return con


# --------------------------------------------------------------------------
# Migrations
# --------------------------------------------------------------------------

@dataclass
class Migration:
    number: int
    path: Path

    @property
    def name(self) -> str:
        return self.path.name


def available_migrations(folder: Optional[Path] = None) -> List[Migration]:
    folder = folder or MIGRATIONS_DIR
    found = []
    for p in sorted(folder.glob("*.sql")):
        m = _MIGRATION_RE.match(p.name)
        if m:
            found.append(Migration(int(m.group(1)), p))
    numbers = [m.number for m in found]
    if numbers != list(range(1, len(numbers) + 1)):
        raise MuninnError(f"Muninn's migration files are out of sequence: {numbers}. Reinstall Asgard.")
    return found


def latest_version(folder: Optional[Path] = None) -> int:
    migrations = available_migrations(folder)
    return migrations[-1].number if migrations else 0


def split_statements(sql: str) -> List[str]:
    """Split a script into statements, keeping trigger bodies whole."""
    statements: List[str] = []
    start = 0
    for i, ch in enumerate(sql):
        if ch == ";" and sqlite3.complete_statement(sql[start:i + 1]):
            text = sql[start:i + 1].strip()
            if _LEADING_COMMENTS_RE.sub("", text).strip(" ;\n"):
                statements.append(text)
            start = i + 1
    rest = _LEADING_COMMENTS_RE.sub("", sql[start:]).strip()
    if rest:
        raise MuninnError(f"Unfinished SQL statement at the end of a migration: {rest[:60]!r}")
    return statements


def _migration_statements(m: Migration) -> List[str]:
    """The statements to run; the runner supplies BEGIN, COMMIT and user_version itself."""
    out = []
    for stmt in split_statements(m.path.read_text(encoding="utf-8-sig")):
        body = _LEADING_COMMENTS_RE.sub("", stmt).rstrip(" ;\n")
        if _TXN_RE.match(body):
            continue
        v = _USER_VERSION_RE.match(body)
        if v:
            if int(v.group(1)) != m.number:
                raise MuninnError(f"{m.name} sets user_version {v.group(1)}, not {m.number}.")
            continue
        out.append(stmt)
    return out


@dataclass
class MigrationReport:
    path: Path
    from_version: int
    to_version: int
    applied: List[str] = field(default_factory=list)
    backup: Optional[Path] = None


def migrate(con: sqlite3.Connection, *, folder: Optional[Path] = None,
            backups: Optional[Path] = None) -> MigrationReport:
    """Bring the schema up to the latest migration. Safe if two processes race."""
    db_path = Path(con.execute("PRAGMA database_list").fetchone()["file"] or ":memory:")
    migrations = available_migrations(folder)
    latest = migrations[-1].number if migrations else 0
    current = user_version(con)
    report = MigrationReport(db_path, current, current)
    if current > latest:
        raise VersionError(f"Muninn is at version {current}, newer than this Asgard understands ({latest}). "
                           "Update Asgard.")
    if current == latest:
        return report
    if not ensure_wal(con) and str(db_path) != ":memory:":
        raise MuninnError("Couldn't switch Muninn to WAL mode; is the folder on a network share?")
    if current > 0:
        report.backup = backup(con, backups, label=f"before-v{current + 1}", keep=MIGRATION_BACKUPS_KEPT)
    for m in migrations:
        if m.number <= current:
            continue
        statements = _migration_statements(m)
        fk_off = FK_OFF_MARKER in m.path.read_text(encoding="utf-8-sig")
        if fk_off:
            con.execute("PRAGMA foreign_keys = OFF")
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                now = user_version(con)
                if now >= m.number:          # another process got here first
                    con.execute("ROLLBACK")
                    continue
                if now != m.number - 1:
                    raise MuninnError(f"Muninn is at version {now}; can't apply {m.name}.")
                for stmt in statements:
                    con.execute(stmt)
                if fk_off:
                    broken = con.execute("PRAGMA foreign_key_check").fetchall()
                    if broken:
                        raise MuninnError(f"{m.name} left {len(broken)} broken foreign keys.")
                con.execute(f"PRAGMA user_version = {m.number}")
                con.execute("COMMIT")
                report.applied.append(m.name)
            except BaseException as exc:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                if isinstance(exc, MuninnError):
                    raise
                if isinstance(exc, sqlite3.DatabaseError):
                    raise MuninnError(f"Muninn's update {m.name} failed and was undone: {exc}") from exc
                raise
        finally:
            if fk_off:
                con.execute("PRAGMA foreign_keys = ON")
    report.to_version = user_version(con)
    return report


# --------------------------------------------------------------------------
# Backups
# --------------------------------------------------------------------------

def backup(con: sqlite3.Connection, folder: Optional[Path] = None, *, label: str = "",
           keep: int = DAILY_BACKUPS_KEPT, today: Optional[dt.date] = None, replace: bool = False) -> Path:
    """A consistent, compacted copy via VACUUM INTO, taken while readers keep working.

    The copy is written under a private name and moved into place, so two
    processes backing up at once never touch each other's file. An existing
    copy with the same name counts as done unless replace=True.
    """
    folder = folder or backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (today or dt.date.today()).strftime("%Y%m%d")
    name = f"muninn-{stamp}-{label}.db" if label else f"muninn-{stamp}.db"
    target = folder / name
    if target.exists() and not replace:
        return target
    tmp = folder / f".{name}.{os.getpid()}-{secrets.token_hex(3)}.tmp"
    try:
        con.execute("VACUUM INTO ?", (str(tmp),))
        if target.exists() and not replace:   # another process finished first
            tmp.unlink()
            return target
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _prune(folder, label, keep)
    return target


def daily_backup(con: sqlite3.Connection, folder: Optional[Path] = None,
                 today: Optional[dt.date] = None) -> Optional[Path]:
    """Back up once a day; returns the new file, or None if today's copy exists."""
    folder = folder or backup_dir()
    stamp = (today or dt.date.today()).strftime("%Y%m%d")
    if (folder / f"muninn-{stamp}.db").exists():
        return None
    return backup(con, folder, today=today)


def _prune(folder: Path, label: str, keep: int) -> None:
    """Keep the newest `keep` copies of one kind: daily, 'manual', or 'before-v<N>'."""
    if label:
        family = re.escape(re.sub(r"\d+$", "", label))
        pattern = re.compile(r"^muninn-\d{8}-" + family + r"\d*\.db$")
    else:
        pattern = re.compile(r"^muninn-\d{8}\.db$")
    copies = sorted((p for p in folder.iterdir() if pattern.match(p.name)),
                    key=lambda p: (p.stat().st_mtime, p.name))
    for old in copies[:-keep] if keep > 0 else copies:
        try:
            old.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# The launcher's entry point
# --------------------------------------------------------------------------

@dataclass
class Status:
    path: Path
    version: int
    created: bool = False
    migrated: List[str] = field(default_factory=list)
    backup: Optional[Path] = None


def prepare(path: Optional[PathLike] = None, *, backups: Optional[Path] = None,
            folder: Optional[Path] = None) -> Status:
    """Create or upgrade Muninn and take the day's backup. Asgard calls this at start."""
    problems = sqlite_problems()
    if problems:
        raise MuninnError("Muninn can't run on this Python: " + "; ".join(problems) +
                          ". Ask IT for Python 3.11 or newer.")
    db = Path(path) if path else default_path()
    existed = db.exists() and db.stat().st_size > 0
    con = connect(db)
    try:
        status = Status(db, user_version(con), created=not existed)
        if existed and status.version > 0:
            ensure_wal(con)
            status.backup = daily_backup(con, backups)
        report = migrate(con, folder=folder, backups=backups)
        status.migrated = report.applied
        status.backup = report.backup or status.backup
        status.version = user_version(con)
        # A run still 'running' after six hours belongs to an app that stopped mid-run.
        con.execute("UPDATE sync_runs SET status = 'failed', finished_at = ?, "
                    "error = coalesce(error, 'Abandoned: the app stopped before the run finished') "
                    "WHERE status = 'running' AND started_at < ?", (utcnow(), ago(6 * 3600)))
        return status
    finally:
        con.close()
