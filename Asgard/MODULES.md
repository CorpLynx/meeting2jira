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

### mcp

| | |
| --- | --- |
| Pin | `mcp==2.3.0`, the official MCP Python SDK; needs Python 3.10+. It brings `mcp-types` at the same version, and about 30 wheels in all |
| Used in | `apps/ysildir/ysildir/server.py` (the server, and `ysildir.cmd check`'s in-memory client), `apps/ysildir/ysildir/results.py` (`ToolError`) |
| Needed for | Ysildir: the MCP server an AI client (Kiro, Copilot, Claude Code) starts over stdio. Baldur, Muninn and every command line work without it |
| Native code | Yes, through its dependencies: `pydantic-core`, `cryptography`, `cffi` and `rpds-py`, plus `pywin32` on Windows. App Control must allow their DLLs. Its HTTP parts (`starlette`, `uvicorn`, `sse-starlette`) are installed but never started |
| Approval | Pending |
| If it's missing | `ysildir.cmd` says what's missing (the package, or Python 3.10+) and exits 2. Agents still record through `baldur.cmd ai record`, and the review still runs at the clipboard tier (`ai pack`, `ai review`) |
| Stdlib alternative | A JSON-RPC 2.0 server over stdio on the standard library: about 300 lines, Python 3.9+, over the same handlers (`baldur_tools.py`, `muninn_tools.py`, `teach.py`). The Ysildir spec's first revision (commit `d510709`) designs it. It has to track MCP's protocol versions by hand |
| Package alternatives | The newest `mcp` 1.x on the mirror (`from mcp.server.fastmcp import FastMCP`, with the same `add_tool`, decorators, annotations and `ToolError`); `fastmcp` 4.x, the standalone framework, which has more dependencies. Either changes only `server.py` and `results.py` |

### pydantic

| | |
| --- | --- |
| Pin | `pydantic==2.14.0`. `mcp` installs it too; it's pinned because Ysildir imports it |
| Used in | `apps/ysildir/ysildir/models.py` (every tool's arguments and results), `apps/ysildir/ysildir/config.py` (reading `ysildir.json`) |
| Needed for | Ysildir's argument and result models, from which the SDK builds each tool's schemas and checks each call |
| Native code | Yes: `pydantic-core` |
| Approval | Pending |
| If it's missing | The same as for `mcp`, which needs it: `ysildir.cmd` says what's missing, and the CLI and clipboard tier still work |
| Stdlib alternative | `dataclasses` with hand-written checks and JSON Schemas, in the standard-library server |
| Package alternatives | `attrs` with `cattrs` (pure Python), or `msgspec` (native). The MCP SDK doesn't build schemas from either, so both go with the standard-library server |

## Deliberately standard library

Reviewed 2026-10-09 against "the best module for each job". Muninn, Baldur and Baldur's agent hooks import no packages. For each job below that is the best choice, not a leftover of the old rule. Each row says what a package would add, and when it would win.

| Job | Common package | What Asgard uses instead | Where | Why that's the better choice here |
| --- | --- | --- | --- | --- |
| Muninn: schema, rules, agent reports, review checks | `SQLAlchemy`, `pydantic`, `jsonschema` | `sqlite3`, numbered SQL migrations, and hand-written checks with plain-language messages | `asgard/muninn/`, including `baldur.py` (`record_agent_estimate`, `check_review`) | Every app and Odin import Muninn, so a package there would become everyone's. The policy keeps it standard library |
| Reading git | `GitPython`, `pygit2` (native), `dulwich` | The `git` command line, through `gitread.run_git()` | `apps/baldur/baldur/gitread.py` | It is exactly the person's git: their config and `includeIf` identities, worktrees, reflog and `patch-id`. GitPython runs the same command line underneath; pygit2 and dulwich would reimplement parts of it |
| GitHub over HTTPS | `requests` or `httpx`, with `truststore` | `urllib.request` with `ssl.create_default_context()` | `apps/baldur/baldur/github.py` | On Windows the default context trusts the Windows certificate store, so TLS inspection works with nothing installed. `requests` and `httpx` need `truststore` for that |
| The GitHub token | `keyring` (pure Python on Windows) | `ctypes` calls to Credential Manager (`CredReadW`, `CredWriteW`) | `apps/baldur/baldur/github.py` | It's the same store either way, and the ctypes code is small and tested. Switch to `keyring` when a second app needs secrets (`docs/platform-consolidation.md` plans an optional `asgard.secrets`) |
| Local days | `python-dateutil`, `pytz`, `tzdata` | `datetime`, with the machine's local time | `apps/baldur/baldur/estimate.py` | Baldur needs the person's local day, which is the machine's zone (US zones only, decided Oct 9). Windows has no time-zone database without `tzdata`, and nothing here needs one |
| Calibration | `numpy`, `scipy.optimize` (native) | A grid search over 270 settings | `apps/baldur/baldur/calibrate.py` | It's the spec's method. Each point re-runs Baldur's own estimator, which is Python, so numpy wouldn't make it faster or change the answer |
| Settings | `pydantic`, `jsonschema` | `json` and hand-written checks | `apps/baldur/baldur/settings.py` | Messages that name the setting and say what to do; a broken file stops estimating instead of falling back to defaults |
| The command line | `click`, `typer` | `argparse` | `apps/baldur/baldur/cli.py` | Enough for the commands, with ASCII output and nothing to install |
| A chat's JSON reply | `json-repair` | `muninn.baldur.parse_reply`, which strips code fences and refuses anything that isn't JSON | `asgard/muninn/baldur.py` | Muninn stays standard library. A refused reply only means asking the chat again, but a repaired one can quietly change what it says: a cut-off list of adjustments becomes a shorter valid one |
| The agent hooks | — | `json`, `re` and `shlex` | `apps/baldur/agents/hooks/` | They run under whatever Python the agent (Kiro) starts, outside Asgard's install, so they can't count on any package |
| Baldur's window | `PySide6-Essentials` (pinned and approved) | `tkinter` | `apps/baldur/baldur/window.py` | It predates the shared Qt window. Moving it is optional (HANDOFF, "Shared window, decided"); if it moves, `desk.py` is already the plain-Python backend a QML page would call |

## Planned (not pinned yet)

Nothing right now. A package is pinned, and gets its section above, in the change that first imports it: the tests fail on a pin that nothing imports.

## Dev only (never shipped)

These come from the repo's `requirements-dev.txt` and are never needed to run Asgard: `pytest`, `pytest-timeout`, `pytest-cov` (the plain `python -m unittest discover -s tests` runs the same tests), `ruff` (lint), `vermin` (Python-version check) and `actionlint-py` (the workflows), each pinned exactly; CI installs the same file (`.github/workflows/checks.yml`). Asgard's tests use only `unittest`; the Playwright and PySide6 tests skip when those aren't installed.

## Adding a package

1. Pin it in `requirements.txt` with a comment: what it's for, why the standard library isn't enough, `native` if it has compiled code, and its approval status.
2. Import it only in the module that needs it, and say what to install when it's missing.
3. Add a section above with every row filled in, including at least one alternative for when it isn't available on-prem.
