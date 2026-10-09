# Modules Odin uses

Odin can use Python packages when they're declared and pinned (`../Asgard/docs/dependency-policy.md`). Every package in Odin's requirements files has a section here, saying where it's used, what happens without it, and what to use instead if it isn't available on-prem. `app/tests/test_guardrails.py` fails if a declared package has no section, if a section names a package nothing declares, or if a file that imports the package isn't listed under **Used in**.

| Requirements file | Packages |
| --- | --- |
| `app/requirements.txt` | None. The daily run (`meeting2jira`) needs only Python |
| `graph-app/requirements.txt` | `msal` |
| `playwright-app/requirements.txt` | `playwright` |

## Packages

### msal

| | |
| --- | --- |
| Pin | `msal==1.31.1` (`graph-app/requirements.txt`) |
| Used in | `graph-app/graph/auth.py` (imported inside one function) |
| Needed for | Signing in to Microsoft Graph for Path D, the planned calendar source. Blocked on an Entra app registration, not on code |
| Native code | No, pure Python. The `msal[broker]` extra (Windows sign-in broker) adds `pymsalruntime`, which is native and deliberately not pinned |
| Approval | Pending |
| If it's missing | `graph-app` stops with a message saying how to install it. Odin's daily run is unaffected: Path A (Outlook COM) and Path B (CSV) need no packages |
| Stdlib alternative | OAuth 2.0 authorization code with PKCE by hand: `urllib.request`, `http.server` for the loopback redirect, `secrets`, `hashlib`, `base64`, `json`, and the existing ctypes DPAPI code in `credstore.py` for the token cache. Possible, but it's the part of OAuth where hand-written code goes subtly wrong (state, nonce, refresh), which is why msal was chosen |
| Package alternatives | `azure-identity` (`InteractiveBrowserCredential`); larger, and it depends on msal anyway. For calendar data without Graph, stay on Path A or B |

### playwright

| | |
| --- | --- |
| Pin | `playwright==1.49.1` (`playwright-app/requirements.txt`) |
| Used in | `playwright-app/export_owa.py` (imported inside `main`) |
| Needed for | Path C: reading the calendar from Outlook on the web for "new Outlook", which has no COM. Dormant; superseded by Graph |
| Native code | Yes: a bundled `node.exe`, which AppLocker may block in a user-profile path. `playwright install msedge` uses the installed Edge |
| Approval | Pending |
| If it's missing | `export_owa.py` stops with install instructions. Use Path A or B instead |
| Stdlib alternative | Path B: export the calendar to CSV from Outlook and run `meeting2jira push --csv`, which is standard library end to end. `owa/mapping.py` is already standard library, so OWA JSON saved by hand from the browser's developer tools maps without Playwright |
| Package alternatives | `selenium` with `msedgedriver` (same Edge debugging policy); Microsoft Graph through `graph-app` (Path D), which is the intended replacement |

## Deliberately standard library

Odin's daily run replaces the usual packages with standard-library modules, which is why it needs no approvals today:

| Common package | What Odin uses instead | Where |
| --- | --- | --- |
| `requests` | `urllib.request` with `ssl.create_default_context()`, which trusts the Windows certificate store (so TLS inspection works) | `app/src/meeting2jira/jira.py` |
| `keyring`, `pywin32` | `ctypes` calls to DPAPI | `app/src/meeting2jira/credstore.py` |
| `python-dateutil`, `pytz` | `datetime` with UTC, and wall-clock minutes for the tour of duty (no time-zone database needed) | `models.py`, `rules.py`, `config.py` |
| SQLAlchemy | `sqlite3` | `state.py` |
| A GUI toolkit (Qt, wx) | `tkinter` | `gui/` |

## Dev only (never shipped)

`pytest`, `pytest-timeout`, `pytest-cov` and `ruff` come from the repo's `requirements-dev.txt` (`vermin` is optional, installed by hand). Odin's shipped tests are plain `unittest`, so they run on the workstation with nothing installed.

## Adding a package

1. Pin it in the deliverable's `requirements.txt` with a comment: what it's for, why the standard library isn't enough, `native` if it has compiled code, and its approval status.
2. Import it only where it's needed, and say what to install when it's missing. The daily run must keep working without anything a scheduled task can't rely on.
3. Add a section above with every row filled in, including at least one alternative for when it isn't available on-prem.
