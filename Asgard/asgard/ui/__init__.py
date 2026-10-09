"""Asgard's shared desktop window (PySide6 + QML): Dashboard, Settings, and each app's own views.

    theme     the token set and how it's layered: built-in < app < your overrides (stdlib)
    prefs     your choices, settings\\ui.json (stdlib)
    registry  finds apps/<id>/ui/manifest.json and the views each app adds (stdlib)
    shell     the Qt side: the window, navigation, the bridge to each app's backend (PySide6)
    qml/      the AsgardUI QML module: components every app's views use

Start it with run(): every app with views (python -m asgard.ui), or one app on its own
(python -m asgard.ui --app heimdall, which is what apps/heimdall/heimdall.pyw does).

PySide6 is imported only inside run(), so the launcher, Muninn and the CLIs never need it.
"""
from __future__ import annotations

import sys
from typing import List, Optional

MISSING_QT = ("This window needs PySide6, which isn't installed for this Python ({python}). "
              "Ask IT to install the approved PySide6-Essentials package (see Asgard's requirements.txt). "
              "The command-line tools work without it.")


def run(app: Optional[str] = None, argv: Optional[List[str]] = None) -> int:
    try:
        from . import shell
    except ImportError as exc:
        if "PySide6" not in str(exc):
            raise
        _tell(MISSING_QT.format(python=sys.executable))
        return 2
    return shell.run(app=app, argv=argv)


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
