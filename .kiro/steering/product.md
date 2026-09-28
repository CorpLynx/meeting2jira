---
inclusion: always
---
# Product: meeting2jira

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
