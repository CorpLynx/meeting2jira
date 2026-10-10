"""The packaged build's entry point: Asgard.exe and asgard-cli.exe both run this (docs/packaging.md).

The two programs stand in for Python, so the launcher, the .cmd wrappers and Ysildir's client
setup start Asgard's scripts exactly as they would with pythonw.exe and python.exe:

    Asgard.exe                              the launcher window (Asgard.pyw); no console
    Asgard.exe --uninstall                  the launcher's own options (--muninn ..., --uninstall)
    asgard-cli.exe SCRIPT.py [ARGS]         one of Asgard's scripts, as python SCRIPT.py ARGS would
    asgard-cli.exe --self-test              check that every part of the build loads on this computer
                                            (--skip Qt,Tk leaves parts out, for a machine without them)

Asgard's own code isn't frozen into the programs. It ships as .py files beside them, at the same
layout as an installed copy, so every path it works out from __file__ (prompts, QML, migrations)
holds; the programs bring Python, its standard library and the pinned packages. Only scripts inside
the build run: it is Asgard, not a general-purpose Python.

Standard library only, like the launcher's start-up path.
"""
from __future__ import annotations

import importlib
import os
import runpy
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

SCRIPT_SUFFIXES = (".py", ".pyw")
APP_FOLDERS = ("baldur", "heimdall", "ysildir")       # apps/<name>, each with its own package


class Refused(Exception):
    """The command can't run; the message is for the person."""


def bundle_root() -> Path:
    """The build's folder: Asgard.pyw, asgard\\ and apps\\ sit there beside the programs."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))).resolve()
    return Path(__file__).resolve().parent.parent          # a checkout: Asgard/


def resolve(argv: Sequence[str], root: Path, cwd: Optional[Path] = None) -> Tuple[Path, List[str]]:
    """(script, its arguments) for a command line, the way python would read it.

    A first argument ending in .py or .pyw is the script, relative to the current folder like
    Python's; it must be inside the build. Anything else goes to the launcher, Asgard.pyw.
    """
    args = list(argv)
    if args and args[0].lower().endswith(SCRIPT_SUFFIXES):
        script = Path(args[0])
        if not script.is_absolute():
            script = (cwd or Path.cwd()) / script
        try:
            script = script.resolve()
            script.relative_to(root.resolve())
        except ValueError:
            raise Refused(f"{args[0]} isn't part of Asgard, so this program won't run it. Asgard's packaged "
                          f"build runs only its own scripts, in {root}.") from None
        except OSError as exc:
            raise Refused(f"Couldn't find {args[0]} ({exc}).") from None
        if not script.is_file():
            raise Refused(f"There is no {args[0]} in Asgard's packaged build ({root}).")
        return script, args[1:]
    return root / "Asgard.pyw", args


def run_script(script: Path, args: Sequence[str]) -> None:
    """Run a script as __main__, with sys.argv and sys.path set up as python would set them."""
    sys.argv = [str(script)] + list(args)
    folder = str(script.parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    runpy.run_path(str(script), run_name="__main__")


# --------------------------------------------------------------------------
# --self-test: what IT runs on the workstation (and the build runs on itself)
# --------------------------------------------------------------------------

def _paths_for_apps(root: Path) -> None:
    for folder in [root] + [root / "apps" / name for name in APP_FOLDERS]:
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))


def _check_sqlite() -> str:
    from asgard.muninn import db
    problems = db.sqlite_problems()
    if problems:
        raise RuntimeError("; ".join(problems))
    import sqlite3
    return f"SQLite {sqlite3.sqlite_version}, with FTS5 and JSON"


def _check_muninn() -> str:
    """Create a Muninn in a temporary folder, migrate it and check it; your own data isn't touched."""
    with tempfile.TemporaryDirectory(prefix="asgard-self-test-") as home:
        saved = os.environ.get("ASGARD_HOME")
        os.environ["ASGARD_HOME"] = home
        try:
            from asgard import muninn
            from asgard.muninn import integrity
            done = muninn.prepare()
            con = muninn.connect()
            try:
                report = integrity.check(con)
            finally:
                con.close()
            errors = [str(f) for f in report.findings if f.level == "error"]
            if errors:
                raise RuntimeError("; ".join(errors))
            return f"schema v{done.version} created and checked in a temporary folder"
        finally:
            if saved is None:
                os.environ.pop("ASGARD_HOME", None)
            else:
                os.environ["ASGARD_HOME"] = saved


def _check_tk() -> str:
    import tkinter
    return f"Tcl/Tk {tkinter.Tcl().eval('info patchlevel')}"


def _check_qt() -> str:
    for name in ("PySide6.QtCore", "PySide6.QtGui", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickControls2"):
        importlib.import_module(name)
    import PySide6
    return f"PySide6 {PySide6.__version__} (the shared window's Qt DLLs load)"


def _check_mcp() -> str:
    from importlib import metadata
    importlib.import_module("ysildir.server")
    return f"mcp {metadata.version('mcp')}, pydantic {metadata.version('pydantic')} (Ysildir's server loads)"


def _check_apps() -> str:
    root = bundle_root()
    for name in ("asgard.launcher", "asgard.install", "asgard.valhalla", "baldur.cli", "baldur.window",
                 "heimdall.cli", "ysildir.config"):
        module = importlib.import_module(name)
        where = Path(getattr(module, "__file__", "") or "")
        # Asgard's code must run from the shipped .py files, or its paths (prompts, QML, migrations) break.
        if where.suffix != ".py" or root not in where.resolve().parents:
            raise RuntimeError(f"{name} loaded from {where or 'the archive'}, not from {root}")
    return "the launcher, setup, Valhalla, Baldur, Heimdall and Ysildir load, from the shipped .py files"


def _check_tls() -> str:
    import ssl
    context = ssl.create_default_context()
    found = len(context.get_ca_certs()) if os.name == "nt" else None
    return "TLS verifies against " + (f"the Windows certificate store ({found} roots loaded)" if found is not None
                                      else "this system's certificates")


CHECKS: List[Tuple[str, Callable[[], str]]] = [
    ("SQLite", _check_sqlite), ("Muninn", _check_muninn), ("Tk", _check_tk), ("Qt", _check_qt),
    ("MCP", _check_mcp), ("Apps", _check_apps), ("TLS", _check_tls)]


def self_test(out: Callable[[str], None] = print, skip: Sequence[str] = ()) -> int:
    root = bundle_root()
    _paths_for_apps(root)
    version = (root / "VERSION").read_text(encoding="utf-8").strip() if (root / "VERSION").exists() else "?"
    out(f"Asgard {version}, packaged build, Python {sys.version.split()[0]}")
    out(f"  Folder  {root}")
    failed = 0
    skipped = {s.strip().lower() for s in skip}
    for name, check in CHECKS:
        if name.lower() in skipped:
            out(f"  [--] {name:<7} skipped")
            continue
        try:
            out(f"  [ok] {name:<7} {check()}")
        except Exception as exc:          # each check reports, and the rest still run
            failed += 1
            out(f"  [!!] {name:<7} {type(exc).__name__}: {exc}")
            if os.environ.get("ASGARD_SELF_TEST_TRACE"):
                out(traceback.format_exc())
    out("All parts load." if not failed else
        f"{failed} part(s) failed. If Windows blocked a DLL, App Control needs to allow this folder's files.")
    return 1 if failed else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--self-test"]:
        skip = args[2].split(",") if args[1:2] == ["--skip"] and len(args) > 2 else []
        return self_test(skip=skip)
    try:
        script, rest = resolve(args, bundle_root())
    except Refused as exc:
        print(f"Asgard: {exc}", file=sys.stderr)
        return 2
    run_script(script, rest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
