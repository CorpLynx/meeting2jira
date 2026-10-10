"""What Asgard's home page does, without a window: the app tiles, starting an app, setting up where one
lives, the shared Muninn database's start-up and counts, and uninstalling.

This is the launcher's logic (asgard/launcher.py is the tkinter window that used the same pieces),
kept as a plain Python class so it can be tested with no Qt and shown by any window. shell.py wraps
it in HomeController for the QML page (qml/AsgardUI/Home.qml).

Rules it keeps:
- Nothing here raises for an ordinary problem: every method returns a dict with a "message" the page
  shows, or {"error": text}. A broken tile list, a missing app or a failed start is a sentence.
- Slow work (creating or upgrading Muninn, which may back it up first) runs on a thread; the page
  asks start_muninn() once and then polls muninn_state(). Nothing holds a Muninn transaction.
- An app is started through Runner and catalog.build_spec, exactly as the tkinter launcher did, so a
  tile behaves the same in either window.

Standard library only (Muninn and the catalog are imported lazily or optionally).
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import __version__, catalog, paths, winutil
from ..catalog import COMING_SOON, MISSING, NEEDS_SETUP, READY, App, CatalogError
from ..runner import LaunchError, Runner, log_tail
from . import theme

try:
    from .. import muninn
except Exception:  # a damaged install still launches apps; About explains
    muninn = None  # type: ignore[assignment]

log = logging.getLogger("asgard.ui.home")

RUNNING = "running"
BADGE_REFRESH_SECONDS = 60
# state -> (label on the tile, tone for the page: success, accent, warning or muted)
PILLS = {
    RUNNING: ("Running", "success"),
    NEEDS_SETUP: ("Set up", "accent"),
    MISSING: ("Not found", "warning"),
    COMING_SOON: ("Coming soon", "muted"),
    READY: ("", ""),
}


def write_log(text: str) -> None:
    try:
        paths.log_dir().mkdir(parents=True, exist_ok=True)
        with open(paths.log_dir() / "launcher.log", "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')}\n{text}\n")
    except OSError:
        pass


class HomeBackend:
    def __init__(self, defaults: Optional[Path] = None, local: Optional[Path] = None,
                 runner: Optional[Runner] = None) -> None:
        self.defaults, self.local = defaults, local
        self.apps: List[App] = []
        self.warnings: List[str] = []
        self.badges: Dict[str, List[Any]] = {}
        self.runner = runner or Runner(paths.log_dir(), extra_env={
            "ASGARD_DATA": str(paths.data_dir()), "ASGARD_APP": str(paths.CODE_ROOT)})
        self._last_running: set = set()
        self._status: Any = None
        self._error: Optional[str] = None
        self._result: Dict[str, Any] = {}
        self._thread: Optional[threading.Thread] = None
        self._last_badges = 0.0
        self.reload()

    # ---- the tiles ---------------------------------------------------------
    def reload(self) -> List[str]:
        """Read the tile list again; returns the warnings (a broken local file, for one)."""
        try:
            self.apps, self.warnings = catalog.load_catalog(self.defaults, self.local)
        except CatalogError as exc:
            self.apps = []
            self.warnings = [f"Asgard's built-in tile list is damaged. Reinstall Asgard.\n\n{exc}"]
        return list(self.warnings)

    def _state(self, app: App) -> str:
        return RUNNING if self.runner.is_running(app.id) else catalog.state(app)

    def _badge_for(self, app: App) -> Dict[str, Any]:
        items = self.badges.get(app.id) or []
        return {"count": sum(int(b.count) for b in items),
                "hint": "; ".join(f"{b.count} {b.text}" for b in items if b.count)}

    def tiles(self, query: str = "") -> List[Dict[str, Any]]:
        q = (query or "").strip().lower()
        out: List[Dict[str, Any]] = []
        for app in self.apps:
            if q and q not in app.name.lower() and q not in app.description.lower() and q not in app.id:
                continue
            state = self._state(app)
            label, tone = PILLS.get(state, ("", ""))
            badge = self._badge_for(app)
            color = app.color if catalog._COLOR_RE.match(app.color or "") else "#56606E"
            kind = (app.launch or {}).get("type")
            target = catalog.expand(str(app.launch.get("target", ""))) if kind not in (None, "url", "internal") else ""
            out.append({
                "id": app.id, "name": app.name, "description": app.description,
                "monogram": app.monogram or app.name[:2], "color": color,
                "colorTop": theme.mix("#FFFFFF", color, 0.14), "ink": theme.readable_on(color),
                "state": state, "pill": label, "tone": tone,
                "badge": badge["count"], "badgeHint": badge["hint"],
                "canOpen": state != COMING_SOON,
                "canChange": app.status == "external" or app.source == "local",
                "hasFolder": bool(target) and Path(target).exists(),
                "hasLog": (paths.log_dir() / f"{app.id}.log").exists(),
            })
        return out

    def total(self) -> int:
        return len(self.apps)

    def _find(self, app_id: str) -> Optional[App]:
        return next((a for a in self.apps if a.id == app_id), None)

    # ---- starting apps -----------------------------------------------------
    def activate(self, app_id: str, again: bool = False) -> Dict[str, Any]:
        """Open an app. "result" tells the page what happened or what it should ask:
        launched, opened, needs_setup, missing, coming_soon, already_running, uninstall, error.
        """
        app = self._find(app_id)
        if app is None:
            return {"result": "error", "message": f"There is no app {app_id!r}. Reload your tiles (F5)."}
        state = catalog.state(app)
        if state == COMING_SOON:
            return {"result": "coming_soon", "message": f"{app.name} is coming soon."}
        if state == NEEDS_SETUP:
            return {"result": "needs_setup", "message": f"Where is {app.name}? Choose the file you use to start it."}
        if state == MISSING:
            target = catalog.expand(str(app.launch.get("target", "")))
            return {"result": "missing", "message": f"Asgard couldn't find:\n{target}\n\nChoose where {app.name} is now?"}
        try:
            spec = catalog.build_spec(app)
        except CatalogError as exc:
            return {"result": "error", "message": str(exc)}
        if spec.kind == "internal":
            if spec.target == "uninstall":
                return {"result": "uninstall", "message": ""}
            return {"result": "error", "message": f"Unknown internal command: {spec.target}"}
        if spec.kind == "url":
            webbrowser.open(spec.target)
            return {"result": "opened", "message": f"Opened {app.name} in your browser."}
        if spec.kind == "shell":
            try:
                winutil.open_path(spec.target)
            except OSError as exc:
                return {"result": "error", "message": f"Couldn't open {app.name}. {exc}"}
            return {"result": "opened", "message": f"Opening {app.name}..."}
        if self.runner.is_running(app.id) and not again:
            return {"result": "already_running", "message": f"{app.name} is already open. Open another window?"}
        try:
            self.runner.start(app.id, app.name, spec)
        except LaunchError as exc:
            return {"result": "error", "message": f"Couldn't open {app.name}. {exc}"}
        return {"result": "launched", "message": f"Opening {app.name}..."}

    def set_up(self, app_id: str, path: str) -> Dict[str, Any]:
        """Remember where an app lives, then open it if that works."""
        app = self._find(app_id)
        if app is None:
            return {"result": "error", "message": f"There is no app {app_id!r}."}
        if not path:
            return {"result": "cancelled", "message": ""}
        try:
            catalog.save_launch_override(app.id, catalog.infer_launch(path), self.local)
        except (CatalogError, OSError) as exc:
            return {"result": "error", "message": f"Couldn't save the location. {exc}"}
        self.reload()
        updated = self._find(app_id)
        if updated is not None and catalog.state(updated) == READY:
            return self.activate(app_id)
        return {"result": "saved", "message": f"Saved where {app.name} is."}

    def poll(self) -> Dict[str, Any]:
        """Call about once a second. Returns {"changed": bool, "problems": [messages]}."""
        problems: List[str] = []
        finished = self.runner.poll()
        for done in finished:
            if done.crashed_early:
                tail = log_tail(done.log_path)
                text = f"{done.name} closed right after starting (exit code {done.returncode})."
                problems.append(text + (f"\n\nLast lines of its log:\n{tail}" if tail else ""))
        running = self.runner.running_ids()
        changed = bool(finished) or running != self._last_running
        self._last_running = running
        return {"changed": changed, "problems": problems}

    # ---- folders and files -------------------------------------------------
    def open_location(self, app_id: str) -> Dict[str, Any]:
        app = self._find(app_id)
        target = catalog.expand(str((app.launch or {}).get("target", ""))) if app else ""
        if not target or not Path(target).exists():
            return {"error": "That app's folder isn't known."}
        winutil.open_path(Path(target).parent)
        return {"message": "Opened its folder."}

    def view_log(self, app_id: str) -> Dict[str, Any]:
        log_file = paths.log_dir() / f"{app_id}.log"
        if not log_file.exists():
            return {"error": "That app hasn't written a log yet."}
        winutil.open_in_editor(log_file)
        return {"message": "Opened its log."}

    def edit_tiles(self) -> Dict[str, Any]:
        try:
            winutil.open_in_editor(catalog.ensure_local_manifest(self.local))
        except OSError as exc:
            return {"error": str(exc)}
        return {"message": "Save the file, then reload your tiles (F5)."}

    def open_data_folder(self) -> Dict[str, Any]:
        winutil.open_path(paths.data_dir())
        return {"message": "Opened Asgard's folder."}

    # ---- Muninn ------------------------------------------------------------
    def start_muninn(self) -> None:
        """Create or upgrade Muninn off the window's thread; a backup can take a moment."""
        if self._thread is not None:
            return
        if muninn is None:
            self._error = "Its files are missing from this install. Run setup again."
            return

        def work() -> None:
            try:
                self._result["status"] = muninn.prepare()
            except Exception as exc:  # reported once the page picks up the result
                self._result["error"] = exc
                self._result["trace"] = traceback.format_exc()

        self._thread = threading.Thread(target=work, name="muninn-prepare", daemon=True)
        self._thread.start()

    def muninn_state(self) -> Dict[str, Any]:
        """{"state": starting | ready | error | missing, "message": str, "notice": str, "damaged": bool}."""
        if muninn is None:
            return {"state": "missing", "message": self._error or "Muninn's files are missing.", "notice": "",
                    "damaged": False}
        if self._thread is not None and self._thread.is_alive():
            return {"state": "starting", "message": "Starting Muninn...", "notice": "", "damaged": False}
        if self._thread is None:
            return {"state": "starting", "message": "Muninn hasn't started yet.", "notice": "", "damaged": False}
        if "error" in self._result:
            exc = self._result["error"]
            self._error = str(exc)
            if not self._result.get("logged"):
                self._result["logged"] = True
                write_log(f"Muninn could not start: {self._result.get('trace', self._error)}")
            return {"state": "error", "message": self._error, "damaged": isinstance(exc, muninn.CorruptError),
                    "notice": "Muninn isn't available, so tiles show no counts. Details are in About."}
        self._status = self._result.get("status")
        notice = ""
        if not self._result.get("announced"):
            self._result["announced"] = True
            for warning in getattr(self._status, "warnings", None) or []:
                write_log(f"Muninn: {warning}")
            if self._status is not None and self._status.migrated and not self._status.created:
                notice = f"Muninn was updated to version {self._status.version}."
            elif getattr(self._status, "warnings", None):
                notice = self._status.warnings[0]
        version = getattr(self._status, "version", "")
        return {"state": "ready", "message": f"Muninn {version}".strip(), "notice": notice, "damaged": False}

    def refresh_badges(self, force: bool = False) -> bool:
        """Re-read the tile counts at most once a minute (or when forced). True if they changed."""
        if muninn is None or self._status is None:
            return False
        if not force and time.monotonic() - self._last_badges < BADGE_REFRESH_SECONDS:
            return False
        try:
            fresh = muninn.tile_badges(self._status.path)
        except Exception:  # counts are a nicety; never stop the page
            log.exception("reading Muninn's tile counts")
            return False
        self._last_badges = time.monotonic()
        if fresh != self.badges:
            self.badges = fresh
            return True
        return False

    def backup(self) -> Dict[str, Any]:
        if muninn is None or self._status is None:
            return {"error": "Muninn isn't ready yet."}
        try:
            con = muninn.connect(self._status.path)
            try:
                target = muninn.backup(con, label="manual", keep=5, replace=True)
            finally:
                con.close()
        except Exception as exc:  # shown to the person; the details go to the log
            write_log(traceback.format_exc())
            return {"error": f"Couldn't back up Muninn. {exc}"}
        return {"message": f"Backed up Muninn to {target.name} in the backups folder."}

    # ---- about and uninstall -----------------------------------------------
    def about(self) -> str:
        import sqlite3
        state = self.muninn_state()
        if state["state"] == "ready" and self._status is not None:
            db = f"Muninn: version {self._status.version}, {self._status.path}"
        elif state["state"] in ("error", "missing"):
            db = f"Muninn: not available. {state['message']}"
        else:
            db = "Muninn: starting"
        return (f"Asgard {__version__}\n\nPython {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}\n"
                f"{sys.executable}\n\nCode: {paths.CODE_ROOT}\nData: {paths.data_dir()}\n{db}")

    def uninstall_info(self) -> Dict[str, Any]:
        from .. import valhalla
        if not valhalla.is_installed():
            return {"installed": False,
                    "message": "Asgard isn't installed for your account, so there's nothing to remove.\n\n"
                               f"This copy is running from:\n{paths.CODE_ROOT}"}
        return {"installed": True,
                "message": "Valhalla removes Asgard, its Start menu shortcut and its Settings > Apps entry.\n\n"
                           "Apps that live elsewhere, such as Odin, aren't touched."}

    def uninstall(self, purge: bool = False) -> Dict[str, Any]:
        from .. import valhalla
        try:
            res = valhalla.uninstall(purge=bool(purge))
        except Exception as exc:  # last resort: say what happened, keep the details in the log
            write_log(traceback.format_exc())
            return {"error": f"Couldn't uninstall Asgard. {exc}"}
        return {"ok": bool(res.ok), "message": valhalla.summary(res)}

    def close(self) -> None:
        self.runner.close()
