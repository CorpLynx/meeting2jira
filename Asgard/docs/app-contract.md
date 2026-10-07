# The Asgard app contract

Oct 6, 2026 · what every app in Asgard provides and may rely on

An app that meets this contract can be installed, launched, scheduled, diagnosed, updated and uninstalled by Asgard without app-specific code in the launcher. Items marked *(planned)* depend on the core modules in [platform-consolidation.md](platform-consolidation.md); until they exist, follow the pattern Baldur uses today.

## Identity and layout

| Item | Rule |
| --- | --- |
| Id | Lower case, one word, a member of `muninn.sync.APPS` if it touches Muninn (`odin`, `baldur`, `loki`, …) |
| Code | `apps/<id>/` with a package `apps/<id>/<id>/`; nothing outside its folder except calls into `asgard.*` |
| Entry points | `apps/<id>/<id>.pyw` (window, no console), `apps/<id>/cli.py` (console), `apps/<id>/<id>.cmd` (tries `py -3`, `py`, `python`) |
| Catalog entry | `asgard/apps.json`: id, name, tagline, status, `target` (`{app}/apps/<id>/<id>.pyw`); *(planned)* the full manifest: version, `muninn.supported`, `user_data`, `scheduled`, `doctor`, `requires` |
| Data | Only under `asgard.paths.data_dir()`: `settings\<id>.json`, `logs\<id>.log`, and a folder `<id>\` for anything else. Never in the app folder, which updates replace wholesale |

## Behaviour

| Item | Rule |
| --- | --- |
| Muninn | Follow [integration/README.md](integration/README.md): `open_app(id, supported=...)`, write only your tables through `asgard.muninn`, never hold a transaction across a network call, git call or UI wait |
| Exit codes (CLI) | 0 ok · 1 ran but found problems (a check failed, some items failed) · 2 can't run (config, credentials, Muninn not ready or wrong version) · 130 interrupted |
| Messages | Say what happened and what to do next, in plain words, with numbers and units. No stack traces on screen; they go to the log |
| Logs | `logs\<id>.log`, rotated at 1 MB, one old copy kept; *(planned)* through `asgard.applog`, which scrubs credentials |
| Settings | One JSON file, `settings\<id>.json`; keys starting `_` are comments; unknown keys warn, never crash; written atomically *(planned: `asgard.jsonfile`)* |
| Secrets | Windows Credential Manager *(planned: `asgard.secrets`)*; an environment override only for tests; never in settings, Muninn, logs or events |
| Network | HTTPS only, TLS always verified, the Windows store trusted, `ca_bundle` and proxy honoured *(planned: `asgard.http`)* |
| Leaving Asgard | Anything sent to another system needs a person's approval first and uses the outgoing protocol (sending row, marker, settle) |
| Windows | Per user, no admin, HKCU only; child processes with no console window; per-user scheduled tasks only through Asgard *(planned: `asgard.scheduler`)* |
| UI | tkinter; network and git work on a worker thread polled with `after()`; Asgard's palette, fonts and DPI handling *(planned: `asgard.ui`)*, not imported from `launcher.py` |
| Updates | Never self-update or download code ([updates.md](updates.md)) |
| Dependencies | Declared and pinned ([dependency-policy.md](dependency-policy.md)); optional imports where possible |

## Definition of done for an app change

1. Tests: unittest style, `ASGARD_HOME` pointed at a temporary folder, no network (fakes in `tests/`), Muninn opened through `open_app` so the guard is on, `muninn.integrity.check()` clean at the end of each Muninn scenario. Tk tests patch `messagebox`.
2. `python tools/run_tests.py` green; `python Asgard/tools/check_muninn_schema.py` green when the schema changed; ruff clean; `vermin -t=3.9-` clean.
3. Its page in `docs/integration/` and the README updated.
4. A change to estimates, approvals or posting has an independent review recorded in `docs/`.
5. Anything only Windows can prove (Credential Manager, Task Scheduler, file locking, real Edge) is marked "needs Windows verification" and added to the HANDOFF checklist until it has run on Windows.
