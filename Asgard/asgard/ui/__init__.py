"""Asgard's shared desktop window (PySide6 + QML): Dashboard, Settings, and each app's own views.

    theme     the token set and how it's layered: built-in < app < your overrides (stdlib)
    prefs     your choices, settings\\ui.json (stdlib)
    registry  finds apps/<id>/ui/manifest.json and the views each app adds (stdlib)
    shell     the Qt side: the window, navigation, the bridge to each app's backend (PySide6)
    qml/      the AsgardUI QML module: components every app's views use

Start it with run(): every app with views (python -m asgard.ui), or one app on its own
(python -m asgard.ui --app heimdall, which is what apps/heimdall/heimdall.pyw does).
run(home=True) adds the Apps page, Asgard's own launcher (asgard/launcher.py starts it that way).
    home      what the Apps page does, with no Qt: tiles, starting apps, Muninn's start-up (stdlib)

PySide6 is imported only inside run(), so the launcher, Muninn and the CLIs never need it.
"""
from __future__ import annotations

import sys
from typing import List, Optional, Tuple

MISSING_QT = ("This window needs PySide6, which isn't installed for this Python ({python}). "
              "Ask IT to install the approved PySide6-Essentials package (see Asgard's requirements.txt). "
              "The command-line tools work without it.")


def available() -> Tuple[bool, str]:
    """Whether the Qt window can open on this computer: (True, "") or (False, why).

    Loads PySide6's QML parts, which loads their DLLs, so a blocked or missing one shows up here and not
    half-way through opening the window. The launcher asks this before choosing between the Qt window and
    the tkinter one (launcher.py itself imports no package).
    """
    try:
        import PySide6.QtGui  # noqa: F401
        import PySide6.QtQml  # noqa: F401
        import PySide6.QtQuickControls2  # noqa: F401
    except (ImportError, OSError) as exc:
        return False, str(exc)
    return True, ""


def run(app: Optional[str] = None, argv: Optional[List[str]] = None, home: bool = False) -> int:
    try:
        from . import shell
    except ImportError as exc:
        if "PySide6" not in str(exc):
            raise
        _tell(MISSING_QT.format(python=sys.executable))
        return 2
    return shell.run(app=app, argv=argv, home=home)


def _tell(message: str) -> None:
    """Say it on stderr (the launcher's log keeps it) and, under pythonw, in a message box."""
    print(f"Asgard: {message}", file=sys.stderr)
    if sys.stderr is None or not sys.stderr.isatty():
        try:
            import tkinter
            from tkinter import messagebox
            root = tkinter.Tk()
            root.withdraw()
            messagebox.showerror("Asgard", message)
            root.destroy()
        except Exception:  # noqa: BLE001 - no Tk either: stderr already has it
            pass
