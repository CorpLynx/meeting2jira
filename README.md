# meeting2jira (POC)

Turns the meetings on your Outlook/Teams calendar into Jira sub-tasks, with optional worklogs. It is built to run on a locked-down federal Windows 11 workstation. It needs no local admin, no Entra app registration, no PowerShell Gallery modules, and no pip installs.

```
Outlook calendar ──(PowerShell: COM export, or a CSV you export)──► JSON/CSV file
      ──(Python, stdlib only: filter → route → dedupe → create)──► Jira sub-tasks (+ worklogs)
```

## Quick start

Copy the **`app/`** folder somewhere in your profile, then from inside it, in a normal (non-admin)
PowerShell window:

```powershell
cd app
.\meeting2jira setup       # environment check, config, token, and connectivity check
.\meeting2jira preview     # your real calendar, nothing created
.\meeting2jira             # for real
```

`app/` is the entire program and needs nothing else from this repo at runtime.

`setup` walks through everything and leaves you with a working config. Step-by-step detail, including
what to do when something is blocked: **[INSTALL.md](INSTALL.md)**.

Everything is also reachable directly, which is what the entry point calls underneath:

```powershell
.\src\windows\Test-Environment.ps1                      # read-only environment report
py -3 -m meeting2jira init                             # %LOCALAPPDATA%\meeting2jira\config.json
py -3 -m meeting2jira set-token                        # paste your Jira PAT; stored DPAPI-encrypted
py -3 -m meeting2jira check                            # Jira access, parent issues, sub-task type
.\src\windows\Invoke-MeetingSync.ps1 -DryRun            # export + push, creating nothing
```

Run `py -3 -m meeting2jira ...` from the repo root, because that's how Python finds the package. If `py` isn't available, use the full path to `python.exe`. (`.\meeting2jira` handles this for you.)

---

## Design notes

### Constraints assumed

- Standard user with no local admin.
- PowerShell may be in **Constrained Language Mode** (AppLocker/WDAC). The execution policy may be `RemoteSigned` or `AllSigned`, enforced by GPO.
- PowerShell Gallery and PyPI are blocked or need approval. Python 3 comes from the agency software catalog.
- Agency TLS inspection with an internal root CA, and a web proxy (possibly a PAC file).
- Jira is on-prem Data Center, which is why it uses a personal access token (bearer auth). Jira Cloud is out of scope.
- The M365 tenant may be GCC, GCC High, or DoD. User consent to apps is restricted, and device-code sign-in is often blocked by Conditional Access.

### Getting calendar data: options considered

| Option | What it needs | Verdict |
|---|---|---|
| **Microsoft Graph** `/me/calendarView` (what the first script used) | An Entra app registration with `Calendars.Read` consent. The Microsoft Graph PowerShell SDK needs PSGallery, and its first-party app is usually not consented in federal tenants. National-cloud endpoints are required for GCC High/DoD. | Best long-term source, but it needs IT. **Phase 2** (see Roadmap). |
| **Outlook object model (COM)** from PowerShell | Classic Outlook, and PowerShell in FullLanguage mode | **Path A (primary).** It reuses your already signed-in Outlook, so there's no auth, no network calls of its own, and no admin. Recurring meetings are expanded for you. |
| **Outlook CSV export** (Import/Export wizard) | Nothing beyond classic Outlook | **Path B (fallback).** A manual step, but it works even under Constrained Language Mode. The export also expands recurrences. |
| Exchange Web Services (EWS) | — | **No.** Microsoft is disabling EWS in Exchange Online starting Oct 1, 2026. |
| Published ICS calendar URL | Anonymous calendar publishing | **No.** Usually disabled in federal tenants, and it would require an RRULE parser. |
| pywin32 / requests / msal / keyring | pip access | **Avoided.** Everything is stdlib, so there are no packages to get approved or to list in an SBOM. |

### Why PowerShell *and* Python

- **PowerShell does the Windows-native parts.** It talks to Outlook over COM (Python would need pywin32 for that), registers the scheduled task, and runs environment checks. It is deliberately thin: it dumps what's on the calendar and makes no decisions.
- **Python does the logic.** That covers filtering, routing rules, templates, dedupe state (sqlite3), Jira HTTP, and credential storage (DPAPI via ctypes). This code is testable offline and easy to iterate on.
  - Python's `urllib` on Windows trusts the **Windows certificate store**, so an agency root CA that Windows trusts just works. `requests` ships its own CA bundle and usually breaks under TLS inspection.
- **They meet at a versioned JSON file** (see [Export schema](#export-schema-v1)). Anything that can write that file can be a source, which is where a Graph source would plug in later.
- `Invoke-MeetingSync.ps1` and `Test-Environment.ps1` stick to cmdlets and core types, so they run under Constrained Language Mode. Only `Export-OutlookMeetings.ps1` needs FullLanguage.

### How a meeting becomes a sub-task

1. **Filters** are hard exclusions: cancelled, all-day, not ended yet, no attendees, declined, private, shown as free, too short or too long, or a subject pattern match.
2. **Rules** are evaluated in order, and the first match wins. A match either routes the meeting to a specific parent issue or skips it.
3. Anything left goes to `jira.default_parent`.
4. **Dedupe**: the meeting's key and a content hash (subject + start + end) are checked against local state. The content hash means a COM run and a CSV run won't duplicate each other.
5. The sub-task is created and recorded right away. Then the optional worklog and optional transition happen.

Safety rails:

- Meetings are only pushed after they've ended (`only_ended`).
- Each run creates at most `max_creates_per_run` sub-tasks.
- `--dry-run` shows the full plan and changes nothing.

---

## Setup

1. **Clear it with your ISSO/supervisor first.** This moves meeting metadata from your mailbox into Jira and runs local scripts. See [Security notes](#security-notes) for a summary you can hand them.
2. **Put the repo in your profile**, e.g. `%USERPROFILE%\tools\meeting2jira`.
   - If you downloaded it and your policy is `RemoteSigned`, clear the downloaded-file mark with `Get-ChildItem -Recurse | Unblock-File`.
   - If `AllSigned` is enforced, the `.ps1` files need signing through your organization's process.
   - Don't work around policy with `-ExecutionPolicy Bypass`.
3. **Run `.\src\windows\Test-Environment.ps1`.** It reports your language mode, execution policy, Python, classic Outlook, and proxy/PAC, then tells you which path to use.
4. **Create the config** with `py -3 -m meeting2jira init`, then edit it (see [Configuration reference](#configuration-reference)). At minimum set `jira.base_url` and `jira.default_parent`, and fix or remove the example `rules`.
5. **Create a PAT** in Jira: *Profile → Personal Access Tokens*, with an expiry date. Then run `py -3 -m meeting2jira set-token`.
6. **Run `py -3 -m meeting2jira check` until everything is `OK`.** It confirms that each parent issue exists and isn't itself a sub-task, and that your sub-task type name is valid. If the type name is wrong, it lists the valid ones.
7. **Dry run, then a real run** (below).

## Usage

### Path A: automatic export via Outlook COM

```powershell
.\src\windows\Invoke-MeetingSync.ps1 -DryRun            # today so far
.\src\windows\Invoke-MeetingSync.ps1 -DaysBack 5        # since midnight 5 days ago
.\src\windows\Invoke-MeetingSync.ps1 -KeepExport -Verbose   # keep the JSON for inspection
```

This exports your calendar to `%LOCALAPPDATA%\meeting2jira\exports\`, pushes it, and deletes the export after a successful run. Re-running over the same window is safe.

By default it does not read the organizer. `-IncludeOrganizer` adds it, but Outlook guards that property and may show an "a program is trying to access e-mail address information" prompt, depending on policy.

### Path B: Outlook CSV export (works under Constrained Language Mode)

1. In classic Outlook: *File → Open & Export → Import/Export → Export to a file → Comma Separated Values*.
2. Pick your **Calendar** folder, choose a file name, and click *Finish*.
3. Enter the date range when prompted.
4. Push the file:

```powershell
.\src\windows\Invoke-MeetingSync.ps1 -Source Csv -CsvPath "$HOME\Documents\calendar.csv" -DryRun
# or, without PowerShell at all:
py -3 -m meeting2jira push --csv "%USERPROFILE%\Documents\calendar.csv" --dry-run
```

5. **Delete the CSV afterwards.** It contains full meeting bodies.

The CSV doesn't include your response status, so declined meetings can't be filtered on this path. Use `skip_subject_patterns` or rules to compensate.

### Scheduling (optional)

```powershell
.\src\windows\Register-MeetingSyncTask.ps1 -At '16:45'     # weekdays, runs as you, only while logged on
Start-ScheduledTask -TaskName meeting2jira-daily           # test it now
.\src\windows\Register-MeetingSyncTask.ps1 -Unregister
```

The task uses `-DaysBack 1`, so meetings that ended after yesterday's run get picked up. If Group Policy blocks task creation you'll see "Access is denied"; run the sync by hand instead.

### Other commands

```powershell
.\meeting2jira status                     # last-run health, then recent sub-tasks
.\meeting2jira doctor                     # environment report, including last-run health
.\meeting2jira selftest                   # unit tests plus the Windows-only checks
py -3 -m meeting2jira forget PROJ-456     # after deleting a sub-task in Jira, allow it to be recreated
py -3 -m meeting2jira push --input <file> --max 100    # raise the per-run cap once (e.g. backfill)
```

Logs are written to `%LOCALAPPDATA%\meeting2jira\logs\`: `meeting2jira.log` from Python and `sync_*.log` transcripts from PowerShell. Transcripts older than 30 days are pruned on each run (`-TranscriptRetentionDays`).

Each real run also writes `%LOCALAPPDATA%\meeting2jira\last_run.json` with the timestamp, counts, exit code, and first error. `status` and `Test-Environment.ps1` read it, so a scheduled task that quietly started failing is visible without opening a log.

### Staying out of trouble

Two things make repeated runs safe:

- **A create that fails ambiguously is not retried blindly.** A proxy timeout or 502/503/504 on the POST that creates an issue may mean Jira created it anyway. Every sub-task carries a deterministic `m2j-<hash>` label, so the next step is an exact JQL lookup for that label. One match is recorded as that meeting's sub-task; zero, several, or a failed search are reported for you to resolve, never guessed at.
- **A failed worklog is retried.** If the sub-task is created but the worklog call fails, the worklog is retried at the start of the next run, up to three attempts, and never logged twice. Only genuine failures are retried: enabling `log_work` does not backfill sub-tasks created while it was off.
- **Overlapping runs fail safely.** If a manual run collides with the scheduled one, the second exits 2 with "another run is probably in progress" rather than corrupting the state that prevents duplicates.

---

## Configuration reference

The config file is `%LOCALAPPDATA%\meeting2jira\config.json`. It's JSON, and any key starting with `_` is treated as a comment. Anything you omit falls back to the defaults in `meeting2jira/config.py`.

**`jira`**

| Key | Default | Notes |
|---|---|---|
| `base_url` | — | Must be `https://`. Jira Data Center only; `check` fails if it points at a Cloud site. |
| `default_parent` | — | Issue key for meetings no rule claims. It must be a standard issue, not a sub-task. |
| `subtask_type` | `Sub-task` | Some projects call it `Subtask`; `check` lists the valid names. |
| `labels` | `["meeting"]` | Handy for JQL reporting: `labels = meeting AND created >= startOfWeek()`. |
| `assign_to_me` | `true` | Uses `/myself` and sets the assignee by `name`. |
| `log_work` | `false` | Adds a worklog equal to the meeting's length, dated at the meeting's start. |
| `transition_to` | `null` | e.g. `"Done"`. Matches either the transition name or the target status. |
| `warn_token_expiry_days` | `14` | Warn this many days before the personal access token expires, so the first sign is not a run of 401s. Reads `/rest/pat/latest/tokens`; older Data Center versions do not expose it, in which case the check quietly does nothing. `0` disables. |
| `dedupe_label` | `true` | Adds a deterministic `m2j-<hash>` label to every sub-task. It is what lets a create that failed ambiguously be resolved with an exact JQL lookup instead of a guess. Turning it off means an ambiguous create is reported for you to sort out by hand. |
| `extra_fields` | `{}` | Merged into the create payload for required custom fields, e.g. `{"customfield_10010": {"value": "Overhead"}}`. |
| `max_creates_per_run` | `40` | Safety cap per run. Must be at least 1: there is no setting for "unlimited", because the cap is what stops a misconfigured first run from filling Jira. For a one-off backfill pass a large `--max`. |
| `ca_bundle` | `null` | PEM file of extra CAs. The Windows store is always used as well. |
| `proxy` | `null` | e.g. `http://proxy.agency.gov:8080`. `null` means use the static Windows proxy settings. PAC files are not read. |

**`filters`**: see `config.example.json` for the full set. Beyond the on/off switches (`skip_declined`, `skip_private`, `skip_all_day`, `teams_only`, `skip_tentative`, `min_minutes`, `max_minutes`), there are two ways to exclude by text:

| Key | Default | Notes |
|---|---|---|
| `skip_subject_contains` | `[]` | Case-insensitive substrings, e.g. `["OOO", "out of office", "PTO", "holiday"]`. The usual place to start. |
| `skip_organizer_contains` | `[]` | Same, against the organizer. Only populated on Path B or with `-IncludeOrganizer`. |
| `skip_location_contains` | `[]` | Same, against the location. |
| `skip_categories` | `[]` | Whole Outlook category names, compared case-insensitively. |
| `skip_subject_patterns` | `[]` | Regular expressions, for anchors and word boundaries. |

The `*_contains` lists match **anywhere in the text, including inside words**, so `"PTO"` also matches `OPTOMETRIST`. When that matters use a regex instead: `"skip_subject_patterns": ["(?i)\\bPTO\\b"]`. A bare string where a list belongs is rejected at load time, because `"OOO"` would otherwise be read as the three substrings `O`, `O`, `O` and skip nearly everything.

Filters are evaluated cheapest-first, so a meeting that is both declined and contains `OOO` reports `declined`. Run `.\meeting2jira preview` to see the exact reason for each skip; the message names the string that matched.

**`tour_of_duty`**: your scheduled working hours, in local wall-clock time. Off by default.

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | When off, nothing below applies and `{tod_status}` is `unknown`. |
| `days` | `["Mon",...,"Fri"]` | Working days. Any day not listed counts as entirely outside your tour. |
| `start` / `end` | `07:00` / `15:30` | 24-hour local time. If `end` is less than or equal to `start` the tour is treated as overnight (e.g. `22:00`–`06:00`), including the part that falls after midnight. |
| `grace_minutes` | `15` | Tolerance at both edges, so a meeting starting a few minutes early still counts as inside. |
| `outside_action` | `label` | What to do with a meeting **entirely** outside the tour: `include` (treat it normally), `label` (add `outside_label`), `route` (file it under `outside_parent`), `skip` (exclude it). |
| `outside_label` | `outside-tod` | Used by `label`. No spaces; Jira labels can't contain them. |
| `outside_parent` | `null` | Required by `route`. Typically a comp-time or overtime issue. |

How a meeting is classified:

- **`inside`** — entirely within the tour (after grace). Handled normally.
- **`partial`** — straddles an edge, e.g. a 15:00 meeting on a tour ending 15:30. **Treated as inside**, because work that ran past the end of your tour is still work and shouldn't be silently dropped. `{minutes_outside_tod}` tells you how much spilled over.
- **`outside`** — doesn't touch the tour at all, including anything on a non-working day. This is what `outside_action` acts on.

Two deliberate design choices worth knowing:

- **This does not control which days are scanned.** The scan window is `-DaysBack`, and widening it is free because the state database makes re-runs idempotent. So "will I miss a meeting that ran late?" is answered by the window, not by this setting.
- **`route` and `skip` are applied before `rules`.** Where time worked outside your tour gets recorded is a timekeeping decision, and a subject-matching rule shouldn't quietly redirect it to a project issue. If you'd rather rules won, use `include` or `label` instead.

Two things it doesn't know about:

- **Holidays.** A holiday falling on a listed working day still counts as inside the tour; there's no holiday calendar available offline. Review the dry run if it matters.
- **Which timezone you're in.** Comparisons use the machine's local time, which is the right answer when your laptop's clock matches your duty station. If you travel with it, meetings are classified against wherever the machine thinks it is.

**`notify`**: making a hidden failure visible. A scheduled task has no window, so `last_run.json`,
`status` and `doctor` all require you to go and look. This pushes a failure into view instead.

| Key | Default | Notes |
|---|---|---|
| `desktop_alert` | `true` | On failure, write `ATTENTION-meeting2jira.txt` to your Desktop (OneDrive-relocated Desktops are handled), falling back to the data directory. Deleted automatically by the next successful run. |
| `alert_after_failures` | `1` | Consecutive failed runs before alerting. `2` rides out a one-off network blip. Tracked as `consecutive_failures` in `last_run.json`. |
| `use_msg_exe` | `false` | Additionally try `msg.exe`. Absent on some Windows builds, so failure is silent. |

A failure *before* Python runs — a missing config, or an Outlook export that produced nothing — is
alerted by `Invoke-MeetingSync.ps1` on the first occurrence, since in that case not one meeting was
even attempted and there is no streak to weigh.

Why it is a file and not a notification: Windows toast needs WinRT type loading, which Constrained
Language Mode blocks, and the orchestrator has to stay CLM-safe. BurntToast needs the PowerShell
Gallery. `mshta.exe` would work with no dependencies but is a well-known living-off-the-land binary
that endpoint protection and AppLocker commonly block — using it would make this tool look like the
thing those controls exist to stop. A file on the Desktop is unglamorous and works everywhere.

**`rules`**: evaluated in order, and the first match wins. Every condition inside `match` must hold (AND).

```json
{ "name": "program syncs", "match": { "subject_regex": "(?i)IPT|program sync", "is_teams": true }, "parent": "PGM-42" }
{ "name": "from the front office", "match": { "organizer_regex": "(?i)director" }, "parent": "ADMIN-3" }
{ "name": "skip social", "match": { "category": ["Social", "Personal"] }, "skip": true }
```

The available `match` keys are `subject_regex`, `organizer_regex`, `location_regex`, `category` (a string or a list), and `is_teams`. The organizer is only populated on Path B, or on Path A with `-IncludeOrganizer`.

**`templates`**: these use Python `str.format` fields.

- Available fields: `{subject}`, `{start_local}`, `{end_local}`, `{start_utc}`, `{end_utc}`, `{minutes}`, `{hours}`, `{organizer}`, `{location}`, `{categories}`, `{response}`, `{source}`, `{parent}`, `{tod_status}`, `{minutes_outside_tod}`.
- Dates take `strftime` codes, e.g. `{start_local:%a %m/%d %H:%M}`.
- Windows doesn't support `%-d`-style codes.

**`csv.datetime_formats`**: `strptime` formats tried in order for the CSV's date and time columns. Add yours if your regional format differs.

## Export schema (v1)

This is the contract between any source and the Python side. `key` must be stable for the same occurrence; the COM exporter uses `GlobalAppointmentID|start_utc`.

```json
{
  "schema_version": 1,
  "source": "outlook-com",
  "exported_at": "2026-09-24T21:00:00Z",
  "range_start": "2026-09-24T04:00:00Z",
  "range_end": "2026-09-24T21:00:00Z",
  "meetings": [
    {
      "key": "040000008200E000...|2026-09-24T14:00:00Z",
      "subject": "Sprint Planning",
      "start_utc": "2026-09-24T14:00:00Z",
      "end_utc": "2026-09-24T15:00:00Z",
      "all_day": false, "is_meeting": true, "is_cancelled": false,
      "response": "accepted",            // organizer|accepted|tentative|declined|none|not_responded|unknown
      "busy_status": "busy",             // free|tentative|busy|oof|elsewhere|unknown
      "is_private": false,
      "location": "Microsoft Teams Meeting",
      "categories": [],
      "organizer": null,
      "is_teams": true
    }
  ]
}
```

(The `//` comments are for illustration only; real files are plain JSON.)

---

## Security notes

A summary to share with your ISSO:

- **Data sent to Jira**: subject, start/end, duration, location, categories, and optionally the organizer. The meeting body and attendee lists are **never** read on Path A.
- **Private/confidential meetings are skipped** by default (`skip_private`). Consider whether meeting subjects in your org can carry CUI before pushing them to Jira, and use `skip_subject_patterns` or rules as needed.
- **Credentials**: the Jira PAT is encrypted with Windows DPAPI, bound to your user account on this machine, in `%LOCALAPPDATA%\meeting2jira\jira_token.dpapi`. It is never logged. Give the PAT an expiry date. The `JIRA_PAT` environment variable overrides the file; it's meant for testing, so don't leave it set.
- **Transport**: HTTPS only, with certificate verification always on. There is intentionally no option to disable TLS verification.
- **Data at rest**: everything stays inside `%LOCALAPPDATA%\meeting2jira` in your own profile.
  - `state.db` keeps issue keys, meeting summaries, start times, and durations, because that is what makes re-runs safe.
  - JSON exports are deleted after a successful run (kept on failure for diagnosis, or with `-KeepExport`).
  - Logs contain meeting subjects: `meeting2jira.log` rotates at 1 MB with 3 backups (so ~4 MB at most, size-capped rather than time-limited), and the PowerShell `sync_*.log` transcripts are pruned after 30 days.
  - **Subjects of private items are withheld from the logs**, not just from Jira, so `skip_private` keeps them out of scope entirely.
  - The Outlook CSV you export by hand on Path B contains full meeting bodies. Delete it after pushing; nothing here does that for you.
- **Footprint**: Python standard library only, with no third-party packages. There's no admin requirement and no persistent service beyond the optional per-user scheduled task.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `running scripts is disabled` / `is not digitally signed` | Execution policy. Use `Unblock-File` for downloaded files under `RemoteSigned`. Under `AllSigned`, get the scripts signed. Otherwise use the Python CLI directly with Path B. |
| `Cannot create type. Only core types are supported in this language mode.` / exporter says COM is blocked | Constrained Language Mode. Use Path B. |
| `Could not start classic Outlook via COM` | "New Outlook" is in use (it has no COM) or classic Outlook isn't installed. Toggle "New Outlook" off, or use Path B. |
| Exporter finds 0 items but you had meetings | Unusual regional date settings can confuse Outlook's `Restrict` filter. Run with `-Verbose` to see the filter string. |
| Outlook prompts about e-mail address access | You used `-IncludeOrganizer` under a strict programmatic-access policy. Drop the switch. |
| `CERTIFICATE_VERIFY_FAILED` | Your agency CA isn't in the Windows store that Python sees. Export the chain as Base-64 PEM and set `jira.ca_bundle`. |
| `returned 'text/html' instead of JSON` | An SSO page or proxy is intercepting REST calls. Ask the Jira admins for the PAT-capable API URL. |
| TLS handshake failure | Jira may require a CAC/PIV client certificate. Python stdlib can't use a smartcard key, so ask the admins for an endpoint that accepts PATs. |
| HTTP 401 | The personal access token has expired or been revoked, or belongs to a different Jira. Create a new one and run `set-token` again. |
| HTTP 403 after earlier failures | Jira CAPTCHA lockout. Log in through the browser once. |
| HTTP 400 `Field '...' is required` | Your project requires custom fields. Add them to `jira.extra_fields`. |
| HTTP 400 about `issuetype` | Wrong `subtask_type`. `check` lists the valid names. |
| Can't reach Jira and `Test-Environment` shows a PAC file | Python ignores PAC scripts. Set `jira.proxy` explicitly. NTLM/Kerberos-authenticated proxies aren't supported by the stdlib, but on-prem Jira usually bypasses the proxy anyway. |
| `py` not recognized, or `python` opens the Microsoft Store | Pass `-Python C:\path\to\python.exe` to the PowerShell scripts. |
| `Could not decrypt ... jira_token.dpapi` | DPAPI data only opens for the same user on the same machine (and may break after a profile reset). Run `set-token` again. |

## Known limitations and roadmap

- **Edited past meetings**: only ended meetings are pushed, so this only happens when a past meeting is edited afterwards. On Path A, a changed time creates a second sub-task; on Path B, even a changed subject does. See HANDOFF.md P2-A.
- **Ambiguous create failures**: if creating an issue times out or gets a 502/504, it is reported rather than retried, because Jira may have created it anyway. Check Jira before re-running. See HANDOFF.md P1-A.
- **Teams detection** is a heuristic based on the Location field, because reading the body would trigger Outlook's guard. It's only used by `teams_only` and `is_teams` rules.
- **CSV path**: no response status; English column headers expected; the `Show time as` numbering should be verified against your own export.
- **Worklog retries**: a failed worklog is recorded as `worklog=no` in `status` but isn't retried yet.
- **Tempo**: if your org uses Tempo Timesheets on Data Center, native Jira worklogs normally appear there too. Confirm with your admins.
- **Phase 2: Microsoft Graph source**, for when COM isn't allowed or new Outlook becomes mandatory. Ask IT for:
  - An Entra app registration: public client, redirect URI `http://localhost`, delegated `Calendars.Read`, admin consent, and compatibility with your Conditional Access policies.
  - The right endpoints for your cloud:
    - Commercial/GCC: `graph.microsoft.com` and `login.microsoftonline.com`
    - GCC High: `graph.microsoft.us` and `login.microsoftonline.us`
    - DoD: `dod-graph.microsoft.us` and `login.microsoftonline.us`

  The source would be a small stdlib module doing auth-code + PKCE with a loopback listener, calling `/me/calendarView` and writing the v1 JSON. Nothing downstream changes.

## Repo layout

See [ARCHITECTURE.md](ARCHITECTURE.md) for flowcharts of how these files interact and what happens
to a single meeting as it moves through the pipeline.

**`app/` is the whole program.** Copy that one folder to a machine and it runs; nothing in it
reaches outside itself. Everything beside it is development support you don't need at runtime.

```
app/                             ← copy this folder to install
  meeting2jira.cmd               entry point on Windows: `.\meeting2jira [command]`
  m2j                            entry point for development off Windows (no Outlook, no DPAPI)
  config.example.json            template copied by `init`
  meeting2jira/
    __main__.py                  CLI: init, set-token, check, push, status, forget
    config.py                    defaults, validation, parse_hhmm
    models.py                    Meeting record, UTC helpers, dedupe identity
    sources.py                   JSON export + Outlook CSV readers
    rules.py                     filters, tour of duty, parent routing
    sync.py                      the push loop, templates, ambiguous-create recovery
    jira.py                      stdlib Jira Data Center REST v2 client
    state.py                     sqlite dedupe state
    credstore.py                 DPAPI token storage (ctypes)
    windows/                     the Windows-native layer
      Test-Environment.ps1         read-only preflight (CLM-safe)
      Export-OutlookMeetings.ps1   Outlook COM → v1 JSON (needs FullLanguage)
      Invoke-MeetingSync.ps1       export + push orchestrator (CLM-safe)
      Register-MeetingSyncTask.ps1 weekday scheduled task for your user
  tests/                         offline unit tests, fixtures, and guardrail tests
  tools/
    Test-PowerShellSyntax.ps1    parse check + PowerShell 7-only syntax detector
    Invoke-WindowsChecks.ps1     the Windows-only checks (5.1 parsing, DPAPI, JSON handoff, CSV push)

ARCHITECTURE.md                  file-interaction and per-meeting flow diagrams
INSTALL.md                       step-by-step setup walkthrough
HANDOFF.md                       status, risks, backlog, and prompts for continued development (Kiro)
.kiro/                          Kiro steering (project rules), subagents and hooks; see KIRO_SETUP.md
tools/                          Kiro dev tooling (compact test runner, hook scripts); never shipped
infra/windows-test-vm/           Terraform for a throwaway Windows host to run those checks on
power-platform/                  Power Automate / Power BI alternatives: feasibility, blockers, blueprints
```

The tests ship inside `app/` on purpose: running them on the target machine is the quickest proof
that the install is sound, and they need nothing but the standard library.

Runtime data never lands in `app/` — config, token, state and logs all live in
`%LOCALAPPDATA%\meeting2jira`, so you can replace the folder wholesale to upgrade.

## Tests

```powershell
.\meeting2jira selftest                                    # both of the below
py -3 -m unittest discover -s tests -v
powershell.exe -NoProfile -File tools\Test-PowerShellSyntax.ps1
powershell.exe -NoProfile -File tools\Invoke-WindowsChecks.ps1   # Windows-only behavior
```

`Invoke-WindowsChecks.ps1` covers what a macOS or Linux checkout cannot: real 5.1 parsing, the DPAPI round trip, the PowerShell-to-Python export handoff, the CSV push path, and the entry point. It does not touch Outlook or Jira, so it is safe to run on the real workstation. `infra/windows-test-vm/` exists to run it without one; see [its README](infra/windows-test-vm/README.md).

`tests/test_guardrails.py` enforces the project's non-negotiables:
- stdlib-only imports
- TLS verification never disabled
- no execution-policy bypass
- no guarded Outlook properties
- CLM-safe helper scripts


The tests are offline. The Jira client tests use a throwaway local HTTP server on 127.0.0.1. The fixtures cover recurring instances, declined, private, all-day, cancelled, future, and duplicate meetings, plus a CSV with an embedded multi-line description and a malformed row.
