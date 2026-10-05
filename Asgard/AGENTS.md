# Working on Asgard: rules for coding agents

Start every session by reading `HANDOFF.md` (state, decisions, what's next), then this file. When you finish a piece of work, update `HANDOFF.md` so the next session starts from the truth.

## Non-negotiables

1. **Standard library only, Python 3.9+.** No third-party packages, no compiled wheels, no `match`, no `X | Y` outside annotations (annotations stay behind `from __future__ import annotations`), no 3.10+ standard-library APIs. Check with `vermin -t=3.9- --no-tips --eval-annotations --violations apps asgard tests`. Separately, Muninn needs Python's bundled SQLite to be 3.37+ with FTS5 and JSON (`muninn.sqlite_problems()`); on Windows that means Python 3.11+, so user-facing messages ask for 3.11.
2. **Windows, per user, no admin.** No services, nothing that needs elevation, no executables written into the user's profile, registry writes only under HKCU. Paths come from `asgard.paths`; tests point `ASGARD_HOME` at a temporary folder.
3. **Muninn's rules.** Only Asgard migrates (`muninn.prepare()`). Apps open Muninn with `muninn.open_app(app, supported=(low, high))`. Each app writes only the tables it owns (the migration's header lists owners), through `asgard.muninn` or with `muninn.transaction()` / `muninn.Run`. Never hold a write transaction across a git call, a network call or a UI wait. Never edit a shipped migration: add `asgard/muninn/migrations/000N_name.sql` and extend `tools/check_muninn_schema.py`.
4. **Baldur biases down.** Every cap, rounding and tie-break goes down; only evidence may raise a number. A change to estimation needs the direction tests to pass and a named test for the new rule. Baldur never writes to Jira; Odin is the only Jira writer.
5. **Decisions are final.** Triggers stop edits to approved or rejected proposals. Change an approval by superseding and inserting, through `muninn.baldur` (`approve`, `approve_day`, `reject`, `reject_day`, `change_approval`).
6. **Metadata only.** Never store diffs or commit bodies in Muninn, and never send code off the machine. AI features run at three tiers (API, MCP, clipboard) and the clipboard tier must always work.

## Definition of done

- `python3 -m unittest discover -s tests` passes (`py -3 -m unittest discover -s tests` on Windows). New behavior has tests; every bug fix has a regression test that fails without the fix.
- vermin reports no violations for Python 3.9.
- When Baldur changes, `bash tools/baldur_smoke.sh` still ends with the worked example: PROJ-42 1h30m, PROJ-51 30m.
- Docs match the code: the snapshot in `docs/` and, if you can reach it, the live Claude Doc (links in `HANDOFF.md`); `README.md` for anything a user sees; `HANDOFF.md` for status.
- Anything that changes estimates, approvals or posting gets an independent review before it's called done: a fresh agent with no stake in the code tries to break it with scripts it runs. Fix what it confirms, add each repro as a test, and record it as in `docs/review-2026-10-04.md`.
- Shipping means a `VERSION` bump and a zip of the repo for Brandon.

## Code conventions

- Each module opens with a docstring saying what it's for and which rules it keeps. Comments explain why, not what.
- Times in Muninn are UTC text, `YYYY-MM-DDTHH:MM:SSZ` (`muninn.to_ts()`, `muninn.from_ts()`). Local days come from `estimate.local_midnight()` and `estimate.local_day()`. Don't let naive datetimes cross a function boundary.
- Git goes through `gitread.run_git()` (quoting off, UTF-8 output, no console window on Windows). The one exception is `gitread.patch_ids()`, which pipes `git log -p` into `git patch-id` itself; give any new pipe the same flags and timeout handling. Read refs explicitly (`--branches --remotes --tags HEAD`, never `--all`) and times as Unix seconds.
- Messages people see use plain words and say what to do next. CLI output is ASCII. Expected failures raise `CliError`, `CollectError`, `GitError`, `MuninnError` or `SettingsError` with that message, never a traceback; the CLI also turns any `sqlite3.Error` into a message.
- `.cmd` and `.ps1` files are ASCII with CRLF line endings; PowerShell must run in Constrained Language Mode (cmdlets only, no `Add-Type`).
- Tests use real temporary git repositories, not a mocked `subprocess`; set `TZ` with `time.tzset()` only where it exists, and make every other test pass in any zone (Windows runs them in the machine's own; `HANDOFF.md` has the zone loop); build datetimes inside the test, not at import time.
- Follow the patterns already here (look at `store.py`, `collect.py` and `asgard/muninn/sync.py`) before inventing new ones.

## Working with Brandon

- He asked for the best technical solution, even when it differs from what he described. Decide, record the decision and its reason in the spec, and tell him in a sentence.
- Ask only when a choice can't be undone and could reasonably go either way, or when you need something only he has: Odin's code or `state.db` schema, agency policy answers, access to his machine.
- Report outcomes, not process: what changed, how you verified it, what's next. Keep it short and specific, with numbers and units.
- Don't commit, push or publish unless he asks.
