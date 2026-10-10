"""Install Asgard for the current user. No admin rights needed.

setup-Asgard.cmd runs this. If your policy blocks .cmd files, run it
yourself from the extracted folder:

    py -3 asgard\\install.py            (or: python asgard\\install.py)

Options: --desktop adds a desktop shortcut; --no-launch skips opening Asgard.

The packaged build (docs/packaging.md) runs this through asgard-cli.exe, and installs the same way:
the whole build (its programs, Python and Asgard's code) is copied to %LOCALAPPDATA%\\Asgard\\app,
beside Asgard's data (Brandon, Oct 10). The folder you extracted can go afterwards.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SOURCE_ROOT = Path(__file__).resolve().parent.parent
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from asgard import __version__, catalog, paths, winutil  # noqa: E402

PAYLOAD = ("Asgard.pyw", "VERSION", "README.md", "asgard", "apps")
MIN_PYTHON = (3, 9)
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


class SetupError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def say(tag: str, text: str) -> None:
    print(f"  [{tag}] {text}", flush=True)


def check_prerequisites() -> None:
    if sys.version_info < MIN_PYTHON:
        raise SetupError(f"Asgard needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; "
                         f"this is {platform.python_version()}.", 2)
    try:
        import tkinter
        tkinter.Tcl()
    except Exception as exc:
        raise SetupError("This Python has no working Tcl/Tk (tkinter), which Asgard's window needs.\n"
                         "       Ask IT for a Python install that includes Tcl/Tk.\n"
                         f"       ({exc})", 2) from exc
    if not (SOURCE_ROOT / "Asgard.pyw").exists() or not (SOURCE_ROOT / "asgard" / "launcher.py").exists():
        raise SetupError("Setup can't find Asgard's files. Extract the whole zip, then run setup again.")
    if paths.app_dir().exists() and SOURCE_ROOT.resolve() == paths.app_dir().resolve():
        raise SetupError("This is the installed copy. Run setup from the folder you downloaded and extracted.")


def load_ledger() -> Dict[str, Any]:
    try:
        with open(paths.ledger_path(), encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def copy_payload(src: Path, dst: Path) -> int:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
    dst.mkdir(parents=True)
    count = 0
    for name in PAYLOAD:
        item = src / name
        if not item.exists():
            continue
        if item.is_dir():
            shutil.copytree(item, dst / name, ignore=ignore)
            count += sum(1 for p in (dst / name).rglob("*") if p.is_file())
        else:
            shutil.copy2(item, dst / name)
            count += 1
    return count


def copy_build(src: Path, dst: Path) -> int:
    """The packaged build: everything in its folder (programs, DLLs, Python, Asgard's code and .pyc)."""
    shutil.copytree(src, dst)
    return sum(1 for p in dst.rglob("*") if p.is_file())


def swap_in(staged: Path, app: Path) -> None:
    """Replace the installed copy in two renames, so a failure leaves the old copy working."""
    for leftover in app.parent.glob("app.old-*"):
        shutil.rmtree(leftover, ignore_errors=True)
    old: Optional[Path] = None
    if app.exists():
        old = app.with_name(f"app.old-{int(time.time())}")
        try:
            os.replace(app, old)
        except OSError as exc:
            shutil.rmtree(staged, ignore_errors=True)
            raise SetupError("Close Asgard and any app you opened from it, then run setup again.\n"
                             f"       ({exc})") from exc
    os.replace(staged, app)
    if old:
        shutil.rmtree(old, ignore_errors=True)


def folder_size_kb(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file()) // 1024


def install(desktop: bool = False) -> Dict[str, Any]:
    data, app = paths.data_dir(), paths.app_dir()
    if paths.FROZEN:
        print(f"  Program  {SOURCE_ROOT} (packaged build, Python {platform.python_version()})")
    else:
        print(f"  Python   {sys.executable} ({platform.python_version()})")
    print(f"  Folder   {data}\n")
    data.mkdir(parents=True, exist_ok=True)

    ledger = load_ledger()
    previous = ledger.get("version")
    note = f", replacing {previous}" if previous and previous != __version__ else (
        ", reinstalled" if previous else "")
    staged = data / "app.new"
    if staged.exists():
        shutil.rmtree(staged)
    count = copy_build(SOURCE_ROOT, staged) if paths.FROZEN else copy_payload(SOURCE_ROOT, staged)
    swap_in(staged, app)
    say("ok", f"Copied {count} files to {app}{note}")
    items: List[Dict[str, str]] = [{"kind": "dir", "path": str(app)}]

    paths.log_dir().mkdir(parents=True, exist_ok=True)
    catalog.ensure_local_manifest()
    # Odin ships with Asgard from 0.4.0; a tile set up to open a copy of Odin from before would
    # keep opening that copy instead.
    old = catalog.retire_launch_override("odin", app)
    if old:
        say("ok", f"Odin is part of Asgard now: its tile opens Asgard's own Odin, not {old}")
    # The installed copy's programs: the packaged build's own (in app\), or the Python running setup.
    python, pythonw = paths.frozen_programs(app) if paths.FROZEN else catalog.python_paths()
    entry, icon = app / "Asgard.pyw", app / "asgard" / "asgard.ico"
    # The packaged build's Asgard.exe opens the launcher by itself; Python needs the script named.
    start = "" if paths.FROZEN else f'"{entry}"'
    shortcut_made = False

    if winutil.IS_WINDOWS:
        targets = [("Start menu", paths.start_menu_dir())]
        if desktop:
            targets.append(("Desktop", winutil.desktop_dir()))
        for label, folder in targets:
            if not folder:
                say("!", f"Couldn't find your {label} folder; skipped that shortcut.")
                continue
            lnk = folder / paths.SHORTCUT_NAME
            try:
                winutil.create_shortcut(lnk, pythonw, start, str(data), str(icon), "Asgard app launcher")
                items.append({"kind": "file", "path": str(lnk)})
                shortcut_made = True
                say("ok", f"{label} shortcut")
            except OSError as exc:
                say("!", f"Couldn't create the {label} shortcut: {exc}")
        try:
            winutil.write_uninstall_entry(paths.UNINSTALL_SUBKEY, {
                "DisplayName": "Asgard",
                "DisplayVersion": __version__,
                "Publisher": "Asgard",
                "InstallLocation": str(app),
                "DisplayIcon": str(icon),
                "UninstallString": " ".join(filter(None, [f'"{pythonw}"', start, "--uninstall"])),
                "QuietUninstallString": " ".join(filter(None, [f'"{python}"', start, "--uninstall --yes"])),
                "InstallDate": dt.date.today().strftime("%Y%m%d"),
                "EstimatedSize": folder_size_kb(app),
                "NoModify": 1,
                "NoRepair": 1,
            })
            items.append({"kind": "regkey", "path": "HKCU\\" + paths.UNINSTALL_SUBKEY})
            say("ok", "Listed in Settings > Apps")
        except OSError as exc:
            say("!", f"Couldn't add Asgard to Settings > Apps ({exc}). The Valhalla tile still uninstalls it.")
    else:
        say("--", "Not Windows: skipped shortcuts and the Settings > Apps entry")

    known = {(i.get("kind"), i.get("path")) for i in items}
    merged = items + [i for i in ledger.get("items", []) if (i.get("kind"), i.get("path")) not in known]
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    history = list(ledger.get("history", [])) + [{"version": __version__, "at": now}]
    record = {"product": "Asgard", "version": __version__, "installed_at": now,
              "python": python, "pythonw": pythonw, "items": merged, "history": history[-20:]}
    if paths.FROZEN:
        record["packaged"] = True           # app\ holds programs: Valhalla removes it once Asgard has closed
    tmp = paths.ledger_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, paths.ledger_path())
    say("ok", "Saved the install record that Valhalla uses to uninstall")
    return {"entry": entry, "pythonw": pythonw, "data": data, "shortcut": shortcut_made}


def launch(entry: Path, pythonw: str, data: Path) -> None:
    if winutil.IS_WINDOWS:
        argv = [pythonw] if paths.FROZEN else [pythonw, str(entry)]
        subprocess.Popen(argv, cwd=str(data), close_fds=True,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="setup-Asgard", description="Install Asgard for the current user.")
    parser.add_argument("--desktop", action="store_true", help="also put a shortcut on the desktop")
    parser.add_argument("--no-launch", action="store_true", help="don't open Asgard when setup finishes")
    args = parser.parse_args(argv)
    print(f"\nAsgard {__version__} setup\n")
    try:
        check_prerequisites()
        done = install(desktop=args.desktop)
    except SetupError as exc:
        print(f"\n  [!!] {exc}\n")
        return exc.code
    except OSError as exc:
        print(f"\n  [!!] Setup stopped: {exc}\n")
        return 1
    print()
    if not args.no_launch:
        try:
            launch(done["entry"], done["pythonw"], done["data"])
        except OSError as exc:
            say("!", f"Couldn't open Asgard: {exc}")
    if done["shortcut"]:
        print("Asgard is installed. Find it in the Start menu; right-click it there to pin it to the taskbar.")
    else:
        print("Asgard is installed. Start it with:")
        print(f'  "{done["pythonw"]}"' + ("" if paths.FROZEN else f' "{done["entry"]}"'))
    return 0


if __name__ == "__main__":
    sys.exit(main())
