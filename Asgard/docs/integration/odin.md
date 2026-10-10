# Odin and Muninn

Identity `odin` · code `apps/odin` (package `odin`), Muninn side `asgard.muninn.odin` · `muninn.open_app("odin", supported=(5, 5))` · an Asgard app since Oct 10, 2026 (Asgard 0.4.0)

Odin is the only app that talks to Jira, in both directions. It turns your finished meetings into Jira sub-tasks and logs their time, mirrors your Jira issues, calendar and worklogs into Muninn for the other apps, resolves the keys they store, and posts the time you approved in Baldur. It never edits or deletes an issue or a worklog in Jira.

Before Oct 2026 Odin was a separate program (`Odin/app`, package `meeting2jira`) that kept its own `state.db`. It moved into Asgard in the steps of [../muninn-design.md](../muninn-design.md#moving-odin-into-muninn); `Odin/` keeps only the optional Graph and OWA exporters, the Power Platform material and Odin's history.

## Tables

| Owns | Reads |
| --- | --- |
| `work_items`, `work_item_aliases`, `work_item_transitions`, `calendar_events`, `worklogs`, `meeting_subtasks` (v5) | `v_unknown_keys`, `v_worklogs_to_post`, `v_day_status`, `v_unpostable_days`, `v_busy_meetings`, `day_proposals` (through the views), `identities` |

Shared operations it uses: `sources` (`jira-dc` and one `calendar:<export>` per export path), `identities` (`jira_user`), `sync_runs`, `sync_cursors`, `events`.

## Where things are

| | |
| --- | --- |
| `apps/odin/odin.cmd` | The command line on Windows: `odin`, `odin preview`, `odin setup`, `check`, `status`, `sync`, `post`, `report`, `doctor`, `schedule`, `csv FILE`, `run ARGS`, `cli ARGS` |
| `apps/odin/cli.py` | The Python entry point (`daily`, `push`, `sync`, `post`, `status`, `forget`, `report`, `init`, `set-token`, `check`) |
| `apps/odin/odin.pyw`, `ui/` | The window: Asgard's shared window with Today, Meetings and My issues (`odin/ui_backend.py`) |
| `apps/odin/windows/` | `Invoke-MeetingSync.ps1` (the Outlook export, then `cli.py daily`), `Export-OutlookMeetings.ps1`, `Register-MeetingSyncTask.ps1` (the "Asgard Odin daily" task), `Test-Environment.ps1` (`odin doctor`) |
| `apps/odin/tools/` | `Invoke-WindowsChecks.ps1` (`odin selftest`), `Test-PowerShellSyntax.ps1` |
| `%LOCALAPPDATA%\Asgard\odin\` | `config.json`, `jira_token.dpapi`, `logs\`, `exports\`, `last_run.json`, `ATTENTION-Odin.txt` on failure, `odin.lock` (locked by Windows during a run), `unrecorded.jsonl` if Muninn ever refused a record, `state.db.migrated-DATE` for 30 days after the import |

## The daily run

`odin` (or the scheduled task) exports yesterday and today from Outlook and runs `cli.py daily` on the file. In order, each write its own short transaction and none held across a Jira call:

1. **What only Odin knew.** Records in the journal (`unrecorded.jsonl`) go into Muninn, then `state.db` is imported once (below).
2. **Posts nobody saw finish.** `stuck_posts()` → `find_worklog(key, marker)` → `resolve_stuck(..., searched=True)`. Odin runs one at a time (`odin.lock`), so a `sending` row older than a few HTTP timeouts belongs to a run that is over.
3. **The calendar.** Every item of the export goes into `calendar_events` (a private item with its times and "Private appointment", never its subject), through one `calendar:<export>` source per export path. A JSON export reads a whole window, so its run is `mode="full"` and sweeps; a CSV, or an export that stopped early (`truncated`), only adds and updates. Then meeting worklogs an earlier run couldn't log are retried.
4. **Meetings to sub-tasks** (`odin/sync.py`). Filters, tour of duty and rules decide; `meeting_subtasks` is checked by calendar key or content hash, and a one-off meeting that moved is found by its calendar id (`store.find_moved`: export fields `global_id` and `is_recurring`), reported as `MOVED`, and left as it is, with its record linked to the meeting's new calendar event; Jira is searched for the meeting's `m2j-<hash>` label on issues you created, and only if there's none is the sub-task created with it; the record is written the moment Jira accepts it (or goes to the journal, and the run stops creating); the new sub-task is read back into Muninn; its time is logged through `begin_meeting_post()`; then the transition.
5. **Jira into Muninn** (`odin/collect.py`), each its own sync run with its own cursor: your issues (`assignee was currentUser()`), each tracked parent and its children, keys other apps mention (one GET each), open issues that aren't yours (in batches of 50), issues no run has seen for a week (only a 404 deletes), and your worklogs.
6. **Approved Baldur days.** `posts_due()` → the issue's worklogs read from Jira again → `begin_post()` → Jira → `finish_post()` or `fail_post()`, at most `muninn.max_posts_per_run` (20), and only when the worklog sync of the same run read everything (not stopped at its limit), so Muninn knows what Jira already holds.
7. **The breadcrumb.** `last_run.json`, and `ATTENTION-Odin.txt` on the Desktop raised or taken down. A run that stops early, for any reason (an expired token, Muninn not ready, a full disk, Ctrl+C), writes them too.

`push` is steps 1 to 4 and 7, `sync` 1, 2, 5 and 7, `post` 1, 2, the key lookups and worklog sync, then 6 and 7. `--dry-run` (`odin preview`) reads Muninn and `state.db`, writes nothing anywhere, and calls nothing in Jira.

## What protects Jira from Odin

- **A sub-task is made once.** `meeting_subtasks` is consulted before every create and written right after, before the worklog and the transition. A record Muninn can't take goes to `unrecorded.jsonl` (flushed to disk) and the run stops creating; the next run writes it first, and a preview consults it. Before every create Jira is searched for the meeting's label (`labels = "m2j-..." AND creator = currentUser()`, in every project): one match is adopted, several or a failed search create nothing and are left for a person. So a sub-task Jira made but Odin never recorded (an answer lost or cut off, a 5xx, the run stopped or killed in between, a full disk) is found by the next run, not made again; a create that fails ambiguously is searched for at once too. With `jira.dedupe_label` off there is nothing to search for. `odin.lock` is locked by Windows for the run, so a second run stays out and a run that died leaves nothing behind; it and the journal sit beside Muninn whatever `--config` says.
- **Time is posted once.** Every worklog goes through a `sending` row with a marker, committed before the call. `fail_post()` only on a definite 4xx (`JiraError.refused`); a timeout, a 5xx or a lost answer leaves it `sending`, and the next start settles it by the marker (a 404 while settling leaves it `sending` too: it may be access lost for now). The Jira client retries a GET on 429/502/503/504 and a write only on 429, which Jira refuses before doing anything; every failure, a cut-off or garbled answer included, is a `JiraError`, ambiguous on a write. A redirect to another host is refused, so the token never follows it.
- **Meeting time.** Logged on the meeting's calendar event (`begin_meeting_post()`, which also refuses a twin from another calendar), or, for history without an event, as a manual post with the stored comment. A retry first reads the sub-task's worklogs from Jira: time logged by hand, or by Odin before Muninn, counts. Retries stop 14 days after the sub-task was made, or after three refusals.
- **Less, never more.** Approved days are posted only for what Jira is missing, after a worklog sync that read everything, and each issue's worklogs are read again just before its time goes (one that can't be read waits). `log_work` turned on later doesn't post old meetings (`worklog_wanted`). A meeting worklog Jira refuses fails the run, like a refused approved day.
- **v3 triggers and `v_double_posts`** as before: a posted worklog only becomes `deleted`, Asgard never sends one over 24 hours, and Odin's tile badge shows anything in Jira twice.
- **Secrets.** The PAT is DPAPI-encrypted for your Windows user (`jira_token.dpapi`; `JIRA_PAT` overrides it for development). Error text Muninn keeps goes through `scrub()`.

## Worklogs, and a deviation from the design

The design reads `/rest/api/2/worklog/updated`, which lists every worklog in the whole Jira. Odin asks for the issues you logged time on instead (`worklogAuthor = currentUser()`, then each issue's worklog list), which grows with your work rather than the instance's, and applies `/rest/api/2/worklog/deleted` for removals (an issue drops out of that query once your only worklog on it is gone). The first run reaches back `muninn.history_days` (365) and may take a while; the scheduled task allows 45 minutes. A per-run limit (`max_issues_per_run`, 500) never stops inside a burst of updates, so a bulk edit can't stall a cursor; while the worklog sync is still catching up, approved days wait. Each run reads back 26 hours before its cursor, because Jira reads JQL dates in the Jira profile's time zone, which needn't be this computer's, and search pages overlap, so an issue updated while the list is read can't make it skip one.

## state.db, imported once

Each row of `synced` becomes a `meeting_subtasks` row with origin `state_db` and its own creation time. A worklog still owed (wanted, not logged, under three attempts) is carried as `worklog_wanted = 1`; everything else as 0, so nothing already in Jira is sent again. Then `state.db` becomes `state.db.migrated-YYYYMMDD`, deleted after 30 days. If any row can't be imported, `state.db` stays and Odin keeps consulting it, so none of its meetings is made again. When an imported meeting shows up in an export, its record is linked to the calendar event. Meeting worklogs from before Muninn are marked as meeting time by `classify_meeting_worklogs()` with the comment template's prefix (`Meeting: %`), strictly. The on-prem `state.db` may differ from this repo's; what to check and extend is in [Odin/MUNINN-MIGRATION.md](../../../Odin/MUNINN-MIGRATION.md).

## Settings (`config.json`, section `muninn`)

| Key | Default | |
| --- | --- | --- |
| `sync_issues` | true | Your issues, tracked parents, key lookups, refreshes |
| `sync_worklogs` | true | Your worklogs, so Baldur sees what Jira holds |
| `post_approved` | true | Post approved Baldur days in the daily run (`odin post` posts whatever this says) |
| `max_posts_per_run` | 20 | |
| `history_days` | 365 | How far the first sync reaches |
| `max_issues_per_run` | 500 | Per stream; a bigger first sync carries on next run |

## Python and packages

Odin runs on what Asgard runs on: the packaged build's `asgard-cli.exe`, else the Python Asgard was installed with (`install-ledger.json`), else a Python 3.9+ whose SQLite is 3.37 or newer with FTS5 (3.11+ on Windows). `odin.cmd` and the PowerShell scripts look in that order. The daily run uses only the standard library, because the Python install may carry no packages; the window uses PySide6 like every Asgard window. The packages considered for Odin and why none is used yet (`jira` retries POSTs on a 503 or a dropped connection) are in [../dependency-policy.md](../dependency-policy.md#considered-for-odin-and-not-used-oct-10-2026).

## Events

Emits `work_item.created/.updated/.moved/.done/.reopened/.deleted`, `worklog.posted`, `worklog.failed`. It doesn't consume events: `posts_due()` finds approved days by itself.

## Tests

`tests/test_odin_*.py`, against a temporary Muninn through `open_app` (guard on) and an in-memory Jira (`tests/fake_jira.py`): the pipeline and its filters, ambiguous-create recovery, the calendar and the record of sub-tasks, the journal and the lock, meeting worklogs through every failure (a refusal, an answer lost before or after Jira logged it, a 500, time logged by hand), the Jira sync and its cursors, posting approved days, the `state.db` import, the window backend, the guardrails, and the daily run end to end. `tests/test_ui_qt.py` loads every Odin page offscreen (on Windows CI), and the packaged build runs Odin's dry-run push.
