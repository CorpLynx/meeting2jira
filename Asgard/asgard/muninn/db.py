"""Opening Muninn: connection settings, versions, migrations and backups.

Only Asgard migrates (muninn.prepare() at launcher start). Other apps open
Muninn with muninn.open_app(), which checks the schema version and never
changes it.
"""
from __future__ import annotations

import datetime as dt
import os
import random
import re
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional, Tuple, Union

from .. import paths
from . import guard

PathLike = Union[str, Path]

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
DB_NAME = "muninn.db"
MIN_SQLITE = (3, 37, 0)          # STRICT tables
DAILY_BACKUPS_KEPT = 7
MIGRATION_BACKUPS_KEPT = 3
FK_OFF_MARKER = "-- muninn: foreign_keys=off"
WAL_LIMIT_BYTES = 64 * 1024 * 1024    # the -wal file shrinks back to this after a checkpoint
BUSY_WAIT_SECONDS = 30.0              # how long a write keeps trying while another app holds the lock
STALE_TMP_SECONDS = 3600              # a half-written backup older than this is left over from a crash

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


class CorruptError(MuninnError):
    """The database file is damaged. The message names the newest backup and how to restore it."""


class BusyError(MuninnError):
    """Another Asgard app held Muninn's write lock for longer than this one would wait."""


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(_TS)


def to_ts(value: dt.datetime) -> str:
    """A datetime as Muninn's UTC text. Naive datetimes are taken as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    try:
        return value.astimezone(dt.timezone.utc).strftime(_TS)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"{value!r} is outside the dates Muninn stores (years 1000 to 9998 in UTC)") from exc


# --------------------------------------------------------------------------
# Waiting for the write lock
# --------------------------------------------------------------------------

def is_busy(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text)


def retry_busy(step: Callable[[], Any], *, total: Optional[float] = None) -> Any:
    """Run step(), trying again with a short random pause while another app holds the write lock.

    SQLite already waits busy_timeout (5 s) inside each attempt. With several apps writing in
    bursts, one can still lose the race that long, so this keeps trying for BUSY_WAIT_SECONDS
    and then says plainly what happened instead of raising "database is locked".
    """
    deadline = time.monotonic() + (BUSY_WAIT_SECONDS if total is None else total)
    pause = 0.02
    while True:
        try:
            return step()
        except sqlite3.OperationalError as exc:
            if not is_busy(exc):
                raise
            if time.monotonic() >= deadline:
                raise BusyError("Muninn is busy in another Asgard app, so this couldn't be saved. "
                                "Nothing was lost; wait a moment and try again.") from exc
            time.sleep(pause + random.random() * pause)
            pause = min(pause * 1.6, 0.25)


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
    # With this on, a row removed by a REPLACE conflict fires its delete triggers, so its search
    # entry goes with it instead of being left behind. No trigger in the schema recurses.
    con.execute("PRAGMA recursive_triggers = ON")
    if not readonly:
        con.execute(f"PRAGMA journal_size_limit = {WAL_LIMIT_BYTES}")
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
    retry_busy(lambda: con.execute("BEGIN IMMEDIATE"))
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


def open_app(app: str, *, supported: Tuple[int, int], path: Optional[PathLike] = None,
             readonly: bool = False) -> sqlite3.Connection:
    """Open Muninn for an app that doesn't migrate (every app but Asgard).

    supported is the range of schema versions the app was written for; it has no default, because
    a default would go stale the first time the schema moves. The connection refuses writes to
    tables the app doesn't own, schema changes, and PRAGMAs that would switch protections off
    (see guard.py).
    """
    if app not in guard.known_apps():
        raise ValueError(f"unknown app {app!r}")
    db = Path(path) if path else default_path()
    if not db.exists():
        raise NotReady(f"Muninn isn't set up yet. Open Asgard once, then start {app} again.")
    problems = sqlite_problems()
    if problems:
        raise MuninnError(f"{app} can't use Muninn on this Python: " + "; ".join(problems) +
                          ". Run it with the same Python as Asgard, or Python 3.11 or newer.")
    try:
        con = connect(db, readonly=readonly)
    except sqlite3.DatabaseError as exc:
        raise _damaged(db, None, exc) from exc
    try:
        try:
            version = user_version(con)
        except sqlite3.DatabaseError as exc:
            if is_busy(exc):
                raise BusyError("Muninn is busy in another Asgard app. Wait a moment and try again.") from exc
            raise _damaged(db, None, exc) from exc
        if version == 0:
            raise NotReady(f"Muninn isn't set up yet. Open Asgard once, then start {app} again.")
        low, high = supported
        if version < low:
            raise VersionError(f"Muninn is at version {version}, but {app} needs version {low} or newer. "
                               "Open Asgard to upgrade it.")
        if version > high:
            raise VersionError(f"Muninn is at version {version}, newer than {app} understands "
                               f"(up to {high}). Update {app}.")
        guard.install(con, app)
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
        folder_shown = backups or backup_dir()
        try:
            report.backup = backup(con, backups, label=f"before-v{current + 1}", keep=MIGRATION_BACKUPS_KEPT)
        except (OSError, sqlite3.Error) as exc:
            raise MuninnError(f"Muninn needs to update its database, and couldn't first make a safety copy of it "
                              f"in {folder_shown} ({exc}). Free some disk space or check that folder can be written "
                              "to, then open Asgard again. Nothing was changed.") from exc
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
    _sweep_stale_temp(folder)
    tmp = folder / f".{name}.{os.getpid()}-{secrets.token_hex(3)}.tmp"
    try:
        con.execute("VACUUM INTO ?", (str(tmp),))
        for attempt in range(40):
            if target.exists() and not replace:   # another process finished first
                tmp.unlink()
                return target
            try:
                os.replace(tmp, target)
                break
            except PermissionError:
                # Windows refuses to replace a file another process has open or has only just
                # written (an antivirus scan of a new file does this too). Found on the Windows lab
                # VM with four backups at once; wait briefly, and a copy that landed meanwhile counts.
                if attempt == 39:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _prune(folder, label, keep)
    return target


def _sweep_stale_temp(folder: Path) -> int:
    """Delete half-written backup copies a crash left behind (each is a whole database). Returns how many."""
    removed = 0
    cutoff = time.time() - STALE_TMP_SECONDS
    try:
        for p in folder.glob(".muninn-*.tmp"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


_BACKUP_RE = re.compile(r"^muninn-\d{8}(?:-[a-z0-9\-]+)?\.db$")


def list_backups(folder: Optional[Path] = None) -> List[Path]:
    """Backup files, newest first."""
    folder = folder or backup_dir()
    if not folder.is_dir():
        return []
    found = [p for p in folder.iterdir() if _BACKUP_RE.match(p.name)]
    return sorted(found, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


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
    warnings: List[str] = field(default_factory=list)   # things that didn't stop startup but you should know
    maintained: Any = None                              # integrity.MaintainReport from today's housekeeping


def console_python() -> str:
    """This Python, as the console python.exe when running under pythonw.exe (which prints nowhere)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        exe = exe.with_name("python.exe")
    return str(exe)


def _restore_command() -> str:
    entry = paths.app_dir() / "Asgard.pyw"
    entry = entry if entry.exists() else paths.CODE_ROOT / "Asgard.pyw"
    return f'"{console_python()}" "{entry}" --muninn restore'


def _damaged(db: Path, backups: Optional[Path], why: Any) -> CorruptError:
    """The error for a database file that won't open: what we saw, the newest backup, how to restore."""
    newest = list_backups(backups)
    reason = str(why).strip().splitlines()[0][:160] if str(why).strip() else "it failed its integrity check"
    if newest:
        when = dt.datetime.fromtimestamp(newest[0].stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        hint = (f"The newest backup is {newest[0].name} from {when}. To put it back, close Asgard and every app "
                f"that uses Muninn, then run: {_restore_command()}\nThe damaged file is kept beside it, renamed.")
    else:
        hint = (f"There is no backup in {backups or backup_dir()} to restore from. Keep the damaged file "
                f"({db}) and ask for help; most of it can usually be read.")
    return CorruptError(f"Muninn's database file is damaged ({reason}). {hint}")


def newest_backup(folder: Optional[Path] = None) -> Optional[Path]:
    found = list_backups(folder)
    return found[0] if found else None


def _verify_copy(path: Path, what: str) -> int:
    """Open a would-be database read-only and check it all; returns its schema version."""
    try:
        probe = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            verdict = [str(r[0]) for r in probe.execute("PRAGMA integrity_check(5)")]
            version = int(probe.execute("PRAGMA user_version").fetchone()[0])
        finally:
            probe.close()
    except sqlite3.DatabaseError as exc:
        raise MuninnError(f"{what} can't be used as a backup ({exc}). Try an older one.") from exc
    if verdict != ["ok"] or not 1 <= version <= latest_version():
        raise MuninnError(f"{what} isn't a usable backup (check: {'; '.join(verdict)[:200]}, schema version "
                          f"{version}). Try an older one.")
    return version


def _in_use(db: Path) -> Optional[str]:
    """Why the database can't be replaced now, or None. Leaves it in rollback-journal mode when free.

    In WAL mode another connection, even an idle one, keeps SQLite from leaving WAL; a connection
    that is only open (no transaction) is invisible to BEGIN EXCLUSIVE, so this asks for the mode
    change instead. The -wal is folded in first, so nothing written so far is lost.
    """
    try:
        probe = sqlite3.connect(str(db), timeout=1.0, isolation_level=None)
    except sqlite3.DatabaseError:
        return None                               # a damaged file can't be probed; that's why we're here
    try:
        mode = probe.execute("PRAGMA journal_mode = DELETE").fetchone()[0].lower()
        if mode != "delete":
            return "another program has it open"
        return None
    except sqlite3.OperationalError as exc:
        return str(exc) if is_busy(exc) else None
    except sqlite3.DatabaseError:
        return None
    finally:
        probe.close()


def restore(backup_file: Optional[PathLike] = None, *, path: Optional[PathLike] = None,
            backups: Optional[Path] = None) -> Path:
    """Put a backup back as Muninn's database. Close every Asgard app first.

    The backup is checked in full, then copied next to the database and checked again, all before
    the current file is touched. Only then is the current file (and its -wal and -shm) renamed to
    muninn.before-restore-<time>.db, never deleted, and the copy moved into its place. If anything
    fails after the rename, the original goes back. Returns the path of the restored database.
    """
    db = Path(path) if path else default_path()
    src = Path(backup_file) if backup_file else newest_backup(backups)
    if src is None or not src.is_file():
        raise MuninnError(f"There is no backup to restore from in {backups or backup_dir()}.")
    live = {db.with_name(db.name + s).resolve() for s in ("", "-wal", "-shm")}
    if src.resolve() in live:
        raise MuninnError(f"{src.name} is the database itself, not a backup. Choose a file from "
                          f"{backups or backup_dir()}.")
    _verify_copy(src, src.name)
    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = db.with_name(f".{db.name}.restore-{os.getpid()}-{secrets.token_hex(3)}.tmp")
    try:
        source = sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(str(tmp))
        try:
            source.backup(target)                 # a consistent copy, whatever state the file was in
            target.execute("PRAGMA journal_mode = DELETE")
        finally:
            target.close()
            source.close()
        _verify_copy(tmp, "The copy of " + src.name)
        moved: List[Tuple[Path, Path]] = []
        if db.exists():
            why = _in_use(db)
            if why:
                raise MuninnError("Close Asgard and every app that uses Muninn, then run the restore again "
                                  f"(the database is in use: {why}).")
            stamp = time.strftime("%Y%m%d-%H%M%S")
            try:
                for suffix in ("", "-wal", "-shm"):
                    part = db.with_name(db.name + suffix)
                    if part.exists():
                        aside = db.with_name(f"{db.stem}.before-restore-{stamp}{db.suffix}{suffix}")
                        os.replace(part, aside)
                        moved.append((aside, part))
                os.replace(tmp, db)
            except OSError as exc:
                for aside, part in reversed(moved):   # put the original back as it was
                    try:
                        os.replace(aside, part)
                    except OSError:
                        pass
                raise MuninnError("Close Asgard and every app that uses Muninn, then run the restore again "
                                  f"(a file is in use: {exc}). Your database is unchanged.") from exc
        else:
            os.replace(tmp, db)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    con = connect(db)
    try:
        ensure_wal(con)
    finally:
        con.close()
    return db


def _restore_copies(db: Path) -> List[Path]:
    return sorted(db.parent.glob(f"{db.stem}.before-restore-*{db.suffix}")) if db.parent.is_dir() else []


def prepare(path: Optional[PathLike] = None, *, backups: Optional[Path] = None,
            folder: Optional[Path] = None) -> Status:
    """Create or upgrade Muninn and take the day's backup. Asgard calls this at start.

    A damaged file raises CorruptError naming the newest backup and the restore command; nothing is
    moved until you ask for the restore. A damaged search index is rebuilt (it holds no data of its
    own), and protections someone removed (a dropped trigger) are put back. Problems that don't stop
    Muninn (a backup that couldn't be written, rows that point at rows that are gone) come back in
    Status.warnings.
    """
    problems = sqlite_problems()
    if problems:
        raise MuninnError("Muninn can't run on this Python: " + "; ".join(problems) +
                          ". Ask IT for Python 3.11 or newer.")
    db = Path(path) if path else default_path()
    existed = db.exists() and db.stat().st_size > 0
    try:
        con = connect(db)
    except sqlite3.DatabaseError as exc:
        raise _damaged(db, backups, exc) from exc
    from . import integrity                      # imported here: integrity needs this module
    try:
        try:
            status = Status(db, user_version(con), created=not existed)
            if existed:
                lines = integrity.file_check(con)
                if lines != ["ok"] and integrity.search_only(lines):
                    integrity.repair_search(con)
                    status.warnings.append("Muninn's search index was damaged and has been rebuilt. "
                                           "Your data was not affected.")
                    lines = integrity.file_check(con)
                if lines != ["ok"]:
                    raise _damaged(db, backups, "; ".join(lines[:2]))
        except sqlite3.DatabaseError as exc:
            if is_busy(exc):
                raise BusyError("Muninn is busy in another Asgard app. Wait a moment and try again.") from exc
            raise _damaged(db, backups, exc) from exc
        if not existed:
            earlier = list_backups(backups) + _restore_copies(db)
            if earlier:
                status.warnings.append(f"Muninn started a new, empty database at {db}, but earlier copies exist "
                                       f"(such as {earlier[0].name}). If your data should be here, close Asgard "
                                       f"and run: {_restore_command()}")
        if existed and status.version > 0:
            ensure_wal(con)
            try:
                status.backup = daily_backup(con, backups)
            except (OSError, sqlite3.Error, MuninnError) as exc:
                status.warnings.append(f"Today's backup wasn't made ({exc}). Your data is unchanged.")
        report = migrate(con, folder=folder, backups=backups)
        status.migrated = report.applied
        status.backup = report.backup or status.backup
        status.version = user_version(con)
        if folder is None:                       # the shipped migrations: their schema is known
            done, failed = integrity.repair_schema(con)
            if done:
                status.warnings.append(f"Muninn put back {len(done)} of its protections that were missing or "
                                       f"altered ({'; '.join(done[:3])}{'; ...' if len(done) > 3 else ''}).")
            for f in failed:
                status.warnings.append(f"Muninn's schema needs a restore: {f}. Run: Asgard.pyw --muninn check")
        broken = con.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            tables = sorted({r[0] for r in broken})
            status.warnings.append(f"{len(broken)} rows in {', '.join(tables)} point at rows that are gone. "
                                   "Run: Asgard.pyw --muninn check")
        # A run still 'running' after six hours belongs to an app that stopped mid-run.
        try:
            retry_busy(lambda: con.execute(
                "UPDATE sync_runs SET status = 'failed', finished_at = ?, "
                "error = coalesce(error, 'Abandoned: the app stopped before the run finished') "
                "WHERE status = 'running' AND started_at < ?", (utcnow(), ago(6 * 3600))), total=10)
        except BusyError:
            status.warnings.append("Abandoned sync runs weren't closed this time (another app was writing).")
        try:
            status.maintained = integrity.maintain(con)
        except (sqlite3.Error, MuninnError) as exc:
            status.warnings.append(f"Housekeeping was skipped ({exc}).")
        return status
    finally:
        con.close()
