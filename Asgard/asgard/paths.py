"""Where Asgard keeps its files.

%LOCALAPPDATA%\\Asgard\\
    app\\                  the installed code (replaced on upgrade)
    apps.local.json       your tile settings (kept across upgrades)
    logs\\                 one log per app, plus launcher.log
    install-ledger.json   what setup created, for Valhalla to undo

Set ASGARD_HOME to use another folder (tests do this).

The packaged build (PyInstaller, docs/packaging.md) runs in place from the folder IT put it in:
CODE_ROOT is that folder, and nothing is copied to app\\. Its two programs stand in for Python:
Asgard.exe for pythonw and asgard-cli.exe for python, each running one of Asgard's scripts.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Tuple

PACKAGE_DIR = Path(__file__).resolve().parent
CODE_ROOT = PACKAGE_DIR.parent  # the folder holding Asgard.pyw; "{app}" in apps.json

UNINSTALL_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Asgard"
SHORTCUT_NAME = "Asgard.lnk"

FROZEN = bool(getattr(sys, "frozen", False))      # running from the packaged build
WINDOWED_PROGRAM = "Asgard"                        # + .exe on Windows: no console, like pythonw
CONSOLE_PROGRAM = "asgard-cli"                     # + .exe on Windows: a console, like python


def frozen_programs() -> Tuple[str, str]:
    """(console, windowed) programs of the packaged build, beside the one running."""
    folder = Path(sys.executable).resolve().parent
    ext = ".exe" if os.name == "nt" else ""
    return str(folder / (CONSOLE_PROGRAM + ext)), str(folder / (WINDOWED_PROGRAM + ext))


def data_dir() -> Path:
    override = os.environ.get("ASGARD_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "Asgard"
    return Path.home() / ".local" / "share" / "Asgard"


def app_dir() -> Path:
    return data_dir() / "app"


def log_dir() -> Path:
    return data_dir() / "logs"


def local_manifest() -> Path:
    return data_dir() / "apps.local.json"


def ledger_path() -> Path:
    return data_dir() / "install-ledger.json"


def start_menu_dir() -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
