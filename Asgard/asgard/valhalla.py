"""Valhalla: uninstall Asgard by undoing what setup recorded, newest first.

Whatever the install record says, Valhalla only deletes folders inside
Asgard's own data folder, Asgard's .lnk shortcuts, and its Settings > Apps
entry. Your tile settings, logs and Muninn database stay unless you ask.

The packaged build runs its programs from app\\, and Windows won't delete a program while it
runs, so when Valhalla runs from there it removes app\\ just after Asgard closes: a hidden
PowerShell (cmdlets only, as Constrained Language Mode allows) waits for it, then deletes it.

    pythonw Asgard.pyw --uninstall              (asks first)
    python  Asgard.pyw --uninstall --yes         (no window; add --purge to delete your data)
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import paths, winutil

DETACHED = 0x00000008 | 0x00000200 | 0x08000000      # detached, own group, no window

USER_DATA = ("apps.local.json", "settings.json", "settings", "logs", "backups",
             "muninn.db", "muninn.db-wal", "muninn.db-shm")      # settings\: each app's own settings files
# Copies `--muninn restore` set aside (muninn.before-restore-<time>.db and its -wal/-shm): whole databases.
USER_DATA_GLOBS = ("muninn.before-restore-*.db*",)


def _user_data(data: Path) -> List[Path]:
    found = [data / n for n in USER_DATA]
    for pattern in USER_DATA_GLOBS:
        found.extend(sorted(data.glob(pattern)) if data.is_dir() else [])
    return found


@dataclass
class Result:
    removed: List[str] = field(default_factory=list)
    kept: List[str] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    after_exit: Optional[str] = None    # app\ of the packaged build running now: removed once it closes

    @property
    def ok(self) -> bool:
        return not self.skipped


def load_ledger() -> Optional[Dict[str, Any]]:
    try:
        with open(paths.ledger_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def is_installed() -> bool:
    return paths.ledger_path().exists() or paths.app_dir().exists()


def _resolve(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p.absolute()


def _within(path: Path, root: Path) -> bool:
    try:
        _resolve(path).relative_to(_resolve(root))
        return True
    except ValueError:
        return False


def _allowed_shortcut(path: Path) -> bool:
    if path.suffix.lower() != ".lnk":
        return False
    roots = [r for r in (paths.start_menu_dir(), winutil.desktop_dir(), paths.data_dir()) if r]
    return any(_within(path, r) for r in roots)


def default_items() -> List[Dict[str, str]]:
    """What setup normally creates, for when the install record is missing."""
    items = [{"kind": "dir", "path": str(paths.app_dir())}]
    for folder in (paths.start_menu_dir(), winutil.desktop_dir()):
        if folder:
            items.append({"kind": "file", "path": str(folder / paths.SHORTCUT_NAME)})
    items.append({"kind": "regkey", "path": "HKCU\\" + paths.UNINSTALL_SUBKEY})
    return items


def _rmtree(path: Path) -> None:
    def retry_writable(func: Any, target: str, *_: Any) -> None:
        os.chmod(target, stat.S_IWRITE)  # read-only files block deletion on Windows
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry_writable)  # novermin (guarded by the version check)
    else:
        shutil.rmtree(path, onerror=retry_writable)


def _remove(path: Path) -> None:
    if path.is_dir():
        _rmtree(path)
    elif path.exists():
        path.unlink()


def remove_after_exit(folder: Path, pid: Optional[int] = None) -> List[str]:
    """Delete folder once this process has exited, from a hidden PowerShell. Returns the command started."""
    from .catalog import powershell_path
    quoted = str(folder).replace("'", "''")
    script = (f"Wait-Process -Id {pid or os.getpid()} -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1; "
              f"Remove-Item -LiteralPath '{quoted}' -Recurse -Force -ErrorAction SilentlyContinue")
    argv = [powershell_path(), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script]
    subprocess.Popen(argv, cwd=tempfile.gettempdir(), close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=DETACHED if os.name == "nt" else 0)
    return argv


# The scheduled tasks Asgard's apps register (baldur.cli.TASK_NAME, Odin's Register-MeetingSyncTask.ps1,
# and Odin's name from before it moved into Asgard). Once the code is gone they would fail every day.
SCHEDULED_TASKS = ("Asgard Baldur collect", "Asgard Odin daily", "meeting2jira-daily")
CREATE_NO_WINDOW = winutil.CREATE_NO_WINDOW


def remove_scheduled_tasks() -> List[str]:
    """Delete the apps' scheduled tasks. Returns the ones removed; a task that isn't there is fine."""
    if not winutil.IS_WINDOWS:
        return []
    removed = []
    for name in SCHEDULED_TASKS:
        try:
            done = subprocess.run(["schtasks", "/Delete", "/F", "/TN", name], capture_output=True, timeout=30,
                                  creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            removed.append(f"scheduled task {name}")
    return removed


def uninstall(purge: bool = False) -> Result:
    data = paths.data_dir()
    try:
        os.chdir(tempfile.gettempdir())  # Windows can't delete a folder that is the current directory
    except OSError:
        pass
    ledger = load_ledger()
    items = list(reversed(ledger.get("items", []))) if ledger else default_items()
    expected_key = ("HKCU\\" + paths.UNINSTALL_SUBKEY).lower()
    res = Result()
    res.removed.extend(remove_scheduled_tasks())
    for item in items:
        kind, raw = item.get("kind"), str(item.get("path", ""))
        try:
            if kind == "regkey":
                if raw.lower().replace("hkey_current_user", "hkcu") != expected_key:
                    res.skipped.append((raw, "not Asgard's registry entry, so left alone"))
                elif winutil.delete_uninstall_entry(paths.UNINSTALL_SUBKEY):
                    res.removed.append("Settings > Apps entry")
            elif kind == "file":
                p = Path(raw)
                if not (_allowed_shortcut(p) or (_within(p, data) and _resolve(p) != _resolve(data))):
                    res.skipped.append((raw, "outside Asgard's folders, so left alone"))
                elif p.exists():
                    p.unlink()
                    res.removed.append(raw)
            elif kind == "dir":
                p = Path(raw)
                if not _within(p, data) or _resolve(p) == _resolve(data):
                    res.skipped.append((raw, "outside Asgard's folder, so left alone"))
                elif p.exists() and winutil.IS_WINDOWS and paths.FROZEN and _within(paths.CODE_ROOT, p):
                    remove_after_exit(p)
                    res.after_exit = raw
                elif p.exists():
                    _rmtree(p)
                    res.removed.append(raw)
            else:
                res.skipped.append((raw, f"unknown item kind {kind!r}"))
        except OSError as exc:
            res.skipped.append((raw, str(exc)))

    for leftover in list(data.glob("app.old-*")) + [data / "app.new"]:
        if leftover.exists():
            shutil.rmtree(leftover, ignore_errors=True)
    try:
        paths.ledger_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        res.skipped.append((str(paths.ledger_path()), str(exc)))

    if purge:
        for target in _user_data(data):
            try:
                if target.exists():
                    _remove(target)
                    res.removed.append(str(target))
            except OSError as exc:
                res.skipped.append((str(target), str(exc)))
        try:
            data.rmdir()  # only succeeds when nothing else is left
        except OSError:
            res.kept = [str(p) for p in data.iterdir()] if data.exists() else []
    else:
        res.kept = [str(p) for p in _user_data(data) if p.exists()]
    return res


def summary(res: Result) -> str:
    lines = ["Removed Asgard and its shortcuts." if res.removed else "There was nothing left to remove."]
    if res.kept:
        lines.append(f"\nKept your data in {paths.data_dir()}.")
    if res.after_exit:
        lines.append(f"\nAsgard's program folder ({res.after_exit}) goes as soon as Asgard closes. If any of it is "
                     "left (another Asgard app was still open), delete it once they've all closed.")
    if res.skipped:
        lines.append("\nNot removed:")
        lines += [f"  {path}: {why}" for path, why in res.skipped]
    return "\n".join(lines)


def confirm_and_run(parent: Any = None) -> bool:
    """Ask, uninstall, report. Returns True if Asgard was uninstalled."""
    from tkinter import messagebox
    if not is_installed():
        messagebox.showinfo("Valhalla", "Asgard isn't installed for your account, so there's nothing to remove."
                            f"\n\nThis copy is running from:\n{paths.CODE_ROOT}", parent=parent)
        return False
    if not messagebox.askyesno("Uninstall Asgard?",
                               "Valhalla removes Asgard, its Start menu shortcut and its Settings > Apps entry.\n\n"
                               "Apps that live elsewhere, such as Odin, aren't touched.\n\nUninstall now?",
                               icon="warning", default="no", parent=parent):
        return False
    purge = messagebox.askyesno("Delete your Asgard data too?",
                                "Also delete your tile settings, logs and Muninn database?\n\n"
                                "Choose No to keep them for a later reinstall.", default="no", parent=parent)
    res = uninstall(purge=purge)
    if res.ok:
        messagebox.showinfo("Asgard is uninstalled", summary(res), parent=parent)
    else:
        messagebox.showwarning("Asgard is mostly uninstalled", summary(res), parent=parent)
    return True


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--yes" in args:
        res = uninstall(purge="--purge" in args)
        print(summary(res))
        return 0 if res.ok else 1
    try:
        import tkinter as tk
    except ImportError:
        print("No window available. Run again with --yes to uninstall (add --purge to delete your data).")
        return 2
    root = tk.Tk()
    root.withdraw()
    try:
        confirm_and_run(root)
    finally:
        root.destroy()
    return 0
