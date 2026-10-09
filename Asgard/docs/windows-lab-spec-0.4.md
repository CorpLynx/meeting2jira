# Windows lab checks for Asgard 0.4.0

Oct 9, 2026 · status: spec, not built · runs on `infra/windows-test-vm/` through `./run-checks.sh asgard`

0.4.0 can't ship until these pass on Windows: Muninn's hardening (schema v3, the guard, restore, self-repair) and Baldur's GitHub keys. macOS can't prove them. This spec adds those checks to `tools/windows_checks.py` and says how to run them. The results go in `docs/windows-lab-<date>.md`, like the 2026-10-06 run.

## Decision: US time zones only

Brandon (Oct 9): Asgard only ever runs in US time zones. This has three consequences:

- **The suite runs in the US zones**, not the old spread. Arizona and Hawaii are in because they have no daylight saving:

| Windows zone | Why |
| --- | --- |
| UTC | Control |
| Eastern Standard Time | Brandon's zone, and the zone the macOS tests use |
| Central Standard Time | |
| Mountain Standard Time | |
| US Mountain Standard Time | Arizona: no daylight saving |
| Pacific Standard Time | |
| Alaskan Standard Time | |
| Hawaiian Standard Time | No daylight saving; the furthest from Eastern (5-6 h) |

- **Review R4 is accepted, not fixed.** Travel within the US shifts a day's bounds by at most 6 hours. So a hand-logged worklog only lands in the wrong day if it started within 6 hours of midnight and the laptop then changes zone before the day's approval is changed. Baldur's own worklogs aren't affected (they count for their approved day).
- **The "midnight daylight-saving zones" limitation no longer applies**, since every US zone changes at 02:00.

## Two runs

The lab installs one python.org Python at boot (`var.python_version`). Run the checks twice, because the guard behaves differently on each:

| Run | Python | Why |
| --- | --- | --- |
| A | 3.11.9 | The oldest Python whose SQLite Muninn accepts. It has no `setconfig`, so defensive mode is off and apps can write the search index's internal tables |
| B | 3.12.10 (the default) | Defensive mode on, as on most current installs |

```bash
cd infra/windows-test-vm
terraform apply -var python_version=3.11.9
./run-checks.sh asgard                        # run A
terraform apply -var python_version=3.12.10 -replace=aws_instance.this
./run-checks.sh asgard                        # run B
terraform destroy
```

Both runs must end with 0 failed. Record each run's Python, SQLite and Defender status in the results file.

## Changes to `tools/windows_checks.py`

Existing checks stay. Two existing checks need a fix:

- `ZONES` becomes the table above.
- "Muninn is created at schema v2" becomes "at the latest schema". Compare with `muninn.SCHEMA_VERSION` rather than a literal, so it doesn't go stale again.

New checks, in a `muninn_phase()` that runs as the administrator after `facts()`. W9 and W10 run in the standard-account phase. Each check works in its own temporary `ASGARD_HOME` and starts any second process with `sys.executable`.

| # | Check | How | Passes when |
| --- | --- | --- | --- |
| W1 | Facts for the record | Print `sqlite3.sqlite_version` and whether `sqlite3.SQLITE_DBCONFIG_DEFENSIVE` exists. Check whether `sqlite_dbpage` exists (`SELECT 1 FROM sqlite_dbpage LIMIT 1` on a scratch db). Read Defender real-time status (`Get-MpComputerStatus`) | Always passes. It's information for the results file |
| W2 | A fresh database is v3 and matches its migrations | `prepare()`, then `integrity.check()` | Version equals `SCHEMA_VERSION`; no findings |
| W3 | Upgrade v2 → v3 on this SQLite | `prepare(folder=)` with only 0001–0002, add a few rows, then `prepare()` | v3; a `before-v3` backup exists; the rows survive; `check()` has no findings, so the schema comparison works on this SQLite's FTS5 |
| W4 | Restore refuses while another app holds the file | A child process opens the db with `open_app("odin")`, reads one row and holds the connection open for 20 s. The parent runs `restore(backup)`. Repeat with the child inside a read transaction, then with it writing | Every attempt raises `MuninnError` "Close Asgard…". The live file is byte-identical afterwards. No `before-restore` file and no `.tmp` file are left behind |
| W5 | Restore works once everything is closed, from the console | `python.exe Asgard.pyw --muninn restore --yes` in the install folder | Exit 0; the db holds the backup's rows; one `muninn.before-restore-*.db` sits beside it |
| W6 | A damaged file stops startup with a usable message | Overwrite the first 4 KB of a checkpointed db, then `prepare()` | `CorruptError` naming the newest backup. Its restore command names `python.exe`, not `pythonw.exe`, and running that command restores |
| W7 | Search-index damage is rebuilt at start | Open with `open_app("loki")` and `DELETE FROM search_data`. Run A should allow it; run B should refuse it. Then `prepare()` | Run B: the delete is refused. Run A: if the delete is allowed, `prepare()` warns "search index was damaged and has been rebuilt" and `check()` then has no findings; if this SQLite refuses it anyway, record that |
| W8 | Backups race under real-time antivirus | 4 processes × 10 rounds, each running `backup(replace=True)` into one folder at once | No exception; every final file passes `integrity_check`; no `.tmp` left. Record whether Defender was on |
| W9 | The maintenance commands as a standard user | `--muninn status`, `check`, `repair`, `backup`, `maintain` and `retention show` against the installed Asgard | Exit 0 each. `check` on a db with a planted cursor past the newest event exits 1 |
| W10 | Uninstall with purge removes restore copies | After W5's restore, `--uninstall --yes --purge` | No `muninn.before-restore-*` file and no Asgard folder remain |

The suite part already covers the guard, the row rules and Baldur's GitHub keys against `fake_github.py`. The `test_muninn*` and `test_baldur*` modules run in every zone. If the Python lacks something a test needs (`setconfig` on 3.11), that test must skip, not fail.

## Not covered, still for the real machine

- The agency Python build, App Control, Constrained Language Mode, TLS inspection, and a real GitHub Enterprise Server and Jira.
- Anything seen on screen: the launcher's damaged-database dialog and the Qt shared window.
- A OneDrive-redirected profile, which the runbook says to avoid.

## Done means

- Runs A and B each print 0 failed.
- `docs/windows-lab-<date>.md` lists both runs, every W check and anything the lab found.
- `HANDOFF.md` "What's next, 2" moves the Windows items to done.

0.4.0 can then ship.
