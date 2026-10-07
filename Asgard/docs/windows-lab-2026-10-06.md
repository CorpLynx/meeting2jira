# Windows lab results (2026-10-06, Asgard 0.3.1)

Asgard ran on a throwaway Windows Server 2022 host (Python 3.12.10, SQLite 3.49.1, Tcl/Tk 8.6.15,
Git for Windows 2.56.0, console code page 437) through `tools/windows_checks.py`, driven by
`infra/windows-test-vm/run-checks.sh asgard`. The last run: **30 checks, 0 failed**. The host has
been destroyed; the tooling to rebuild it is in `infra/windows-test-vm/`.

## What ran

| Part | Result |
| --- | --- |
| Facts: Python 3.9+, SQLite 3.37+ with FTS5 and JSON, Tk opens a window, git | pass |
| The whole suite (223 tests) in UTC, Eastern, India, Line Islands (UTC+14) and Newfoundland (UTC-3:30) | pass in all five |
| As a standard (non-admin) account: `setup-Asgard.cmd` from a folder with spaces, Start menu shortcut, Settings > Apps entry | pass |
| Muninn created at schema v2 by the launcher's own call | pass |
| `baldur.cmd` setup, collect, estimate, days, approve, report in a code page 1252 pipe, repository in a folder with spaces | pass; the worked example gives PROJ-42 1h30m and PROJ-51 30m, sessions 09:20-12:25 and 14:00-15:05 |
| `baldur.cmd schedule` adds the weekly task (pythonw, `collect --quiet`, interactive only), `--remove` deletes it | pass |
| GitHub token saved in, read from and removed from Windows Credential Manager | pass |
| Quiet uninstall from the Settings > Apps string removes the app, shortcut and registry entry; `--purge` leaves no Asgard folder | pass |

Skipped in the suite: 13 tests, 11 that need Playwright (optional, Heimdall's `fill`), one that needs
`time.tzset`, and the schema-check wrapper (both need a settable time zone). The Baldur window, desk
and GitHub tests (18) all ran and passed under Windows Tk.

## What the lab found (all fixed)

1. A test fed git a commit through a text pipe; Windows turned `\n` into `\r\n` and git refused it.
2. `Valhalla --purge` left `settings\` behind, so the data folder stayed and "Kept your data" was wrong.
3. Four backups at once could fail on Windows when `os.replace` met a file another process had just
   written (`PermissionError`). It now retries briefly; the race test runs four rounds.
4. (Lab only) the standard account couldn't run a scheduled task that stores its password, because
   Windows Server grants batch logon to administrators only. The bootstrap now grants that one right.
   Baldur's own task is interactive-only and needs no such right.

## What it does not prove

- The agency's Python build, AppLocker or App Control, Constrained Language Mode, TLS inspection or
  PAC proxies, PIV sign-in. The lab is an unmanaged Server image.
- Anything seen on screen. SSM runs non-interactively: Tk opens and the tests drive the window with
  message boxes patched, but nobody looked at the window, the Start menu entry or the tile.
- Edge, Outlook, Teams, a real GitHub Enterprise Server, Jira.
- The notification-area alerts (not built).

These still need Brandon's trial on the real machine (`HANDOFF.md`, "What's next" 1).
