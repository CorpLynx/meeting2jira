"""The Qt side of Asgard's desktop window. Thin on purpose: the logic it shows lives in each app's
plain-Python backend, and the look lives in theme.py and the AsgardUI QML module.

Objects the QML sees (root context properties):
    theme       QQmlPropertyMap of tokens for the page on screen (theme.accent, theme.surface, ...)
    themeCtl    mode, per-app accents for the sidebar, apply()
    shell       title, subtitle, version, toast()
    navigation  the sidebar items, the page on screen, bridgeFor(app)
    dashboard   cards from each app's backend.dashboard() and Muninn's tile counts
    prefs       Settings page: mode, per-app accents, text size, settings files

Rules it keeps:
- Backends never see Qt. A Bridge calls them and turns results into plain dicts and lists; an
  expected problem (any ValueError, which every Asgard *Error is) comes back as {"error": text}
  for the page to show, anything else is logged with its traceback and summarised.
- Slow work runs off the UI thread: callAsync() on the thread pool, and long jobs (Heimdall's
  fill, which drives Edge) as a child process through spawn(), so the window never freezes and
  Playwright never loads into it.
- Nothing here holds a Muninn transaction: dashboard reads go through tile_badges(), read-only.
"""
from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import (Property, QCoreApplication, QEvent, QObject, QProcess, QRunnable, QThreadPool, QUrl,
                            Signal, Slot)
from PySide6.QtGui import QDesktopServices, QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine, QQmlPropertyMap
from PySide6.QtQuickControls2 import QQuickStyle

from asgard import paths, winutil

from . import prefs as prefs_mod
from . import registry, theme

log = logging.getLogger("asgard.ui")

HERE = Path(__file__).resolve().parent
QML_DIR = HERE / "qml"
ICON = HERE.parent / "asgard.png"
SUITE_ID = "asgard"


def _version() -> str:
    try:
        return (HERE.parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def plain(value: Any) -> Any:
    """Something QML can take: dicts, lists, str, numbers, bools, None."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [plain(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "to_json"):
        return plain(value.to_json())
    return str(value)


def _failure(where: str, exc: BaseException) -> Dict[str, Any]:
    if isinstance(exc, ValueError):
        return {"error": str(exc)}
    log.error("%s failed:\n%s", where, "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    return {"error": f"{where} hit an unexpected problem ({type(exc).__name__}). Details are in "
                     f"{paths.log_dir()}."}


class _Task(QRunnable):
    def __init__(self, fn) -> None:
        super().__init__()
        self.fn = fn

    def run(self) -> None:
        try:
            self.fn()
        except RuntimeError:
            pass   # the window closed while this ran; its result has nowhere to go


# ------------------------------------------------------------------ theme


class ThemeController(QObject):
    changed = Signal()

    def __init__(self, apps: List[registry.AppUI], prefs: prefs_mod.Prefs, home_app: str) -> None:
        super().__init__()
        self.apps = {a.id: a for a in apps}
        self.prefs = prefs
        self.home_app = home_app            # whose colours Dashboard and Settings wear
        self.current_app = home_app
        self.tokens = QQmlPropertyMap(self)
        self._accents: Dict[str, str] = {}
        self.warnings: List[str] = []
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda *_: self.apply())
        self.apply()

    def mode(self) -> str:
        if self.prefs.mode in theme.MODES:
            return self.prefs.mode
        try:
            from PySide6.QtCore import Qt
            dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
        except AttributeError:           # Qt before 6.5 can't tell; light is the safe default
            dark = False
        return "dark" if dark else "light"

    def layers(self, app_id: str) -> List[Any]:
        app = self.apps.get(app_id)
        out: List[Any] = []
        if app is not None:
            out.append((f"apps/{app_id}/ui/manifest.json", app.theme))
        out.append(("ui.json theme", self.prefs.theme))
        if app_id:
            out.append((f'ui.json apps "{app_id}"', self.prefs.app_theme(app_id)))
        return out

    def resolve(self, app_id: str):
        return theme.resolve(self.mode(), *self.layers(app_id))

    def apply(self) -> None:
        tokens, warnings = self.resolve(self.current_app)
        for key, value in tokens.items():
            self.tokens.insert(key, value)
        self._accents = {app_id: self.resolve(app_id)[0]["accentInk"] for app_id in self.apps}
        self.warnings = sorted(set(warnings))
        self.changed.emit()

    def set_app(self, app_id: str) -> None:
        app_id = app_id or self.home_app
        if app_id != self.current_app:
            self.current_app = app_id
            self.apply()

    def _get_accents(self) -> Dict[str, str]:
        return dict(self._accents)

    def _get_mode(self) -> str:
        return self.mode()

    appAccents = Property("QVariantMap", _get_accents, notify=changed)
    resolvedMode = Property(str, _get_mode, notify=changed)


# ------------------------------------------------------------------ shell and bridge


class ShellController(QObject):
    toastRequested = Signal(str, str)

    def __init__(self, title: str, subtitle: str) -> None:
        super().__init__()
        self._title, self._subtitle = title, subtitle

    @Slot(str, str)
    def toast(self, text: str, kind: str = "info") -> None:
        if kind == "error":
            log.warning("%s", text)
        self.toastRequested.emit(text, kind or "info")

    @Slot(str, result=bool)
    def openPath(self, path: str) -> bool:
        """Open a file or folder the way a double-click would."""
        if not path:
            return False
        target = Path(path)
        if not target.exists():
            self.toast(f"{target} doesn't exist yet.", "error")
            return False
        return QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    @Slot(str, result=bool)
    def openFolder(self, path: str) -> bool:
        target = Path(path)
        return self.openPath(str(target if target.is_dir() else target.parent))

    title = Property(str, lambda self: self._title, constant=True)
    subtitle = Property(str, lambda self: self._subtitle, constant=True)
    version = Property(str, lambda self: _version(), constant=True)
    dataFolder = Property(str, lambda self: str(paths.data_dir()), constant=True)


class Bridge(QObject):
    """One app's backend, as QML sees it."""
    output = Signal(str)
    finished = Signal(int)
    runningChanged = Signal()
    asyncDone = Signal(str, "QVariant")

    def __init__(self, app: registry.AppUI, shell: ShellController) -> None:
        # Parented, so QML never takes ownership (and garbage-collects it) after bridgeFor().
        super().__init__(shell)
        self.app = app
        self.shell = shell
        self._backend: Any = None
        self._loaded = False
        self._proc: Optional[QProcess] = None
        self._partial = ""

    def backend(self) -> Any:
        if not self._loaded:
            self._backend = self.app.load_backend()   # may raise; callers turn it into an error
            self._loaded = True
        return self._backend

    def invoke(self, method: str, args: List[Any]) -> Dict[str, Any]:
        where = f"{self.app.name} {method}"
        if not method or method.startswith("_"):
            return {"error": f"{method!r} isn't something {self.app.name} can do."}
        try:
            target = getattr(self.backend(), method, None)
            if not callable(target):
                return {"error": f"{self.app.name} has no {method!r}."}
            result = plain(target(*args))
        except Exception as exc:  # noqa: BLE001 - every failure becomes a message for the page
            return _failure(where, exc)
        return result if isinstance(result, dict) else {"value": result}

    @Slot(str, "QVariantList", result="QVariant")
    def call(self, method: str, args: List[Any]) -> Dict[str, Any]:
        return self.invoke(method, list(args or []))

    @Slot(str, str, "QVariantList")
    def callAsync(self, token: str, method: str, args: List[Any]) -> None:
        values = list(args or [])
        QThreadPool.globalInstance().start(_Task(lambda: self.asyncDone.emit(token, self.invoke(method, values))))

    @Slot(str, "QVariantList", result=bool)
    def spawn(self, method: str, args: List[Any]) -> bool:
        """Ask the backend for a command line and run it as a child process, streaming its output."""
        if self.running:
            self.shell.toast(f"{self.app.name} is already running something. Wait, or press Stop.", "error")
            return False
        reply = self.invoke(method, list(args or []))
        argv = reply.get("value")
        if reply.get("error") or not isinstance(argv, list) or not argv:
            self.shell.toast(reply.get("error") or f"{self.app.name} {method} gave no command to run.", "error")
            return False
        program = _windowless(str(argv[0]))
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = proc.processEnvironment()
        if env.isEmpty():
            from PySide6.QtCore import QProcessEnvironment
            env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("ASGARD_APP", str(registry.CODE_ROOT))
        proc.setProcessEnvironment(env)
        proc.readyReadStandardOutput.connect(self._read)
        proc.finished.connect(self._done)
        proc.errorOccurred.connect(self._error)
        self._proc = proc
        self._partial = ""
        proc.start(program, [str(a) for a in argv[1:]])
        self.runningChanged.emit()
        return True

    @Slot()
    def stop(self) -> None:
        if self._proc is not None and self._proc.state() != QProcess.ProcessState.NotRunning:
            # The whole tree: a script's children (Odin's daily run is PowerShell running Python)
            # would otherwise carry on after the window says it stopped.
            winutil.kill_tree(int(self._proc.processId()))
            self._proc.kill()

    def _read(self) -> None:
        if self._proc is None:
            return
        text = self._partial + bytes(self._proc.readAllStandardOutput()).decode("utf-8", "replace")
        *lines, self._partial = text.replace("\r\n", "\n").split("\n")
        for line in lines:
            if line.strip():
                self.output.emit(line)

    def _done(self, code: int, _status: Any = None) -> None:
        self._read()
        if self._partial.strip():
            self.output.emit(self._partial)
        self._partial = ""
        self._proc = None
        self.runningChanged.emit()
        self.finished.emit(int(code))

    def _error(self, error: Any) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self.shell.toast(f"{self.app.name} couldn't start its helper ({self._proc.program() if self._proc else '?'}).",
                             "error")
            self._proc = None
            self.runningChanged.emit()
            self.finished.emit(-1)

    running = Property(bool, lambda self: self._proc is not None, notify=runningChanged)
    appId = Property(str, lambda self: self.app.id, constant=True)
    appName = Property(str, lambda self: self.app.name, constant=True)


def _windowless(program: str) -> str:
    """On Windows, run python.exe children as pythonw.exe so no console window flashes up.

    In the packaged build, asgard-cli.exe children run as Asgard.exe, for the same reason.
    """
    path = Path(program)
    if paths.FROZEN:
        console, windowed = paths.frozen_programs()
        return windowed if path.name.lower() == Path(console).name.lower() else program
    if sys.platform == "win32" and path.name.lower() == "python.exe":
        quiet = path.with_name("pythonw.exe")
        if quiet.exists():
            return str(quiet)
    return program


# ------------------------------------------------------------------ navigation


class Navigation(QObject):
    changed = Signal()

    def __init__(self, apps: List[registry.AppUI], bridges: Dict[str, Bridge], themes: ThemeController) -> None:
        super().__init__()
        self.apps = apps
        self.bridges = bridges
        self.themes = themes
        self.pages: Dict[str, Dict[str, Any]] = {
            "dashboard": {"key": "dashboard", "title": "Dashboard", "icon": "Db", "app": "",
                          "source": QUrl.fromLocalFile(str(QML_DIR / "AsgardUI" / "Dashboard.qml"))},
            "settings": {"key": "settings", "title": "Settings", "icon": "St", "app": "",
                         "source": QUrl.fromLocalFile(str(QML_DIR / "AsgardUI" / "Settings.qml"))},
        }
        for app in apps:
            for view in app.views:
                self.pages[view.key] = {"key": view.key, "title": view.title, "icon": view.icon, "app": app.id,
                                        "source": QUrl.fromLocalFile(str(view.qml))}
        self._current = "dashboard"
        self._history: List[str] = []

    def _items(self) -> List[Dict[str, Any]]:
        out = [{"kind": "page", "key": "dashboard", "title": "Dashboard", "icon": "Db", "app": ""}]
        for app in self.apps:
            out.append({"kind": "header", "key": f"{app.id}:", "title": app.name, "icon": "", "app": app.id})
            out.extend({"kind": "page", "key": v.key, "title": v.title, "icon": v.icon, "app": app.id}
                       for v in app.views)
        return out

    @Slot(str, result=bool)
    def go(self, key: str) -> bool:
        if key not in self.pages:
            return False
        if key != self._current:
            self._history.append(self._current)
            self._current = key
            self.themes.set_app(self.pages[key]["app"])
            self.changed.emit()
        return True

    @Slot(result=bool)
    def back(self) -> bool:
        if not self._history:
            return False
        self._current = self._history.pop()
        self.themes.set_app(self.pages[self._current]["app"])
        self.changed.emit()
        return True

    @Slot(str, result=QObject)
    def bridgeFor(self, app_id: str) -> Optional[QObject]:
        return self.bridges.get(app_id)

    items = Property("QVariantList", _items, constant=True)
    currentKey = Property(str, lambda self: self._current, notify=changed)
    currentApp = Property(str, lambda self: self.pages[self._current]["app"], notify=changed)
    currentTitle = Property(str, lambda self: self.pages[self._current]["title"], notify=changed)
    currentSource = Property(QUrl, lambda self: self.pages[self._current]["source"], notify=changed)


# ------------------------------------------------------------------ dashboard


class Dashboard(QObject):
    changed = Signal()
    _arrived = Signal("QVariantList")

    def __init__(self, apps: List[registry.AppUI], bridges: Dict[str, Bridge], single: bool) -> None:
        super().__init__()
        self.apps = apps
        self.bridges = bridges
        self.single = single
        self._groups: List[Dict[str, Any]] = []
        self._loading = False
        self._arrived.connect(self._store)

    @Slot()
    def refresh(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.changed.emit()
        QThreadPool.globalInstance().start(_Task(lambda: self._arrived.emit(self.collect())))

    def collect(self) -> List[Dict[str, Any]]:
        try:
            from asgard.muninn.badges import tile_badges
            badges = tile_badges()
        except Exception:  # noqa: BLE001 - counts are a nicety; never let them stop the dashboard
            log.exception("reading Muninn's tile counts")
            badges = {}
        groups: List[Dict[str, Any]] = []
        for app in self.apps:
            cards: List[Dict[str, Any]] = []
            for b in badges.get(app.id, []):
                cards.append({"label": b.text, "value": str(b.count), "detail": "from Muninn", "target": ""})
            bridge = self.bridges[app.id]
            if app.backend:
                reply = bridge.invoke("dashboard", [])
                if reply.get("error"):
                    cards.append({"label": "Couldn't load", "value": "!", "detail": reply["error"], "target": "",
                                  "tone": "error"})
                else:
                    for card in reply.get("value") or []:
                        target = card.get("view") or ""
                        cards.append({"label": card.get("label", ""), "value": str(card.get("value", "")),
                                      "detail": card.get("detail", ""), "tone": card.get("tone", ""),
                                      "target": f"{app.id}:{target}" if target else ""})
            groups.append({"app": app.id, "name": app.name, "subtitle": app.subtitle, "cards": cards})
        if not self.single:
            known = {a.id for a in self.apps}
            for app_id, items in sorted(badges.items()):
                if app_id not in known and items:
                    groups.append({"app": app_id, "name": app_id.title(), "subtitle": "Counts from Muninn",
                                   "cards": [{"label": b.text, "value": str(b.count), "detail": "", "target": ""}
                                             for b in items]})
        return groups

    def _store(self, groups: List[Dict[str, Any]]) -> None:
        self._groups = list(groups)
        self._loading = False
        self.changed.emit()

    groups = Property("QVariantList", lambda self: self._groups, notify=changed)
    loading = Property(bool, lambda self: self._loading, notify=changed)


# ------------------------------------------------------------------ settings


class PrefsController(QObject):
    changed = Signal()

    def __init__(self, prefs: prefs_mod.Prefs, themes: ThemeController, apps: List[registry.AppUI],
                 bridges: Dict[str, Bridge], shell: ShellController, home_app: str) -> None:
        super().__init__()
        self.prefs = prefs
        self.themes = themes
        self.apps = apps
        self.bridges = bridges
        self.shell = shell
        self.home_app = home_app
        themes.changed.connect(self.changed)

    def _save(self) -> bool:
        try:
            self.prefs.save()
        except (prefs_mod.PrefsError, OSError) as exc:
            self.shell.toast(str(exc), "error")
            return False
        self.themes.apply()
        self.changed.emit()
        return True

    @Slot(str, result=bool)
    def setMode(self, mode: str) -> bool:
        if mode not in prefs_mod.MODE_CHOICES:
            return False
        self.prefs.mode = mode
        return self._save()

    @Slot(str, str, result=str)
    def setAccent(self, app_id: str, value: str) -> str:
        """Returns "" when saved, or the reason it wasn't."""
        value = value.strip()
        if value and not value.startswith("#"):
            value = "#" + value
        problem = theme.check_token("accent", value)
        if problem:
            return problem[0].upper() + problem[1:] + "."
        self.prefs.clear_token(app_id, "accent")
        self.prefs.set_token(app_id, "accent", value.upper())
        return "" if self._save() else "Not saved; see the message."

    @Slot(str, result=bool)
    def resetAccent(self, app_id: str) -> bool:
        self.prefs.clear_token(app_id, "accent")
        return self._save()

    @Slot(int, result=bool)
    def setFontSize(self, size: int) -> bool:
        if theme.check_token("fontSize", size):
            return False
        if size == theme.SIZES["fontSize"]:
            self.prefs.clear_token(None, "fontSize")
        else:
            self.prefs.set_token(None, "fontSize", size)
        return self._save()

    @Slot(str, str, result=str)
    def contrastNote(self, foreground: str, background: str) -> str:
        """A short note when two colours fall below WCAG AA for body text."""
        try:
            ratio = theme.contrast(foreground, background)
        except (ValueError, IndexError):
            return ""
        return "" if ratio >= 4.5 else f"Low contrast ({ratio:.1f}:1); text in this colour is adjusted to stay readable."

    def _appearance(self) -> List[Dict[str, Any]]:
        rows = []
        ids = [("", "Asgard", "Dashboard and Settings, and any app without its own colour")] if \
            self.home_app == SUITE_ID else []
        ids += [(a.id, a.name, a.subtitle) for a in self.apps]
        mode = self.themes.mode()
        for app_id, name, detail in ids:
            app_layer = self.themes.layers(app_id)[:1] if app_id else []
            default = theme.resolve(mode, *app_layer)[0]["accent"]
            current = self.themes.resolve(app_id)[0]["accent"]
            custom = "accent" in (self.prefs.app_theme(app_id) if app_id else self.prefs.theme)
            rows.append({"app": app_id, "name": name, "detail": detail, "accent": current,
                         "default": default, "custom": custom})
        return rows

    def _files(self) -> List[Dict[str, Any]]:
        files = [{"app": "", "label": "UI preferences (colours, mode, text size)", "path": str(self.prefs.path)}]
        for app in self.apps:
            if not app.backend:
                continue
            reply = self.bridges[app.id].invoke("settings_files", [])
            for item in (reply.get("value") or []) if not reply.get("error") else []:
                files.append({"app": app.id, "label": f"{app.name}: {item.get('label', '')}",
                              "path": str(item.get("path", ""))})
        return files

    def _warnings(self) -> List[str]:
        return list(self.prefs.warnings) + list(self.themes.warnings)

    mode = Property(str, lambda self: self.prefs.mode, notify=changed)
    fontSize = Property(int, lambda self: int(self.themes.tokens.value("fontSize") or 13), notify=changed)
    appearance = Property("QVariantList", _appearance, notify=changed)
    files = Property("QVariantList", _files, notify=changed)
    warnings = Property("QVariantList", _warnings, notify=changed)


# ------------------------------------------------------------------ assembly


class Shell:
    """Everything the window needs, built without starting the event loop (tests use this)."""

    def __init__(self, app: Optional[str] = None, root: Optional[Path] = None,
                 prefs_path: Optional[Path] = None) -> None:
        self.apps, self.discovery_warnings = registry.discover(root, only=app)
        if app and not self.apps:
            raise registry.RegistryError(" ".join(self.discovery_warnings))
        single = bool(app)
        home = self.apps[0].id if single else SUITE_ID
        title = self.apps[0].name if single else "Asgard"
        subtitle = (self.apps[0].subtitle or "Asgard") if single else f"{len(self.apps)} apps"
        self.prefs = prefs_mod.load(prefs_path)
        self.shell = ShellController(title, subtitle)
        self.themes = ThemeController(self.apps, self.prefs, home)
        self.bridges = {a.id: Bridge(a, self.shell) for a in self.apps}
        self.navigation = Navigation(self.apps, self.bridges, self.themes)
        self.dashboard = Dashboard(self.apps, self.bridges, single)
        self.prefs_ctl = PrefsController(self.prefs, self.themes, self.apps, self.bridges, self.shell, home)
        self.engine = QQmlApplicationEngine()
        self.qml_warnings: List[str] = []
        self.engine.warnings.connect(self._on_warnings)
        self.engine.addImportPath(str(QML_DIR))
        ctx = self.engine.rootContext()
        for name, obj in (("theme", self.themes.tokens), ("themeCtl", self.themes), ("shell", self.shell),
                          ("navigation", self.navigation), ("dashboard", self.dashboard),
                          ("prefs", self.prefs_ctl)):
            ctx.setContextProperty(name, obj)

    def _on_warnings(self, errors: Any) -> None:
        for e in errors:
            text = e.toString()
            self.qml_warnings.append(text)
            log.warning("QML: %s", text)

    def load(self) -> bool:
        self.engine.load(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
        if not self.engine.rootObjects():
            return False
        for message in self.discovery_warnings + self.prefs.warnings + self.themes.warnings:
            self.shell.toast(message, "warning")
        self.dashboard.refresh()
        return True

    def close(self) -> None:
        """Tear down the QML before the objects it binds to, so closing logs no errors."""
        for bridge in self.bridges.values():
            bridge.stop()
        QThreadPool.globalInstance().waitForDone(5000)
        for obj in self.engine.rootObjects():
            obj.deleteLater()
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def make_app(argv: Optional[List[str]] = None) -> QGuiApplication:
    QQuickStyle.setStyle("Basic")     # the only built-in style that takes every colour from us
    existing = QGuiApplication.instance()
    if existing is not None:
        return existing
    qapp = QGuiApplication(argv or [sys.argv[0]])
    qapp.setApplicationName("Asgard")
    qapp.setOrganizationName("Asgard")
    if ICON.exists():
        qapp.setWindowIcon(QIcon(str(ICON)))
    return qapp


def run(app: Optional[str] = None, argv: Optional[List[str]] = None) -> int:
    _setup_logging()
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
    qapp = make_app(argv)
    try:
        shell = Shell(app=app)
    except registry.RegistryError as exc:
        from . import _tell
        _tell(str(exc))
        return 2
    if not shell.load():
        from . import _tell
        _tell("The window couldn't be built. Details are in the log:\n" + "\n".join(shell.qml_warnings[:5]))
        return 1
    code = qapp.exec()
    shell.close()
    return code
