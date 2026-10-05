---
inclusion: always
---
# Product: Odin (package name `meeting2jira`)

## Naming: Odin is the product, `meeting2jira` is the identifier

The program is called **Odin**. `meeting2jira` is still the technical identifier and has NOT been
renamed, deliberately. Treat these as load-bearing and do not rename them casually:

| Identifier | Why renaming it is dangerous |
|---|---|
| `%LOCALAPPDATA%\meeting2jira` | Holds `state.db`, `config.json` and the DPAPI token. Renaming the folder orphans the dedupe state, and the next run re-creates **every meeting ever synced** as duplicate sub-tasks. Needs a migration, not a rename. |
| `Odin/app/src/meeting2jira/` | What `python -m meeting2jira` resolves to. 33 callers and 10 import sites, plus the guardrail wiring tests. |
| `m2j-<hash>` dedupe label | `recover_created_issue` finds an ambiguously-created sub-task by exact JQL on this label. Changing it strands every existing sub-task. |
| `meeting2jira.cmd` / `-owa` / `-graph` | The scheduled task's action path points at the filename. |
| `meeting2jira-daily` task name | Renaming leaves an orphaned task still running the old path. |
| `logging.getLogger("meeting2jira")` | The log namespace all module loggers hang off. |

Prose, titles and user-facing text say Odin. A full identifier rename is a versioned migration with
a state-upgrade plan — see HANDOFF.md.

Odin is one app in the **Asgard** suite (`Asgard/`: launcher, the shared Muninn database, Baldur's
git time estimates). Odin is decided to move into Muninn and stay the only app that writes to Jira;
the plan is `Asgard/docs/muninn-design.md`. This file's scope rules are Odin's; Asgard's are in
`Asgard/AGENTS.md`.

A personal tool for a federal employee whose organization requires every meeting to be tracked in Jira. It reads *their own* Outlook/Teams calendar and creates one Jira **sub-task** per attended meeting under a configured parent issue. It can optionally log work (a worklog equal to the meeting length) and transition the sub-task (e.g. to Done).

## Users and environment
- A single standard (non-admin) user on a locked-down federal Windows 11 workstation.
- Classic Outlook, and **Jira Data Center only**, authenticated with a personal access token
  (bearer). Jira Cloud is explicitly out of scope: no basic auth, no `email`, no `accountId`.
- The M365 tenant may be GCC, GCC High, or DoD.

## Scope boundaries (do not expand without the user asking)
- **One-way**: calendar → Jira. Never modify the calendar. Never delete Jira issues.
- **Own calendar only**: no shared, delegate, or other people's calendars.
- **Only meetings that have ended**; never future meetings (worklogs must reflect time actually spent).
- **Conservative by default**: skip private, declined, cancelled, all-day, and free-time items, and cap creates per run. `--dry-run` must always show exactly what would happen.
- **Never silently drop real work.** Time the user actually spent must not disappear because of a
  classification edge case. A meeting that overruns the end of their tour of duty counts as inside
  it; enabling a setting must not retroactively rewrite history either way.
- **The scan window is not a correctness mechanism.** Dedupe (state DB, key or content hash) is
  what prevents duplicates, so widening the window is always safe. Don't add deferral queues or
  "pending" states to try to catch late meetings; widen `-DaysBack` instead.

## What "good" looks like
- Running it daily (manually or via a scheduled task) produces exactly one sub-task per real meeting, with no duplicates across re-runs or across sources (COM vs CSV).
- Failures are loud, specific, and actionable (they say what to change), never silent.
- A security reviewer (ISSO) can read the README's security notes and approve it without surprises.
