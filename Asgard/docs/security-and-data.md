# Security and data handling

Oct 6, 2026 · for the ISSO review · covers Asgard 0.4.0 and Muninn schema v3

Asgard is a per-user desktop tool set. It runs on the catalog's Python as the signed-in user, needs no admin rights, opens no network port, and keeps its data in `%LOCALAPPDATA%\Asgard` (BitLocker covers it at rest; the folder is readable only by the user and administrators). This page lists what it stores, what leaves the machine, how secrets are kept, and what the protections do and don't cover.

## What is stored

| Where | What | Sensitivity |
| --- | --- | --- |
| `muninn.db` | Jira issue metadata (key, summary, status, assignee, labels; not descriptions), your calendar entries (title, times, response; not bodies or attendees), your worklogs, git commit metadata (subject, author, times, file counts; not code or diffs), pull request titles and reviews, time estimates and your approvals | Work metadata; may be CUI depending on project names and titles |
| `muninn.db` (planned apps) | Meeting recap summaries and action items (never transcripts), BLUFs, accomplishments and review drafts, submission field values (SeCcHm, BEARs) | Submission fields can hold the same detail as those systems; handled like their exports |
| `backups\` | Copies of `muninn.db`: 7 daily, 3 before each schema upgrade, 5 manual | Same as the database |
| `muninn.before-restore-*.db` | The database as it was before a restore, kept so the restore can be undone | Same as the database; removed by an uninstall with purge |
| `settings\`, `apps.local.json` | Preferences, folder paths, server URLs | Low |
| `logs\` | Errors and run summaries | Low; credentials are masked (below) |
| Windows Credential Manager | GitHub token (Baldur); other service tokens as apps arrive | Secret |
| `%LOCALAPPDATA%\meeting2jira\` | Odin's own config, DPAPI-protected Jira token, state | Odin's existing review applies |

## What leaves the machine

| Flow | Who | When | Approval |
| --- | --- | --- | --- |
| Jira worklogs (writes) | Odin only | After you approve a day in Baldur, or log a meeting or time in Odin | Your approval per day; Odin never edits or deletes a Jira worklog |
| Jira, GitHub, calendar (reads) | Odin, Baldur | Syncs you run or schedule | Your own tokens and access |
| Confluence attachment, SeCcHm form | Bifrost, Heimdall (planned) | After you approve; the SeCcHm Submit click stays yours | Per submission |
| AI completions | Mímir's tier 1 (planned) | Only for purposes your settings allow | An approved endpoint and data flow, per purpose |
| AI client reads | Ysildir (planned), stdio to VS Code | When the client calls a tool | Copilot's MCP policy; per-tool review |

Nothing is sent anywhere else: no telemetry, no update check unless an update source is configured ([updates.md](updates.md)).

## Secrets

- Tokens live in Windows Credential Manager (Asgard) or Odin's DPAPI file, protected by the user's Windows credentials. They are never written to Muninn, settings or logs.
- **Belt and braces at the database boundary.** `asgard.muninn.redact` masks anything shaped like a credential before it is stored: passwords and tokens in URLs, `Authorization` headers, bearer tokens, `token=`/`password=` pairs, GitHub and AWS key formats, JWTs and private keys. It is applied to source URLs, git remote URLs, error text, sync problems and every event payload. It recognises secrets by shape, so an opaque secret typed into free text isn't caught; it is a net, not the control.
- TLS verification is never disabled anywhere (a guardrail test in Odin; the consolidation spec adds one for Asgard).

## Protections inside Muninn

| Risk | Protection |
| --- | --- |
| One app's bug corrupts another app's data | Every app's connection carries an SQLite authorizer: writes only to the tables that app owns, no schema changes, no attaching other files, no switching protections off (`foreign_keys`, `query_only`, `writable_schema`, `user_version`, …). On Python 3.12+ SQLite's defensive mode also makes the search index's internal tables read-only. |
| Time posted to Jira twice | A `sending` row with a random marker is saved before every Jira call; a post in doubt blocks further posts for that issue and day; a stuck post is marked failed only after Jira was searched for the marker; triggers stop a posted worklog going back or being deleted; double posts show on Odin's tile and in `--muninn check` |
| Posting without consent | Baldur time can be inserted only for an approved proposal (trigger); decided proposals can't be edited (trigger) |
| Bad data from any writer | STRICT tables; CHECKs on every timestamp, enum, JSON column and state rule; Jira keys validated in every cross-app column; size limits (a day's approval ≤ 1440 minutes; an Asgard worklog ≤ 24 hours) |
| History rewritten | `events` is append-only (triggers); submission status history is written by trigger |
| File damage | `quick_check` at every start; a damaged file stops with the newest backup named and the restore command; restore verifies the backup and keeps the old file |
| A failed upgrade | A backup before every migration; each migration is all-or-nothing |
| Unbounded growth | Daily statistics refresh and WAL checkpoint; WAL size limit 64 MB; pruning of operational rows is off until retention is confirmed against the records schedule |

**Not covered:** another program running as the same user can read or change the file directly (as with any per-user data); the authorizer applies to connections opened through Asgard, and a direct `sqlite3` connection bypasses it, though the schema's triggers and CHECKs still apply and `--muninn check` detects the damage they can't prevent.

## Retention

Off by default. When switched on (`Asgard.pyw --muninn retention on`), housekeeping deletes sync run records older than 180 days, estimate runs older than 90 days that hold no decision and no open day, and reflog entries older than a year. Facts, decisions, worklogs, events and accomplishments are kept for the life of the database. Confirm these periods against the records schedule before switching it on.

## Removal

Uninstall without purge removes the program and keeps the data. With purge it removes the database and its `-wal`/`-shm`, backups, restore copies, settings and logs. Credential Manager entries and scheduled tasks are removed when they're in the install ledger (Baldur's weekly task isn't yet: a known gap in the consolidation spec).

## Dependencies

Third-party packages are allowed when declared, pinned exactly and justified, with pure-Python wheels preferred because App Control blocks unsigned DLLs ([dependency-policy.md](dependency-policy.md)). The requirements files are the bill of materials. `asgard.muninn` and the launcher's start-up path use only the standard library.
