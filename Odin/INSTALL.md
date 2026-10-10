# Installing Odin

Odin is an Asgard app (since Oct 10, 2026), so it installs with Asgard. This page takes you from Asgard's setup to a weekday schedule. Before you start, clear it with your ISSO or supervisor: Odin moves meeting metadata from your mailbox into Jira and runs local scripts ([security notes](README.md#security-notes)).

## 1. Install Asgard

Follow [Asgard's install steps](../Asgard/README.md#install): the Python install (your agency's Python 3.11 or newer) or the packaged build. Setup copies Asgard, Odin included, to `%LOCALAPPDATA%\Asgard\app` and opens the launcher. Odin runs on the same Python as Asgard, so there is nothing more to install.

Odin's command line is `odin.cmd` in `%LOCALAPPDATA%\Asgard\app\apps\odin`. Open a Command Prompt there (or add that folder to your PATH) to use the `odin` commands below.

## 2. Moving from Odin

If you used Odin before it moved into Asgard (as `Odin`):

- Its folder, `%LOCALAPPDATA%\odin` (or `%LOCALAPPDATA%\meeting2jira` from the first version), moves to `%LOCALAPPDATA%\Asgard\odin` the first time any Odin command runs: your config, the DPAPI-encrypted token and the logs come across unchanged.
- Its record of which meetings already have sub-tasks (`state.db`) moves into Muninn on the first real run, before anything is created, so no meeting is created again. The old file is kept as `state.db.migrated-DATE` for 30 days. Run `odin preview` first if you want to see that for yourself: it reads `state.db` and lists those meetings as already there.
- `odin schedule` replaces the old `meeting2jira-daily` task with "Asgard Odin daily".
- If you had pointed Asgard's Odin tile at your old copy, setup puts it back to Asgard's own Odin.

## 3. Set it up

Click the **Odin** tile. On **Today**:

1. **Create settings**, then edit the file that opens. At minimum set `jira.base_url` and `jira.default_parent` (the issue your meeting sub-tasks go under), and fix or remove the example `rules`. Save, then **Reload**.
2. Create a personal access token in Jira (your profile, *Personal Access Tokens*, with an expiry date), paste it, and **Save token**. Odin encrypts it for your Windows account.
3. **Check the setup.** It confirms Jira answers, each parent issue exists and isn't itself a sub-task, and your sub-task type is valid (it lists the valid names if not).

The same from the command line: `odin setup` walks through the environment check, the config, the token and the check.

## 4. Preview, then go live

**Preview** (`odin preview`) exports yesterday and today from Outlook and shows what would be created and posted, changing nothing. Then **Run now** (`odin`). The first real run also reads a year of your Jira issues and worklogs into Muninn, so it takes longer than later ones.

---

## 5. Tune what gets skipped

The filters most people edit are the plain substring lists in `config.json`. They are
case-insensitive and match anywhere in the text:

```json
"filters": {
  "skip_subject_contains": ["OOO", "out of office", "PTO", "holiday", "do not schedule"],
  "skip_organizer_contains": [],
  "skip_location_contains": [],
  "skip_categories": ["Personal"]
}
```

Add an entry, run `odin preview`, and check the skip reason appears. The reason text
names the exact string that matched, e.g. `subject contains 'ooo'`.

These match inside words, so `"PTO"` would also catch `OPTOMETRIST`. When you need word
boundaries, use a regular expression in `skip_subject_patterns` instead:

```json
"skip_subject_patterns": ["(?i)\\bPTO\\b"]
```

Full list of filters and routing rules: [configuration reference](README.md#configuration-reference).

---

## 6. Log time automatically (optional)

Off by default. To log each meeting's duration as work against its sub-task, set:

```json
"jira": { "log_work": true }
```

The logged time is the meeting's real length, taken from the calendar item's start and end, so a
45-minute meeting logs 45 minutes. If Jira refuses the worklog, the sub-task still exists and the
worklog is retried by the next runs for 14 days, at most three times; if the answer was lost, the
next run first checks Jira for it, so the time is never logged twice. Time you log on the sub-task
yourself counts.

Turning this on applies from that point forward. It does **not** go back and log time against
sub-tasks created while it was off, so switching it on will not suddenly post worklogs across
months of old issues.

---

## 7. Set your working hours (optional)

If you want meetings outside your tour of duty treated differently, turn on `tour_of_duty`:

```json
"tour_of_duty": {
  "enabled": true,
  "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
  "start": "07:00",
  "end": "15:30",
  "grace_minutes": 15,
  "outside_action": "label",
  "outside_label": "outside-tod",
  "outside_parent": null
}
```

`outside_action` decides what happens to a meeting that falls **entirely** outside those hours,
including anything on a non-working day:

| Value | Effect |
|---|---|
| `include` | Nothing special. Same as leaving the feature off. |
| `label` | Created as normal, plus the `outside_label` label so it's findable in Jira. |
| `route` | Filed under `outside_parent` instead, e.g. a comp-time or overtime issue. |
| `skip` | Not created at all. |

A meeting that merely *runs past* the end of your tour counts as inside, and is never skipped or
rerouted — work that overran is still work. `odin preview` marks wholly-outside meetings
with a `*` so you can see the classification before anything is created.

This setting does **not** change which days get scanned. See the note at the end of the next
section for why that matters.

---

## 8. Run it on a schedule (optional)

```
odin schedule            # as you, only while logged on
odin unschedule
```

If `tour_of_duty` is enabled, the task is scheduled 30 minutes after your tour ends, so the day's
meetings have finished; otherwise it defaults to 16:45. Override with
`odin schedule -At '17:15'`.

Outlook COM needs your interactive session, so the task only runs while you are logged on. It
looks back one day, picking up meetings that ended after the previous run.

**On meetings that run late:** you don't need to do anything clever here. Only ended meetings are
pushed, and Muninn's record of sub-tasks means re-running over the same days can't create duplicates. So a
meeting that was still in progress at run time is simply picked up by the next run's one-day
lookback. If you want a wider safety margin, widen the window — it costs nothing:

```
odin run -DaysBack 3
```

If Group Policy blocks task creation you will see "Access is denied"; run `odin` (or **Run now**) by
hand instead.

A scheduled task is invisible when it breaks, so check on it now and then:

```
odin status
```

That leads with the last run's result and warns if it was a failure, if nothing has run for several
days, or if there is an unresolved alert. `odin doctor` reports the same thing alongside
the environment check.

You should not have to remember to check, though. **If a run fails, a file named
`ATTENTION-Odin.txt` appears on your Desktop**, naming the error and the command that
diagnoses it. It is deleted automatically by the next successful run. To ride out a one-off network
blip instead of being told immediately:

```json
"notify": { "alert_after_failures": 2 }
```

The token is also watched: you get a warning starting 14 days before your personal access token
expires (`jira.warn_token_expiry_days`), so the first sign of trouble is not a week of failed runs.
Some older Jira versions do not expose expiry dates, in which case the check silently does nothing.

---

## Command reference

```
odin                 daily run: export yesterday and today, push, read Jira into Muninn, post approved days
odin preview         same, but change nothing
odin setup           first-run walkthrough
odin check           verify config, token, Muninn and Jira access
odin status          last-run health, recent sub-tasks, what waits to be posted
odin sync            read Jira into Muninn only
odin post            post approved Baldur days only (post --dry-run lists them)
odin report          every meeting sub-task as a CSV for Power BI (report --no-subjects)
odin doctor          read-only environment report
odin schedule        register the weekday scheduled task
odin unschedule      remove the scheduled task
odin csv <file>      the daily run from an Outlook CSV export
odin selftest        unit tests plus the Windows-only checks
odin run [args]      Invoke-MeetingSync.ps1 passthrough, e.g. run -DaysBack 7
odin cli [args]      Python CLI passthrough, e.g. cli forget PROJ-501
```

Exit codes: `0` fine, `1` finished with per-item errors (a meeting, an approved day Jira refused) or a
failed check, `2` configuration, usage, credential or Muninn problem, or another Odin run in progress,
`130` interrupted.

---

## If Outlook COM is blocked

Under Constrained Language Mode, or without classic Outlook, export the calendar by hand:

1. Outlook: *File → Open & Export → Import/Export → Export to a file → Comma Separated Values*.
2. Choose your **Calendar** folder, a filename, and a date range.
3. Push it:

```
odin csv "%USERPROFILE%\Documents\calendar.csv" -DryRun
odin csv "%USERPROFILE%\Documents\calendar.csv"
```

4. **Delete the CSV afterwards** — it contains full meeting bodies.

The CSV has no response status, so declined meetings cannot be filtered on this path. Compensate
with `skip_subject_contains` or routing rules. Meetings already pushed via COM are still recognized,
so the two paths will not duplicate each other.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No config found` | Click the Odin tile and **Create settings**, or run `odin setup`. |
| `Muninn isn't set up yet` | Open Asgard once; it creates Muninn. |
| `no Python that can run Odin was found` | Odin needs the Python Asgard runs on (3.11 or newer on Windows, for Muninn's SQLite). The message lists every candidate tried and why each was rejected. Run Odin from Asgard's folder, or pass `odin run -Python "C:\path\to\python.exe"`. |
| `Another Odin run is in progress` | The scheduled task is running, or the window is. Wait for it. Windows lets go of the lock the moment a run ends, however it ended, so this only appears while one really is running. |
| Scripts won't run at all | `odin doctor` reports execution policy and the internet-zone mark. Use `Unblock-File`, never `-ExecutionPolicy Bypass`. |
| `...returned 'text/html' instead of JSON` | An SSO or proxy page is intercepting the API. Ask the Jira admins which URL accepts token-authenticated REST calls. |
| Certificate errors | Export your agency CA chain as PEM and set `jira.ca_bundle`. Never disable verification. |
| `Could not decrypt jira_token.dpapi` | DPAPI only opens for the same user on the same machine. Paste the token again on **Today**, or run `odin set-token`. |
| A security prompt from Outlook | Something read a guarded property. Check whether `-IncludeOrganizer` is in play; without it the export avoids guarded fields. |
| Export produced 0 items | Regional date format. Run `odin run -DaysBack 1 -Verbose` and inspect the `Restrict` filter it prints. |
| Approved days weren't posted | The run's worklog sync didn't finish, so Odin couldn't tell what Jira already holds; the run says what failed, and the days wait for the next run. |

More detail: [README troubleshooting](README.md#troubleshooting).

---

## Developing off Windows

On macOS or Linux there is no Outlook and no DPAPI, but the pipeline, filters, Muninn and the Jira client all run. From `Asgard/`:

```bash
python3 -m unittest discover -s tests -p "test_odin_*.py"     # Odin's tests, on a temporary Muninn
export JIRA_PAT='...'                                         # set-token needs DPAPI, so use the env var
python3 apps/odin/cli.py push --csv tests/fixtures/odin/sample_outlook.csv --dry-run
```

`infra/windows-test-vm/` stands up a throwaway Windows Server host for the checks that need a real Windows PowerShell 5.1: see its [README](../infra/windows-test-vm/README.md).
