<!-- Snapshot exported 2026-10-04 from the live Claude Doc: https://claude.ai/code/artifact/bc161cb2-9cfc-4f28-9e1e-8a1075972208
     The live doc is the source of truth; diagrams appear here only as placeholders.
     Edited here on 2026-10-09 (branch claude/baldur-estimation), not yet in the live doc: the v4
     sentences and check count in the schema paragraph, Baldur's tables in "Who owns what", "Agent estimates (v4)", the
     two v4 indexes, the migrations list, and the encryption open decision; and the 2026-10-09 review's fixes in
     "Agent estimates (v4)". Edited on 2026-10-10: v5 (`meeting_subtasks`) in the schema paragraph, "Who owns what", the
     Odin tables, the indexes, the migrations list, "Moving Odin into Muninn" and the state.db open decision.
     Carry these over to the live doc. -->

# Muninn data layer design

Oct 3, 2026 · @Brandon

Muninn is one SQLite file of normalized facts, each keyed by its source system's ID, plus an append-only event log. Every app writes through one Python module, so the schema, provenance and migrations live in one place. Jira issue keys join the apps: Baldur records the keys it finds in git, Odin resolves them and is the only app that writes to Jira, and Freya keeps a durable copy of every issue you finish.

The executable version ships in Asgard 0.2 and later as `asgard/muninn/migrations/0001_initial.sql`: 34 tables plus the search index, 48 indexes, 30 triggers and 10 views. The `asgard.muninn` package applies it and carries each app's write rules. Schema v2 (`0002_copies_arent_activity.sql`, Asgard 0.3.1) takes squash copies out of `v_activity`. Schema v3 (`0003_hardening.sql`, Asgard 0.4.0) moves rules that held only in Python into the database. It adds:

- Jira-key checks on every cross-app key column.
- Size limits: 1440 minutes for an approval and for a day's approvals together; 24 hours for a worklog Asgard sends.
- Posted worklogs can't go back or be deleted.
- Baldur time only for an approved day.
- Event cursors can't pass the newest event.
- `v_double_posts` and its tile badge.
- `pull_request_commits`, `pull_requests.merge_commit_sha` and `commits.message_keys` for Baldur's GitHub evidence.
- Day status that counts a Baldur worklog for its approved day.
- `run_id` indexes for pruning.

Schema v4 (`0004_agent_estimates.sql`, also Asgard 0.4.0) adds Baldur's agent estimates and is additive. Schema v5 (`0005_odin_meetings.sql`, also Asgard 0.4.0) adds `meeting_subtasks`, the record of which meetings Odin has made Jira sub-tasks for, which replaces the `synced` table of Odin's own `state.db`; it is additive too. At v5 the schema has 38 tables plus the search index, 60 indexes, 63 triggers and 11 views.

The schema's own script runs 204 checks. The tests cover migrations, sync runs, Odin's flows, Baldur's approvals, the tile badges, the guard and the hardening (`tests/test_muninn*.py`). Two independent reviews of the hardening are recorded in [review-2026-10-06.md](review-2026-10-06.md), and running the file by hand is [muninn-operations.md](muninn-operations.md).

## Rules every app follows

These nine rules are the contract. The writer module and the schema enforce them, so no app has to remember them. From Asgard 0.4 the connection `open_app()` returns also carries a guard (`asgard/muninn/guard.py`):

- An SQLite authorizer refuses writes to tables the app doesn't own, schema changes, ATTACH, and setting any PRAGMA but `busy_timeout`, `cache_size` and `foreign_keys = ON`.
- Per-connection triggers keep the app to its own rows in the shared tables: its own events, runs, cursors, sources it reads and identity kinds.

How each app integrates is in [integration/](integration/README.md).

1. **One writer module.** Apps call `muninn.upsert_*()` and `muninn.emit()`, never raw SQL. The module ships inside Asgard as `asgard.muninn`, and Odin imports it from the Asgard install, so there is one copy of the schema, migrations and connection settings.
2. **Every fact carries provenance.** A row records its source system (`source_id`), its ID there (Jira id, commit SHA, meeting ID) and the sync run that last touched it.
3. **Upserts are idempotent.** Natural keys are UNIQUE, and writes use `INSERT … ON CONFLICT DO UPDATE`, so re-running a sync changes nothing unless the source changed. Items that vanish at the source get `deleted_at`, never a DELETE.
4. **Times are UTC ISO-8601 text,** like 2026-10-02T14:05:00Z. A day is a `local_date` in your Windows time zone; bare dates are only for calendar facts such as review periods.
5. **One writer per table.** Each table has one owning app, listed below. Every app may read every table; `events` is the one table all apps append to.
6. **Jira keys join the apps.** Outside Odin, a Jira issue is stored by its key as text (`work_item_key`), never by Odin's row id. Odin resolves every key in `work_item_aliases`, so a key it hasn't seen yet, or one that moved projects, never blocks another app's write.
7. **Events are append-only.** Each meaningful change emits one `events` row (`work_item.done`, `day_proposal.approved`) that is never updated or deleted. Each consuming app keeps its place in `event_cursors`.
8. **People approve anything that leaves Asgard, and only Odin writes to Jira.** You approve time in Baldur; Odin posts only approved minutes. BLUFs and submissions carry their own approved state. CHECK constraints and a trigger enforce the order, and a decided proposal can't be edited, only replaced.
9. **No secrets, minimal content.** Tokens live in Credential Manager. Store summaries, keys and links rather than transcripts or full ticket descriptions, unless an open decision below says otherwise.

## Who owns what

Each app writes only its own tables and reads anything it needs; the views in the last column are the contracts between apps.

| App | Writes (sole owner) | Reads from other apps |
| --- | --- | --- |
| Odin | `work_items`, `work_item_aliases`, `work_item_transitions`, `calendar_events`, `worklogs`, `meeting_subtasks` | `v_unknown_keys` (keys to look up), `v_worklogs_to_post` (approved time to post) |
| Baldur | `repos`, `commits`, `commit_work_items`, `reflog_entries`, `pull_requests`, `pull_request_commits`, `pr_reviews`, `calibration_runs`, `estimate_runs`, `work_sessions`, `session_commits`, `session_allocations`, `day_proposals`, `time_actuals`, `agent_estimates`, `agent_estimate_commits` | `work_item_aliases` and `work_items` (titles, status), `v_busy_meetings`, `v_day_status` (what Jira already holds) |
| Loki | `meetings`, `action_items`, `blufs` | `calendar_events`, `commits`, `pull_requests`, `work_items` |
| Freya | `accomplishments`, `review_periods`, `review_drafts`, `citations` | `events` (done and reopened), `work_items`, `v_day_status`, `commits`, `pull_requests`, `blufs` |
| Heimdall, Bifrost | `submissions`, `submission_status_history` | `work_items` |
| Shared | `meta`, `sources`, `identities`, `sync_runs`, `sync_cursors`, `events`, `event_cursors` | — |
| Asgard launcher | Runs migrations; writes no rows | `v_tile_badges` |

Odin moves into Muninn (decided): its Jira cache, calendar and worklogs live only here, written through `asgard.muninn`. Its Assigned to Me view and tracked-parent pull become queries on these tables. Moving Odin into Muninn, below, lists the steps.

## Entity map

&#91;embedded content: Muninn tables by app · 34 tables\]

Solid arrows are foreign keys. Dashed arrows are joins by Jira key, resolved through `work_item_aliases`, so Baldur can store a key Odin hasn't fetched yet and Freya's record survives an issue moving projects.

## Conventions

One set of conventions keeps all 34 tables consistent, and SQLite enforces most of them, so a bad value fails at write time instead of surfacing in Freya's output.

| Thing | Convention | Enforced by |
| --- | --- | --- |
| Tables | STRICT: every value must fit its column type, so `'yes'` in an INTEGER column fails | STRICT table option (SQLite 3.37+) |
| Row IDs | `id INTEGER PRIMARY KEY` | rowid alias |
| Source IDs | The source system's ID is a natural key: (source\_id, jira\_id), (repo\_id, sha) | UNIQUE constraint |
| Timestamps | TEXT, UTC, YYYY-MM-DDTHH:MM:SSZ, whole seconds; names end in `_at` | `CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', col) IS col)` |
| Dates | TEXT YYYY-MM-DD; names end in `_on`, or `local_date` for a day in your time zone | `CHECK (date(col) IS col)` |
| Booleans | INTEGER 0 or 1, named `is_*` or `by_me` | `CHECK (col IN (0,1))` |
| Enums | Lowercase TEXT from a closed list | `CHECK (col IN (…))` |
| JSON | TEXT holding an array or an object (labels, payload, params) | `CHECK (json_valid(col) AND json_type(col) = …)` |
| Jira issues outside Odin | `work_item_key` TEXT, resolved through `work_item_aliases` | Writer module; `v_unknown_keys` |
| Links across apps | A row may point at another app's row (`worklogs.proposal_id`); the owner never has to update it | Foreign key |
| Ownership | `is_mine` set at write time by matching identities | Writer module |
| Provenance | `source_id`, `first_seen_at`, `last_seen_at`, `run_id` on synced tables | Schema + writer |
| Removal | `deleted_at` instead of DELETE | Writer module |
| Names | snake\_case plural tables; `ix_` and `ux_` index prefixes | Review |

The timestamp check uses `IS`, not `=`. `strftime()` returns NULL for garbage, and a CHECK that evaluates to NULL passes; `IS` makes it fail while still allowing NULL in optional columns. It rejects spaces, a missing `Z`, fractional seconds and impossible dates such as month 13.

The writer module sets these on every connection; the preflight script reports your SQLite version.

```sql
-- once per database file (persists)
PRAGMA journal_mode = WAL;     -- readers never block the writer
-- every connection
PRAGMA foreign_keys = ON;      -- SQLite ships with foreign keys off
PRAGMA busy_timeout = 5000;    -- wait up to 5 s for another writer
PRAGMA synchronous = NORMAL;   -- safe with WAL, faster commits
```

## Provenance, sync and events

Seven shared tables record where every fact came from, when, and which changes each app has already handled; none holds work data. `meta` is plain key/value pairs (owner\_upn, created\_at, asgard\_version).

**`sources`**: one row per external system.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `kind` | TEXT | jira, confluence, git, github, calendar, teams, secchm, manual | `manual` = pasted or file input |
| `name` | TEXT | `UNIQUE` | `jira-dc`, `github`, `local-git`, `paste` |
| `base_url` | TEXT | nullable | NULL for local git and pasted input |
| `created_at` | ts | defaults to now |  |

**`identities`**: your accounts in each system. Every `is_mine` and `by_me` flag comes from matching these at write time.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `kind` | TEXT | git\_email, git\_name, jira\_user, github\_login, m365\_upn, display\_name |  |
| `value` | TEXT | `COLLATE NOCASE`; `UNIQUE (kind, value)` | `Me@Agency.gov` matches `me@agency.gov` |
| `source_id` | INTEGER | FK `sources`, nullable |  |

**`sync_runs`**: one row per collector run.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `app` | TEXT | muninn, huginn, odin, baldur, loki, freya, heimdall, bifrost, ysildir, valkyrie |  |
| `source_id` | INTEGER | FK `sources` |  |
| `stream` | TEXT | not null | `issues`, `worklogs, calendar, commits:<repo>, pull``s` |
| `mode` | TEXT | incremental, full | Only full runs may set `deleted_at` |
| `started_at`, `finished_at` | ts | start defaults to now |  |
| `status` | TEXT | running, ok, partial, failed |  |
| `items_seen`, `items_changed` | INTEGER | ≥ 0 |  |
| `cursor_before`, `cursor_after` | TEXT |  | Lets you replay a bad run |
| `error` | TEXT |  |  |

**`sync_cursors`**: the high-water mark per `(source_id, stream)`, such as Jira's last `updated` time or a repo's last SHA. The writer advances it only after the run's transaction commits, so a crash re-reads instead of skipping.

**`events`**: the append-only change log and the one table every app writes.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `at` | ts | defaults to now |  |
| `app` | TEXT | same list as `sync_runs.app` |  |
| `kind` | TEXT | `noun.verb`, checked by GLOB | `work_item.updated`, `bluf.posted` |
| `entity_type`, `entity_id` | TEXT, INTEGER | type not null | Table name and row id |
| `ref` | TEXT |  | External ID: Jira key, SHA |
| `run_id` | INTEGER | no foreign key, by design | Events outlive pruned runs |
| `payload` | TEXT | JSON object | Changed fields, old and new |

Triggers `events_no_update` and `events_no_delete` abort any UPDATE or DELETE on `events`.

**`event_cursors`**: one row per consuming app, holding the last event id it handled. A consumer reads `events WHERE id > last_event_id AND kind IN (…)` and advances its cursor in the same transaction as the writes those events caused, so a crash replays an event instead of skipping it. `ix_events_kind` plus the rowid every index carries serves that read.

The events other apps act on:

| Kind | Emitted by | Acted on by |
| --- | --- | --- |
| `work_item.done` | Odin, when a sync sees an issue enter the done category; payload has resolution and resolved\_at | Freya creates or refreshes the accomplishment |
| `work_item.reopened` | Odin, when an issue leaves done | Freya marks the accomplishment reopened |
| `work_item.moved` | Odin, when an issue's key changes; payload has old and new key | Baldur relabels its review screen; nothing is rewritten |
| `day_proposal.approved` | Baldur | Odin posts on its next run instead of waiting for a sync |
| `worklog.posted`, `worklog.failed` | Odin | Baldur shows "in Jira" or the error beside the day |

Every other change emits a `created` or `updated` event, which Ysildir uses to answer "what changed since…".

## Work tables (Odin)

Odin owns six tables: Jira issues, every key they have had, their status history, your calendar, your worklogs, and the sub-tasks it made for your meetings. The numeric `jira_id` is the real key, because an issue's key changes when it moves projects.

**Sync scope.** Odin keeps four sets of issues current:

1. **Yours:** `assignee was currentUser()`, which feeds the Assigned to Me view and sets `is_mine`.
2. **Children of the parents you track,** from Odin's existing tracked-parent pull.
3. **Keys other apps mention** that no alias covers yet (`v_unknown_keys`). Odin fetches each with `GET /rest/api/2/issue/{key}`, because one bad key fails a whole JQL `key in (…)` query. Jira answers an old key with the moved issue, which Odin records as a `moved` alias; a 404 becomes `not_found`, retried after 7 days.
4. **Mentioned issues not yet done,** refreshed with `key in (…)` in batches of 50, so their move to done reaches Freya even when they aren't assigned to you.

Incremental runs ask for `updated >= odin.jql_time(cursor)`, two minutes early because JQL compares to the minute. An issue no run has seen for a week is checked with Jira one at a time, and only a 404 marks it deleted, since an issue can leave Odin's scope without being deleted.

**`work_items`**: one row per Jira issue.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `source_id` | INTEGER | FK sources |  |
| `jira_id` | TEXT | UNIQUE (source\_id, jira\_id) | Stable numeric id, 10234 |
| `key` | TEXT | UNIQUE (source\_id, key) | The current key, XYZ-45 |
| `project_key`, `issue_type` | TEXT | not null |  |
| `summary` | TEXT | not null | No description column yet (open decision) |
| `status` | TEXT | not null | As named in your workflow |
| `status_category` | TEXT | todo, in\_progress, done | Jira's category, renamed |
| `resolution` | TEXT | NULL while open | Done, Won't Do, Duplicate; Freya skips the ones that aren't wins |
| `priority`, `epic_key`, `parent_key`, `assignee`, `reporter`, `sprint` | TEXT | nullable | `parent_key` drives the tracked-parent pull |
| `is_mine` | INTEGER | 0 or 1 | Assigned to you now or ever; never flips back to 0 |
| `labels`, `components` | TEXT | JSON array |  |
| `story_points` | REAL | ≥ 0 |  |
| `url` | TEXT | not null | The link Freya cites |
| `created_at`, `updated_at`, `resolved_at` | ts | Jira's own times | `updated_at` gates every upsert |
| `due_on` | date |  |  |
| `first_seen_at`, `last_seen_at` | ts | not null | The deletion sweep reads `last_seen_at` |
| `deleted_at` | ts |  | Set by full syncs only |
| `run_id` | INTEGER | FK sync\_runs, set null on prune |  |

**`work_item_aliases`**: every key an issue has had, plus keys Odin looked up and couldn't find. Other apps join through this table, never through `work_items.key`.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `key` | TEXT | primary key | ABC-123 |
| `work_item_id` | INTEGER | FK work\_items, cascade; NULL exactly when not\_found |  |
| `status` | TEXT | current, moved, not\_found | One current key per issue |
| `checked_at` | ts | not null | `v_unknown_keys` retries not\_found after 7 days |

**`work_item_transitions`**: status changes from Jira's changelog, which give Baldur and Freya real start and finish times.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `work_item_id` | INTEGER | FK work\_items, cascade |  |
| `changelog_id` | TEXT | UNIQUE (work\_item\_id, changelog\_id) | Jira's history id |
| `at` | ts | not null |  |
| `from_status`, `to_status` | TEXT | to\_status not null |  |
| `to_category` | TEXT | todo, in\_progress, done |  |
| `author` | TEXT |  |  |
| `by_me` | INTEGER | 0 or 1 | Feeds `v_activity` |

**`calendar_events`**: your calendar, from Odin's meeting sync. Baldur reads it through `v_busy_meetings` to keep meeting time out of estimates, and Loki links its recaps to it.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `source_id`, `external_id` | INTEGER, TEXT | UNIQUE (source\_id, external\_id) | Outlook or Graph event id |
| `title` | TEXT | not null |  |
| `starts_at`, `ends_at` | ts | ends\_at ≥ starts\_at |  |
| `is_all_day`, `is_cancelled` | INTEGER | 0 or 1 |  |
| `show_as` | TEXT | busy, tentative, free, oof, unknown |  |
| `response` | TEXT | organizer, accepted, tentative, declined, none |  |
| `logged_as_key` | TEXT |  | Jira key Odin logs this meeting against, if any |
| `first_seen_at`, `last_seen_at`, `deleted_at`, `run_id` | ts, INTEGER |  | Same provenance as `work_items` |

**`worklogs`**: every worklog of yours in Jira, whoever made it, plus the ones Odin is sending.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `work_item_id` | INTEGER | FK work\_items |  |
| `jira_worklog_id` | TEXT | UNIQUE (work\_item\_id, jira\_worklog\_id); set exactly when posted or deleted |  |
| `origin` | TEXT | jira, baldur, meeting, manual | jira = made outside Asgard, such as by hand in the browser |
| `state` | TEXT | sending, posted, failed, deleted | deleted = removed in Jira after it was posted |
| `started_at` | ts | not null |  |
| `seconds` | INTEGER | > 0 |  |
| `comment` | TEXT |  | Odin's posts carry a marker, `[asgard:b-9f3c1a2b]` |
| `proposal_id` | INTEGER | FK day\_proposals; set exactly when origin = baldur | The approval it posts |
| `calendar_event_id` | INTEGER | FK calendar\_events; set exactly when origin = meeting |  |
| `posted_at`, `created_at` | ts | posted requires posted\_at |  |
| `error` | TEXT |  | Jira's message when a post fails |

Five table checks keep the states honest: a Jira id exists exactly for posted and deleted rows, posted needs `posted_at`, rows found in Jira are never sending or failed, and the origin decides whether `proposal_id` or `calendar_event_id` is set. Odin finds your worklogs by the issues you logged time on (`worklogAuthor = currentUser()`, then each issue's worklog list), keeping only yours, and `/worklog/deleted?since=` marks removed ones. This deviates from the first design, which read `/worklog/updated?since=`: that lists every worklog in the whole Jira, which on a large Data Center is most of the instance's history ([integration/odin.md](integration/odin.md#worklogs-and-a-deviation-from-the-design)).

**`meeting_subtasks`** (v5): one row per meeting Odin made a Jira sub-task for. It is all that stands between a re-run and a pile of duplicate sub-tasks, so Odin consults it before creating anything and writes it the moment Jira accepts the create, before the worklog and the transition.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `meeting_key` | TEXT | UNIQUE | The calendar item's own key: Outlook's id and start, or `csv:<hash>` |
| `content_hash` | TEXT | 32 lower-case hex | sha256 of the normalised subject, start and end; matches a meeting across export paths |
| `calendar_event_id` | INTEGER | FK calendar\_events, set null on delete | NULL for history imported from `state.db` |
| `issue_key`, `parent_key` | TEXT | Jira keys in capitals | The sub-task and the issue it was made under |
| `summary` | TEXT | 1 to 255 characters | The sub-task's summary as created |
| `started_at`, `minutes` | ts, INTEGER | minutes 0 to 1440 |  |
| `worklog_wanted` | INTEGER | 0 or 1 | Whether `log_work` was on when it was made, so turning it on later doesn't backfill months |
| `worklog_comment` | TEXT |  | Rendered when it was made, so a retry needs no calendar |
| `origin` | TEXT | odin, state\_db |  |
| `created_at` | ts | not null |  |

Odin finds a meeting's record by `meeting_key` or `content_hash`, so the Outlook and CSV paths recognise each other's work, and two records may share a hash. The meeting's time is a worklog on the sub-task (origin meeting, linked through `calendar_event_id`), written through the posting protocol below, so it is never posted twice. The trigger `meeting_subtasks_are_facts` lets only `calendar_event_id` change; removing a row (`odin forget KEY`) is the one deliberate way to push a meeting again.

## Code and time tables (Baldur)

Baldur owns thirteen tables in two groups: what happened in git and on GitHub, as metadata only (no code, no diffs), and the time estimates built from it. They name Jira issues by key, so Baldur never waits on Odin to record work.

### Git and GitHub facts

**`repos`**: one row per repository Baldur scans, or reviews on GitHub. A table check requires a GitHub name or a local path.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `source_id` | INTEGER | FK sources, nullable | The GitHub source |
| `name` | TEXT | not null | Folder or repo name |
| `github_repo` | TEXT | UNIQUE, nullable | owner/name, for the GitHub API |
| `local_path` | TEXT | UNIQUE, nullable | NULL for repos you only review on GitHub |
| `remote_url`, `default_branch` | TEXT |  | `default_branch` bounds `git log main..branch` |
| `active` | INTEGER | 0 or 1 | 0 stops scanning without losing history |
| `last_scanned_at`, `created_at` | ts |  |  |

**`commits`**: one row per commit.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `repo_id` | INTEGER | FK repos |  |
| `sha` | TEXT | length 40 or 64; UNIQUE (repo\_id, sha) | SHA-1 or SHA-256 repos |
| `patch_id` | TEXT |  | From `git patch-id`, so a cherry-picked change counts once |
| `author_name`, `author_email` | TEXT | email COLLATE NOCASE | Matched against identities |
| `authored_at`, `committed_at` | ts | not null | Baldur uses `authored_at`; rebases rewrite the other |
| `subject` | TEXT | not null | First line only |
| `files_changed`, `additions`, `deletions` | INTEGER | ≥ 0 | From `git log --numstat` |
| `is_merge`, `is_mine` | INTEGER | 0 or 1 | Merge commits aren't collected, so `is_merge = 1` marks a squash copy of commits already made (a pull request squash-merged on GitHub, or a local `git merge --squash`): never work, no keys, kept only so the sessions it links share one length limit. Work queries filter `is_merge = 0` |
| `branch_hint` | TEXT |  | Branch it was made on, saved at collection because branches get deleted |
| `first_seen_at`, `run_id` | ts, INTEGER |  |  |

**`commit_work_items`**: which Jira keys a commit belongs to, and how Baldur knows. Columns `commit_id` (FK, cascade), `work_item_key` and `method` (reflog, branch, pr, message, manual); primary key (commit\_id, work\_item\_key). Baldur tries the methods in that order and keeps only keys from projects in its `project_keys` setting. There is no `work_item_id`: aliases resolve the key when it's read, so a moved issue needs no rewrite.

**`reflog_entries`**: `repo_id` FK, `ref` (HEAD or the branch whose reflog it came from), `at` ts, `action` (checkout, commit, rebase, reset, merge, pull, branch), `sha`, `message`; UNIQUE (repo\_id, ref, at, sha, action). Checkouts give a session's true start and the branch you were on. Git expires reflog entries after 90 days (30 for commits no longer on any branch), so Baldur collects at least weekly.

**`pull_requests`**: one row per PR, yours or one you were asked to review; UNIQUE (repo\_id, number).

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id`, `repo_id`, `number` | INTEGER | number > 0 |  |
| `title`, `author`, `url` | TEXT | not null |  |
| `is_mine` | INTEGER | 0 or 1 | You opened it |
| `head_ref` | TEXT | not null | Branch name; GitHub keeps it after the branch is deleted |
| `work_item_key` | TEXT |  | Parsed from `head_ref`, else from the title |
| `state` | TEXT | open, closed, merged | merged requires `merged_at` |
| `is_draft` | INTEGER | 0 or 1 | Drafts don't notify |
| `review_requested` | INTEGER | 0 or 1 | Your review is requested right now |
| `notified_at` | ts |  | Set when Baldur notifies you; cleared when a review is re-requested |
| `created_at`, `updated_at`, `merged_at`, `closed_at` | ts | first two not null |  |
| `additions`, `deletions` | INTEGER | ≥ 0 |  |
| `first_seen_at`, `last_seen_at`, `run_id` | ts, ts, INTEGER |  | Same provenance as `work_items` |

**`pr_reviews`**: `pr_id` FK (cascade), `github_id` (unique per PR), `reviewer`, `is_mine`, `state` (approved, changes\_requested, commented, dismissed), `submitted_at` ts, `comment_count` and `notified_at`. Reviews you write feed `v_activity`; reviews others leave on your PRs notify you once.

### Time estimates

Each estimate run freezes its settings, recomputes sessions from scratch and proposes minutes per ticket per local day. You approve days in Baldur, approved rows never change, and Odin posts from them.

**`calibration_runs`**: each fit of the settings against your real hours: `gap_minutes` > 0, `lead_in_minutes` ≥ 0, `ambient_weight` 0–1, `days_used` > 0, `mean_abs_error_min`, `bias_min` ≤ 0 (calibration may only bias low) and `is_active`. A partial unique index allows exactly one active row.

**`estimate_runs`**: one row per run: `date_from` ≤ `date_to`, `model_version` (baldur-1), `params` (JSON object) with `params_hash`, and `calibration_id`. Every number a run produces traces back to these frozen settings.

**`work_sessions`**: sessions as one run computed them. Recomputed, never edited.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `estimate_run_id` | INTEGER | FK estimate\_runs, cascade |  |
| `local_date` | date | not null | The day it counts toward |
| `started_at`, `ended_at` | ts | ended\_at ≥ started\_at |  |
| `start_basis` | TEXT | reflog, lead\_in | A reflog checkout, or the lead-in before the first commit |
| `policy` | TEXT | ambient, overlap, independent | How meetings were handled; never two discounts |
| `focused_minutes`, `ambient_minutes` | REAL | ≥ 0 |  |
| `counted_minutes` | REAL | ≤ focused + ambient | After weighting and the day cap |
| `commit_count` | INTEGER | > 0 |  |

**`session_commits`** lists each session's commits. **`session_allocations`** splits a session's counted minutes across keys by commit share: `session_id` (FK, cascade), `work_item_key` (NULL = untracked) and `minutes` ≥ 0, with a unique index allowing one row per session and key, including one untracked row.

**`day_proposals`**: the unit you review, minutes per ticket per local day.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `estimate_run_id` | INTEGER | FK estimate\_runs, cascade |  |
| `local_date` | date | not null |  |
| `work_item_key` | TEXT | NULL = untracked | Untracked time is shown, never approved |
| `minutes_raw` | REAL | ≥ 0 | Before rounding |
| `minutes_proposed` | INTEGER | ≤ minutes\_raw | Rounded down to 15 |
| `minutes_final` | INTEGER | required when approved | What you approved; may differ from the proposal |
| `first_started_at` | ts | not null | The worklog's start time in Jira |
| `basis`, `basis_hash` | TEXT | not null | Plain-text reasoning, and its hash to detect changes |
| `review` | TEXT | JSON object | The optional AI review |
| `status` | TEXT | proposed, approved, rejected, superseded |  |
| `decided_at` | ts | required when approved or rejected |  |

Three rules hold its lifecycle together:

- **One open and one approved row per day and ticket,** by two partial unique indexes. A new run supersedes the open row, and proposes nothing when its `basis_hash` matches a rejected one.
- **Decided rows are final.** The trigger `day_proposals_decided_are_final` blocks every change except approved → superseded, so changing an approved number means superseding it and inserting the new row in one transaction (`baldur.change_approval()`).
- **Decisions are kept.** The trigger `day_proposals_decisions_are_kept` refuses to delete any row that was approved or rejected, so pruning an estimate run that holds one fails instead of dropping approved time.

**`time_actuals`**: real hours you note during a calibration trial: `on_date`, `minutes` 0–1440, `work_item_key` (NULL = the whole day) and `note`. `ux_time_actuals_day_item` allows one row per day and ticket, including one whole-day row.

### Agent estimates (v4)

An AI coding agent that worked a change with you can record what it thinks your working time on that change was. It records through Baldur's CLI (`baldur.cmd ai record`) or Ysildir's MCP tool, both of which call `asgard.muninn.baldur.record_agent_estimate()`. A report is evidence, never a number on its own: Baldur's AI-assisted method uses it only to move minutes between tickets or lower them, so a day never rises because of one. Baldur owns both tables.

**`agent_estimates`**: one report per row.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key | Shown as `r12` |
| `recorded_at` | ts | not null |  |
| `via` | TEXT | cli, mcp, window | How it arrived |
| `agent`, `model`, `guide_version` | TEXT | agent 1–40 characters, model ≤ 80, guide ≤ 40 | Which tool, which model, which version of Baldur's agent guide it followed |
| `work_item_key` | TEXT | a key like PROJ-123, or NULL | The ticket the agent named; NULL means its commits' keys |
| `local_date` | date | not null | The day the work happened |
| `started_at`, `ended_at` | ts | ended ≥ started | Only when the agent read them from a clock |
| `minutes` | INTEGER | 1–1440 | The agent's estimate of your working time on the change |
| `minutes_low` | INTEGER | 1 to `minutes` | The low end of a range; Baldur counts it, since of two readings the smaller wins |
| `confidence` | TEXT | high, medium, low |  |
| `summary` | TEXT | 1–300 characters | One sentence. Baldur refuses code, diff lines and control characters before it gets here |
| `report_hash` | TEXT | unique | A digest of what the report says and the commits it cites. The same report is stored once while it counts; sent again after it was withdrawn, it's stored again with a suffix |
| `status`, `withdrawn_at` | TEXT, ts | recorded, withdrawn; a withdrawal has its time | A newer report from the same agent on the same commits withdraws the older one |

**`agent_estimate_commits`**: the commits a report is about: `estimate_id` and `sha` (7–64 lower-case hex characters). A SHA may be short, because an agent can record before Baldur has collected the commit; Baldur matches it by prefix when it reads the report. A report without commits is shown to you and never counted.

Reports are facts. The trigger `agent_estimates_are_facts` allows one change, recorded → withdrawn with its time. `agent_estimates_are_kept` and the two triggers on `agent_estimate_commits` refuse every other edit or delete. Adding a commit to a report afterwards is refused by `agent_estimate_commits_with_their_report`: once the report's `agent_estimate.recorded` event exists it is sealed, and before that only the connection that just inserted it may add commits (`last_insert_rowid()`). `agent_estimates_recorded_now` keeps `recorded_at` within two minutes of now, so a report can't be back-dated (review R14 and P7, added Oct 10). On a database made before those triggers, a commit added later still changes the report's digest: Baldur then doesn't count the report, and `--muninn check` reports it until it's withdrawn (`muninn.baldur.report_digest`). `ix_agent_estimates_day` serves "the day's recorded reports", and `ix_agent_estimate_commits_sha` serves "reports citing these commits".

## Meeting and BLUF tables (Loki)

Loki owns three tables. They keep what a BLUF needs (titles, Copilot's notes, decisions, owners) and nothing from raw transcripts.

**`meetings`**: one row per meeting Loki has notes for.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `source_id` | INTEGER | FK sources | teams, or paste for pasted recaps |
| `external_id` | TEXT | UNIQUE (source\_id, external\_id) | Graph meeting id; for pasted recaps, a hash of title and start |
| `calendar_event_id` | INTEGER | FK calendar\_events, set null | The calendar entry Odin synced, when Loki can match it |
| `title` | TEXT | not null |  |
| `starts_at`, `ends_at` | ts | ends\_at ≥ starts\_at |  |
| `organizer` | TEXT |  |  |
| `attendee_count` | INTEGER | ≥ 0 |  |
| `recap_origin` | TEXT | graph\_insights, graph\_transcript, paste, file | Which of Loki's paths produced it |
| `source_link` | TEXT |  | Recap or join link: what a BLUF cites |
| `notes_summary` | TEXT |  | Copilot's notes or the pasted recap, never the transcript |
| `first_seen_at` | ts | not null |  |
| `run_id` | INTEGER | FK sync\_runs, set null |  |

**`action_items`**: `meeting_id` FK (cascade), `text`, `owner`, `is_mine`, `due_on` date, `status` (open, done, dropped), `work_item_key` for a Jira issue created from it, `created_at`.

**`blufs`**: every BLUF Loki drafts, for meetings and code changes alike.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `subject_type` | TEXT | meeting, commit\_range, pull\_request, work\_item, adhoc |  |
| `subject_ref` | TEXT | not null | Meeting `external_id`, `repo:sha1..sha2`, PR URL or Jira key |
| `bottom_line` | TEXT | not null | The one sentence |
| `body_md` | TEXT | not null | The full message as sent |
| `sections` | TEXT | JSON object | `{"decisions":[],"actions":[],"risks":[],"asks":[]}`; Freya reads decisions here |
| `tier` | TEXT | api, mcp, clipboard, manual |  |
| `model` | TEXT |  | NULL for clipboard and manual |
| `prompt_version` | TEXT | not null | `loki-v3`: ties output changes to prompt changes |
| `state` | TEXT | draft, approved, posted, rejected |  |
| `sent_via` | TEXT | clipboard, mailto, graph\_mail, teams |  |
| `created_at`, `approved_at`, `posted_at` | ts |  |  |

Two table checks: `approved` requires `approved_at`, and `posted` requires both `posted_at` and `sent_via`. The IDs a BLUF cites go in `citations`, described with Freya's tables.

## Review tables (Freya)

Freya owns four tables: a durable record of each issue you finished, review periods, generated drafts, and citations, the guardrail that every ID a draft cites resolves to a real row before the text can be marked final.

**`accomplishments`**: one row per issue you finished, created when Odin emits `work_item.done`. Freya keeps it if the issue is yours (`is_mine`, or your commits or approved minutes on any of its keys) and its resolution counts; Won't Do, Duplicate and Cannot Reproduce don't.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `jira_id` | TEXT | UNIQUE | Survives key changes; the join back to `work_items` |
| `work_item_key` | TEXT | not null | The key at the last refresh |
| `summary`, `issue_type`, `epic_key`, `url` | TEXT | summary, type, url not null | Copied, so the record outlives the Jira issue |
| `state` | TEXT | done, reopened |  |
| `resolution` | TEXT |  |  |
| `first_done_at`, `last_done_at` | ts | last ≥ first |  |
| `reopened_count` | INTEGER | ≥ 0 |  |
| `story_points` | REAL | ≥ 0 |  |
| `approved_minutes` | INTEGER | ≥ 0 | From `v_day_status`, across all the issue's keys |
| `commit_count`, `merged_pr_count` | INTEGER | ≥ 0 | Your commits (distinct patch ids) and merged PRs |
| `first_activity_at`, `last_activity_at` | ts |  | First and last commit, transition or approved day |
| `impact_note` | TEXT |  | Your words: the result no data source has |
| `highlight` | INTEGER | 0 or 1 | Lead with this one |
| `stats_refreshed_at` | ts | not null |  |
| `frozen_at` | ts |  | Set when the review period closes; refreshes stop |

It is a copy, not a move. Moving done issues out of `work_items` would break key resolution for late commits and reopened issues, and at a few thousand rows a year, indexes keep Odin's and Baldur's tables fast. The copy is what makes Freya's record durable: your note and the numbers as of the review survive the issue being edited, moved or deleted in Jira.

**`review_periods`**: `id`, `label` (`UNIQUE`, such as `FY2026 annual`), `starts_on` and `ends_on` dates (end on or after start), and `rubric`, a JSON array of your performance elements and standards, entered once per period. Setting closed\_at freezes that period's accomplishments.

**`review_drafts`**: one row per generated section.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `period_id` | INTEGER | FK `review_periods` |  |
| `element` | TEXT | not null | Performance element from the rubric |
| `version` | INTEGER | ≥ 1; `UNIQUE (period_id, element, version)` | Every regeneration is a new version |
| `text` | TEXT | not null |  |
| `tier` | TEXT | api, mcp, clipboard, manual |  |
| `model`, `prompt_version` | TEXT | prompt version not null |  |
| `status` | TEXT | draft, final, discarded |  |
| `created_at` | ts | defaults to now |  |

**`citations`**: every ID cited by a BLUF or a review draft.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `owner_type` | TEXT | bluf, review\_draft |  |
| `owner_id` | INTEGER | not null | The citing row; two possible tables, so no foreign key |
| `evidence_type` | TEXT | accomplishment, work\_item, commit, pull\_request, meeting, bluf |  |
| `evidence_ref` | TEXT | `UNIQUE (owner_type, owner_id, evidence_type, evidence_ref)` | Exactly what the text cites: `XYZ-45`, a SHA, `asgard#7` |
| `evidence_id` | INTEGER |  | Resolved row id; NULL until resolved |
| `verified` | INTEGER | 0 or 1; 1 requires `evidence_id` |  |

How the guardrail runs:

1. Freya's prompt asks for IDs in brackets; the writer parses them into `citations` with `verified = 0`.
2. The checker resolves each `evidence_ref` against its table and sets `evidence_id` and `verified = 1`.
3. Any row left in `v_unverified_citations` blocks marking the draft final. In testing, an invented `XYZ-999` stayed unverified while the real key resolved.

## Submission tables (Heimdall, Bifrost)

Heimdall and Bifrost share one `submissions` table with a `system` column, plus a history table that a trigger fills, so no app can forget to log a status change. States run draft → approved → submitted → in\_review → accepted or returned, and withdrawn ends a submission at any point.

**`submissions`**: one row per SeCcHm request or BEARs workbook.

| Column | Type | Rules | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | primary key |  |
| `system` | TEXT | secchm, bears |  |
| `title` | TEXT | not null |  |
| `state` | TEXT | draft, approved, submitted, in\_review, accepted, returned, withdrawn |  |
| `external_id` | TEXT | `UNIQUE (system, external_id)` | SeCcHm number, or the Confluence page id for BEARs |
| `external_url` | TEXT |  |  |
| `fields` | TEXT | JSON object | The values filled in, by field name |
| `artifact_path` | TEXT |  | The generated .xlsx for BEARs |
| `attachment_id`, `attachment_version` | TEXT, INTEGER | version ≥ 1 | The Confluence attachment; re-uploads bump the version |
| `work_item_key` | TEXT |  | Related Jira issue, if any |
| `created_at`, `approved_at`, `submitted_at` | ts |  |  |
| `last_polled_at`, `next_poll_at` | ts |  | The poll queue reads `next_poll_at` |
| `last_status_raw` | TEXT |  | Status text exactly as the system shows it |

Two table checks carry rule 8: any state past draft requires `approved_at`, and submitted or later requires `submitted_at`.

**`submission_status_history`**: `submission_id` FK (cascade), `at` ts (defaults to now), `from_state`, `to_state`, `raw_status`. Triggers `submissions_first_history` and `submissions_state_history` add a row on insert and on every state change; in testing, a draft taken through three changes produced four rows.

`fields` can hold the same sensitive detail as the systems themselves, so it gets the same handling as their exports.

## Search and views

One FTS5 table indexes text from eight tables, and ten views reshape the normalized tables into what the apps actually read. Five of the views are the contracts between apps, so their rules live in SQL rather than in each app.

**`search`** is an FTS5 table with `title`, `body` and an unindexed `kind`, using the `porter unicode61` tokenizer so "submit" also finds "submitted". Its rowid packs the source row as `entity_id * 16 + type code`, decoded with `rowid >> 4` and `rowid & 15`, so each trigger updates one row by rowid instead of scanning.

| Code | Table | title | body |
| --- | --- | --- | --- |
| 1 | `work_items` | key + summary | epic, type, labels, components |
| 2 | `commits` | subject | short SHA, branch |
| 3 | `pull_requests` | title | number, author, branch |
| 4 | `meetings` | title | notes\_summary |
| 5 | `action_items` | text | owner, Jira key |
| 6 | `blufs` | bottom line | body\_md |
| 7 | `submissions` | title | system, external ID, Jira key |
| 8 | `accomplishments` | key + summary | epic, impact note |

Insert, update and delete triggers on each table keep the index current. In testing, an issue moved from `ABC-123` to `XYZ-45` left no stale hit.

```sql
SELECT kind, rowid >> 4 AS entity_id, title
  FROM search
 WHERE search MATCH 'bears'
 ORDER BY bm25(search)
 LIMIT 20;
```

| View | Returns | Read by |
| --- | --- | --- |
| `v_busy_meetings` | Calendar events that take your time: busy or tentative, and not declined, cancelled, all-day or deleted | Baldur's meeting policy |
| `v_activity` | Your commits (not squash copies, from v2), reflog entries, PR reviews and Jira transitions as one timeline: at, kind, repo\_id, ref\_id, label | Baldur's session builder |
| `v_unknown_keys` | Keys other apps store that no alias covers, plus not\_found keys last checked over 7 days ago | Odin's key lookup (contract) |
| `v_day_status` | Per local day and issue: the newest approved minutes, the development time Jira already holds, the day's bounds in UTC, and the approval's start and basis | Baldur's review screen; Odin (contract) |
| `v_worklogs_to_post` | The `v_day_status` rows Odin should post, with `minutes_to_post` | Odin's posting loop (contract) |
| `v_unpostable_days` | Approved time Jira can't take: the issue was deleted, or its key was never found | Baldur's review screen (contract) |
| `v_time_by_item_month` | Approved minutes per ticket per month | Freya |
| `v_review_evidence` | Accomplishments, merged PRs and posted BLUFs inside each review period | Freya, Ysildir |
| `v_unverified_citations` | Citations the checker could not resolve | Freya's final-draft gate |
| `v_tile_badges` | app, priority, n and label for each non-zero count: reviews requested, days to review, days that can't be posted, worklogs to post, keys to look up, posts to check, citations to check, wins without a note | Asgard launcher (contract) |

`v_day_status` carries the posting rules, so Odin's loop stays a few lines:

- **Keys resolve through aliases.** An approval under ABC-123 and a later one under XYZ-45 count as one issue, and the newest approval wins.
- **Days are local.** Bounds are local midnight converted to UTC, so an 11:30 pm worklog lands on the right day, a 25-hour daylight-saving day works, and the worklog index is used.
- **Logged time is development time.** It counts your worklogs from any origin except meetings, in state sending or posted, because Baldur's estimate already leaves meetings out.
- **Seconds round up** to whole minutes, so Odin never posts a minute too many.

`v_worklogs_to_post` adds three:

- **The issue must exist** and not be deleted.
- **Each approval posts at most once,** so a worklog you delete in Jira isn't posted again until you approve that day again.
- **Nothing is offered on top of a post in doubt.** While any post for an issue and day is still `sending`, that day waits, so a post that turns out to have failed can't leave approved time behind.

## Indexes and the queries they serve

Each of the 48 explicit indexes exists for a named query; the 18 marked checked are confirmed with EXPLAIN QUERY PLAN in the test script. UNIQUE constraints and primary keys already index the natural keys and most foreign keys, so they are not repeated here.

| Index | On (partial filter) | Serves | Checked |
| --- | --- | --- | --- |
| `ix_events_at` | events (at) | Ysildir: what changed since a time |  |
| `ix_events_entity` | events (entity\_type, entity\_id) | History of one row | Yes |
| `ix_events_kind` | events (kind), plus the implicit rowid | A consumer reading past its cursor | Yes |
| `ix_sync_runs_app_started` | sync\_runs (app, started\_at) | Last run per app |  |
| `ix_work_items_mine_resolved` | work\_items (resolved\_at) where is\_mine = 1 | Your resolved issues in a period | Yes |
| `ix_work_items_open_mine` | work\_items (status\_category, updated\_at) where mine and not deleted | Odin's Assigned to Me view |  |
| `ix_work_items_updated` | work\_items (source\_id, updated\_at) | Odin: incremental sync |  |
| `ix_work_items_epic` | work\_items (epic\_key) where not null | Grouping by epic |  |
| `ix_work_items_parent` | work\_items (parent\_key) where not null | Odin: children of a tracked parent | Yes |
| `ix_work_items_last_seen` | work\_items (source\_id, last\_seen\_at) where not deleted | Odin: deletion sweep after a full sync | Yes |
| `ix_aliases_item` | work\_item\_aliases (work\_item\_id) | Every key of one issue | Yes |
| `ux_aliases_current` | work\_item\_aliases (work\_item\_id) where current; unique | One current key per issue | Yes |
| `ix_transitions_mine_at` | work\_item\_transitions (at) where by\_me = 1 | `v_activity` |  |
| `ix_calendar_events_starts` | calendar\_events (starts\_at) where not deleted | Meetings in a day, for Baldur |  |
| `ix_worklogs_item_started` | worklogs (work\_item\_id, started\_at) | `v_day_status`: one day's time on an issue | Yes |
| `ix_worklogs_sending` | worklogs (created\_at) where sending | Odin's restart check for stuck posts | Yes |
| `ix_worklogs_proposal` | worklogs (proposal\_id) where not null | Has this approval been posted | Yes |
| `ix_worklogs_calendar_event` | worklogs (calendar\_event\_id) where not null | Has this meeting been logged |  |
| `ix_commits_mine_authored` | commits (authored\_at) where is\_mine = 1 | Baldur: your commits in a window | Yes |
| `ix_commits_repo_authored` | commits (repo\_id, authored\_at) | Per-repo history; Loki's commit ranges |  |
| `ix_commits_patch_id` | commits (patch\_id) where not null | Counting a cherry-picked change once |  |
| `ix_commit_items_key` | commit\_work\_items (work\_item\_key) | Commits for a ticket; `v_unknown_keys` | Yes |
| `ix_reflog_at` | reflog\_entries (at) | `v_activity` |  |
| `ix_prs_mine_merged` | pull\_requests (merged\_at) where is\_mine = 1 | Freya's evidence |  |
| `ix_prs_review_requested` | pull\_requests (updated\_at) where open and review requested | Baldur's review list; tile badge | Yes |
| `ix_prs_key` | pull\_requests (work\_item\_key) where not null | PRs for a ticket | Yes |
| `ix_pr_reviews_mine_at` | pr\_reviews (submitted\_at) where is\_mine = 1 | `v_activity` |  |
| `ix_pr_reviews_unnotified` | pr\_reviews (pr\_id) where not notified and not yours | Reviews to tell you about |  |
| `ux_calibration_active` | calibration\_runs (is\_active) where active; unique | Exactly one active calibration |  |
| `ix_sessions_run_day` | work\_sessions (estimate\_run\_id, local\_date) | Sessions of a run by day |  |
| `ux_allocations` | session\_allocations (session\_id, coalesce(work\_item\_key, '')); unique | One row per session and key |  |
| `ix_allocations_key` | session\_allocations (work\_item\_key) | Time per ticket |  |
| `ix_day_proposals_run` | day\_proposals (estimate\_run\_id) | Pruning a run |  |
| `ux_day_proposals_open` | day\_proposals (local\_date, coalesce(work\_item\_key, '')) where proposed; unique | Days waiting for review; one open row each | Yes |
| `ux_day_proposals_approved` | day\_proposals (local\_date, work\_item\_key) where approved; unique | `v_day_status`; one approval each | Yes |
| `ux_time_actuals_day_item` | time\_actuals (on\_date, coalesce(work\_item\_key, '')); unique | One actual per day and ticket |  |
| `ix_agent_estimates_day` | agent\_estimates (local\_date) where recorded | Baldur: a day's agent reports (v4) | Yes |
| `ix_agent_estimate_commits_sha` | agent\_estimate\_commits (sha) | Baldur: reports citing a day's commits (v4) | Yes |
| `ix_meeting_subtasks_hash` | meeting\_subtasks (content\_hash) | Odin: has this meeting a sub-task (v5) | Yes |
| `ix_meeting_subtasks_issue` | meeting\_subtasks (issue\_key) | Odin: forget a sub-task (v5) | Yes |
| `ix_meeting_subtasks_event` | meeting\_subtasks (calendar\_event\_id) where not null | Clearing the link when an event goes (v5) |  |
| `ix_meetings_starts` | meetings (starts\_at) | Meetings by day |  |
| `ix_meetings_calendar_event` | meetings (calendar\_event\_id) where not null | Recap for a calendar entry |  |
| `ix_action_items_meeting` | action\_items (meeting\_id) | A meeting's actions; cascade deletes |  |
| `ix_action_items_open` | action\_items (due\_on) where open | Open actions by due date |  |
| `ix_blufs_subject` | blufs (subject\_type, subject\_ref) | BLUFs about one meeting or ticket |  |
| `ix_blufs_posted` | blufs (posted\_at) where posted | Freya's evidence |  |
| `ix_accomplishments_done` | accomplishments (last\_done\_at) where done | Freya: wins in a period | Yes |
| `ix_accomplishments_key` | accomplishments (work\_item\_key) | Citation checks by key |  |
| `ix_citations_evidence` | citations (evidence\_type, evidence\_ref) | Where a ticket is cited |  |
| `ix_citations_unverified` | citations (owner\_type, owner\_id) where unverified | Final-draft gate; tile badge |  |
| `ix_submissions_poll` | submissions (next\_poll\_at) where submitted or in review | Heimdall and Bifrost poll queue | Yes |
| `ix_submission_history` | submission\_status\_history (submission\_id, at) | Status timeline |  |

SQLite uses a partial index only when the query repeats its filter, so `WHERE is_mine = 1` must appear literally in Freya's and Baldur's queries. `v_day_status` reads every approved proposal, about a thousand rows a year, with one index lookup each; that is fine at this size.

## Write path

A sync never holds Muninn's one write lock while it waits on the network. It fetches, then writes in short batches, and saves its cursor only when the run ends cleanly, so another app can approve or post at any moment.

1. Open a `sync_runs` row (running) and read the stream's cursor from `sync_cursors`.
2. Fetch a page from the source, with no transaction open.
3. Write the page in one `BEGIN IMMEDIATE` batch. Each item runs in its own savepoint, so its row, aliases, history and events are saved together or not at all.
4. Repeat for each page. If an item fails, the run notes it and carries on; it ends `partial` and keeps its old cursor, so the next run retries that item and re-reads the others without writing them twice.
5. When the run ends without an error, save the cursor and close the run in one short transaction. If SQLite rolls a batch back on its own (disk full, I/O error), the run stops rather than write anything unprotected.

Deletions are confirmed, not guessed: an issue a run didn't see is checked with Jira before it's marked deleted, and a calendar window marks only the events that run didn't see.

The upsert, abridged; the package builds the full statement:

```sql
INSERT INTO work_items (source_id, jira_id, key, project_key, issue_type, summary, status,
                        status_category, resolution, priority, epic_key, parent_key, assignee,
                        reporter, is_mine, labels, components, story_points, sprint, url,
                        created_at, updated_at, resolved_at, due_on,
                        first_seen_at, last_seen_at, run_id)
VALUES (:source_id, :jira_id, :key, ..., :due_on, :now, :now, :run_id)
ON CONFLICT (source_id, jira_id) DO UPDATE SET
    key = excluded.key, summary = excluded.summary, status = excluded.status, ...,
    is_mine = max(work_items.is_mine, excluded.is_mine),
    deleted_at = NULL, run_id = excluded.run_id
WHERE excluded.updated_at > work_items.updated_at
   OR (excluded.updated_at = work_items.updated_at          -- same second: compare the values
       AND (excluded.status IS NOT work_items.status OR excluded.summary IS NOT work_items.summary OR ...))
   OR excluded.is_mine > work_items.is_mine
   OR work_items.deleted_at IS NOT NULL
RETURNING id;

UPDATE work_items SET last_seen_at = :now WHERE id = :id;
```

- **No-op when unchanged.** The WHERE skips the update when Jira's `updated_at` hasn't moved and no value differs, so RETURNING yields nothing and no event is written.
- **Same-second changes still land.** Jira's milliseconds are dropped, so when `updated_at` ties, the values are compared.
- **Created, moved, done.** The row read just before the upsert tells created from updated, and supplies the old key and status category for the moved, done and reopened events.
- **Ownership sticks.** `max()` keeps `is_mine` at 1 once you were ever assigned.
- **Revived.** An issue marked deleted comes back if Jira returns it.

How Odin calls the package. The run records itself, writes each fetched page in one short batch, and emits created, updated, moved, done and reopened events for each issue:

```python
from asgard import muninn
from asgard.muninn import odin

con = muninn.open_app("odin", supported=(1, 3))
jira = muninn.ensure_source(con, "jira", "jira-dc", "https://jira.example.gov")
ctx = odin.JiraContext.load(con, jira, epic_field="customfield_10008")

with muninn.Run(con, "odin", jira, "issues") as run:
    since = odin.jql_time(run.cursor) if run.cursor else None
    for page in client.search_pages(jql, expand="changelog", updated_since=since):  # no lock held
        with run.batch():                                                         # one short write
            for issue in page:
                odin.upsert_issue(run, issue, ctx, mine=True)   # aliases, history and events included
                run.advance_cursor(odin.parse_time(issue["fields"]["updated"]))
```

## Cross-app flows

Three flows carry work between apps. Each is a few statements in one transaction, and the test script runs each end to end.

### A Jira key from a branch name

1. Baldur reads keys from, in order, the reflog's checkout lines, the branch (`git log main..branch`), the PR's `head_ref`, then the commit message, keeping only projects in `project_keys`. It writes `commit_work_items` and moves on.
2. `v_unknown_keys` lists any key no alias covers.
3. Odin looks each one up and records `current`, `moved` or `not_found` in `work_item_aliases`.
4. Baldur's review screen flags unresolved keys, and `v_worklogs_to_post` skips them until they resolve.

### Approved time to a Jira worklog

1. You approve a day in Baldur (`baldur.approve()`). The proposal becomes approved with `minutes_final`, and Baldur emits `day_proposal.approved`.
2. Odin reads `v_worklogs_to_post` (`odin.posts_due()`). For each row, `odin.begin_post()` writes a `sending` worklog and commits before Jira is called. The row has origin baldur, the proposal's id, `minutes_to_post`, a start kept inside the approved day, and a comment holding the proposal's basis, what Jira already held, the approved figure and when, and a random marker such as `[asgard:b-9f3c1a2b]`.
3. Odin posts to the issue's current key. On success, `finish_post()` saves Jira's id and emits `worklog.posted`. A definite refusal (a 4xx) goes to `fail_post()`, which offers the day again. After a timeout the row stays `sending`.
4. While any post for an issue and day is `sending`, that day waits. At startup, `odin.stuck_posts()` lists `sending` rows older than two minutes. Odin searches the issue's worklogs for each marker and calls `resolve_stuck()`: found means posted, not found means failed.

Odin never edits or deletes a worklog in Jira. If you lower an approved day after it posted, Baldur shows that Jira holds more than you approved, and you fix it there. Approved time Jira can't take, because the issue was deleted or its key was never found, shows in `v_unpostable_days` and on Baldur's tile.

### A finished issue to Freya

1. Odin's sync sees an issue enter the done category and emits `work_item.done` with its resolution; leaving done emits `work_item.reopened`.
2. Freya reads events past its cursor, keeps the issues that are yours and whose resolution counts, and upserts `accomplishments` with stats from `v_day_status`, `commit_work_items` and `pull_requests` across all the issue's keys. It advances its cursor in the same transaction.
3. Freya refreshes stats each time it opens, until the review period closes and sets `frozen_at`. Your `impact_note` and `highlight` are never overwritten.

## Moving Odin into Muninn

Done on Oct 10, 2026 (Asgard 0.4.0): Odin is an Asgard app (`apps/odin`) and keeps its records in Muninn. Each step of the plan, and what became of it:

1. **Asgard installed.** Odin ships with Asgard now; setup installs it, and the packaged build carries it.
2. **Load the package.** Odin opens Muninn with `muninn.open_app("odin", supported=(5, 5))` and runs on Asgard's Python.
3. **Issues.** Your issues, each tracked parent (default parent, rules, tour-of-duty parent) and its children, through `odin.upsert_issue()` in a `muninn.Run` per stream. Assigned to Me is `odin.assigned_to_me()`, in Odin's window.
4. **Calendar and meeting logging.** Every export item goes through `odin.upsert_calendar_event()`; a whole-window export sweeps. A meeting's sub-task is recorded in `meeting_subtasks` (v5), and its time logged through `odin.begin_meeting_post()`.
5. **Worklogs.** By issue rather than `/worklog/updated` (above); the first run reaches back a year.
6. **The new jobs.** Key lookups, posting approved Baldur days (after the worklog sync of the same run, capped per run), and the crash check at start.
7. **What only Odin knew.** `state.db` imported into `meeting_subtasks`; owed worklogs carried, logged ones not; earlier meeting worklogs classified strictly by comment, start and length.
8. **state.db retired.** Renamed `state.db.migrated-DATE`, deleted after 30 days; kept and consulted if any row couldn't move.

Baldur's meeting policy and its posting now have what they waited for: meetings in `calendar_events` and Odin posting approved days.

## Migrations, backup and retention

Schema changes are forward-only numbered SQL files, each applied after an automatic backup, so recovering from a bad migration means restoring the morning's copy.

- **Version.** `PRAGMA user_version` holds the schema version; `0001_initial.sql` sets it to 1.
- **One migrator.** Only Asgard applies migrations, at startup and off its window's thread. Every other app, including Odin, opens Muninn with `muninn.open_app()`, which checks the version against the range the app declares and never changes it.
- **Files.** Later changes are `asgard/muninn/migrations/0002_<name>.sql`, `0003_…`. So far: `0002_copies_arent_activity.sql`, `0003_hardening.sql`, `0004_agent_estimates.sql` and `0005_odin_meetings.sql`. The runner wraps each in `BEGIN IMMEDIATE`, rechecks the version inside the lock and sets `user_version` itself, so two processes upgrading at once apply each file once. A file that fails is undone whole.
- **Constraint changes.** SQLite's ALTER TABLE can't change a CHECK, so those use the create-copy-drop-rename recipe. A file that starts with `-- muninn: foreign_keys=off` runs with foreign keys off and must pass `PRAGMA foreign_key_check` before it commits.
- **Backups.** `VACUUM INTO` copies Muninn to `%LOCALAPPDATA%\Asgard\backups` once a day (7 kept), before each schema upgrade (3 kept) and from Asgard's menu (5 kept). Each copy is written under a private name and moved into place, so two processes never touch the same file.
- **Abandoned runs.** At startup, Asgard closes any sync run still marked running after six hours as failed.
- **Checks at start.** `prepare()` runs `quick_check`. A damaged file stops with the newest backup and the restore command named; nothing is moved until you ask. A damaged search index is rebuilt, and Muninn's own protections (triggers, indexes, views) that are missing or altered are put back, each with a warning.
- **Housekeeping.** Once a day, after the backup: `PRAGMA optimize`, a WAL checkpoint, removal of half-written backup copies, and pruning if retention is on.
- **By hand.** `Asgard.pyw --muninn status|check|repair|backup|restore [FILE]|maintain|retention on|off|show` ([muninn-operations.md](muninn-operations.md)). `restore` checks the backup in full, keeps the current file as `muninn.before-restore-<time>.db`, and puts it back if anything fails partway.
- **Export.** Valhalla's export is the same copy, zipped to a folder you pick.

Retention defaults. Pruning is **off** until you confirm them (`--muninn retention on`); when on, it runs at most 2 s a day in small transactions:

| Data | Kept | Why |
| --- | --- | --- |
| Facts: work items, aliases, calendar, worklogs, commits, PRs, meetings, BLUFs, submissions | Life of the database | Freya needs at least a full year, and rows are small |
| Accomplishments | Life of the database | Freya's durable record, frozen once a period closes |
| events | Life of the database | Append-only by trigger; history is the point |
| Estimate runs with no decided proposal and no day still open for review | 90 days, then pruned with their sessions | Each run is a full recompute; decisions stay for audit, and a posted approval can't be deleted |
| sync\_runs | 180 days, then pruned | Operational only; facts' run\_id becomes NULL |
| reflog\_entries | 1 year | Only recent calibration uses them |
| Transcripts | Never stored | Rule 9 |
| Backups | Last 7 daily copies | Undo window for a bad migration or sync |

Rows are short text, so a year of one engineer's work should stay in the tens of megabytes. Nothing is moved or deleted to keep tables small; indexes do that job.

## Open decisions

- [ ] **Jira descriptions.** Freya writes better with them, but they enlarge the CUI footprint. Default: summaries only.
- [ ] **Scope.** One database per engineer (this design), or a team view later? A team view needs a server, which this design avoids.
- [ ] **Raw API payloads.** Keep them for debugging? Default: no, only `sync_runs.error`.
- [ ] **Versions.** Keep every BLUF and review-draft version, or prune superseded ones once a period closes?
- [x] **state.db layout.** Settled on 2026-10-10: `state.db` holds one table, `synced`, which Odin's own code defined. v5's `meeting_subtasks` takes its place, and Odin imports it once. The on-prem copy may differ from this repo's; the checks and extensions are in [Odin/MUNINN-MIGRATION.md](../../Odin/MUNINN-MIGRATION.md).
- [x] **Database engine.** Decided (Oct 9): stay on SQLite now that pinned packages are allowed. Postgres or SQL Server need a server and admin rights; DuckDB is a native wheel with a single-writer file lock across processes; SQLCipher and apsw are native extensions App Control may block; an ORM such as SQLAlchemy would become every app's dependency, because every app imports `asgard.muninn`. Revisit only for the team view under **Scope**.
- [ ] **Posting mode.** Odin posts approved worklogs on its next run (proposed, since approving in Baldur is the consent), or waits for a Post button in Odin?
- [ ] **Which resolutions count as wins.** Proposed: all except Won't Do, Duplicate and Cannot Reproduce, editable in Freya's settings.
- [x] **Time zone travel.** Decided (Brandon, Oct 9): US time zones only. A day is local to wherever the laptop is when Baldur estimates it and Odin posts it. The rare shift is accepted rather than pinning a zone in `meta`. Baldur's own worklogs count for their approved day whatever the zone; a worklog logged by hand near midnight can fall in the neighbouring day after a move between US zones (at most 6 hours apart).
- [ ] **Encryption beyond BitLocker.** SQLCipher replaces `sqlite3` with a compiled module. That would make Muninn's package depend on a package, which every app and Odin import; the dependency policy keeps it standard library. It would also need IT approval.
- [ ] **Retention.** Confirm the defaults above against your records schedule.
