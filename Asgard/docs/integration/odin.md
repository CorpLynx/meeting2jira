# Odin and Muninn

Identity `odin` · package `asgard.muninn.odin` (ready, 0.2+) · Odin's move is being done separately; this page is its contract.

Odin is the only app that talks to Jira, in both directions. It mirrors your Jira issues, calendar and worklogs into Muninn, resolves the keys other apps store, and posts the time you approved. It never edits or deletes a worklog in Jira.

## Tables

| Owns | Reads |
| --- | --- |
| `work_items`, `work_item_aliases`, `work_item_transitions`, `calendar_events`, `worklogs` | `v_unknown_keys`, `v_worklogs_to_post`, `v_day_status`, `day_proposals` (status only), `identities` |

Shared operations it uses: `sources`, `identities`, `sync_runs`, `sync_cursors`, `events`.

## How Odin calls Muninn

| Job | Calls | Notes |
| --- | --- | --- |
| Open | `muninn.open_app("odin", supported=(1, 3))` | Needs Asgard's Python (3.9+, and SQLite 3.37+, so 3.11+ on Windows). Odin's own floor is 3.8, so its Muninn path runs only when Asgard is installed; see "Before Odin can import Muninn". |
| Issue sync | `JiraContext.load`, then per page `run.batch()` → `upsert_issue(run, raw, ctx)`; `run.advance_cursor(parse_time(updated))`; `jql_time(run.cursor)` for the next JQL | Emits created, updated, moved, done, reopened |
| Deletions | `not_seen_since()` → ask Jira for each → `mark_issue_deleted()` on a 404 only | Leaving Odin's JQL scope is not a deletion |
| Key lookups | `unknown_keys()` → `GET /issue/{key}` → `record_lookup(run, key, raw_or_None, ctx)` | Writes aliases: current, moved or not_found |
| Calendar | `upsert_calendar_event(run, event_from_graph(raw) or event_from_outlook(item))`; then `sweep_calendar(run, start, end)` | The sweep needs `mode="full"` and a run with no problems (v3) |
| Meeting keys | `set_meeting_key(con, event_id, key)` | Stored upper case; refuses anything that isn't a key |
| Worklog sync | `upsert_worklog(run, raw, ctx)`; `mark_worklog_deleted(run, id)` from `/worklog/deleted` | Only your worklogs are stored; a marker in the comment reconciles a `sending` row |
| Posting | `posts_due()` → `begin_post()` → Jira → `finish_post()` or `fail_post()` | Same pattern for `begin_meeting_post()` and `begin_manual_post()` |
| Crash check (start) | `stuck_posts()` → search the issue's worklogs for each marker → `resolve_stuck(con, id, found_id, searched=True)` | Without `searched=True`, a missing id is refused: guessing "not there" would post twice |
| History | `adopt_meeting_worklog()`, `classify_meeting_worklogs("Meeting:%")` | One-time, strict matching only |

## What protects Odin's data

- **Never twice.** A `sending` row and marker are committed before each Jira call. `v_worklogs_to_post` offers nothing for an issue and day while a post is in doubt, and each approval posts at most once.
- **v3 triggers.** A posted worklog can only become `deleted`; a worklog Asgard sent is never deleted unless it failed; Baldur time needs an approved proposal at insert; Asgard never creates a worklog over 24 hours. Worklogs read from Jira are stored as Jira has them, whatever their length.
- **`v_double_posts`** and Odin's first tile badge show any approval or meeting in Jira more than once, and `--muninn check` reports it as an error with the worklog ids to delete.
- **Secrets.** `fail_post` and sync failures store error text through `scrub()`. The Jira PAT stays in Odin's DPAPI file.

## Events

Emits `work_item.created/.updated/.moved/.done/.reopened/.deleted`, `worklog.posted`, `worklog.failed`. May consume `day_proposal.approved` to post without waiting for its next scheduled run; `posts_due()` already finds the work, so consuming is an optimisation, not a requirement.

## Before Odin can import Muninn

These are decisions for whoever moves Odin, recorded so they aren't rediscovered:

1. **The import rule.** Odin's guardrail rejects `import asgard` because `Odin/app/` must run with nothing else installed. Proposed: one Odin module may import `asgard.muninn` from the Asgard install folder; everything else in Odin works unchanged without it, and the guardrail checks that the import is confined to that module and is optional.
2. **The Python.** Odin's scheduled task runs on whatever Python Odin found. Its Muninn path must run on Asgard's (recorded in `%LOCALAPPDATA%\Asgard\install-ledger.json`), or `open_app` raises its "run it with the same Python as Asgard" error.
3. **Exit codes stay Odin's.** 2 for config and credential errors is a contract with its scheduled task; Muninn errors map to 2 there too.
4. **The order.** The eight steps in [../muninn-design.md](../muninn-design.md#moving-odin-into-muninn). Until step 4 ships, Baldur's meeting policy is `independent`; until step 6, Baldur is report-only.

## Tests Odin needs

- Every Muninn call above against a temporary Muninn, through `open_app` (guard on).
- A post interrupted at each point (before the call, after Jira answered, after a timeout) ends with exactly one worklog in Jira and `integrity.check()` clean.
- A calendar window synced incrementally never sweeps.
