# Asgard handoff (2026-10-04, Asgard 0.3.0)

Asgard is a suite of small Python apps for a federal software engineer, Brandon, on a locked-down Windows 11 workstation. A launcher shows one tile per app, and the apps share one SQLite database, Muninn. Built so far: the launcher, installer and uninstaller; Muninn's schema and its Python package; and Baldur's git collector and time estimator, driven from a command line until its window is built. 153 tests pass. The 3 install tests need tkinter and skip without it (`python3` 3.13 in the build environment has none; `python3.12` runs all 153). On Windows, the schema-check wrapper and the 25-hour-day test skip, because Windows Python can't switch time zones. Read `AGENTS.md` before changing anything; it holds the rules this project runs on.

## What to read, in order

1. This file.
2. `AGENTS.md`: the rules, the definition of done, and how to work with Brandon.
3. `docs/baldur-spec.md`: what Baldur does and why; the build-order list marks what's built.
4. `docs/muninn-design.md`: every table, the write rules, Odin's move into Muninn (eight steps).
5. `docs/asgard-toolset-evaluation.md`: the environment's constraints and the plan for every app.
6. `docs/review-2026-10-04.md`: the independent review of Baldur and how each finding was fixed.
7. `README.md`: user and maintainer guide (install, tiles, Muninn API for app authors, Baldur CLI).

The three specs are snapshots of live Claude Docs that Brandon can open and comment on. The live docs are the source of truth; each snapshot's first line has its link. If you can't reach them, edit the snapshot and tell Brandon what changed so he can carry it over.

| Doc | Live link |
| --- | --- |
| Baldur spec | https://claude.ai/code/artifact/e8664f14-4d0c-417a-988c-d2ea23148df2 |
| Muninn data layer design | https://claude.ai/code/artifact/bc161cb2-9cfc-4f28-9e1e-8a1075972208 |
| Asgard toolset evaluation | https://claude.ai/code/artifact/cdb819cb-6de0-4832-b107-cce9d234efb7 |

## The environment and the person

- **Workstation.** Windows 11, standard user, no admin rights. AppLocker or App Control enforcing; unsigned PowerShell runs in Constrained Language Mode; TLS inspection; PIV smart-card sign-in; software only from the agency catalog.
- **Consequences.** Python standard library only (no pip installs, no compiled wheels), Python 3.9 or newer from the catalog, tkinter for UI, everything per-user under `%LOCALAPPDATA%\Asgard`. Code must run on 3.9, but Muninn also needs the SQLite bundled with Python to be 3.37+ with FTS5 and JSON; on Windows that in practice means Python 3.11 or newer, which is why Muninn's error messages and the preflight ask for 3.11. No new executables, no services, nothing that needs admin. `tools/asgard_preflight.ps1` checks the policies that matter.
- **Brandon** works in Jira (Data Center, personal access tokens), git and GitHub, Outlook and Teams. He already has an app called **Odin** (outside this repo) that syncs Jira and logs meeting time, with its own `state.db`. He asked for "the best technical solution possible, even if it's not exactly as I said", so make well-reasoned decisions, record them in the specs, and tell him what you decided and why.
- **His style.** Plain, concise writing; numbers with units; no filler. Messages the apps show say what happened and what to do next.

## The suite

| App | Job | Status |
| --- | --- | --- |
| Asgard | Launcher, installer (`setup-Asgard.cmd`), tiles with Muninn badge counts | Built (0.1 to 0.3) |
| Muninn | Shared SQLite database and `asgard.muninn` package | Built: schema v1 (34 tables, 48 indexes, 30 triggers, 10 views) |
| Valhalla | Uninstaller, replays the install ledger | Built |
| Baldur | Work-time estimates from git; pull request alerts | Collector, estimator and CLI built; window, GitHub, calibration and AI review not yet |
| Odin | Jira issues, calendar, worklogs (Brandon's existing app) | External; moving into Muninn is decided, Muninn's `odin.py` side is built |
| Freya | Yearly review drafts with citations | Specified (toolset doc, Muninn tables), not built |
| Loki | BLUFs for meetings and code changes | Specified, not built |
| Heimdall | SeCcHm submissions | Specified, not built |
| Bifrost | BEARs workbooks and Confluence uploads | Specified, not built |
| Ysildir | MCP server so approved AI tools can use Asgard | Specified, not built |
| Valkyrie | Setup scripts for new engineers | Specified, not built |

Names reserved in the specs: Huginn (scheduled collectors), Mímir (the one module that picks an AI tier: API, MCP or clipboard).

## Repository map

| Path | What it is |
| --- | --- |
| `Asgard.pyw`, `asgard/launcher.py` | The launcher window (tkinter): tiles, search, badges, Muninn backup, refresh every 60 s |
| `asgard/install.py`, `setup-Asgard.cmd` | Per-user install to `%LOCALAPPDATA%\Asgard\app`, Start menu shortcut, Settings > Apps entry, install ledger |
| `asgard/valhalla.py` | Uninstall from the ledger, asking before deleting data |
| `asgard/catalog.py`, `asgard/apps.json`, `asgard/runner.py` | Tile catalog (defaults plus `apps.local.json`) and how apps are started |
| `asgard/paths.py`, `asgard/winutil.py` | Where files live (`ASGARD_HOME` overrides for tests); per-user registry through `winreg`, shortcuts through COM with ctypes (PowerShell as the fallback) |
| `asgard/muninn/` | `db.py` (connect, migrate, backup, `transaction`), `sync.py` (`Run`, events, identities), `odin.py` (Odin's flows and posting protocol), `baldur.py` (approval rules), `badges.py` (tile counts) |
| `asgard/muninn/migrations/0001_initial.sql` | Schema v1; never edit a shipped migration, add `0002_...` |
| `apps/baldur/baldur/` | `settings.py`, `keys.py`, `gitread.py`, `collect.py`, `estimate.py` (pure), `store.py`, `report.py`, `cli.py` |
| `apps/baldur/cli.py`, `apps/baldur/baldur.cmd` | CLI entry points (the `.cmd` tries `py -3`, then `py`, then `python`) |
| `tests/` | `test_baldur.py` (82), `test_muninn.py` (48), `test_catalog.py` (14), `test_runner.py` (5), `test_install.py` (3), `test_muninn_schema.py` (1, wraps the 124 schema checks; skipped on Windows) |
| `tools/` | `asgard_preflight.ps1` (policy checks), `check_muninn_schema.py` (124 checks), `baldur_smoke.sh` (end-to-end demo), `make_icon.py` |
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

The worked example in the spec (meetings 09:00-12:00 and 12:30-16:30, eight commits) is a named test for all three policies: ambient 1h30m + 30m, overlap 3h + 1h under a 270 m cap, independent 3h + 1h flagged.

## Decisions already made (don't reopen without a reason)

- One package, stdlib only, Python 3.9+; tkinter; per-user install; no admin.
- Muninn is one SQLite file in WAL mode; only Asgard migrates (`muninn.prepare()`); apps open it with `open_app()` and write only their own tables through the package.
- Odin moves into Muninn (Brandon decided) in the eight steps in the Muninn doc. Odin is the only Jira writer; Baldur proposes, you approve in Baldur, Odin posts the shortfall with a crash-safe sending row and marker.
- Baldur's principles: every number carries its basis; every cap, rounding and tie-break goes down; nothing reaches Jira without approval; say what can't be seen; metadata only (no diffs leave the machine unless AI review is switched to content mode).
- From the 2026-10-03/04 build and review: identity is git email only; checkout start uses the latest checkout onto the first commit's branch; collection rescans `history_days` (120) every run; `basis_hash` covers only number-changing settings; worktrees fold into their repository; stacked branches resolve to the smallest branch; stash entries and squash copies aren't work.

## What's next

In priority order. Each step should end with tests, the specs updated, and a version bump.

1. **Real-machine trial (Brandon, with your help).** Run `baldur.cmd setup --from-git --project <KEY> --root <folder>`, `collect`, `estimate`, `days` and `report` on his Windows machine and compare a known week. This is the first run on Windows: watch git versions (2.39+ gets `--since-as-filter`), paths with spaces, the console code page, and `schedule` (Task Scheduler with `/IT`, untested on a real machine). Fix what it finds before building more.
2. **Baldur window (spec build step 7).** `apps/baldur/baldur.pyw` (tkinter, same look as the launcher): week view, the day report, edit a figure, approve or reject a day, Copy for timesheet. Reuse `store.compute`, `store.plan`, `report.render_day` and `muninn.baldur`. Then set Baldur's tile in `asgard/apps.json` to `"status": "available", "launch": {"type": "python", "target": "{app}\\apps\\baldur\\baldur.pyw"}`. Keep git, network and database-lock work off the UI thread: the launcher's pattern is a worker thread (`muninn.prepare()` in `_start_muninn`) that the window polls with `after()`.
3. **GitHub (build step 4).** Read-only token in Credential Manager; pull requests, reviews, the `review-requested:@me` search with conditional requests; review-only `repos` rows; alerts via `Shell_NotifyIcon`; PR head-branch keys (`keys.choose(pr_branches=...)` is ready); the squash rule with PR data. Needs Brandon's answer on token access and the GitHub host.
4. **Odin into Muninn (steps 2 to 7 of the Muninn doc's plan).** This happens in Odin's code, which isn't in this repo. Step 7's one-time importer needs Brandon's `sqlite3 state.db .schema` output (schema only, no data); he hasn't sent it yet.
5. **Calibration (build step 8).** Enter real hours into `time_actuals`; grid search gap 60-180 m, lead-in 0-60 m, ambient weight 0.3-0.8 for the lowest error among settings that bias low; accepting writes an active `calibration_runs` row and updates `baldur.json`.
6. **Odin posting (step 9)** and **AI review (step 10)**, per the spec.
7. **The other apps**, in the toolset evaluation's order, each writing only its own Muninn tables.

## Waiting on Brandon

- Odin's `state.db` schema (`sqlite3 state.db .schema`), for the step-7 importer.
- The spec's open decisions: post to Jira or report only; Odin's calendar fields; GitHub access (token allowed? Enterprise or github.com?); alerts (notifications at logon, or the tile badge only?); whether AI review is approved and in which mode; whether PR reviews become loggable time; trial length (two or four weeks).
- Optional: his old `gitwork.py` and its 34 tests, if he wants anything from it compared against Baldur.

## Known limitations

- Midnight daylight-saving zones (not US ones) disagree by an hour between SQLite and Python on a day's start.
- Past-dated commits from a wrong clock count on their date.
- Reflog entries inside a session don't extend it (deliberate, biases low).
- The day report and basis say nothing yet about PR reviews (needs the GitHub step).
- Everything Windows-specific in Baldur (`baldur.cmd`, `schedule`, code pages) was written for Windows but tested only on Linux, with the code page simulated.

## Lessons that cost time

- `git for-each-ref` writes a byte as `%1f`; `%x1f` is `git log`'s spelling and comes out as literal text.
- `git log --all` includes `refs/stash`; `--since` stops at the first older commit; read times as Unix seconds (`%at`, `%ct`, `--date=unix`) because offsets can be malformed.
- A new worktree's HEAD reflog has no checkout entry; the replay seeds from the worktree's branch.
- `PRAGMA journal_mode = WAL` doesn't wait on a busy database; `ensure_wal()` retries. `muninn.transaction()` refuses to nest; `Run.batch()` nests with savepoints.
- Tests set `TZ=America/New_York` with `time.tzset()` (POSIX only) so daylight saving can be tested; build test datetimes inside test functions, not at import time. Every other test must pass in any zone, because Windows runs them in the machine's own: one test assumed "now + 2 h" fell on the same local day and failed in zones from UTC+4 up to UTC+6 until it was fixed.
- `.gitattributes` fixes line endings in git checkouts and `git archive` zips (GitHub's Download ZIP); a zip built any other way keeps the files' own endings, so keep `.cmd` and `.ps1` files CRLF on disk.
- `setup --from-git` once read `git config --global`, which skips `[include]` files; it now reads config the way git does. An email set per folder with `includeIf` still needs `--email`.
- In this build environment `python3` (3.13) has no tkinter; `python3.12` does, and `xvfb-run` with ImageMagick `import` takes launcher screenshots.
- Re-run the reviewer-style checks after any change to estimation: the direction tests, the worked example, and a fresh independent review for anything that changes the numbers people approve.
