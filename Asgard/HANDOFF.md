# Asgard handoff (2026-10-10, Asgard 0.4.0 in progress, not shipped)

## Latest: Odin is an Asgard app, on Muninn (2026-10-10)

Brandon asked for all of the Muninn design's steps for moving Odin in, with Odin as an Asgard app. Done; [docs/integration/odin.md](docs/integration/odin.md) is the contract.

- **Where it is.** `apps/odin` (package `odin`, was `Odin/app/src/meeting2jira`), with `odin.cmd`, `cli.py`, `odin.pyw`, its window pages in `ui/`, the Windows scripts in `windows/` and `tools/`, and its tests in `tests/test_odin_*.py` with `tests/fake_jira.py` (an in-memory Jira). `Odin/` keeps the optional Graph and OWA exporters (which now hand off to Asgard's `odin.cmd`), the Power Platform material and Odin's own history docs. `gitwork.py` was dropped (Baldur has its own), and so was `Odin/gui`, a broken stub.
- **Muninn v5** (`0005_odin_meetings.sql`, additive, labelled 0.4.0): `meeting_subtasks`, the record of which meetings have sub-tasks, replacing `state.db`'s `synced`. Baldur and Ysildir accept v4 to v5; Odin needs v5.
- **The daily run** (`cli.py daily`, from `Invoke-MeetingSync.ps1` after the Outlook export): the journal and the one-time `state.db` import, settling posts nobody saw finish, the calendar into `calendar_events`, meetings to sub-tasks (recorded at once; a record Muninn refuses goes to `unrecorded.jsonl` and the run stops creating), meeting worklogs through Muninn's posting protocol, Jira into Muninn (issues, tracked parents, key lookups, refreshes, deletion checks, worklogs), then approved Baldur days, only after that run's worklog sync. `push`, `sync` and `post` run parts; `report` writes a CSV for Power BI.
- **Decisions made on the way** (each in odin.md):
  - Worklogs are read by issue (`worklogAuthor = currentUser()`) plus `/worklog/deleted`, not `/worklog/updated`, which lists every worklog in the instance.
  - A refusal is only a 4xx; a 500 on a write may have landed, so it is settled by its marker like a timeout.
  - Meeting time is retried for 14 days and up to three refusals; time already on the sub-task counts.
  - Private items go into Muninn with their times only. Each export path is its own calendar source, so a full export from one never deletes another's items.
  - `odin.lock` keeps runs apart. It is an OS file lock beside Muninn, released when its run ends however it ends, so nothing is ever taken over (review #7).
  - A per-run limit never stops inside a burst of updates, so a bulk edit can't stall a cursor.
  - A run that stops early, for any reason, writes `last_run.json` and the Desktop alert too.
  - Before every create Odin looks in Jira for the meeting's label on issues you created, so a sub-task Jira made but Odin never recorded is found, not made again (review #2). Approved days wait while the worklog sync is still catching up, and each issue's worklogs are read again just before its time goes (review #1).
  - The scheduled task is "Asgard Odin daily" (the old `meeting2jira-daily` is removed), allowed 45 minutes for the first year-long sync. Valhalla removes Baldur's and Odin's tasks.
- **As an Asgard app.** The tile is available and opens Odin's pages in the shared window (Today, Meetings, My issues); Today takes the token and runs the daily run, a sync or posting as a child process. Setup retires a tile setting that opened a copy of Odin from before. Odin runs on Asgard's Python: the packaged build's `asgard-cli.exe`, else the ledger's Python, else one with SQLite 3.37 and FTS5. Its scripts are ASCII with CRLF like Asgard's. The packaged build's self-test loads Odin, and the build runs its dry-run push (checked with a local build).
- **Packages.** Brandon asked whether packages such as `jira` would simplify Odin. Reviewed and not used yet ([docs/dependency-policy.md](docs/dependency-policy.md#considered-for-odin-and-not-used-oct-10-2026)): `jira`'s session retries POSTs on a 503 or a dropped connection, which would make a sub-task or a worklog twice, and it brings 11 packages the Python install may not have. The window is where Odin uses a package (PySide6).
- **The independent review** ([docs/review-2026-10-10-odin.md](docs/review-2026-10-10-odin.md)) confirmed 2 blockers (a sync stopped at its limit let Odin post time Jira already held; a sub-task Jira made but Odin never recorded was made again), 3 should-fix and 5 minor findings. All are fixed with regression tests, and the reviewer's three scripts pass. Verifying on Python 3.10 found one more: Jira's `+0000` offsets didn't parse before 3.11 (fixed).
- **Verified with Odin in.** 705 Asgard tests (190 of them Odin's). Python 3.11 with the MCP SDK and PySide6: 674 pass, 31 skip. Without them: 632 pass, 73 skip; the repo-root runner agrees (632). Python 3.10, 3.12 and 3.13 pass too. Also clean: the schema check (204 checks), vermin for 3.9, ruff, both exporters' suites, and the packaged build's local run with the fixes (self-test, Muninn v5, Baldur, Odin's dry-run push, setup in place). Windows CI failed on `e04daff`: 16 tests that run `main()` left `odin.log` open, so Windows couldn't delete their temporary folder. `OdinTestCase` now closes Odin's log handlers first, and the whole suite passes under an emulation of that Windows rule (deleting a folder fails while a file in it is open), which fails 20 tests without the fix.
- **Still to do, on the workstation:** a real daily run against Jira Data Center (the first sync's length, `/worklog/deleted` on that Jira version), the Outlook export under the new script paths, and the window at real DPI.

## Before that: branch `claude/baldur-estimation` (2026-10-09, merged with main at 03d276f)

Baldur's estimation is finished, in two methods side by side (`docs/baldur-spec.md`, "Two ways to estimate"). Showing it in a window comes later, with the GUI work (the shared Qt window, below).

- **The manual engine (no AI).**
  - **Calibration is built** (`calibrate.py`; CLI `actual`, `actuals`, `calibrate [--accept]`). It is the grid search over gap, lead-in and ambient weight, keeping the lowest daily error among settings that estimate low on average.
  - **A search-index repair fix.** SQLite 3.45 can't rebuild an emptied search index, because the table won't open; `integrity.repair_search` now puts back an empty index's own records first.
- **The AI-assisted method.**
  - **Agent estimates.** Muninn **schema v4** (`0004_agent_estimates.sql`, owned by Baldur) keeps what an AI coding agent says your time on a change was. It is recorded through `asgard.muninn.baldur.record_agent_estimate()` (CLI `ai record`, or Ysildir's `baldur_record_estimate`).
  - **Checking.** `assist.py` turns the reports, or an AI review pasted back at the clipboard tier (`ai pack`, `ai review`), into suggestions. They are checked by `muninn.baldur.check_review`: they never raise a day, every one cites evidence, they never add a ticket, they never move time Jira already holds, and they round down.
  - **Taking them.** `approve --date D --ai ID` takes the suggestions you were shown (the id names them; `ai show` prints the command). Odin's worklog comment gets the `Reviewed:` line `ai show` showed, word for word.
  - **`review_mode=content` is refused** (rule 6).
- **Agent files.** `apps/baldur/prompts/agent-guide.md` is the guide (`ai guide`). `apps/baldur/agents/` holds the Kiro steering, a guard hook that blocks approvals and settings changes, and an after-commit reminder. `ai kiro --into REPO` installs them.
- **Ysildir is built** (`apps/ysildir/`, `docs/integration/ysildir.md`), from the Kiro spec in `.kiro/specs/ysildir-mcp/`. It is the MCP server that teaches agents Baldur and Muninn (instructions, `asgard_guide`, resources, four prompts), takes their estimates and runs the day's review at the MCP tier through Baldur's own functions, and answers read-only questions from Muninn.
  - **Built on** the official MCP SDK (`mcp` 2.3.0, stdio only), with `pydantic` for the models. It needs Python 3.10+. The alternatives, if the SDK isn't approved on-premises, are in `MODULES.md`.
  - **Twelve tools, each with a switch** in `settings\ysildir.json`. Tools that send Muninn data start off. A file Ysildir can't read leaves only `asgard_guide` on.
  - **No decision is a tool.** Kiro's guard hook also blocks `ysildir.cmd tools --on/--off`, `setup` and `ysildir.json`.
  - **The client** starts the Python that ran `ysildir.cmd setup` (Kiro, VS Code or Claude Code).
  - **Tests:** `tests/test_ysildir.py`, 55 of them, run through the SDK's own client in memory and over stdio. They skip without the SDK, and the mutation checks fail a test each time.
  - **Still to do, on the workstation:** spec task 0 (reconcile; IT approval of the SDK's five compiled wheels) and task 10 (the acceptance walkthrough with Kiro).
  - **The Baldur agent guide is now `baldur-agent-2`.** It adds worked examples for the MCP route.
- **Modules.** Baldur and Muninn stay standard library: `asgard.muninn` must, and the Baldur parts have no gap a package fills (the review is in `MODULES.md`, "Deliberately standard library"). Ysildir pins `mcp==2.3.0` and `pydantic==2.14.0`, each with its `MODULES.md` section; only Ysildir imports them.
- **Verified with the packaged build in.** 503 Asgard tests: with the MCP SDK installed (Python 3.11), 474 pass and 29 skip (Tk, PySide6, Playwright, Python 3.12). Without the SDK, Ysildir's 42 SDK tests skip too (432 pass). On Python 3.9, 430 pass. The repo-root runner passes 552. `packaging/build.py` passed end to end on Linux (Python 3.12.11), and actionlint is clean on the workflow. Also clean: the schema check (171 checks), vermin, ruff, and the smoke script (PROJ-42 1h30m, PROJ-51 30m). Each of ten mutations to Ysildir's guarantees fails a test.
- **The independent review is done** (`docs/review-2026-10-09.md`). It found 16 defects in calibration and the AI-assisted method (3 blockers), and all are fixed, each with a regression test. Its scripts were re-run against the fixes, and all 16 pass. What people see changes:
  - `approve --date D --ai ID` takes only the figures, and the exact `Reviewed:` lines, that `ai show` showed;
  - time already in Jira never moves;
  - your own figure can't keep time the AI moved;
  - calibration leaves out days every setting estimates alike;
  - summaries refuse code lines and control characters;
  - the Kiro guard reads quoted commands.

  Open: R14's schema-level fix (a v5 trigger), and P5 and P7.
- **The packaged build is built** (`docs/packaging.md`). PyInstaller makes one folder, `Asgard.exe` (windowed) and `asgard-cli.exe` (console), with Python 3.12, PySide6 and the MCP SDK. Playwright is left out.
  - **It installs like the Python install** (Brandon, Oct 10). Setup (`setup-Asgard.cmd`, which finds `asgard-cli.exe`) copies the whole build into `%LOCALAPPDATA%\Asgard\app`, beside Muninn, settings and logs, and points the shortcut and Settings > Apps there. Valhalla running from that copy removes `app\` just after Asgard exits (a hidden PowerShell `Wait-Process`, then `Remove-Item`), since Windows won't delete running programs. The GitHub token stays in Credential Manager.
  - **The two programs stand in for `pythonw` and `python`** and run only Asgard's own scripts (`packaging/frozen_main.py`). Asgard's code ships as `.py` files at the installed layout, so nothing in the apps changed. Process starts ask `paths.frozen_programs()` (the launcher, Baldur's weekly task, the shared window, Ysildir's client entry, Muninn's restore hint); the `.cmd` wrappers prefer `asgard-cli.exe`.
  - **`packaging/build.py`** is the local script and the workflow's: pinned packages in `build\venv`, the tests, PyInstaller, then checks through the built programs (self-test, Muninn, Baldur from setup to a day, Ysildir, Heimdall, setup copying the build into `app\` and its self-test there, an outside script refused, nothing written into the folder), then `payload.sha256` and the zip. It passed on Linux. `.github/workflows/asgard-package.yml` runs it on Windows Server 2025 and publishes a release on an `asgard-vX.Y.Z` tag.
  - **New with it:** `Asgard.pyw --muninn prepare` (create or update Muninn without a window). Kiro's guard now also blocks `--uninstall` and reads the build's programs like `python`. Fixed: `ysildir.cmd` used `>/dev/null`, which `cmd.exe` doesn't have, so it never found Python.
  - **The first Windows run found Windows-only problems**, now fixed. `muninn.connect()` left a damaged file open when its setup pragmas failed, which locked it on Windows while the error was kept, so a restore couldn't replace it. Seventeen tests deleted their temporary folder before their own `addCleanup` closed its connections (cleanups run after `tearDown`). Every test now registers the folder's removal as its first cleanup. A Linux emulation of Windows' file locking (refusing to delete a folder with a file still open) reproduced 16 of the 17 and passes now.
  - **Green on Windows** (Oct 10, run 8): every check passed, Qt included, and setup copied the build into `app\` where its self-test passed again. The zip is 85 MB, with 3,086 files, and is uploaded as the `asgard-windows-x64` artifact.
  - **Still to do, on the workstation:** `asgard-cli.exe --self-test` in `%LOCALAPPDATA%\Asgard\app` under App Control, and whether IT allows that folder (by path, hash or signature).
- **Odin's files moved under Asgard** (Brandon, Oct 10): `%LOCALAPPDATA%\Asgard\odin` (`ASGARD_HOME\odin` when set). Every Odin entry point moves the old `%LOCALAPPDATA%\meeting2jira` there once, and Odin refuses to run while the old `state.db` is left behind. Tests: `tests/test_odin_data_dir.py`. `.github/workflows/odin-checks.yml` runs Odin's tests, the Windows PowerShell 5.1 parse and each launcher's move on Windows. With this, everything Asgard and Odin keep is under `%LOCALAPPDATA%\Asgard`, except Baldur's GitHub token in Credential Manager.
- **Owed before calling it done (AGENTS.md):**
  - carrying the snapshot edits in `docs/baldur-spec.md` and `docs/muninn-design.md` over to the live docs (each snapshot's header lists them);
  - a `VERSION` bump and zip when this ships.
- **Version label.** Schema v3 and v4 are labelled Asgard 0.4.0, the next release. If 0.4.0 ships from main before this branch merges, relabel v4 (`0004_agent_estimates.sql`, the docs) as 0.5.0.

Asgard is a suite of small Python apps for a federal software engineer, Brandon, on a locked-down Windows 11 workstation. A launcher shows one tile per app, and the apps share one SQLite database, Muninn. Built so far: the launcher, installer and uninstaller; Muninn's schema (v4: v3 hardened, plus agent estimates), its Python package and its maintenance commands; Baldur's git collector, estimator, GitHub sync, window, calibration and AI-assisted method; Heimdall; the shared Qt window; and Ysildir, the MCP server for AI clients. 503 Asgard tests: 474 pass with the MCP SDK installed, and 29 skip without tkinter, PySide6, Playwright or Python 3.12 (Ysildir's SDK tests skip too where the SDK isn't installed).

**Where this lives now.** Since 0.3.1 Asgard is the `Asgard/` folder of Brandon's `meeting2jira` repository, next to `Odin/` (since Oct 10 only Odin's optional exporters, Power Platform material and history docs; Odin itself is `apps/odin`). Paths in this file are relative to `Asgard/`; `../Odin/` means the sibling folder. From the repo root, `python tools/run_tests.py` runs both suites through pytest (500 pass on 2026-10-09, after this branch's merge); the plain `unittest` command below still works from `Asgard/`. The repo's `.kiro/steering/` rules describe Odin; for `Asgard/`, this file and `AGENTS.md` win. The retired first attempt at Muninn and Baldur (`munnin-layer/`, `baldur/`) is archived in `../context-docs/` and isn't used. On Windows, the schema-check wrapper and the 25-hour-day test skip, because Windows Python can't switch time zones. Read `AGENTS.md` before changing anything; it holds the rules this project runs on.

## What to read, in order

1. This file.
2. `AGENTS.md`: the rules, the definition of done, and how to work with Brandon.
3. `docs/baldur-spec.md`: what Baldur does and why; the build-order list marks what's built.
4. `docs/muninn-design.md`: every table, the write rules, Odin's move into Muninn (eight steps).
5. `docs/asgard-toolset-evaluation.md`: the environment's constraints and the plan for every app.
6. `docs/packaging.md`: the packaged build (PyInstaller), its decisions, building it locally and in GitHub Actions, and what IT needs on the workstation.
7. `docs/review-2026-10-04.md`, `docs/review-2026-10-06.md` and `docs/review-2026-10-09.md`: the independent reviews (Baldur's estimator; Muninn's hardening and Baldur's GitHub keys; calibration and the AI-assisted method) and how each finding was fixed.
7a. `docs/integration/` (how each app uses Muninn), `docs/muninn-operations.md` (the runbook), `docs/security-and-data.md` (for the ISSO), `docs/platform-consolidation.md`, `docs/updates.md`, `docs/dependency-policy.md`, `docs/app-contract.md`.
8. `README.md`: user and maintainer guide (install, tiles, Muninn API for app authors, Baldur CLI).

The three specs are snapshots of live Claude Docs that Brandon can open and comment on. The live docs are the source of truth; each snapshot's first line has its link. If you can't reach them, edit the snapshot and tell Brandon what changed so he can carry it over.

| Doc | Live link |
| --- | --- |
| Baldur spec | https://claude.ai/code/artifact/e8664f14-4d0c-417a-988c-d2ea23148df2 |
| Muninn data layer design | https://claude.ai/code/artifact/bc161cb2-9cfc-4f28-9e1e-8a1075972208 |
| Asgard toolset evaluation | https://claude.ai/code/artifact/cdb819cb-6de0-4832-b107-cce9d234efb7 |

## The environment and the person

- **Workstation.** Windows 11, standard user, no admin rights. AppLocker or App Control enforcing; unsigned PowerShell runs in Constrained Language Mode; TLS inspection; PIV smart-card sign-in; software only from the agency catalog.
- **Consequences.** Packages are allowed when declared and pinned (`docs/dependency-policy.md`, Oct 2026): the best module for each job (Brandon, Oct 9), pure-Python when two are as good, each with its alternatives in `MODULES.md`; `asgard.muninn` and the launcher's start-up stay standard library. Python 3.9 or newer from the catalog, tkinter or PySide6 for UI, everything per-user under `%LOCALAPPDATA%\Asgard`. Code must run on 3.9, but Muninn also needs the SQLite bundled with Python to be 3.37+ with FTS5 and JSON; on Windows that in practice means Python 3.11 or newer, which is why Muninn's error messages and the preflight ask for 3.11. No new executables, no services, nothing that needs admin. `tools/asgard_preflight.ps1` checks the policies that matter.
- **Brandon** works in Jira (Data Center, personal access tokens), git and GitHub, Outlook and Teams. He already had an app called **Odin** (then `../Odin/app`, Python package `meeting2jira`) that turns meetings into Jira sub-tasks and logs their time; since Oct 10 it is an Asgard app (`apps/odin`) and keeps its records in Muninn. He asked for "the best technical solution possible, even if it's not exactly as I said", so make well-reasoned decisions, record them in the specs, and tell him what you decided and why.
- **His style.** Plain, concise writing; numbers with units; no filler. Messages the apps show say what happened and what to do next.

## The suite

| App | Job | Status |
| --- | --- | --- |
| Asgard | Launcher, installer (`setup-Asgard.cmd`), tiles with Muninn badge counts | Built (0.1 to 0.3.1) |
| Muninn | Shared SQLite database and `asgard.muninn` package | Built: schema v4, unshipped (v2 took squash copies out of `v_activity`; v3 is the hardening, `0003_hardening.sql`; v4 adds Baldur's agent estimates, `0004_agent_estimates.sql`). Ownership guard on every app connection, secret scrubbing, damage detection and restore, schema self-repair, daily housekeeping, `Asgard.pyw --muninn ...` commands |
| Valhalla | Uninstaller, replays the install ledger | Built |
| Baldur | Work-time estimates from git; pull request alerts | Collector, estimator, CLI, window and GitHub sync built (PR keys reviewed twice, `docs/review-2026-10-06.md`); calibration and the AI-assisted method built and reviewed (`docs/review-2026-10-09.md`) (agent estimates, the checked review at the clipboard tier, Kiro agent files); alerts and the review's API and MCP tiers not yet |
| Odin | Meetings to Jira sub-tasks; Jira issues, calendar and worklogs into Muninn; approved Baldur days to Jira | Built: `apps/odin` on Muninn v5, with its window pages (`docs/integration/odin.md`) |
| Freya | Yearly review drafts with citations | Specified (toolset doc, Muninn tables), not built |
| Loki | BLUFs for meetings and code changes | Specified, not built |
| Heimdall | SeCcHm submissions | CLI and window built: form file, templates, `fill` through Playwright and Edge, stopping before Submit. Its tile opens the shared Qt window (Templates, Form, Dashboard, Settings). Tested against a fake ServiceNow with Chrome, and the window offscreen, on macOS only. Real Edge, PIV, the SeCcHm form and the window on Windows are untested. No Muninn `submissions` rows yet |
| Shared window | `asgard.ui`: one PySide6/QML window apps plug pages into | Built (shell, theming, Dashboard, Settings, Heimdall's and Odin's pages). Bifrost's pages not built; the launcher and Baldur's window are still tkinter |
| Bifrost | BEARs workbooks and Confluence uploads | Specified, not built |
| Ysildir | MCP server so approved AI tools can use Asgard | Built (`apps/ysildir/`, on the MCP SDK 2.3.0): teaching, Baldur's agent intake, day view and MCP-tier review, Muninn reads, switches, `setup` for Kiro, VS Code and Claude Code. Not yet tried on the workstation (spec tasks 0 and 10); no tile window (the GUI comes later) |
| Valkyrie | Setup scripts for new engineers | Specified, not built |

Names reserved in the specs: Huginn (scheduled collectors), Mímir (the one module that picks an AI tier: API, MCP or clipboard).

## Repository map

| Path | What it is |
| --- | --- |
| `Asgard.pyw`, `asgard/launcher.py` | The launcher window (tkinter): tiles, search, badges, Muninn backup, refresh every 60 s |
| `asgard/install.py`, `setup-Asgard.cmd` | Per-user install to `%LOCALAPPDATA%\Asgard\app`, Start menu shortcut, Settings > Apps entry, install ledger. The packaged build is copied whole, programs included (`paths.FROZEN`) |
| `packaging/` | The packaged build: `asgard.spec` (PyInstaller, one folder), `frozen_main.py` (the entry point of `Asgard.exe` and `asgard-cli.exe`, and `--self-test`), `build.py` (the build and its checks, locally and in CI), `requirements-build.txt`. `.github/workflows/asgard-package.yml` runs `build.py` on Windows |
| `asgard/valhalla.py` | Uninstall from the ledger, asking before deleting data |
| `asgard/catalog.py`, `asgard/apps.json`, `asgard/runner.py` | Tile catalog (defaults plus `apps.local.json`) and how apps are started |
| `asgard/paths.py`, `asgard/winutil.py` | Where files live (`ASGARD_HOME` overrides for tests); per-user registry through `winreg`, shortcuts through COM with ctypes (PowerShell as the fallback) |
| `asgard/muninn/` | `db.py` (connect, `open_app`, migrate, backup, restore, `prepare`, `transaction`, `retry_busy`), `guard.py` (authorizer and row rules per app), `integrity.py` (housekeeping, `check`, schema drift, repairs), `redact.py` (secret scrubbing), `keys.py` (Jira key rule), `cli.py` (`--muninn` commands), `sync.py` (`Run`, events, identities), `odin.py` (Odin's flows and posting protocol), `baldur.py` (approval rules, agent estimates, the review check), `badges.py` (tile counts) |
| `asgard/muninn/migrations/` | `0001_initial.sql` (v1), `0002_copies_arent_activity.sql` (v2), `0003_hardening.sql` (v3), `0004_agent_estimates.sql` (v4) and `0005_odin_meetings.sql` (v5, Odin's meeting sub-tasks): not shipped, so they may still change; frozen once 0.4.0 ships. Never edit a shipped migration. Baldur and Ysildir open Muninn with `supported=(4, 5)`; Odin with `(5, 5)` |
| `apps/baldur/baldur/` | `settings.py`, `keys.py`, `gitread.py`, `collect.py`, `estimate.py` (pure), `store.py`, `report.py`, `cli.py`, `github.py`, `desk.py` and `window.py` (the window), `calibrate.py` (calibration), `assist.py` (the AI-assisted method), `agents.py` (installs the Kiro files) |
| `apps/baldur/prompts/`, `apps/baldur/agents/` | `review.md` (the review prompt, `baldur-review-3`), `agent-guide.md` (the agent guide, `baldur-agent-2`); the Kiro steering, `hooks/guard_baldur.py` and `hooks/after_commit.py` |
| `apps/baldur/cli.py`, `apps/baldur/baldur.cmd` | CLI entry points (the `.cmd` tries `py -3`, then `py`, then `python`) |
| `apps/heimdall/heimdall/` | `form.py` (the form file, `settings\heimdall.json`), `templates.py` (`settings\heimdall-templates.json`, and `plan()`: form default < template < `--set`), `browser.py` (the only module that imports Playwright, lazily), `cli.py` (`init`, `fields`, `template list/show/save/edit/delete`, `fill`; `--json` for a UI) |
| `asgard/ui/` | Shared window: `theme.py` (tokens; built-in < app manifest < `ui.json`; derived colours keep 4.5:1 contrast), `prefs.py` (`settings\ui.json`), `registry.py` (finds `apps/<id>/ui/manifest.json`), `shell.py` (the only PySide6 module: theme map, navigation, `Bridge`, dashboard, settings), `qml/AsgardUI/` (components, Dashboard, Settings). `python -m asgard.ui [--app ID]` |
| `apps/heimdall/ui/`, `heimdall/ui_backend.py`, `heimdall.pyw` | Heimdall's pages (manifest, Templates.qml, Form.qml), their plain-Python backend (fill runs the CLI as a child process), and the window entry point the tile starts |
| `apps/heimdall/cli.py`, `apps/heimdall/heimdall.cmd` | CLI entry points, same pattern as Baldur's |
| `tests/` | `test_baldur.py` (115), `test_baldur_assist.py` (41: the AI-assisted method, agent intake, the Kiro guard and installer), `test_baldur_calibrate.py` (16), `test_baldur_github_review.py` (19), `test_ui.py` (18: theme, prefs, registry, Heimdall's backend), `test_ui_qt.py` (8; skip without PySide6; offscreen: every page loads with no QML warnings, a second app plugs in from a manifest alone, accents follow the page and Settings), `test_heimdall.py` (24), `test_heimdall_browser.py` (13; 11 skip without Playwright, about 2 min with it; `fake_servicenow.py` is its deliberately awkward instance, keep the traps), `test_muninn.py` (48), `test_muninn_hardening.py` (45), `test_muninn_review.py` (29), `test_dependencies.py` (9: pins, imports and `MODULES.md`), `test_catalog.py` (14), `test_runner.py` (5), `test_install.py` (3), `test_packaging.py` (18: the build's entry point, frozen process starts, setup in place, the `.cmd` wrappers, the build's pins), `test_muninn_schema.py` (1, wraps the schema checks; skipped on Windows) |
| `MODULES.md` | Every package Asgard pins: where it's imported, what happens without it, stdlib and other-package alternatives. `tests/test_dependencies.py` checks it |
| `tools/` | `asgard_preflight.ps1` (policy checks), `check_muninn_schema.py` (171 checks, applying every migration), `baldur_smoke.sh` (end-to-end demo), `make_icon.py`, `windows_checks.py` |
| `docs/` | Spec snapshots, the review record, a launcher screenshot |

## How to run things

```
python3 -m unittest discover -s tests          # Windows: py -3 -m unittest discover -s tests
vermin -t=3.9- --no-tips --eval-annotations --violations apps asgard tests   # if vermin is available
bash tools/baldur_smoke.sh                      # Linux/macOS/WSL: the spec's worked example, end to end
python3 apps/baldur/cli.py --help

# Windows Python has no time.tzset, so there the tests run in the machine's own zone. Check any zone like this:
for z in UTC Asia/Kolkata Pacific/Kiritimati America/St_Johns; do TZ=$z python3 -c "import time, sys, unittest; del time.tzset; sys.argv = ['t', 'discover', '-s', 'tests']; unittest.main(module=None, exit=False, verbosity=0)" 2>&1 | tail -1; done
```

The smoke script should end with PROJ-42 1h30m and PROJ-51 30m approved, from sessions 09:20-12:25 and 14:00-15:05. On Windows, Brandon installs with `setup-Asgard.cmd` (or `py -3 asgard\install.py`), opens Asgard once so Muninn exists, then uses `%LOCALAPPDATA%\Asgard\app\apps\baldur\baldur.cmd` as the README describes.

**Packaging.** A release is a zip of this folder under an `Asgard/` prefix (`Asgard-X.Y.Z.zip`), without `__pycache__`. Setup installs only `install.PAYLOAD` (`Asgard.pyw`, `VERSION`, `README.md`, `asgard/`, `apps/`), so anything else in the zip is harmless. Once the code is in git, `git archive --prefix=Asgard/ -o Asgard-X.Y.Z.zip HEAD` makes a release that leaves out tests, docs and the handoff files (`.gitattributes` export-ignore). A handoff zip like this one must be a plain zip of everything, since `git archive` would leave those out.

## How Baldur works (short version)

1. **Collect** (`collect.py`, `gitread.py`): for each repository under `repo_roots`, read git with no database lock, then write in one batch: your commits (by git email; co-authored ones as `is_mine = 0`), each working folder's HEAD reflog, and each commit's Jira keys with how Baldur knows them (reflog branch, then branch membership, then message; PR head branches slot in before messages once the GitHub step collects them). Weaker evidence never replaces stronger; manual keys always win.
2. **Estimate** (`estimate.py`, pure functions): dedupe (SHA anywhere; patch-id and time-and-subject per project), build sessions on one timeline (idle gap 120 m; start at the checkout onto the first commit's branch, else 30 m lead-in; end at the last commit; clamp 15 to 240 m), split at local midnight, weigh meeting overlap by policy (ambient 0.5 by default), cap the day (480 m), attribute by commit count, round down to 15 m.
3. **Store** (`store.py`): read and write in one `BEGIN IMMEDIATE` transaction; store a proposal only when it says something new (hash, or a number not already decided), supersede open rows, never touch decided ones. Every proposal carries its basis text and `basis_hash`.
4. **Review** (`report.py`, `cli.py`): the day report shows meetings, development, what Jira holds, what Odin will post, untracked time, sessions and flags. Approval goes through `muninn.baldur` (`approve`, `approve_day`, `reject`, `reject_day`, `change_approval`). Odin, not Baldur, posts to Jira.
5. **Calibrate** (`calibrate.py`): fit the gap, lead-in and ambient weight to the real hours you note (`actual`), keeping only fits that run low on average; `calibrate --accept` writes them.
6. **Assist** (`assist.py`, rules in `muninn.baldur`): turn agent estimates (`agent_estimates`, v4) or a pasted AI review into suggestions. `check_review` checks every one: it never raises a day, it cites evidence, it adds no ticket, it never moves time Jira already holds, and it rounds down. `approve --date D --ai ID` takes the ones you were shown, and Odin's comment adds the `Reviewed:` line you saw.

The worked example in the spec (meetings 09:00-12:00 and 12:30-16:30, eight commits) is a named test for all three policies: ambient 1h30m + 30m, overlap 3h + 1h under a 270 m cap, independent 3h + 1h flagged.

## Decisions already made (don't reopen without a reason)

- Python 3.9+; per-user install; no admin. Packages: the best module for each job (Brandon, Oct 9), declared, pinned and in `MODULES.md` with its alternatives (`docs/dependency-policy.md`); Muninn and the launcher's start-up stay standard library.
- Muninn is one SQLite file in WAL mode; only Asgard migrates (`muninn.prepare()`); apps open it with `open_app(app, supported=...)` (no default range) and write only their own tables through the package.
- Muninn hardening (Oct 2026):
  - **Guard and rows.** The guard (authorizer plus per-connection TEMP triggers) enforces ownership down to rows in the shared tables. A source belongs to the first app that runs it.
  - **Damage.** A damaged file stops startup with the newest backup and the restore command named; nothing moves automatically. Search-index damage and missing protections are repaired at start.
  - **Housekeeping.** Retention pruning is off until Brandon confirms the periods.
  - **Stuck posts.** `resolve_stuck` needs `searched=True` before marking a post failed.
  - **Day status.** A Baldur worklog counts for its approved day and for the day it starts on.
  - **Secrets.** Scrubbing is a safety net at the writer boundary, not the control.
- Baldur's PR keys (Oct 2026): from stored membership. The PR a commit was made for wins: merged > open > draft > closed, then the commit's own repository, then the smallest. A squash is detected by patch id against the PR's commits; rebased copies share the PR's key. Fall-back is the keys stored from the whole message.
- Odin moved into Muninn (Brandon decided) in the eight steps in the Muninn doc, done Oct 10. Odin is the only Jira writer; Baldur proposes, you approve in Baldur, Odin posts the shortfall with a crash-safe sending row and marker.
- Baldur's principles: every number carries its basis; every cap, rounding and tie-break goes down; nothing reaches Jira without approval; say what can't be seen; metadata only (no diffs leave the machine unless AI review is switched to content mode).
- GitHub is Brandon's GitHub Enterprise Server (2026-10-04). `github_api` takes its host and stores `https://HOST/api/v3`; only remotes on that host get `github_repo`; committers named "GitHub" or "GitHub Enterprise" write squash copies, from any address. A bad `github_api` turns GitHub off with a warning instead of stopping Baldur, because it never changes a number.
- From the 2026-10-03/04 build and review: identity is git email only; checkout start uses the latest checkout onto the first commit's branch; collection rescans `history_days` (120) every run; `basis_hash` covers only number-changing settings; worktrees fold into their repository; stacked branches resolve to the smallest branch; stash entries and squash copies aren't work.
- Squash copies (review #20, 0.3.1): stored with `is_merge = 1` and no keys (merge commits aren't collected, so that means a copy). A copy is never work, but the sessions it links share one length limit: together they keep no more than the single session they'd form with the copy counted, every session in the stretch scaled alike (`estimate.share_limits`). Skipping a copy can't raise a day, and no ticket in the stretch rises. A SHA any clone marks as a copy isn't work in any clone. Two edges are accepted and recorded in the review: the day cap shares the freed room with other tickets, and with `min_session_minutes` above `idle_gap_minutes` the fix gives what skipping gave.
- **Two ways to estimate (2026-10-09).** Baldur keeps the manual engine and the AI-assisted method side by side.
  - **AI-assisted figures are suggestions.** `muninn.baldur.check_review` checks every one: it never raises a day, it cites evidence that was sent, it adds no ticket, it never moves time Jira already holds, and it rounds down. Only `approve --date D --ai ID` takes them, and only while they're the ones you were shown.
  - **An agent estimate** is the person's working time on a change (not the agent's running time, and not a "without AI" size). It is stored as a fact, and its low end counts.
  - **A stored review** counts only while its pack hash matches the day's evidence and it passes `check_review` again.
  - **`review_mode=content` is refused** under rule 6.
  - **Calibration** fits only to real hours you noted, never to approvals, and accepting needs at least 10 days.
- **Ysildir (built 2026-10-09).**
  - Built on the official MCP SDK (`mcp` 2.3.0, `MCPServer`), stdio transport only. It needs
    Python 3.10+, and IT must approve five native wheels. If it isn't available on-premises, the
    alternatives in order are: `mcp` 1.x, `fastmcp`, a standard-library server, the CLI and
    clipboard tier (design, "Modules").
  - Writes only through Baldur's functions, under Baldur's identity.
  - Never a tool for a decision.
  - A switch per tool, with tools that send Muninn data off by default.
  - The client config names the Python that ran `setup` (it has the SDK), not `py -3`, which could pick another.

## What's next

In priority order. Each step should end with tests, the specs updated, and a version bump.

1. **Real-machine trial (Brandon, with your help).** Run `baldur.cmd setup --from-git --project <KEY> --root <folder>`, `collect`, `estimate`, `days` and `report` on his Windows machine and compare a known week. This is the first run on Windows: watch git versions (2.39+ gets `--since-as-filter`), paths with spaces, the console code page, and `schedule` (Task Scheduler with `/IT`, untested on a real machine). Fix what it finds before building more.
2. **Ship 0.4.0.** Bump `VERSION`, run the full suite (`python tools/run_tests.py` from the repo root), `tools/check_muninn_schema.py`, vermin and `baldur_smoke.sh`, then zip. Merge `claude/baldur-estimation` first, and do the independent review it owes (below), since its v4 migration ships in 0.4.0. After this, `0003_hardening.sql` is frozen. Before shipping, run `docs/windows-lab-spec-0.4.md` on the lab (two runs, Python 3.11.9 and 3.12.10, US zones): restore with an app holding the file, defensive mode on 3.11, the schema comparison on Windows' SQLite, backups under Defender.
2a. *(Done: Baldur window, spec build step 7.)* `apps/baldur/baldur.pyw` (tkinter, same look as the launcher): week view, the day report, edit a figure, approve or reject a day, Copy for timesheet. Reuse `store.compute`, `store.plan`, `report.render_day` and `muninn.baldur`. Then set Baldur's tile in `asgard/apps.json` to `"status": "available", "launch": {"type": "python", "target": "{app}\\apps\\baldur\\baldur.pyw"}`. Keep git, network and database-lock work off the UI thread: the launcher's pattern is a worker thread (`muninn.prepare()` in `_start_muninn`) that the window polls with `after()`.
3. *(Done, except alerts: GitHub, build step 4.)* Read-only token in Credential Manager; pull requests, reviews, the `review-requested:@me` search with conditional requests; review-only `repos` rows; alerts via `Shell_NotifyIcon`; PR head-branch keys (`keys.choose(pr_branches=...)` is ready); the squash rule with PR data. The host is decided (Enterprise Server; `settings.github_api_url()` and `Settings.github_host()` are ready). Needs Brandon's answer on token access (fine-grained or classic) and the server's version.
3a. *(Done: the independent review, `docs/review-2026-10-09.md`.)* 16 confirmed findings, all fixed with regression tests. Still open from it: R14's stronger fix (a v5 trigger on `agent_estimate_commits`) and the notes P5 and P7; decide with the next migration.
3b. **Agents on the workstation.** Install Baldur's Kiro files in one repository (`baldur.cmd ai kiro --into ...`), then reconcile them with the on-premises steering that already asks agents for estimates (`apps/baldur/agents/README.md`, "Reconciling"). Run a calibration trial alongside: the real hours you note there also show whether agents' estimates help.
3c. **Ysildir on the workstation** (`../.kiro/specs/ysildir-mcp/tasks.md`). Task 0 reconciles the spec with the on-premises steering and checks the SDK's approval, then stops for Brandon. Task 10 is the acceptance walkthrough with Kiro. Until IT approves the SDK's compiled wheels, agents use `baldur.cmd ai record` and the clipboard review.
3d. **The AI-assisted figures in a window** (with the planned GUI work). `desk.load_day` already returns them in `DayView.ai`, and `desk.approve_day(..., take_ai=True)` takes them. Moving Baldur's window to the shared Qt window is optional ("Shared window, decided", below); if it moves, a `ui/manifest.json`, QML pages and a backend over `desk.py` are the pattern Heimdall set.
4. *(Done Oct 10: Odin into Muninn, all eight steps, and Odin as an Asgard app; "Latest" above.)* Next for Odin: a real daily run on the workstation against Jira Data Center, watching the first sync's length and `/worklog/deleted` on that version, and the window at real DPI. Carried over from Odin's own backlog (`../Odin/HANDOFF.md`, section 6, whose approach notes still hold with `state.db` read as `meeting_subtasks`): P0, the target-machine checks (`odin selftest`, `odin doctor`); P2-A, a past meeting edited after its sub-task was made (a new time makes a second sub-task; on the CSV path a new subject does too); P2-B, CSV hardening (a header map for non-English Outlook, the `Show time as` numbering); P2-C, the Graph source (`../Odin/graph-app`, blocked on an Entra app registration).
5. *(Done on `claude/baldur-estimation`: calibration, build step 8.)* `actual`, `actuals`, `calibrate [--accept]`; the trial itself is Brandon's.
6. *(Done Oct 10: Odin posts approved Baldur days.)* Then **alerts** (`Shell_NotifyIcon`, once Brandon picks notifications or the badge alone), then the AI review's **API tier** (Mímir, once an endpoint is approved).
7. **The other apps**, in the toolset evaluation's order, each writing only its own Muninn tables.

## Waiting on Brandon

- *(Settled Oct 10.)* Which Odin the Muninn plan means, and Odin's no-Asgard guardrail: Brandon made Odin an Asgard app, and Odin now has the issue sync, Assigned to Me and the tracked-parent pull the plan assumed.
- **Odin's posting default.** `muninn.post_approved` is on, so the daily run posts approved Baldur days (at most 20 per run). Say if it should start off.
- The spec's open decisions: post to Jira or report only; Odin's calendar fields; GitHub access (token allowed? fine-grained or classic? which server version?); alerts (notifications at logon, or the tile badge only?); whether AI review is approved and in which mode; whether PR reviews become loggable time; trial length (two or four weeks).
- **Shared window, needs Windows verification:** the PySide6 wheel loading under App Control; `heimdall.pyw` from the tile (pythonw, no console); Segoe UI and DPI scaling; Match Windows following the Windows setting; Narrator reading the sidebar, buttons and toasts; a real fill from the window (child process, output streaming, Stop). Only offscreen rendering on macOS has run.
- **Shared window, decided Oct 2026:** new windows are PySide6 + QML through `asgard.ui` (Brandon: PySide6 approved, in use on-prem), pinned at 6.10.3, the last release for Python 3.9. App backends stay plain Python so they're testable without Qt. Odin's pages are built (Today, Meetings, My issues). Next: Bifrost; moving the launcher and Baldur's tkinter windows is optional.
- **Heimdall approvals.** Packages are now allowed when declared and pinned (`docs/dependency-policy.md`), so Playwright no longer needs an exception; its line in `requirements.txt` still says `approval: pending`. Also open: ISSO or system-owner approval for automated filling of SeCcHm.
- **Muninn retention.** Switch on (`--muninn retention on`) once these are confirmed against the records schedule: sync runs 180 days; estimate runs with no decision or open day 90 days; reflog entries 1 year.
- **The AI-assisted method.**
  - Is an AI coding agent's estimate acceptable evidence for moving or lowering time?
  - Is an AI chat approved for the review in `metadata` mode?
  - What does the on-premises steering ask agents to estimate: time spent, which is Baldur's figure, or a "without AI" size, which doesn't belong in Baldur?
- **Ysildir.** The ISSO's view on the default switches (which tools may send Muninn data to the AI client; requirement 6.1 of the spec, as built), and IT's approval of the SDK's five compiled wheels (`pydantic-core`, `cryptography`, `cffi`, `rpds-py`, `pywin32`).
- *(Settled Oct 10.)* Odin's old `gitwork.py` was dropped when Odin moved into Asgard; Baldur's collector does that job.

## Known limitations

- US time zones only (decided Oct 9). After a move between US zones, a worklog logged by hand within 6 hours of midnight can count for the neighbouring day (review R4, accepted).
- Past-dated commits from a wrong clock count on their date.
- Reflog entries inside a session don't extend it (deliberate, biases low).
- The day report and basis say nothing yet about PR reviews (needs the GitHub step).
- Everything Windows-specific in Baldur (`baldur.cmd`, `schedule`, code pages) was written for Windows but tested only on Linux, with the code page simulated.

## Lessons that cost time

- `git for-each-ref` writes a byte as `%1f`; `%x1f` is `git log`'s spelling and comes out as literal text.
- `git log --all` includes `refs/stash`; `--since` stops at the first older commit; read times as Unix seconds (`%at`, `%ct`, `--date=unix`) because offsets can be malformed.
- A new worktree's HEAD reflog has no checkout entry; the replay seeds from the worktree's branch.
- On Windows a folder can't be deleted while a file in it is open. A test that runs an app's `main()` leaves its log file handler open (Odin's `odin.log`), so close the handlers before the temporary folder goes (`OdinTestCase` does). On Linux, the packaged build needs `LD_LIBRARY_PATH` set to the uv Python's `lib` folder, or its venv's copied `python` can't load Tcl/Tk and the self-test fails.
- `PRAGMA journal_mode = WAL` doesn't wait on a busy database; `ensure_wal()` retries. `muninn.transaction()` refuses to nest; `Run.batch()` nests with savepoints.
- Tests set `TZ=America/New_York` with `time.tzset()` (POSIX only) so daylight saving can be tested; build test datetimes inside test functions, not at import time. Every other test must pass in any zone, because Windows runs them in the machine's own: one test assumed "now + 2 h" fell on the same local day and failed in zones from UTC+4 up to UTC+6 until it was fixed.
- `.gitattributes` fixes line endings in git checkouts and `git archive` zips (GitHub's Download ZIP); a zip built any other way keeps the files' own endings, so keep `.cmd` and `.ps1` files CRLF on disk.
- `setup --from-git` once read `git config --global`, which skips `[include]` files; it now reads config the way git does. An email set per folder with `includeIf` still needs `--email`.
- In the original build environment `python3` (3.13) had no tkinter; `python3.12` did, and `xvfb-run` with ImageMagick `import` took launcher screenshots. On Brandon's Mac, Homebrew `python3` needs `python-tk` for the install tests.
- macOS's temp folder is under `/var`, a link to `/private/var`, and Windows can hand out 8.3 short names; Baldur stores resolved paths, so tests resolve their temp folder before comparing (`MuninnCase.setUp`).
- `urllib.parse` answers differently across Python versions (ports such as `+443`) and its errors repeat the URL; `settings.github_api_url()` parses by hand so a token typed into `github_api` is never echoed.
- Re-run the reviewer-style checks after any change to estimation: the direction tests, the worked example, and a fresh independent review for anything that changes the numbers people approve.
- Lowering part of a day by moving a session's start is a trap: the minutes left go to whichever commits are still inside, so one ticket rises while the day falls. Lower a session by scaling it, as the day cap does, so its split across tickets stays the one its commits give. The second review of #20 caught this.
- An open-ended independent review of an estimator change can run past an hour and time out. Give the reviewer a short list of named checks and a time budget.
- An SQLite authorizer sees FTS5's own writes to its shadow tables exactly as it sees an app's (no trigger name), so the guard must allow them; defensive mode (Python 3.12+) is what makes them read-only to ordinary SQL.
- With `recursive_triggers` on, `INSERT OR REPLACE` fires delete triggers; the guard's TEMP 'never deleted' triggers rely on that to stop REPLACE. Don't use REPLACE anywhere (a test bans it).
- Views that compare with `'now'` (`v_unknown_keys`, the 'posts to check' badge) make fixed dates in tests age out: `check_muninn_schema.py` broke a week after it was written. Make such rows relative to the clock.
- macOS clears old files under `/tmp`, which broke a dev venv there (vermin and pip half-deleted). Recreate it rather than debug it.
