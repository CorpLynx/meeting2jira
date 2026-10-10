---
inclusion: always
---
# Product: Odin, an Asgard app

Odin is one app in the **Asgard** suite (`Asgard/`: the launcher, the shared Muninn database, Baldur's
time estimates, Heimdall, Ysildir). Since Oct 10, 2026 its code is `Asgard/apps/odin` (package `odin`)
and it keeps its records in Muninn. It is the only app that writes to Jira. Asgard's rules
(`Asgard/AGENTS.md`) apply to it; this file adds what is specific to Odin.

A personal tool for a federal employee whose organization requires every meeting to be tracked in
Jira. Odin:
- reads *their own* Outlook/Teams calendar and creates one Jira **sub-task** per attended meeting
  under a configured parent issue, optionally logging its time and transitioning it;
- reads their Jira issues, calendar and worklogs into Muninn for Asgard's other apps;
- posts the days they approved in Baldur that Jira is missing.

## Load-bearing identifiers (don't rename casually)

| Identifier | Why renaming it is dangerous |
|---|---|
| `m2j-<hash>` dedupe label | Finds an ambiguously created sub-task by exact JQL. Changing it strands every existing sub-task. |
| `meeting_subtasks` (Muninn v5) | The record of which meetings have sub-tasks. Losing it re-creates every meeting. |
| `%LOCALAPPDATA%\Asgard\odin` | config, the DPAPI token, `last_run.json`, the journal and lock. Moved from `%LOCALAPPDATA%\meeting2jira` in Oct 2026: every entry point moves the old folder whole, once, and `state.db` is imported into Muninn on the first run. |
| `odin.cmd`, task "Asgard Odin daily" | The scheduled task's action points at the script. The old `meeting2jira-daily` task is removed by `odin schedule` and by Valhalla. |
| `meeting2jira-graph.cmd`, `meeting2jira-owa.cmd` | The exporters' launchers in `Odin/` kept their names; they find Asgard's `odin.cmd`. |

## Users and environment
- A single standard (non-admin) user on a locked-down federal Windows 11 workstation.
- Classic Outlook, and **Jira Data Center only**, with a personal access token (bearer). Jira Cloud is
  out of scope: no basic auth, no `email`, no `accountId`.
- The M365 tenant may be GCC, GCC High, or DoD.

## Scope boundaries (do not expand without the user asking)
- **Calendar to Jira, one way.** Never modify the calendar. Never edit or delete a Jira issue or worklog.
- **Own calendar only**: no shared, delegate, or other people's calendars.
- **Only meetings that have ended**; never future meetings (worklogs reflect time actually spent).
- **Conservative by default**: skip private, declined, cancelled, all-day and free items, and cap
  creates and posts per run. `--dry-run` shows exactly what would happen and changes nothing.
- **Post less, never more.** Approved Baldur days are posted only for what Jira is missing. When Odin
  can't tell whether something reached Jira, it leaves it for the next run or a person, never sends it twice.
- **Never silently drop real work.** A meeting that overruns the end of a tour of duty counts as inside it.
- **The scan window is not a correctness mechanism.** Muninn's record (key or content hash) prevents
  duplicates, so widening `-DaysBack` is always safe. Don't add deferral queues for late meetings.

## What "good" looks like
- Running it daily produces exactly one sub-task per real meeting, with no duplicates across re-runs
  or sources (COM vs CSV), and Jira's time matches what was approved, never more.
- Failures are loud, specific and actionable (they say what to change), never silent:
  `last_run.json`, `odin status`, and `ATTENTION-Odin.txt` on the Desktop.
- A security reviewer (ISSO) can read `Odin/README.md`'s security notes and approve it without surprises.
