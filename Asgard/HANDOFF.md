# Asgard handoff (2026-10-04, Asgard 0.3.1)

Asgard is a suite of small Python apps for a federal software engineer, Brandon, on a locked-down Windows 11 workstation. A launcher shows one tile per app, and the apps share one SQLite database, Muninn. Built so far: the launcher, installer and uninstaller; Muninn's schema and its Python package; and Baldur's git collector and time estimator, driven from a command line until its window is built. 191 tests pass. The 3 install tests need tkinter and skip without it.

**Where this lives now.** Since 0.3.1 Asgard is the `Asgard/` folder of Brandon's `meeting2jira` repository, next to `Odin/` (Odin's code, docs and tests). Paths in this file are relative to `Asgard/`; `../Odin/` means the sibling folder. From the repo root, `python tools/run_tests.py` runs both suites through pytest (305 tests: 191 Asgard, 114 Odin); the plain `unittest` command below still works from `Asgard/`. The repo's `.kiro/steering/` rules describe Odin; for `Asgard/`, this file and `AGENTS.md` win. The retired first attempt at Muninn and Baldur (`munnin-layer/`, `baldur/`) is archived in `../context-docs/` and isn't used. On Windows, the schema-check wrapper and the 25-hour-day test skip, because Windows Python can't switch time zones. Read `AGENTS.md` before changing anything; it holds the rules this project runs on.

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
- **Brandon** works in Jira (Data Center, personal access tokens), git and GitHub, Outlook and Teams. He already has an app called **Odin** (`../Odin/`, Python package `meeting2jira`) that syncs Jira and logs meeting time, with its own `state.db` (schema in `../Odin/app/src/meeting2jira/state.py`). He asked for "the best technical solution possible, even if it's not exactly as I said", so make well-reasoned decisions, record them in the specs, and tell him what you decided and why.
- **His style.** Plain, concise writing; numbers with units; no filler. Messages the apps show say what happened and what to do next.

## The suite

| App | Job | Status |
| --- | --- | --- |
| Asgard | Launcher, installer (`setup-Asgard.cmd`), tiles with Muninn badge counts | Built (0.1 to 0.3.1) |
| Muninn | Shared SQLite database and `asgard.muninn` package | Built: schema v2 (34 tables, 48 indexes, 30 triggers, 10 views; v2 only takes squash copies out of `v_activity`) |
| Valhalla | Uninstaller, replays the install ledger | Built |
| Baldur | Work-time estimates from git; pull request alerts | Collector, estimator and CLI built; GitHub Enterprise Server settings and remote matching built (0.3.1); window, GitHub API sync, calibration and AI review not yet |
| Odin | Jira issues, calendar, worklogs (Brandon's existing app) | Separate app in `../Odin/`; moving into Muninn is decided, Muninn's `odin.py` side is built |
| Freya | Yearly review drafts with citations | Specified (toolset doc, Muninn tables), not built |
| Loki | BLUFs for meetings and code changes | Specified, not built |
| Heimdall | SeCcHm submissions | CLI built: form file, templates, `fill` through Playwright and Edge, stopping before Submit. Tested against a fake ServiceNow with Chrome on macOS only. Real Edge, PIV and the SeCcHm form are untested. No tile or Muninn `submissions` rows yet |
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
| `asgard/muninn/migrations/` | `0001_initial.sql` (schema v1), `0002_copies_arent_activity.sql` (v2); never edit a shipped migration, add `0003_...`. Baldur opens Muninn with `supported=(2, 2)` |
| `apps/baldur/baldur/` | `settings.py`, `keys.py`, `gitread.py`, `collect.py`, `estimate.py` (pure), `store.py`, `report.py`, `cli.py` |
| `apps/baldur/cli.py`, `apps/baldur/baldur.cmd` | CLI entry points (the `.cmd` tries `py -3`, then `py`, then `python`) |
| `apps/heimdall/heimdall/` | `form.py` (the form file, `settings\heimdall.json`), `templates.py` (`settings\heimdall-templates.json`, and `plan()`: form default < template < `--set`), `browser.py` (the only module that imports Playwright, lazily), `cli.py` (`init`, `fields`, `template list/show/save/edit/delete`, `fill`; `--json` for a UI) |
| `apps/heimdall/cli.py`, `apps/heimdall/heimdall.cmd` | CLI entry points, same pattern as Baldur's |
| `tests/` | `test_heimdall.py` (24), `test_heimdall_browser.py` (13; 11 skip without Playwright, about 2 min with it; `fake_servicenow.py` is its deliberately awkward instance, keep the traps), `test_baldur.py` (96), `test_muninn.py` (48), `test_catalog.py` (14), `test_runner.py` (5), `test_install.py` (3), `test_muninn_schema.py` (1, wraps the 125 schema checks; skipped on Windows) |
| `tools/` | `asgard_preflight.ps1` (policy checks), `check_muninn_schema.py` (125 checks, applying every migration), `baldur_smoke.sh` (end-to-end demo), `make_icon.py` |
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
- GitHub is Brandon's GitHub Enterprise Server (2026-10-04). `github_api` takes its host and stores `https://HOST/api/v3`; only remotes on that host get `github_repo`; committers named "GitHub" or "GitHub Enterprise" write squash copies, from any address. A bad `github_api` turns GitHub off with a warning instead of stopping Baldur, because it never changes a number.
- From the 2026-10-03/04 build and review: identity is git email only; checkout start uses the latest checkout onto the first commit's branch; collection rescans `history_days` (120) every run; `basis_hash` covers only number-changing settings; worktrees fold into their repository; stacked branches resolve to the smallest branch; stash entries and squash copies aren't work.
- Squash copies (review #20, 0.3.1): stored with `is_merge = 1` and no keys (merge commits aren't collected, so that means a copy). A copy is never work, but the sessions it links share one length limit: together they keep no more than the single session they'd form with the copy counted, every session in the stretch scaled alike (`estimate.share_limits`). Skipping a copy can't raise a day, and no ticket in the stretch rises. A SHA any clone marks as a copy isn't work in any clone. Two edges are accepted and recorded in the review: the day cap shares the freed room with other tickets, and with `min_session_minutes` above `idle_gap_minutes` the fix gives what skipping gave.

## What's next

In priority order. Each step should end with tests, the specs updated, and a version bump.

1. **Real-machine trial (Brandon, with your help).** Run `baldur.cmd setup --from-git --project <KEY> --root <folder>`, `collect`, `estimate`, `days` and `report` on his Windows machine and compare a known week. This is the first run on Windows: watch git versions (2.39+ gets `--since-as-filter`), paths with spaces, the console code page, and `schedule` (Task Scheduler with `/IT`, untested on a real machine). Fix what it finds before building more.
2. **Baldur window (spec build step 7).** `apps/baldur/baldur.pyw` (tkinter, same look as the launcher): week view, the day report, edit a figure, approve or reject a day, Copy for timesheet. Reuse `store.compute`, `store.plan`, `report.render_day` and `muninn.baldur`. Then set Baldur's tile in `asgard/apps.json` to `"status": "available", "launch": {"type": "python", "target": "{app}\\apps\\baldur\\baldur.pyw"}`. Keep git, network and database-lock work off the UI thread: the launcher's pattern is a worker thread (`muninn.prepare()` in `_start_muninn`) that the window polls with `after()`.
3. **GitHub (build step 4).** Read-only token in Credential Manager; pull requests, reviews, the `review-requested:@me` search with conditional requests; review-only `repos` rows; alerts via `Shell_NotifyIcon`; PR head-branch keys (`keys.choose(pr_branches=...)` is ready); the squash rule with PR data. The host is decided (Enterprise Server; `settings.github_api_url()` and `Settings.github_host()` are ready). Needs Brandon's answer on token access (fine-grained or classic) and the server's version.
4. **Odin into Muninn (steps 2 to 7 of the Muninn doc's plan).** This happens in Odin's code, in `../Odin/app/src/meeting2jira/`. Odin targets Python 3.8 and `%LOCALAPPDATA%\meeting2jira`, so it reaches Muninn through `ASGARD_APP` as the README shows, and only when Asgard is installed. Step 7's one-time importer reads Odin's `synced` table; its schema is `_SCHEMA` plus `_MIGRATIONS` in `../Odin/app/src/meeting2jira/state.py`.
5. **Calibration (build step 8).** Enter real hours into `time_actuals`; grid search gap 60-180 m, lead-in 0-60 m, ambient weight 0.3-0.8 for the lowest error among settings that bias low; accepting writes an active `calibration_runs` row and updates `baldur.json`.
6. **Odin posting (step 9)** and **AI review (step 10)**, per the spec.
7. **The other apps**, in the toolset evaluation's order, each writing only its own Muninn tables.

## Waiting on Brandon

- **Which Odin the Muninn plan means.** Its steps 3 to 6 assume an Odin with a Jira issue sync, an Assigned to Me view and a tracked-parent pull. The Odin in `../Odin/` has none of them: it reads a calendar export and creates one sub-task per meeting under a configured parent. `begin_meeting_post()` needs the issue in `work_items`, which only step 3 provides, so until that exists the only part of the plan this Odin can ship is the calendar mirror (step 4's first half), which is what moves Baldur off `independent`.
- **Odin's guardrail.** Odin's `test_stdlib_only` rejects `import asgard`, and Odin's rules say `Odin/app/` needs nothing outside itself. Step 2 needs Brandon to allow an optional import (proposed: one Odin module may import `asgard.muninn` from the Asgard install; Odin works unchanged without it, and the guardrail checks that).
- The spec's open decisions: post to Jira or report only; Odin's calendar fields; GitHub access (token allowed? fine-grained or classic? which server version?); alerts (notifications at logon, or the tile badge only?); whether AI review is approved and in which mode; whether PR reviews become loggable time; trial length (two or four weeks).
- **Heimdall and the stdlib rule.** `apps/heimdall/heimdall/browser.py` needs Playwright, and AGENTS.md says Asgard is standard library only. Either record Heimdall's `fill` as the one exception (the toolset doc assumes Playwright for it), or move the browser half out of `apps/` as a separate add-on. Also open: ISSO or system-owner approval for automated filling of SeCcHm.
- Optional: whether to compare Odin's old `gitwork.py` (`../Odin/app/src/meeting2jira/gitwork.py`, tests in `../Odin/app/tests/test_gitwork.py`) against Baldur.

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
- In the original build environment `python3` (3.13) had no tkinter; `python3.12` did, and `xvfb-run` with ImageMagick `import` took launcher screenshots. On Brandon's Mac, Homebrew `python3` needs `python-tk` for the install tests.
- macOS's temp folder is under `/var`, a link to `/private/var`, and Windows can hand out 8.3 short names; Baldur stores resolved paths, so tests resolve their temp folder before comparing (`MuninnCase.setUp`).
- `urllib.parse` answers differently across Python versions (ports such as `+443`) and its errors repeat the URL; `settings.github_api_url()` parses by hand so a token typed into `github_api` is never echoed.
- Re-run the reviewer-style checks after any change to estimation: the direction tests, the worked example, and a fresh independent review for anything that changes the numbers people approve.
- Lowering part of a day by moving a session's start is a trap: the minutes left go to whichever commits are still inside, so one ticket rises while the day falls. Lower a session by scaling it, as the day cap does, so its split across tickets stays the one its commits give. The second review of #20 caught this.
- An open-ended independent review of an estimator change can run past an hour and time out. Give the reviewer a short list of named checks and a time budget.
