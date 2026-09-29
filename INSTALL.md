# Installing meeting2jira

Getting from a fresh copy of this folder to sub-tasks appearing in Jira. For what the options
*mean*, see the [configuration reference](README.md#configuration-reference); for why it is built
this way, see [design notes](README.md#design-notes).

**Before you start:** clear this with your ISSO or supervisor. It copies meeting metadata out of
your mailbox into Jira and runs local scripts. [Security notes](README.md#security-notes) is
written to be handed over as-is.

---

## 1. Requirements

| Need | Notes |
|---|---|
| Windows 10/11 | Windows PowerShell 5.1, which ships with Windows. PowerShell 7 is not required. |
| Python 3.8 or newer | From your agency software catalog. Nothing is installed with pip. |
| Classic Outlook, signed in | Only for the automatic export. "New Outlook" (`olk.exe`) has no COM interface. Without it you can still use the CSV path. |
| Jira Data Center account | Plus a personal access token. Jira Cloud is not supported. |

No local admin. No app registration. No PowerShell modules. No pip.

---

## 2. Copy the program folder into your profile

You only need **`app/`**. It is the whole program: the entry point, the PowerShell scripts, the
Python package, and its tests. Nothing in it refers to anything outside itself, so the rest of the
repo (Terraform, docs, project rules) can stay behind.

Put it somewhere you can write without admin rights, for example:

```
%USERPROFILE%\meeting2jira
```

Everything below assumes you are **inside that folder**:

```powershell
cd "$env:USERPROFILE\meeting2jira"
```

If you copied it from a zip, Windows marks the files as internet-sourced and `RemoteSigned` will
refuse to run them. Clear the mark:

```powershell
Get-ChildItem -Recurse "$env:USERPROFILE\meeting2jira" | Unblock-File
```

Your config, token, state database, and logs are kept separately in
`%LOCALAPPDATA%\meeting2jira`, not in this folder. That means upgrading is just replacing the folder
— nothing you have set up is lost.

If your policy is `AllSigned`, the `.ps1` files have to be signed through your organization's
code-signing process. Do not use `-ExecutionPolicy Bypass` to get around either case.

---

## 3. Run setup

From the repo folder in a normal, non-admin PowerShell or Command Prompt window:

```powershell
.\meeting2jira setup
```

That walks through all of it:

1. **Environment check.** Reports language mode, execution policy, Python, classic Outlook, and
   proxy configuration, then says whether you can use the automatic export or need the CSV path.
2. **Creates the config** at `%LOCALAPPDATA%\meeting2jira\config.json`.
3. **Opens it in Notepad.** Fill in the two required values, save, and close to continue:
   - `jira.base_url` — e.g. `https://jira.agency.gov`
   - `jira.default_parent` — the issue every meeting hangs off by default, e.g. `PROJ-123`

   Also delete or edit the example `rules`, which reference issues that do not exist in your Jira.
4. **Stores your Jira token** (DPAPI-encrypted, your user and machine only), then **verifies
   everything**: Jira reachability, authentication, that each parent issue exists and is not itself
   a sub-task, and that your sub-task type name is valid.

Create the token first in Jira under *Profile → Personal Access Tokens*. Set an expiry and note
the date; nothing here can renew it for you.

If the last step reports problems, fix them and re-run:

```powershell
.\meeting2jira check
```

---

## 4. Preview, then go live

```powershell
.\meeting2jira preview     # your real calendar; creates nothing
```

Read the output carefully. Each line is either `WOULD` (a sub-task would be created, and under
which parent), `SKIP` with a reason, or `EXISTS`. Confirm that:

- meetings you expect are present, with sensible durations
- personal appointments, declined meetings, and anything private are being skipped
- the parents are the issues you intended

Then, for real:

```powershell
.\meeting2jira
```

Run it a second time. Everything should report `EXISTS` and nothing new should appear in Jira.
That is the duplicate protection working.

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

Add an entry, run `.\meeting2jira preview`, and check the skip reason appears. The reason text
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
45-minute meeting logs 45 minutes. If a worklog call fails, the sub-task still gets created and
the worklog is retried on the next run, up to three attempts.

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
rerouted — work that overran is still work. `.\meeting2jira preview` marks wholly-outside meetings
with a `*` so you can see the classification before anything is created.

This setting does **not** change which days get scanned. See the note at the end of the next
section for why that matters.

---

## 8. Run it on a schedule (optional)

```powershell
.\meeting2jira schedule            # as you, only while logged on
.\meeting2jira unschedule
```

If `tour_of_duty` is enabled, the task is scheduled 30 minutes after your tour ends, so the day's
meetings have finished; otherwise it defaults to 16:45. Override with
`.\meeting2jira schedule -At '17:15'`.

Outlook COM needs your interactive session, so the task only runs while you are logged on. It
looks back one day, picking up meetings that ended after the previous run.

**On meetings that run late:** you don't need to do anything clever here. Only ended meetings are
pushed, and the state database means re-running over the same days can't create duplicates. So a
meeting that was still in progress at run time is simply picked up by the next run's one-day
lookback. If you want a wider safety margin, widen the window — it costs nothing:

```powershell
.\meeting2jira sync -DaysBack 3
```

If Group Policy blocks task creation you will see "Access is denied"; run `.\meeting2jira` by hand
instead.

A scheduled task is invisible when it breaks, so check on it now and then:

```powershell
.\meeting2jira status
```

That leads with the last run's result and warns if it was a failure, if nothing has run for several
days, or if there is an unresolved alert. `.\meeting2jira doctor` reports the same thing alongside
the environment check.

You should not have to remember to check, though. **If a run fails, a file named
`ATTENTION-meeting2jira.txt` appears on your Desktop**, naming the error and the command that
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
meeting2jira                 daily run: export yesterday and today, push to Jira
meeting2jira preview         same, but create nothing
meeting2jira setup           first-run walkthrough
meeting2jira check           verify config, token and Jira access
meeting2jira status          last-run health, then recent sub-tasks
meeting2jira doctor          read-only environment report
meeting2jira schedule        register the weekday scheduled task
meeting2jira unschedule      remove the scheduled task
meeting2jira csv <file>      push from an Outlook CSV export
meeting2jira selftest        unit tests plus the Windows-only checks
meeting2jira sync [args]     Invoke-MeetingSync.ps1 passthrough, e.g. sync -DaysBack 7
meeting2jira cli  [args]     Python CLI passthrough, e.g. cli forget PROJ-501
```

Exit codes: `0` fine, `1` finished with per-item errors or a failed check, `2` configuration,
usage, or credential problem, `130` interrupted.

---

## If Outlook COM is blocked

Under Constrained Language Mode, or without classic Outlook, export the calendar by hand:

1. Outlook: *File → Open & Export → Import/Export → Export to a file → Comma Separated Values*.
2. Choose your **Calendar** folder, a filename, and a date range.
3. Push it:

```powershell
.\meeting2jira csv "$HOME\Documents\calendar.csv" -DryRun
.\meeting2jira csv "$HOME\Documents\calendar.csv"
```

4. **Delete the CSV afterwards** — it contains full meeting bodies.

The CSV has no response status, so declined meetings cannot be filtered on this path. Compensate
with `skip_subject_contains` or routing rules. Meetings already pushed via COM are still recognized,
so the two paths will not duplicate each other.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No config found` | Run `.\meeting2jira setup`. |
| `No working Python 3.8+ found` | The message lists every candidate it tried and why each was rejected — start there. Discovery searches PATH, the registry, and the usual install directories, and **runs** each candidate rather than trusting that it exists. Common causes: `py.exe` installed with no 3.x registered, only the Microsoft Store stub present, or a 2.x. Escape hatch: `.\meeting2jira sync -Python "C:\path\to\python.exe"`. |
| Scripts won't run at all | `.\meeting2jira doctor` reports execution policy and the internet-zone mark. Use `Unblock-File`, never `-ExecutionPolicy Bypass`. |
| `...returned 'text/html' instead of JSON` | An SSO or proxy page is intercepting the API. Ask the Jira admins which URL accepts token-authenticated REST calls. |
| Certificate errors | Export your agency CA chain as PEM and set `jira.ca_bundle`. Never disable verification. |
| `Could not decrypt jira_token.dpapi` | DPAPI only opens for the same user on the same machine. Run `.\meeting2jira cli set-token` again. |
| A security prompt from Outlook | Something read a guarded property. Check whether `-IncludeOrganizer` is in play; without it the export avoids guarded fields. |
| Export produced 0 items | Regional date format. Run `.\meeting2jira sync -DaysBack 1 -Verbose` and inspect the `Restrict` filter it prints. |

More detail: [README troubleshooting](README.md#troubleshooting).

---

## Developing off Windows

On macOS or Linux there is no Outlook and no DPAPI, but the pipeline, filters, and Jira client all
run. Use `./m2j` (named differently only because the Python package already owns the name
`meeting2jira` at the repo root):

```bash
./m2j selftest                                  # unit tests
export JIRA_PAT='...'                           # set-token needs DPAPI, so use the env var
./m2j preview tests/fixtures/sample_outlook.csv
```

`infra/windows-test-vm/` (outside `app/`) stands up a throwaway Windows Server host for the checks
that need a real Windows PowerShell 5.1: see its [README](infra/windows-test-vm/README.md).
