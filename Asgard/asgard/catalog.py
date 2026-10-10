"""Tile definitions.

The default tiles ship in asgard/apps.json and are replaced on upgrade.
Your changes go in %LOCALAPPDATA%\\Asgard\\apps.local.json, which setup never
overwrites. Each app has an id, a name, a two-letter monogram, a colour, a
status (available, external, coming_soon) and a launch block:

    {"type": "python",     "target": "C:\\Tools\\Odin\\odin.pyw", "interpreter": "...\\pythonw.exe"}
    {"type": "powershell", "target": "C:\\Tools\\Thing\\start.ps1"}
    {"type": "exe",        "target": "C:\\Tools\\Thing\\thing.exe", "args": ["--quiet"]}
    {"type": "open",       "target": "C:\\Tools\\Thing\\Thing.lnk"}     (like a double-click)
    {"type": "url",        "target": "https://jira.example.gov"}
    {"type": "internal",   "command": "uninstall"}

Paths may use {app} (Asgard's code folder), {data} (Asgard's data folder),
{python}, {pythonw} and environment variables such as %USERPROFILE%.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import paths

STATUSES = ("available", "external", "coming_soon")
LAUNCH_TYPES = ("python", "powershell", "exe", "open", "url", "internal")
EXTENSION_TYPES = {
    ".py": "python", ".pyw": "python", ".ps1": "powershell", ".exe": "exe",
    ".cmd": "open", ".bat": "open", ".lnk": "open", ".url": "open",
}
VENV_NAMES = (".venv", "venv", "env")

READY = "ready"
NEEDS_SETUP = "needs_setup"
MISSING = "missing"
COMING_SOON = "coming_soon"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_APP_FIELDS = ("name", "description", "monogram", "color", "status")


class CatalogError(Exception):
    """A manifest could not be read or is invalid."""


@dataclass
class App:
    id: str
    name: str
    description: str = ""
    monogram: str = ""
    color: str = "#56606E"
    status: str = "available"
    launch: Dict[str, Any] = field(default_factory=dict)
    source: str = "default"

    def __post_init__(self) -> None:
        if not self.monogram:
            self.monogram = self.name[:2].title()
        self.monogram = self.monogram[:2]


@dataclass
class LaunchSpec:
    kind: str  # process | shell | url | internal
    target: str
    argv: List[str] = field(default_factory=list)
    cwd: Optional[str] = None
    console: bool = False


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def default_manifest_path() -> Path:
    return paths.PACKAGE_DIR / "apps.json"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8-sig") as fh:  # utf-8-sig: Notepad may add a BOM
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise CatalogError(f"{path.name}, line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    except OSError as exc:
        raise CatalogError(f"{path.name}: {exc.strerror or exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError(f"{path.name}: the file must hold a JSON object {{...}}")
    return data


def _validate_app(entry: Dict[str, Any], where: str) -> App:
    app_id = str(entry.get("id", ""))
    if not _ID_RE.match(app_id):
        raise CatalogError(f"{where}: id {app_id!r} must be lowercase letters, digits, - or _")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CatalogError(f"{where}: app {app_id!r} needs a name")
    color = entry.get("color", "#56606E")
    if not _COLOR_RE.match(str(color)):
        raise CatalogError(f"{where}: app {app_id!r} colour must look like #2B5797")
    status = entry.get("status", "available")
    if status not in STATUSES:
        raise CatalogError(f"{where}: app {app_id!r} status must be one of {', '.join(STATUSES)}")
    launch = entry.get("launch") or {}
    if not isinstance(launch, dict):
        raise CatalogError(f"{where}: app {app_id!r} launch must be an object")
    if launch.get("type") and launch["type"] not in LAUNCH_TYPES:
        raise CatalogError(f"{where}: app {app_id!r} launch type must be one of {', '.join(LAUNCH_TYPES)}")
    return App(id=app_id, name=name.strip(), description=str(entry.get("description", "")),
               monogram=str(entry.get("monogram", "")), color=str(color), status=status,
               launch=dict(launch))


def _merge(app: App, override: Dict[str, Any]) -> App:
    merged = {"id": app.id, "name": app.name, "description": app.description,
              "monogram": app.monogram, "color": app.color, "status": app.status,
              "launch": dict(app.launch)}
    for key in _APP_FIELDS:
        if key in override:
            merged[key] = override[key]
    new_launch = override.get("launch")
    if isinstance(new_launch, dict):
        if new_launch.get("type") and new_launch.get("type") != merged["launch"].get("type"):
            merged["launch"] = dict(new_launch)
        else:
            merged["launch"].update(new_launch)
    result = _validate_app(merged, "apps.local.json")
    result.source = "local"
    return result


def load_catalog(defaults: Optional[Path] = None,
                 local: Optional[Path] = None) -> Tuple[List[App], List[str]]:
    """Return (visible apps in display order, warnings about the local file)."""
    defaults = defaults or default_manifest_path()
    local = local or paths.local_manifest()
    base = _read_json(defaults)  # a broken shipped file is a bug: let it raise
    apps: List[App] = []
    for i, entry in enumerate(base.get("apps", [])):
        apps.append(_validate_app(entry, f"{defaults.name} entry {i + 1}"))
    by_id = {a.id: a for a in apps}
    if len(by_id) != len(apps):
        raise CatalogError(f"{defaults.name}: two apps share an id")

    warnings: List[str] = []
    if not local.exists():
        return apps, warnings
    try:
        user = _read_json(local)
    except CatalogError as exc:
        warnings.append(f"Couldn't read your tile settings, so Asgard is showing the default tiles.\n\n{exc}")
        return apps, warnings

    overrides = user.get("apps", {})
    if isinstance(overrides, list):  # also accept [{"id": ...}, ...]
        overrides = {str(o.get("id")): o for o in overrides if isinstance(o, dict)}
    if not isinstance(overrides, dict):
        warnings.append('In apps.local.json, "apps" must be an object keyed by app id.')
        overrides = {}

    for app_id, override in overrides.items():
        if not isinstance(override, dict):
            warnings.append(f"apps.local.json: the entry for {app_id!r} isn't an object; skipped.")
            continue
        try:
            if app_id in by_id:
                by_id[app_id] = _merge(by_id[app_id], override)
            else:
                new = _validate_app(dict(override, id=app_id), "apps.local.json")
                new.source = "local"
                by_id[app_id] = new
                apps.append(new)
        except CatalogError as exc:
            warnings.append(f"{exc}\nThat tile keeps its default settings.")

    order = [a.id for a in apps]
    wanted = [i for i in user.get("order", []) if i in by_id]
    order = wanted + [i for i in order if i not in wanted]
    hidden = set(user.get("hidden", []))
    return [by_id[i] for i in order if i not in hidden], warnings


# --------------------------------------------------------------------------
# State and launching
# --------------------------------------------------------------------------

def python_paths(target: Optional[str] = None) -> Tuple[str, str]:
    """(python.exe, pythonw.exe) next to the interpreter running Asgard.

    In the packaged build, Asgard's own scripts run under asgard-cli.exe and Asgard.exe. A script
    outside it (Odin's, say) needs a real Python with its own packages, so it gets the one on PATH.
    """
    if paths.FROZEN:
        inside = target is None or _within(Path(target), paths.CODE_ROOT)
        return paths.frozen_programs() if inside else system_python()
    exe = Path(sys.executable)
    if os.name != "nt":
        return str(exe), str(exe)
    py, pyw = exe.with_name("python.exe"), exe.with_name("pythonw.exe")
    return (str(py) if py.exists() else str(exe)), (str(pyw) if pyw.exists() else str(exe))


def system_python() -> Tuple[str, str]:
    """(console, windowless) Python from PATH, or the py launcher; the packaged build has none of its own."""
    if os.name != "nt":
        found = shutil.which("python3") or shutil.which("python") or "python3"
        return found, found
    py = shutil.which("python.exe") or shutil.which("py.exe") or "py.exe"
    pyw = shutil.which("pythonw.exe") or shutil.which("pyw.exe") or "pyw.exe"
    return py, pyw


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def expand(value: str) -> str:
    py, pyw = python_paths()
    text = (str(value).replace("{app}", str(paths.CODE_ROOT))
            .replace("{data}", str(paths.data_dir()))
            .replace("{pythonw}", pyw).replace("{python}", py))
    return os.path.expanduser(os.path.expandvars(text))


def _is_link(target: str) -> bool:
    return "://" in target or target.lower().startswith(("shell:", "mailto:"))


def state(app: App) -> str:
    if app.status == "coming_soon":
        return COMING_SOON
    launch = app.launch or {}
    kind = launch.get("type")
    if kind == "internal":
        return READY
    target = launch.get("target")
    if not kind or not target:
        return NEEDS_SETUP
    if kind == "url" or (kind == "open" and _is_link(str(target))):
        return READY
    return READY if Path(expand(target)).exists() else MISSING


def find_venv_interpreter(script: Path) -> Optional[str]:
    """A virtual environment beside the script (or one folder up), if there is one."""
    if os.name == "nt":
        rel = (Path("Scripts") / "pythonw.exe", Path("Scripts") / "python.exe")
    else:
        rel = (Path("bin") / "python",)
    for folder in (script.parent, script.parent.parent):
        for name in VENV_NAMES:
            for r in rel:
                candidate = folder / name / r
                if candidate.exists():
                    return str(candidate)
    return None


def infer_launch(path: str) -> Dict[str, Any]:
    """A launch block for a file someone picked in the Set up dialog."""
    target = Path(path)
    kind = EXTENSION_TYPES.get(target.suffix.lower(), "open")
    launch: Dict[str, Any] = {"type": kind, "target": str(target)}
    if kind == "python":
        venv = find_venv_interpreter(target)
        if venv:
            launch["interpreter"] = venv
    return launch


def powershell_path() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.exists():
        return str(candidate)
    return shutil.which("powershell") or shutil.which("pwsh") or "powershell.exe"


def build_spec(app: App) -> LaunchSpec:
    launch = app.launch or {}
    kind = launch.get("type")
    if kind == "internal":
        return LaunchSpec("internal", str(launch.get("command", "")))
    target = expand(str(launch.get("target", "")))
    if kind == "url":
        if not target.lower().startswith(("https://", "http://")):
            raise CatalogError(f"{app.name}: a url tile needs an http:// or https:// address")
        return LaunchSpec("url", target)
    if kind == "open":
        return LaunchSpec("shell", target)
    args = [expand(a) for a in launch.get("args", [])]
    console = bool(launch.get("console", False))
    if kind == "python":
        py, pyw = python_paths(target)
        interpreter = expand(launch["interpreter"]) if launch.get("interpreter") else (py if console else pyw)
        argv = [interpreter, target] + args
    elif kind == "powershell":
        argv = [powershell_path(), "-NoProfile", "-File", target] + args
    elif kind == "exe":
        argv = [target] + args
    else:
        raise CatalogError(f"{app.name}: unknown launch type {kind!r}")
    cwd = expand(launch["cwd"]) if launch.get("cwd") else str(Path(target).parent)
    return LaunchSpec("process", target, argv, cwd, console)


# --------------------------------------------------------------------------
# Saving the user's choices
# --------------------------------------------------------------------------

LOCAL_TEMPLATE: Dict[str, Any] = {
    "_help": "Your Asgard tiles. Setup never overwrites this file. See README.md for examples.",
    "apps": {},
    "hidden": [],
}


def ensure_local_manifest(local: Optional[Path] = None) -> Path:
    local = local or paths.local_manifest()
    if not local.exists():
        local.parent.mkdir(parents=True, exist_ok=True)
        _write_json(local, LOCAL_TEMPLATE)
    return local


def save_launch_override(app_id: str, launch: Dict[str, Any], local: Optional[Path] = None) -> None:
    local = local or paths.local_manifest()
    data = _read_json(local) if local.exists() else json.loads(json.dumps(LOCAL_TEMPLATE))
    apps = data.get("apps")
    if isinstance(apps, list):
        apps = {str(o.get("id")): o for o in apps if isinstance(o, dict)}
    if not isinstance(apps, dict):
        apps = {}
    entry = apps.setdefault(app_id, {})
    entry["launch"] = launch
    data["apps"] = apps
    _write_json(local, data)


def retire_launch_override(app_id: str, inside: Path, local: Optional[Path] = None) -> Optional[str]:
    """Drop a tile's launch setting that points outside Asgard's code folder, for an app that now
    ships with Asgard (Odin, Oct 2026): the old setting would keep opening the copy from before.
    Other settings for the tile (name, colour) stay. Returns the target removed, if any.
    """
    local = local or paths.local_manifest()
    if not local.exists():
        return None
    try:
        data = _read_json(local)
    except CatalogError:
        return None                       # a broken file is the person's to fix; never overwrite it
    apps = data.get("apps")
    entry = apps.get(app_id) if isinstance(apps, dict) else None
    launch = entry.get("launch") if isinstance(entry, dict) else None
    if not isinstance(launch, dict) or not launch.get("target"):
        return None
    target = Path(expand(str(launch["target"])))
    try:
        target.resolve().relative_to(Path(inside).resolve())
        return None                       # already the copy that ships with Asgard
    except ValueError:
        pass
    del entry["launch"]
    if entry.get("status") == "external":
        del entry["status"]
    _write_json(local, data)
    return str(target)


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\r\n" if os.name == "nt" else "\n") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
