# Modules Asgard uses

Asgard can use Python packages when they're declared and pinned ([docs/dependency-policy.md](docs/dependency-policy.md)). Every package in `requirements.txt` has a section here, saying where it's used, what happens without it, and what to use instead if it isn't available on-prem. `tests/test_dependencies.py` fails if a declared package has no section, if a section names a package that isn't declared, or if a file that imports the package isn't listed under **Used in**.

What always stays standard library: `asgard/muninn/` (every app and Odin import it) and the launcher's start-up path (`paths`, `launcher`, `catalog`, `install`, `valhalla`, `runner`, `winutil`), so setup and the launcher work before any package is installed.

## Packages

### playwright

| | |
| --- | --- |
| Pin | `playwright==1.49.1` |
| Used in | `apps/heimdall/heimdall/browser.py` (only when `heimdall fill` runs; imported inside functions) |
| Needed for | Driving the installed Edge to fill the SeCcHm form. Every other Heimdall command, and Heimdall's window, works without it |
| Native code | Yes: it runs a bundled `node.exe`. AppLocker blocks it in a user-profile path, so IT installs it in an allowed folder. With `channel="msedge"` it uses the installed Edge and downloads no browser |
| Approval | Pending |
| If it's missing | `fill` stops with a message saying to ask IT for it. `fill --dry-run` (and **Dry run** in the window) still lists every value to type by hand |
| Stdlib alternative | No standard-library module can drive a browser. The fallback is fill-assist: `webbrowser.open()` on the catalog item's address plus the dry-run list to copy from (`webbrowser` is stdlib). Slower, but needs nothing installed |
| Package alternatives | `selenium` with `msedgedriver` (pure Python, but the driver is an `.exe` and it needs the same `RemoteDebuggingAllowed` Edge policy); `pywinauto` driving Edge through Windows UI Automation (pure Python plus `comtypes`; no debugging port, but fragile against web pages); Power Automate for desktop, if the tenant allows it |

### PySide6-Essentials

| | |
| --- | --- |
| Pin | `PySide6-Essentials==6.10.3` (the last release that supports Python 3.9). It brings `shiboken6` at the same version |
| Used in | `asgard/ui/shell.py` (the shared window), and the QML it loads: `asgard/ui/qml/` and `apps/*/ui/*.qml` |
| Needed for | Asgard's shared window: Heimdall's tile, `python -m asgard.ui`. The launcher, Muninn and every command line work without it |
| Native code | Yes: Qt DLLs and `.pyd` files. App Control must allow them |
| Approval | Approved by Brandon, Oct 2026 (in use on-prem) |
| If it's missing | Heimdall's tile shows a message saying what to install (`asgard.ui.run`); the `heimdall` commands still do everything the window does |
| Stdlib alternative | `tkinter`, which the launcher and Baldur's window already use. `asgard/ui/theme.py`, `prefs.py` and `registry.py` are standard library, so a tkinter shell could reuse the tokens, preferences and manifests; the QML pages would be rewritten as Tk widgets, and each app's backend (plain Python) stays as it is |
| Package alternatives | `PyQt6` (and `PyQt6-Qt6`): the same Qt, so the QML files work unchanged; `shell.py` needs its imports renamed (`Signal` to `pyqtSignal`, `Slot` to `pyqtSlot`, `Property` to `pyqtProperty`). GPL or a commercial licence, which IT may weigh differently from PySide6's LGPL. The full `PySide6` package works too; it's larger |

## Dev only (never shipped)

These come from the repo's `requirements-dev.txt` and are never needed to run Asgard: `pytest`, `pytest-timeout`, `pytest-cov` (the plain `python -m unittest discover -s tests` runs the same tests), `ruff` (lint). `vermin` (Python-version check) is installed by hand. Asgard's tests use only `unittest`; the Playwright and PySide6 tests skip when those aren't installed.

## Adding a package

1. Pin it in `requirements.txt` with a comment: what it's for, why the standard library isn't enough, `native` if it has compiled code, and its approval status.
2. Import it only in the module that needs it, and say what to install when it's missing.
3. Add a section above with every row filled in, including at least one alternative for when it isn't available on-prem.
