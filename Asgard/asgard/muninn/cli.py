"""Looking after Muninn by hand.

    python Asgard.pyw --muninn status            version, size, backups, today's housekeeping
    python Asgard.pyw --muninn check             everything that should never happen (exit 1 if any)
    python Asgard.pyw --muninn repair            rebuild the search index, fix event cursors
    python Asgard.pyw --muninn backup            a copy now, kept apart from the daily ones
    python Asgard.pyw --muninn restore [FILE]    put a backup back (the newest if FILE is left out)
    python Asgard.pyw --muninn maintain          run today's housekeeping again
    python Asgard.pyw --muninn retention on|off  prune old operational rows daily, or don't (default off)

Also `python -m asgard.muninn ...` from the install folder. Use python.exe, not pythonw.exe:
pythonw has no console to print to. Exit codes: 0 fine, 1 the check found errors, 2 couldn't run.
docs/muninn-operations.md explains each one.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional

from . import db, integrity


def _out(text: str = "") -> None:
    print(text, flush=True)


def _open(path: Optional[Path]) -> sqlite3.Connection:
    target = path or db.default_path()
    if not target.exists():
        raise db.NotReady(f"There is no Muninn database at {target} yet. Open Asgard once to create it.")
    return db.connect(target)


def _size(p: Path) -> str:
    try:
        n = p.stat().st_size
    except OSError:
        return "-"
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


def cmd_status(path: Optional[Path], backups: Optional[Path]) -> int:
    target = path or db.default_path()
    con = _open(path)
    try:
        version = db.user_version(con)
        maintained = con.execute("SELECT value FROM meta WHERE key = 'maintained_on'").fetchone()
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        retention = integrity.retention_on(con)
    finally:
        con.close()
    wal = target.with_name(target.name + "-wal")
    _out(f"Muninn     {target}")
    _out(f"Schema     version {version} (this Asgard knows up to {db.latest_version()})")
    _out(f"Size       {_size(target)}, write-ahead log {_size(wal) if wal.exists() else 'none'}, journal {mode}")
    _out(f"SQLite     {sqlite3.sqlite_version}; Python {sys.version.split()[0]}")
    _out(f"Housekept  {maintained[0] if maintained else 'never'}; retention {'on' if retention else 'off'}")
    found = db.list_backups(backups)
    _out(f"Backups    {len(found)} in {backups or db.backup_dir()}")
    for b in found[:10]:
        _out(f"           {b.name}  {_size(b)}")
    return 0


def cmd_check(path: Optional[Path]) -> int:
    con = _open(path)
    try:
        report = integrity.check(con)
    finally:
        con.close()
    if not report.findings:
        _out("Muninn is healthy: nothing to report.")
        return 0
    for f in report.findings:
        _out(str(f))
    errors = sum(f.level == "error" for f in report.findings)
    _out(f"\n{errors} errors, {len(report.findings) - errors} other findings.")
    return 0 if report.ok else 1


def cmd_repair(path: Optional[Path]) -> int:
    con = _open(path)
    try:
        for line in integrity.repair(con):
            _out(line)
    finally:
        con.close()
    return 0


def cmd_backup(path: Optional[Path], backups: Optional[Path]) -> int:
    con = _open(path)
    try:
        target = db.backup(con, backups, label="manual", keep=5, replace=True)
    finally:
        con.close()
    _out(f"Backed up to {target} ({_size(target)}).")
    return 0


def cmd_restore(path: Optional[Path], backups: Optional[Path], file: Optional[str], yes: bool) -> int:
    src = Path(file) if file else db.newest_backup(backups)
    if src is None:
        raise db.MuninnError(f"There is no backup in {backups or db.backup_dir()} to restore from.")
    if not src.is_file() and not src.is_absolute():
        src = (backups or db.backup_dir()) / src          # a bare name from the backups folder
    target = path or db.default_path()
    _out(f"This puts {src} back as {target}.")
    _out("Anything saved since that backup is lost. The current file is kept beside it, renamed "
         f"{target.stem}.before-restore-<time>{target.suffix}.")
    if not yes:
        if not sys.stdin or not sys.stdin.isatty():
            raise db.MuninnError("Add --yes to restore without being asked.")
        if input("Close every Asgard app first. Type yes to go on: ").strip().lower() != "yes":
            _out("Nothing was changed.")
            return 2
    restored = db.restore(src, path=path, backups=backups)
    _out(f"Restored. Open Asgard; it brings {restored.name} up to date if the backup is older.")
    return 0


def cmd_maintain(path: Optional[Path]) -> int:
    con = _open(path)
    try:
        r = integrity.maintain(con, force=True)
    finally:
        con.close()
    pruned = ", ".join(f"{n} {t}" for t, n in r.pruned.items()) or "nothing"
    _out(f"Housekeeping done in {r.seconds:.1f} s. Pruned: {pruned}"
         + (" (more tomorrow)" if r.more_to_prune else "")
         + ("" if r.retention else " (retention is off)") + ".")
    return 0


def cmd_retention(path: Optional[Path], value: str) -> int:
    con = _open(path)
    try:
        if value != "show":
            integrity.set_retention(con, value == "on")
        on = integrity.retention_on(con)
    finally:
        con.close()
    days = ", ".join(f"{t} {d} days" for t, d in integrity.RETENTION_DAYS.items())
    _out(f"Retention is {'on' if on else 'off'}. When on, housekeeping deletes {days}; "
         "facts, decisions, worklogs and events are kept for good.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="Asgard.pyw --muninn", description="Look after Muninn, Asgard's database.")
    p.add_argument("--db", type=Path, help=argparse.SUPPRESS)          # tests point these at a temp folder
    p.add_argument("--backups", type=Path, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="version, size, backups, housekeeping")
    sub.add_parser("check", help="look for damage and for anything that should never happen")
    sub.add_parser("repair", help="rebuild the search index and fix event cursors")
    sub.add_parser("backup", help="make a copy now")
    r = sub.add_parser("restore", help="put a backup back; close every Asgard app first")
    r.add_argument("file", nargs="?", help="the backup to restore (default: the newest)")
    r.add_argument("--yes", action="store_true", help="don't ask first")
    sub.add_parser("maintain", help="run housekeeping now")
    t = sub.add_parser("retention", help="prune old operational rows daily (default off)")
    t.add_argument("value", choices=("on", "off", "show"))
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            return cmd_status(args.db, args.backups)
        if args.command == "check":
            return cmd_check(args.db)
        if args.command == "repair":
            return cmd_repair(args.db)
        if args.command == "backup":
            return cmd_backup(args.db, args.backups)
        if args.command == "restore":
            return cmd_restore(args.db, args.backups, args.file, args.yes)
        if args.command == "maintain":
            return cmd_maintain(args.db)
        return cmd_retention(args.db, args.value)
    except db.MuninnError as exc:
        print(f"Muninn: {exc}", file=sys.stderr)
        return 2
    except sqlite3.DatabaseError as exc:
        print(f"Muninn: {exc}. If the file is damaged, run: Asgard.pyw --muninn restore", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
