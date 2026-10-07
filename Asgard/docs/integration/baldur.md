# Baldur and Muninn

Identity `baldur` · integrated (schema 2–3) · `apps/baldur/`, approval rules in `asgard.muninn.baldur`

Baldur collects your git and GitHub activity, estimates development time per ticket per day, and lets you approve it. It never writes to Jira: approved days go to Odin through `v_worklogs_to_post`. Every number is biased down; only evidence raises one.

## Tables

| Owns | Reads |
| --- | --- |
| `repos`, `commits`, `commit_work_items`, `reflog_entries`, `pull_requests`, `pr_reviews` (facts) | `v_busy_meetings` (meeting time to leave out) |
| `estimate_runs`, `work_sessions`, `session_commits`, `session_allocations`, `day_proposals`, `calibration_runs`, `time_actuals` (estimates) | `v_worklogs_to_post`, `v_day_status`, `v_unpostable_days` (what Jira holds and can't take) |
| | `work_item_aliases`, `work_items` (titles, status), `identities` (which commits are yours) |

## Where each write happens

| Module | Writes | How |
| --- | --- | --- |
| `collect.py` | repos, commits, commit keys, reflog | One `muninn.Run(con, "baldur", src, "commits:<folder>")` per repository; remote URLs pass through `redact_url()`; git errors through `scrub()` |
| `github.py` | pull requests, reviews, `pr`-method keys | Runs on streams `github:user`, `github:pulls <owner/name>`, `github:review-requested`; list requests are conditional (ETag), so an unchanged repo costs one request |
| `store.py` | an estimate run with its sessions and proposals | One `muninn.transaction()`; emits `estimate_run.created`. Re-estimating supersedes open proposals, never decided ones |
| `asgard.muninn.baldur` | approve, approve_day, reject, reject_day, change_approval | Each one transaction with its `day_proposal.approved` event; figures are whole minutes 0–1440 and keys compare without regard to case |
| `desk.py`, `window.py`, `cli.py` | nothing directly | They call the modules above, so the window, the CLI and any future Ysildir tool follow the same rules |

## What protects Baldur's data

- **Decisions are final.** Triggers stop any edit to an approved or rejected proposal except approved → superseded, and stop deleting a decided one. Changing a figure is `change_approval()`: supersede and insert in one transaction.
- **One open and one approved row per day and ticket** (unique partial indexes).
- **Rounding only goes down** (`minutes_proposed <= minutes_raw`); untracked time is never approved (CHECK).
- **v3.** Keys in `commit_work_items`, `pull_requests`, `day_proposals`, `session_allocations` and `time_actuals` must look like `PROJ-123`; an approval is at most 1440 minutes.
- **Squash copies** (`is_merge = 1`) never count as activity (v2's `v_activity`).

## Events

Emits `estimate_run.created`, `day_proposal.approved`. Consumes nothing today; it reads Odin's results through `v_day_status` and `v_worklogs_to_post`, which are always current. If the review screen should refresh on `worklog.posted`, consume `["worklog.posted", "worklog.failed", "work_item.moved"]` in one call (one cursor per app).

## Schema range

`baldur.cli.SCHEMA = (2, 3)`. v3 needed no code change; the range was widened after the suite passed against it.

## Still to do

- Alerts (Windows notifications) wait on Brandon's choice between notifications and the tile badge alone.
- PR reviews as loggable time is an open decision in the Baldur spec.
- An independent review of the GitHub PR-key change is owed (rule: changes to estimates need one).
