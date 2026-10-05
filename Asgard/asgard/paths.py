"""Where Asgard keeps its files.

%LOCALAPPDATA%\\Asgard\\
    app\\                  the installed code (replaced on upgrade)
    apps.local.json       your tile settings (kept across upgrades)
    logs\\                 one log per app, plus launcher.log
    install-ledger.json   what setup created, for Valhalla to undo

Set ASGARD_HOME to use another folder (tests do this).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

PACKAGE_DIR = Path(__file__).resolve().parent
CODE_ROOT = PACKAGE_DIR.parent  # the folder holding Asgard.pyw; "{app}" in apps.json

UNINSTALL_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Asgard"
SHORTCUT_NAME = "Asgard.lnk"


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
