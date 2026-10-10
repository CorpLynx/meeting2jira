# Odin

**Odin is an Asgard app now** (Oct 10, 2026): its code is [`Asgard/apps/odin`](../Asgard/apps/odin), it installs with Asgard, opens from the Odin tile, and keeps its records in Muninn, Asgard's database. How it works and what protects Jira: [Asgard/docs/integration/odin.md](../Asgard/docs/integration/odin.md). Installing and using it: [INSTALL.md](INSTALL.md) and Asgard's [README](../Asgard/README.md#odin-meetings-and-jira).

Odin turns the meetings on your Outlook/Teams calendar into Jira sub-tasks and logs their time, reads your Jira issues and worklogs into Muninn for Asgard's other apps, and posts the days you approve in Baldur. It is built for a locked-down federal Windows 11 workstation: no local admin, no Entra app registration, no PowerShell Gallery modules, and nothing to pip install for the daily run.

```
Outlook calendar ──(PowerShell: COM export, or a CSV you export)──► JSON/CSV file
      ──(Python: filter → route → dedupe in Muninn → create)──► Jira sub-tasks (+ worklogs)
Jira ──(issues, worklogs)──► Muninn ──(approved Baldur days)──► Jira worklogs
```

This folder keeps what isn't part of Asgard:

| | |
| --- | --- |
| `graph-app/` | Path D: the calendar from Microsoft Graph (`msal`), for when COM isn't allowed. Hands its export to Asgard's `odin.cmd` |
| `playwright-app/` | Path C, contingency only: the calendar from OWA through a browser. Hands off the same way |
| `power-platform/` | Power Automate desktop and Power BI material (`power-bi/report.ps1` wraps `odin report`) |
| `README.md`, `INSTALL.md`, `ARCHITECTURE.md`, `MODULES.md`, `HANDOFF.md` | This page and its companions; `HANDOFF.md` is the history of Odin before it moved |

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
| **Microsoft Graph** `/me/calendarView`, via `msal` | An Entra app registration with delegated calendar consent, and pip for `msal`. National-cloud endpoints are required for GCC High/DoD. | **The intended primary path (Path D).** It is the only source that works on classic *and* new Outlook, needs no browser automation, and raises no terms-of-service question. Blocked on an IT ticket, not on code. Planned for `graph-app/`; see Roadmap and HANDOFF.md P2-C. |
| **Outlook object model (COM)** from PowerShell | Classic Outlook, and PowerShell in FullLanguage mode | **Path A (primary).** It reuses your already signed-in Outlook, so there's no auth, no network calls of its own, and no admin. Recurring meetings are expanded for you. |
| **Outlook CSV export** (Import/Export wizard) | Nothing beyond classic Outlook | **Path B (fallback).** A manual step, but it works even under Constrained Language Mode. The export also expands recurrences. |
| **OWA's own calendar API**, read through a browser Playwright drives | pip (`playwright`), plus installed Edge | **Path C — contingency only, and dormant. Not part of the installed deliverable.** It lives in `playwright-app/`, which is never copied to the workstation, so Odin's daily run stays standard library. It exists for one scenario: forced onto new Outlook (which removes COM *and* the Import/Export wizard, breaking A and B at once) *and* Graph not yet approved. It reads undocumented internal APIs, so prefer Path D wherever it is available. See `playwright-app/README.md` for its status and removal condition. |
| Exchange Web Services (EWS) | — | **No.** Microsoft is disabling EWS in Exchange Online starting Oct 1, 2026. |
| Published ICS calendar URL | Anonymous calendar publishing | **No.** Usually disabled in federal tenants, and it would require an RRULE parser. |
| pywin32 / requests / keyring / jira | pip access | **Not needed.** Packages are allowed when declared and pinned, but Odin's daily run still uses only the standard library, because Asgard's Python install may carry no packages. The `jira` package was reviewed on Oct 10, 2026 and not used: its session retries POSTs on a 503 or a dropped connection, which could create a sub-task twice ([dependency policy](../Asgard/docs/dependency-policy.md#considered-for-odin-and-not-used-oct-10-2026)). [MODULES.md](MODULES.md) lists every package the exporters use with its alternatives. |
| `msal` | pip access | **Accepted for Path D only**, and in a separate folder — never in Odin's daily run. Hand-rolling OAuth PKCE, a loopback listener and token refresh is the part most likely to be subtly wrong, and `msal` also unlocks brokered sign-in (below). Plain `msal` is pure Python; the `msal[broker]` extra pulls the native `pymsalruntime`, which is a larger approval surface and a deliberate, separate decision. |

### Why PowerShell *and* Python

- **PowerShell does the Windows-native parts.** It talks to Outlook over COM (Python would need pywin32 for that), registers the scheduled task, and runs environment checks. It is deliberately thin: it dumps what's on the calendar and makes no decisions.
- **Python does the logic.** That covers filtering, routing rules, templates, the record of sub-tasks (in Muninn), Jira HTTP, and credential storage (DPAPI via ctypes). This code is testable offline and easy to iterate on.
  - Python's `urllib` on Windows trusts the **Windows certificate store**, so an agency root CA that Windows trusts just works. `requests` ships its own CA bundle and usually breaks under TLS inspection.
- **They meet at a versioned JSON file** (see [Export schema](#export-schema-v1)). Anything that can write that file can be a source, which is where a Graph source would plug in later.
- `Invoke-MeetingSync.ps1` and `Test-Environment.ps1` stick to cmdlets and core types, so they run under Constrained Language Mode. Only `Export-OutlookMeetings.ps1` needs FullLanguage.

### How a meeting becomes a sub-task

1. **Filters** are hard exclusions: cancelled, all-day, not ended yet, no attendees, declined, private, shown as free, too short or too long, or a subject pattern match.
2. **Rules** are evaluated in order, and the first match wins. A match either routes the meeting to a specific parent issue or skips it.
3. Anything left goes to `jira.default_parent`.
4. **Dedupe**: the meeting's key and a content hash (subject + start + end) are checked against Muninn's `meeting_subtasks`. The content hash means a COM run and a CSV run won't duplicate each other.
5. The sub-task is created and recorded right away. Then its time is logged (through a marker-protected worklog, so it is never logged twice) and the optional transition happens.

Safety rails:

- Meetings are only pushed after they've ended (`only_ended`).
- Each run creates at most `max_creates_per_run` sub-tasks.
- `--dry-run` shows the full plan and changes nothing.

---

## Setup

[INSTALL.md](INSTALL.md) has the steps. In short: clear it with your ISSO/supervisor first (this moves meeting metadata from your mailbox into Jira; [Security notes](#security-notes) is a summary to hand them), install Asgard, open the Odin tile, create the settings, paste a Jira personal access token, check the setup, preview, then run it or schedule it.

## Configuration reference

The config file is `%LOCALAPPDATA%\Asgard\odin\config.json`. It's JSON, and any key starting with `_` is treated as a comment. Anything you omit falls back to the defaults in `Asgard/apps/odin/odin/config.py`.

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

**`muninn`**: what Odin does with Muninn beyond the meeting push, which always uses it.

| Key | Default | Notes |
|---|---|---|
| `sync_issues` | `true` | Your issues, the parents your meetings go under and their children, and keys Asgard's other apps mention, read into Muninn. |
| `sync_worklogs` | `true` | Your worklogs, read into Muninn, so Baldur sees what Jira already holds. |
| `post_approved` | `true` | Post the days you approved in Baldur that Jira is missing. Only approved minutes, only after that run's worklog sync, at most `max_posts_per_run`. `odin post` posts whatever this says. |
| `max_posts_per_run` | `20` | |
| `history_days` | `365` | How far back the first sync reaches. |
| `max_issues_per_run` | `500` | Per stream. A bigger first sync carries on next run. |

**`filters`**: see `config.example.json` for the full set. Beyond the on/off switches (`skip_declined`, `skip_private`, `skip_all_day`, `teams_only`, `skip_tentative`, `min_minutes`, `max_minutes`), there are two ways to exclude by text:

| Key | Default | Notes |
|---|---|---|
| `skip_subject_contains` | `[]` | Case-insensitive substrings, e.g. `["OOO", "out of office", "PTO", "holiday"]`. The usual place to start. |
| `skip_organizer_contains` | `[]` | Same, against the organizer. Only populated on Path B or with `-IncludeOrganizer`. |
| `skip_location_contains` | `[]` | Same, against the location. |
| `skip_categories` | `[]` | Whole Outlook category names, compared case-insensitively. |
| `skip_subject_patterns` | `[]` | Regular expressions, for anchors and word boundaries. |

The `*_contains` lists match **anywhere in the text, including inside words**, so `"PTO"` also matches `OPTOMETRIST`. When that matters use a regex instead: `"skip_subject_patterns": ["(?i)\\bPTO\\b"]`. A bare string where a list belongs is rejected at load time, because `"OOO"` would otherwise be read as the three substrings `O`, `O`, `O` and skip nearly everything.

Filters are evaluated cheapest-first, so a meeting that is both declined and contains `OOO` reports `declined`. Run `odin preview` to see the exact reason for each skip; the message names the string that matched.

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

- **This does not control which days are scanned.** The scan window is `-DaysBack`, and widening it is free because Muninn's record of sub-tasks makes re-runs idempotent. So "will I miss a meeting that ran late?" is answered by the window, not by this setting.
- **`route` and `skip` are applied before `rules`.** Where time worked outside your tour gets recorded is a timekeeping decision, and a subject-matching rule shouldn't quietly redirect it to a project issue. If you'd rather rules won, use `include` or `label` instead.

Two things it doesn't know about:

- **Holidays.** A holiday falling on a listed working day still counts as inside the tour; there's no holiday calendar available offline. Review the dry run if it matters.
- **Which timezone you're in.** Comparisons use the machine's local time, which is the right answer when your laptop's clock matches your duty station. If you travel with it, meetings are classified against wherever the machine thinks it is.

**`notify`**: making a hidden failure visible. A scheduled task has no window, so `last_run.json`,
`status` and `doctor` all require you to go and look. This pushes a failure into view instead.

| Key | Default | Notes |
|---|---|---|
| `desktop_alert` | `true` | On failure, write `ATTENTION-Odin.txt` to your Desktop (OneDrive-relocated Desktops are handled), falling back to the data directory. Deleted automatically by the next successful run. |
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
- **Credentials**: the Jira PAT is encrypted with Windows DPAPI, bound to your user account on this machine, in `%LOCALAPPDATA%\Asgard\odin\jira_token.dpapi`. It is never logged. Give the PAT an expiry date. The `JIRA_PAT` environment variable overrides the file; it's meant for testing, so don't leave it set.
- **Transport**: HTTPS only, with certificate verification always on. There is intentionally no option to disable TLS verification.
- **Data at rest**: everything stays inside `%LOCALAPPDATA%\Asgard\odin` in your own profile, beside Asgard's files (`ASGARD_HOME\odin` when that's set). Odin used `%LOCALAPPDATA%\meeting2jira` before Oct 2026; the first run of any command moves it here whole (INSTALL.md).
  - Muninn (`%LOCALAPPDATA%\Asgard\muninn.db`) keeps issue keys, meeting sub-task summaries, start times and durations (that is what makes re-runs safe), your calendar's times and titles (a private item's title is withheld), and your Jira issues' and worklogs' metadata.
  - JSON exports are deleted after a successful run (kept on failure for diagnosis, or with `-KeepExport`).
  - Logs contain meeting subjects: `odin.log` rotates at 1 MB with 3 backups (so ~4 MB at most, size-capped rather than time-limited), and the PowerShell `sync_*.log` transcripts are pruned after 30 days.
  - **Subjects of private items are withheld from the logs**, not just from Jira, so `skip_private` keeps them out of scope entirely.
  - The Outlook CSV you export by hand on Path B contains full meeting bodies. Delete it after pushing; nothing here does that for you.
- **Footprint**: Python standard library only for the daily run; Odin's window uses PySide6, like every Asgard window. Packages are pinned in `Asgard/requirements.txt` and listed in Asgard's `MODULES.md`. There's no admin requirement and no persistent service beyond the optional per-user scheduled task.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `running scripts is disabled` / `is not digitally signed` | Execution policy. Use `Unblock-File` for downloaded files under `RemoteSigned`. Under `AllSigned`, get the scripts signed. Otherwise use the Python CLI directly with Path B. |
| `Cannot create type. Only core types are supported in this language mode.` / exporter says COM is blocked | Constrained Language Mode. Use Path B. |
| `Could not start classic Outlook via COM` | "New Outlook" is in use (it has no COM) or classic Outlook isn't installed. Toggle "New Outlook" off and use Path A or B. If you can't toggle it back, note that new Outlook also removed the Import/Export wizard, so Path B is gone too — use `playwright-app/` (Path C). |
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
- **Ambiguous create failures**: if creating an issue times out or gets a 502/504, Odin searches for the sub-task's `m2j-<hash>` label: exactly one match is recorded, anything else is reported for you to check in Jira.
- **Teams detection** is a heuristic based on the Location field, because reading the body would trigger Outlook's guard. It's only used by `teams_only` and `is_teams` rules.
- **CSV path**: no response status; English column headers expected; the `Show time as` numbering should be verified against your own export.
- **Worklog retries**: a meeting worklog Jira refused is retried by the next runs for 14 days, at most three times; one whose answer was lost is checked against Jira by its marker first.
- **Tempo**: if your org uses Tempo Timesheets on Data Center, native Jira worklogs normally appear there too. Confirm with your admins.
- **Phase 2 (Path D): Microsoft Graph source** — the intended primary path, for when COM isn't allowed or new Outlook becomes mandatory. It is the only source that covers classic *and* new Outlook with no browser automation. **Blocked on an IT ticket, not on code.** Ask IT for:
  - An Entra app registration: public client (no secret), redirect URI `http://localhost`, delegated calendar consent, admin consent, and compatibility with your Conditional Access policies.
  - Prefer **`Calendars.ReadBasic`** over `Calendars.Read` if it fits: it is meant to exclude meeting bodies and attendee lists, matching the data minimization this tool already practises on Path A. Confirm the exact exclusions in current Graph docs before quoting it.
  - Whether an **existing internal app** already has a calendar scope and can add your account. That may be far quicker than a new registration.
  - The right endpoints for your cloud:
    - Commercial/GCC: `graph.microsoft.com` and `login.microsoftonline.com`
    - GCC High: `graph.microsoft.us` and `login.microsoftonline.us`
    - DoD: `dod-graph.microsoft.us` and `login.microsoftonline.us`

  It will live in `graph-app/` and use `msal` — it's a separate folder so the daily run never depends on it. `msal` replaces a hand-rolled PKCE flow, loopback listener and token-refresh logic, and it also offers brokered Windows sign-in (silent, and the device satisfies MFA/device-compliance Conditional Access), which matters for an unattended scheduled task. Note that the broker extra pulls the native `pymsalruntime`, a bigger approval surface than pure-Python `msal`: decide that one with IT. The token cache is DPAPI-protected via the existing `credstore.py`. Nothing downstream changes, and switching sources cannot duplicate sub-tasks because `content_hash` is source-independent. See HANDOFF.md P2-C.

## Where things are

| | |
| --- | --- |
| Odin's code | `Asgard/apps/odin/` (package `odin`; `odin.cmd`, `cli.py`, `odin.pyw`, `ui/`, `windows/`, `tools/`) |
| Its tests | `Asgard/tests/test_odin_*.py`, with `Asgard/tests/fake_jira.py`; the exporters' tests are in their own folders |
| Its contract with Muninn | [Asgard/docs/integration/odin.md](../Asgard/docs/integration/odin.md) |
| Its files | `%LOCALAPPDATA%\Asgard\odin\` (config, token, logs, exports, `last_run.json`); its records are in `%LOCALAPPDATA%\Asgard\muninn.db` |
| How the pieces fit | [ARCHITECTURE.md](ARCHITECTURE.md) |
