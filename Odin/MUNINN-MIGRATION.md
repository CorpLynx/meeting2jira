# Spec: make Odin's state.db import survive the on-prem copy

Status: rewritten Oct 10, 2026 for the merged code (PR #2). Audience: the model working on the
on-prem Odin. Earlier today this file specified a separate import script. That is no longer needed:
Odin is now an Asgard app (`Asgard/apps/odin`, package `odin`), and it already moves `state.db`
into Muninn itself. This file lists what is left: the on-prem `state.db` may differ from the one in
this repo, and the shipped importer was written against the repo's layout.

Where the on-prem code disagrees with this file, the on-prem code wins; report the difference.

## What already exists (don't rebuild it)

`Asgard/apps/odin/odin/history.py`, documented in `Asgard/docs/integration/odin.md` ("state.db,
imported once"):

- `read_rows()` reads `SELECT * FROM synced`, so a table with fewer columns (v0.1.0) or extra
  columns works.
- `_record()` turns a row into a `meeting_subtasks` record (Muninn schema v5, origin `state_db`).
  It needs `key`, `content_hash` (32 lowercase hex), `issue_key`, `parent` and `start_utc`.
- `import_state_db()` inserts rows in one transaction. A row it can't take is listed in
  `ImportResult.failed`, and then `state.db` is **not** renamed: `Legacy` keeps answering "does
  this meeting have a sub-task" from the old file, so nothing is created twice.
- On success it renames the file to `state.db.migrated-YYYYMMDD` and deletes it after 30 days.
- A dry run (`odin preview`) reads `state.db` through `Legacy` and writes nothing.
- Meeting worklogs from before Muninn are marked as meeting time by
  `classify_meeting_worklogs()`, using the comment template's prefix.

So with a drifted `state.db` the failure mode is already safe: rows are reported, the file stays,
no duplicates. The work below makes it *succeed* instead of merely failing safe.

## Rules

- **Never write to Jira** from anything in this spec. Reads only.
- **Never edit or delete `state.db`** outside the existing rename-after-success path. It is the
  only record of those sub-tasks until the import is complete.
- **Fail safe, not guess.** A row that can't be mapped with certainty goes in `failed`; it is never
  given an invented hash, key or time.
- **Don't edit a shipped Muninn migration.** This spec needs none. If a change seems to need a
  schema change, stop and report.
- Keep Odin's rules from `Asgard/AGENTS.md`: Python 3.9+, packages pinned, times are UTC text
  (`YYYY-MM-DDTHH:MM:SSZ`), Jira keys through `muninn.normalize_key()`, and tests that fail
  without the change.

## 1. Look before changing anything

Add `odin history inspect` (or the nearest equivalent to Odin's CLI): prints the schema of
`state.db` only, with no row data:

- tables, and `PRAGMA table_info` for each;
- indexes and `PRAGMA user_version`;
- row count of `synced`;
- for each of `key`, `content_hash`, `issue_key`, `parent`, `start_utc`, `minutes`: whether it is
  present, and how many rows have a value that `_record()` would refuse (for example a
  `content_hash` that isn't 32 hex characters, or a `start_utc` that doesn't parse).

Run it on the workstation and keep the output. That output decides whether sections 2 and 3 are
needed at all. If every column is present and no row would be refused, the shipped importer
already works and you can stop here after running section 5.

## 2. Column differences

Only if `inspect` shows renamed or missing columns:

- Add one `COLUMN_MAP` in `history.py` (logical name → accepted column names) and apply it in
  `read_rows()`, so `_record()` keeps seeing the names it sees today. On-prem renames go there and
  nowhere else.
- Required (a row without these can't be recorded): `key`, `content_hash`, `issue_key`, `parent`,
  `start_utc`. Optional: `summary`, `minutes`, `created_at`, `worklog_wanted`, `worklog_logged`,
  `worklog_attempts`, `worklog_comment`, `worklog_id`.
- If a required column is missing from the whole table, raise `HistoryError` naming the column and
  the `COLUMN_MAP` entry to edit. Don't fall through to per-row failures: 5,000 identical failure
  lines hide the cause.
- If `minutes` is missing but an end time column exists, compute `minutes` from the two times;
  otherwise use 0 (`SubtaskRecord` already allows it) and say so in the result.
- Columns and tables the importer doesn't use are listed in `odin preview`'s output as "not
  imported". Nothing is dropped silently.

## 3. Hashes that aren't Odin's current format

`_record()` refuses a `content_hash` that isn't 32 lowercase hex characters. Older or modified
Odin builds may have written something else. Don't loosen the check: `meeting_subtasks` finds a
past meeting by `meeting_key` or `content_hash`, and a value the current code can't recompute
would never match, so loosening hides the problem. Instead:

- Count such rows in `inspect`.
- If there are any, tell Brandon before changing code. The options are to import them with the
  hash recomputed from `summary` and times (only if the summary template can be reversed and
  verified), or to leave them in `state.db` and let `Legacy` keep answering for them. The second
  is already how it works.

## 4. Marking earlier meeting worklogs by id

Today `classify_meeting_worklogs()` matches by comment, start and length. A row that has
`worklog_id` records exactly which Jira worklog Odin posted for that meeting, which is stronger.
After `history` has imported and the worklog sync has run (so those worklogs are in Muninn):

- For each imported record with a `worklog_id` that is not already linked, find
  `worklogs.jira_worklog_id = worklog_id` on that sub-task, then
  `odin.adopt_meeting_worklog(con, worklog_id_in_muninn, calendar_event_id)`. This needs the
  record's calendar event, which exists once the meeting shows up in an export (the code already
  links the record then; adopt at the same point).
- Leave `classify_meeting_worklogs()` for rows without `worklog_id`, as now.
- Never adopt anything else. Reading development time as a meeting is the safe failure; reading a
  meeting as development time makes Baldur propose posting it again.

## 5. Tests (use the existing patterns)

`Asgard/tests/test_odin_*.py` build a temporary Muninn through `open_app` and use
`tests/fake_jira.py`. Build `state.db` fixtures in code, not as checked-in files:

| Fixture | Proves |
| --- | --- |
| The repo layout, plus four rows | Baseline import; second run changes nothing |
| No `worklog_*` columns (v0.1.0) | Optional columns stay optional |
| One renamed column, one extra column, one extra table | `COLUMN_MAP` works; extras are reported |
| A required column missing | `HistoryError` names the column; `state.db` untouched |
| One row with a bad hash | It's in `failed`, `state.db` is not renamed, `Legacy` still answers for it |
| A row with `worklog_id` and a matching Jira worklog | Adopted by id, no comment match needed |
| A row with `worklog_id` that Jira doesn't have | Left alone, reported |

Assertions to keep: `state.db`'s bytes are unchanged on a dry run and on a failed import; the
fake Jira sees only GET requests from this code; `muninn.integrity.check()` is clean at the end;
`v_double_posts` is empty.

## 6. Verify on the workstation

Needs target-machine verification until it has run there (the real `state.db`, Asgard's Python,
Jira Data Center):

1. `odin history inspect`; compare with the layout in `Odin/app/src/meeting2jira/state.py`.
2. `odin preview`: the number of meetings it would create must be what you expect (usually 0 for
   anything already synced). A nonzero number for old meetings means `Legacy` isn't matching.
3. A real daily run; the log line "Moved N sub-task record(s) from state.db into Muninn" and no
   "couldn't move" warning.
4. `Asgard.pyw --muninn check` is clean.
5. Add what you could not run to the checklist in `Asgard/HANDOFF.md`.

## Still open on the Asgard side

- Odin stores each calendar event under its export `key` (`store.event_from_meeting`), not under
  the id `muninn.odin.event_from_outlook()` would build. That is consistent for Odin. Anything else
  that writes the same calendar into Muninn must use the same key, or a meeting lands twice in
  `v_busy_meetings`.
- `odin history inspect` is a new command; the CLI has no `history` command today
  (`Asgard/apps/odin/odin/cli.py`), so pick the nearest fit.
- `muninn.integrity.check()` on an app (`open_app`) connection reports a guard refusal as a
  damaged search index. Run checks through `Asgard.pyw --muninn check`.
