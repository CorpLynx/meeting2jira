# Running Muninn

Oct 6, 2026 · the runbook for the database file · schema v3

Day to day there is nothing to do: Asgard checks the file, backs it up and tidies it each time it starts. This page is for when something says otherwise.

## Where things are

| Path (`%LOCALAPPDATA%\Asgard\`) | What |
| --- | --- |
| `muninn.db`, `muninn.db-wal`, `muninn.db-shm` | The database and its write-ahead log. Copy all three or none; never copy just `muninn.db` while Asgard runs |
| `backups\muninn-YYYYMMDD.db` | Daily copy, 7 kept |
| `backups\muninn-YYYYMMDD-before-vN.db` | Taken before each schema upgrade, 3 kept |
| `backups\muninn-YYYYMMDD-manual.db` | From the menu or `--muninn backup`, 5 kept |
| `muninn.before-restore-<time>.db` | The file as it was before a restore |
| `logs\launcher.log` | Start-up problems, including Muninn's warnings |

Keep the folder out of OneDrive: a synced database fights the sync engine for its locks.

## Commands

Run from a console with `python.exe` (not `pythonw.exe`, which prints nothing), in Asgard's install folder (`%LOCALAPPDATA%\Asgard\app`):

```
python Asgard.pyw --muninn status            version, sizes, backups, last housekeeping
python Asgard.pyw --muninn check             look for damage and for anything that should never happen
python Asgard.pyw --muninn repair            rebuild the search index; fix event cursors
python Asgard.pyw --muninn backup            a copy now
python Asgard.pyw --muninn restore [FILE]    put a backup back (newest if FILE is left out); asks first
python Asgard.pyw --muninn maintain          run housekeeping now
python Asgard.pyw --muninn retention on|off|show
```

Exit codes: 0 fine, 1 `check` found errors, 2 couldn't run.

## When something is wrong

| You see | It means | Do |
| --- | --- | --- |
| "Muninn's database file is damaged (…)" at start | The file failed its integrity check. Nothing has been moved | Close every Asgard app and Odin, then run the restore command the message shows. Anything since that backup is lost; the damaged file is kept as `muninn.before-restore-<time>.db` |
| Same, with "There is no backup" | Damaged and no copy exists | Don't delete the file; copy all three files somewhere safe and ask for help. Most of it can usually be read |
| "Muninn is busy in another Asgard app" | Another app held the write lock for 30 s | Wait and retry. If it repeats, close apps one at a time to find the one stuck mid-write |
| "Muninn is at version N, newer than X understands" | The app is older than the database | Update that app (or Asgard) |
| "… needs version N or newer. Open Asgard to upgrade it." | The database is older than the app | Open Asgard once; it upgrades after taking a backup |
| "Muninn isn't set up yet" | No database yet | Open Asgard once |
| "Muninn needs to update its database, and couldn't first make a safety copy" | The backups folder can't be written (disk full, permissions) | Free space or fix the folder; nothing was changed |
| "Today's backup wasn't made" (launcher message) | Same cause, but the database is fine | Fix the folder; Asgard keeps working meanwhile |
| "N rows … point at rows that are gone" | Broken links, usually from a tool that wrote with foreign keys off | Run `check` for details; restore a backup from before it started if the rows matter |

## What `check` reports, and the fix

| Finding | Fix |
| --- | --- |
| `[error] file`: damaged | `--muninn restore` (close everything first) |
| `[error] links`: rows pointing at missing rows | Restore a backup from before it started, or ask for help |
| `[error] search`: index damaged · `[warning] search`: entries missing, out of date or left over | `--muninn repair` |
| `[error] worklogs`: time in Jira N times | Delete the extra worklog in Jira (the message names the ids); Odin's next sync records it |
| `[warning] worklogs`: posts in doubt over an hour | Run Odin; it searches Jira for each and settles it |
| `[warning] keys`: keys Odin can never resolve | Re-key those rows in the app that owns them |
| `[error] events`: a cursor past the newest event | `--muninn repair` |
| `[info] runs`: runs that stopped without finishing | Nothing; Asgard closes them as failed at its next start |

## Undoing a bad upgrade

1. Close every Asgard app and Odin.
2. `python Asgard.pyw --muninn restore muninn-YYYYMMDD-before-vN.db`
3. Put back the previous Asgard version (see [updates.md](updates.md)); otherwise the next start upgrades the file again.

## Moving to a new machine

1. On the old machine: `--muninn backup`, then copy the new file from `backups\`.
2. Install Asgard on the new machine and open it once.
3. Close it and run `--muninn restore <the copied file>`.

Tokens don't move: Credential Manager entries and Odin's DPAPI file only open for the same user on the same machine. Set them again on the new one.

## Retention

Off by default, because how long federal records must be kept is the records officer's decision. A year of one engineer's data is under 20 MB, two thirds of it sync run records. When retention is on, housekeeping prunes old sync runs (180 days), estimate runs with no decision or open day (90 days) and reflog entries (1 year), at most 2 s a day, in small transactions.

## What housekeeping does each day

At the first Asgard start each day, after the integrity check and the backup: refresh the query planner's statistics (`PRAGMA optimize`), fold the write-ahead log back into the file (`wal_checkpoint`), delete half-written backup copies a crash left (older than an hour), close runs abandoned over six hours ago, and prune if retention is on.
