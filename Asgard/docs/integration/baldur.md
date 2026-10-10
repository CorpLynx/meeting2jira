# Baldur and Muninn

Identity `baldur` · integrated (schema 4) · `apps/baldur/`, approval and agent-estimate rules in `asgard.muninn.baldur`

Baldur collects your git and GitHub activity, estimates development time per ticket per day, and
lets you approve it. It never writes to Jira: approved days go to Odin through
`v_worklogs_to_post`. Every number is biased down; only evidence raises one.

It estimates in two ways, side by side:

- **The manual engine** (`estimate.py`, `store.py`, `calibrate.py`) uses only git, the calendar
  and your settings.
- **The AI-assisted method** (`assist.py`) suggests figures beside the engine's, from an AI coding
  agent's estimates (`agent_estimates`) or a checked AI review.

## Tables

| Owns | Reads |
| --- | --- |
| `repos`, `commits`, `commit_work_items`, `reflog_entries`, `pull_requests`, `pull_request_commits`, `pr_reviews` (facts) | `v_busy_meetings` (meeting time to leave out) |
| `estimate_runs`, `work_sessions`, `session_commits`, `session_allocations`, `day_proposals`, `calibration_runs`, `time_actuals` (estimates and calibration) | `v_worklogs_to_post`, `v_day_status`, `v_unpostable_days` (what Jira holds and can't take) |
| `agent_estimates`, `agent_estimate_commits` (v4: what an AI coding agent said your time on a change was) | `work_item_aliases`, `work_items` (titles, status), `identities` (which commits are yours) |

## Where each write happens

| Module | Writes | How |
| --- | --- | --- |
| `collect.py` | repos, commits, commit keys, reflog | One `muninn.Run(con, "baldur", src, "commits:<folder>")` per repository; remote URLs pass through `redact_url()`; git errors through `scrub()` |
| `github.py` | pull requests, reviews, `pr`-method keys | Runs on streams `github:user`, `github:pulls <owner/name>`, `github:review-requested`; list requests are conditional (ETag), so an unchanged repo costs one request |
| `store.py` | an estimate run with its sessions and proposals | One `muninn.transaction()`; emits `estimate_run.created`. Re-estimating supersedes open proposals, never decided ones |
| `calibrate.py` | `time_actuals` (your real hours), `calibration_runs` | `note` and `forget` are one transaction each. `accept` writes `baldur.json`, then the active calibration and its `calibration.accepted` event in one transaction; if Muninn refuses, the settings file is put back as it was |
| `asgard.muninn.baldur` | approve, approve_day, reject, reject_day, change_approval | One transaction each, with its `day_proposal.approved` event. Figures are whole minutes 0–1440, and keys compare without regard to case. `review=` records which AI-assisted figure you took |
| `asgard.muninn.baldur` | record_agent_estimate, withdraw_agent_estimate | One transaction each, with `agent_estimate.recorded` or `.withdrawn`. Every field is validated, and code-like summaries are refused. The same report twice is stored once, and a newer report from the same agent on the same commits withdraws the older one |
| `asgard.muninn.baldur` | store_review (`day_proposals.review` on open rows) | `check_review` runs inside the transaction against the open rows as Muninn holds them, so a reply checked against a stale day can't be stored |
| `desk.py`, `window.py`, `cli.py`, `assist.py` | nothing directly | They call the modules above, so the window, the CLI and Ysildir (`docs/integration/ysildir.md`) all follow the same rules |

## What protects Baldur's data

- **Decisions are final.** Triggers stop any edit to an approved or rejected proposal except
  approved → superseded, and stop deleting a decided one. Changing a figure is
  `change_approval()`: supersede and insert in one transaction.
- **One open row and one approved row per day and ticket** (unique partial indexes).
- **Rounding only goes down** (`minutes_proposed <= minutes_raw`). Untracked time is never
  approved (a CHECK).
- **v3.** Keys in `commit_work_items`, `pull_requests`, `day_proposals`, `session_allocations` and
  `time_actuals` must look like `PROJ-123`. An approval is at most 1440 minutes.
- **v4. Agent estimates are facts.**
  - A report's figures, commits and text can't be edited or deleted.
  - Its status may only go from recorded to withdrawn, with a withdrawal time.
  - Minutes are 1–1440, and the low end can't exceed them.
  - The summary is at most 300 characters.
  - Commit SHAs are 7–64 lower-case hex characters.
- **AI-assisted figures can't raise a day.** Every suggestion, from an agent or a review, goes
  through `check_review`. The day never rises, every adjustment cites evidence that was sent, no
  ticket is added, and nothing goes below zero; the figures round down again.
- **Squash copies** (`is_merge = 1`) never count as activity (v2's `v_activity`).

## Events

- **Emits:** `estimate_run.created`, `day_proposal.approved`, `calibration.accepted`,
  `agent_estimate.recorded` and `agent_estimate.withdrawn`.
- **Consumes:** nothing today. It reads Odin's results through `v_day_status` and
  `v_worklogs_to_post`, which are always current.

If the review screen should refresh on `worklog.posted`, consume
`["worklog.posted", "worklog.failed", "work_item.moved"]` in one call, because each app has one
cursor.

## Schema range

`baldur.cli.SCHEMA = (4, 5)` (v5 adds only Odin's `meeting_subtasks`). The AI-assisted method reads `agent_estimates` on every day view,
so Baldur needs v4.

## Still to do

- Alerts (Windows notifications) wait on Brandon's choice between notifications and the tile badge
  alone.
- PR reviews as loggable time is an open decision in the Baldur spec.
- An independent review of the GitHub PR-key change is owed (rule: changes to estimates need one).
- An independent review of calibration and the AI-assisted method is owed too (this branch).
- The API tier of the AI review (Mímir) and Ysildir's MCP tools aren't built. The clipboard tier
  works.
