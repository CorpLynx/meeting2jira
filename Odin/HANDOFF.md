# Odin: handoff for continued development (Kiro)

> **History, kept for the record (Oct 10, 2026).** This is the handoff for Odin as a standalone program (`Odin/app`, with its own `state.db`). Odin is now an Asgard app: its code is `Asgard/apps/odin` (package `odin`), its records are in Muninn, and its rules are Asgard's `AGENTS.md`. Paths, commands and the backlog below describe the old layout. What is current: [Asgard/docs/integration/odin.md](../Asgard/docs/integration/odin.md) (the contract and what protects Jira), Asgard's [HANDOFF.md](../Asgard/HANDOFF.md) (where things stand), [INSTALL.md](INSTALL.md) and [README.md](README.md). The backlog's open items still apply and are listed in Asgard's HANDOFF ("What's next", item 4): P0 the target-machine checks, P2-A edited past meetings, P2-B CSV hardening, P2-C the Graph source. P3's `report` is done (`odin report`, from Muninn).

This document hands the Odin proof of concept to an AI coding agent (Kiro) and to the person working with it. It covers:

- where the code stands
- what has and hasn't been proven
- where the risks are
- the rules that must not be broken
- a prioritized backlog with acceptance criteria

Read it together with `README.md` (user-facing design, setup, and config) and the steering files in `.kiro/steering/`, which Kiro loads automatically.

---

## 1. How to use this (for the human)

1. **Open the repo folder in Kiro.**
   - The `.kiro/steering/` files load automatically: `product.md`, `tech.md`, `structure.md` and `workflow.md` in every session; `powershell.md` and `python-testing.md` when editing those files; `debug-playbook.md` when the agent is stuck; `handoff.md` on `/handoff`.
   - Check the *Agent Steering* panel to confirm they're listed.
   - First time only: follow `KIRO_SETUP.md` (enable `.kiroignore`, confirm the subagent model IDs, check the hooks load).
2. **Start with the orientation prompt** in [section 8](#8-starter-prompts). It makes the agent read everything and run the checks before touching code.
3. **Use a Kiro spec for each backlog item** in [section 6](#6-backlog). The backlog entries are written so they can be pasted into a spec request as-is.
4. **You are the only one who can run the Windows target-machine checks** in [section 5](#5-target-machine-validation-checklist-human). Paste the results back into Kiro; that's what drives the P0 fixes.

**If Kiro isn't running on the federal workstation itself**, it cannot verify anything involving Outlook, DPAPI, Task Scheduler, or Windows PowerShell 5.1 behavior. The steering tells it to label such work "needs target-machine verification" instead of claiming it's done. Hold it to that.

---

## 2. What this is, in one paragraph

This is a personal tool that reads the user's own Outlook/Teams calendar and creates one Jira sub-task per attended, already-ended meeting, with an optional worklog and transition. It has to run on a locked-down federal Windows 11 machine: no admin, no app registration, no PowerShell Gallery, no pip, TLS inspection, and possibly Constrained Language Mode. That's why it's split this way:

- **Thin PowerShell** reads classic Outlook over COM, or the user supplies an Outlook CSV export.
- **Standard-library Python** does all the logic: filters, routing rules, dedupe, and the Jira REST calls.
- **The two meet at a versioned JSON contract.**

The README's "Design notes" section explains why each alternative (Graph, EWS, ICS, third-party packages) was rejected or deferred.

---

## 3. Status snapshot (v0.1.0)

| Area | State | Evidence |
|---|---|---|
| Python pipeline (filters, rules, templates, dedupe, cap, dry-run) | **Working** | `tests/test_pipeline.py` with a 12-item fixture (recurring instances, declined, private, all-day, cancelled, future, duplicate) |
| Outlook CSV parsing | **Working** on sample data | CSV fixture with a multi-line description and a malformed row |
| Jira client (Data Center PAT, retries, error hints, worklog format, transitions) | **Working** against a mock | `tests/test_jira_client.py` (local `http.server`) |
| End-to-end CLI (`init`, `check`, `push`, re-run dedupe, `status`) | **Working** against a mock Jira | Manual run; 4 created, then 4 `EXISTS` on re-run |
| Non-negotiables | **Enforced** | `tests/test_guardrails.py`, mutation-tested (each injected violation caught) |
| PowerShell syntax / 5.1 compatibility | **Parses; no PS7-only syntax** | `tools/Test-PowerShellSyntax.ps1` under pwsh 7.6, mutation-tested |
| PS → Python JSON handoff | **Verified in pwsh 7** for 0, 1, and 2 items; 5.1 array quirk guarded on both sides | Manual; `unwrap_ps_array` has a test |
| `Invoke-MeetingSync.ps1` CSV path | **Ran** under pwsh 7 on Linux | Manual |
| Outlook COM export | **VERIFIED on the target workstation**: ran against a real Outlook profile and produced sub-tasks in Jira Data Center. This was the largest unverified risk in the project. | User-reported, on-prem |
| DPAPI token storage (ctypes) | **Verified**: round trip on Windows Server 2022, PowerShell 5.1.20348.5622 | `tools/Invoke-WindowsChecks.ps1` on the AWS lab VM (`infra/windows-test-vm/`); ciphertext confirmed to not contain the plaintext token |
| Windows PowerShell 5.1 runtime behavior | **Partially verified**: real parsing, CLM enforcement, and the CLI push path confirmed under actual 5.1.20348.5622 (not just parse-only) | `tools/Invoke-WindowsChecks.ps1`; the array-wrapping quirk (risk #4) did **not** reproduce on this build - `unwrap_ps_array` still guards it, but the quirk itself may be build/hotfix-specific rather than universal to 5.1 |
| Scheduled task registration and run | **Partially verified**: the `-At` derivation from `tour_of_duty.end` runs correctly under 5.1 (15:30 + 30m = 16:00). The `Register-ScheduledTask` call itself is still unverified: the task uses `LogonType Interactive`, and under SSM the caller is SYSTEM, whose `USERDOMAIN\USERNAME` is the machine account with no interactive SID ("No mapping between account names and security IDs"). Needs a workstation or an RDP session. | check 8 in `tools/Invoke-WindowsChecks.ps1` |
| `playwright-app/` mapping and pipeline parity (Path C — **dormant contingency, not installed**) | **Working offline**: OWA JSON → schema v1 → unmodified `push --dry-run`. An OWA export and a COM export of the same meeting produce different `key`s but an **identical `content_hash`**, so `state.find()` dedupes across the two sources. **Field lookup is case-insensitive** — found by research, not by a failing run: Graph returns camelCase but the Outlook endpoint returns PascalCase ([Microsoft's own comparison](https://learn.microsoft.com/en-us/outlook/rest/compare-graph)), and an independent project reading the same calendar (`yusufaltunbicak/outlook-cli`) reads `Subject` / `Start.DateTime` / `Sensitivity` / `ResponseStatus.Response`. Because this exporter discovers its endpoint rather than choosing it, it can be handed either; camelCase-only would have skipped every event on such a tenant, or worse, mapped the subject and time while dropping the private/declined flags. | `playwright-app/tests/` — 66 tests (28 mapping, 5 parity, 7 retention, 26 browser) |
| `playwright-app/` browser layer (Path C — **dormant**) | **Verified against a realistic fake, never against real OWA.** `tests/fake_owa.py` serves assets, latency, injectable failures, a self-referential `nextLink`, a relocatable calendar endpoint, and three layered decoys that each defeat one discovery check on its own. Building it found **seven** real defects in `capture.py`: discovery locking onto the telemetry beacon; the wait loop charging a full slice per wakeup and abandoning slow discovery; a leaked discovery page; `debug_endpoints` referencing a deleted constant; an unbounded paging loop; the expired-session diagnosis degrading to the generic message because it questioned an already-closed page; and first-weak-wins candidate selection, which would have exported Graph's `insights/events` list the day the calendar endpoint was renamed. Two tests were found to be passing vacuously and were tightened. **Nine mutations are each caught by at least one test.** Warm end-to-end cost ~505ms. **The endpoint OWA actually uses is still an assumption** — run `odin-owa discover` on the target machine, then `tools/sanitize_capture.py` to replace the invented fixture. | `playwright-app/tests/test_capture.py` (26, skipped without Playwright) |
| Test-Environment.ps1 on Windows | **Verified**: executes end to end under 5.1, including the `last_run.json` health block against planted recent/failed/stale files. The Outlook-registry and language-mode branches still report whatever the lab host happens to be, so read them on the real machine. | check 7 in `tools/Invoke-WindowsChecks.ps1` |

Python compatibility: `vermin` reports a minimum of 3.7. The project promises 3.8+.

**Windows verification lab:** `infra/windows-test-vm/` is a Terraform stack for a throwaway Windows Server 2022 EC2 instance, reached only through SSM Session Manager (no inbound rules, no key pair, no RDP on the internet). It reuses the account's existing VPC/subnet rather than creating a dedicated one. `tools/Invoke-WindowsChecks.ps1` runs on it and proves the checks a macOS/Linux checkout cannot: real 5.1 parsing (not just AST inspection), the DPAPI round trip, the PowerShell-to-Python export handoff for 0/1/2 items, and the CSV push path end to end. It still cannot touch Outlook COM (no Office on the base AMI) or Task Scheduler (SSM runs as SYSTEM, and the task needs an interactive logon) - see the updated rows above. `infra/windows-test-vm/run-checks.sh` drives it: sync the repo to S3, run the script via `AWS-RunPowerShellScript`, print the result. `terraform destroy` tears it down; nothing there should be left running.

**Fixed during handoff:** `JiraClient.request()` used to retry 502/503/504 for every method, including the POST that creates issues. A proxy timeout after Jira had already created the issue would have produced a duplicate. Non-GET calls now retry only on 429. Tests: `test_post_is_not_retried_on_ambiguous_5xx` and `test_post_is_retried_on_429`.

### Shipped since v0.1.0

| Item | What changed | Tests |
|---|---|---|
| **P1-A** ambiguous create recovery | `JiraError.ambiguous` marks failures where the write may have landed (non-GET 502/503/504, timeouts, resets). Every sub-task carries a deterministic `m2j-<content_hash[:10]>` label (`jira.dedupe_label`, default on), so recovery is an exact JQL lookup rather than summary matching. One match is recorded; zero, several, or a failed search are reported and left alone. | `tests/test_recovery.py` (9), plus ambiguity classification and `search_issue_keys` in `test_jira_client.py` |
| **P1-C** worklog retry | Additive migration adds `worklog_id`, `worklog_comment`, `worklog_attempts`, `worklog_wanted`. The rendered comment is stored at create time so a later run can retry without the `Meeting`. Retries happen at the start of `push`, bounded at `MAX_WORKLOG_ATTEMPTS = 3`, and never double-log. `add_worklog` now returns the worklog id. | `tests/test_state.py` (6), including migrating a real v0.1.0 database without losing rows |
| **P1-B** (partial) run visibility | `last_run.json` + `status` + `Test-Environment.ps1` health line; transcript pruning. See P1-B below for what's left. | `LastRunTests` in `test_pipeline.py` |
| Modular text exclusions | `filters.skip_subject_contains` / `skip_organizer_contains` / `skip_location_contains` / `skip_categories`: case-insensitive substring lists, the knobs a user actually edits (OOO, PTO, holiday). A bare string where a list belongs is rejected, since `"OOO"` would otherwise iterate as three one-character needles. | `test_text_exclusion_filters`, `test_text_exclusion_filters_reject_bad_config` |
| Entry point | `odin.cmd` so `.\odin` does the daily run, plus `setup`, `preview`, `doctor`, `schedule`, `csv`, `selftest`, and passthroughs. `m2j` is the off-Windows equivalent. No execution-policy bypass anywhere. | Check #6 in `tools/Invoke-WindowsChecks.ps1`, since batch has no parse-only mode |
| PowerShell resilience | **Real bug fixed:** `Invoke-MeetingSync.ps1` ran `python.exe` under `$ErrorActionPreference = 'Stop'`, so a single stderr line from Python became a terminating error that discarded Python's exit code and skipped the export cleanup. Also validates the export path before use, and tolerates a transcript already running. | Exercised by the lab run; `Test-Environment.ps1` was not affected (it never sets `Stop`) |
| **Data folder under Asgard** (Oct 10) | Odin's files (config, DPAPI token, `state.db`, logs, exports, Graph cache, OWA profile) live in `%LOCALAPPDATA%\Asgard\odin` (`ASGARD_HOME\odin` when set), beside Asgard's. Every entry point (the Python apps, the three `.cmd` launchers, the sync and export scripts) moves the old `%LOCALAPPDATA%\meeting2jira` there whole, once. Odin refuses to run while the old `state.db` is outside the new folder, since a run without it would re-create every synced meeting. **Needs target-machine verification:** the real move on the workstation. | `tests/test_data_dir.py` (8); `.github/workflows/odin-checks.yml` runs each `.cmd` launcher's move on Windows and parses every script under Windows PowerShell 5.1 |
| Log handle leak | `_setup_logging` closed nothing before `handlers.clear()`, leaking a file handle per call. | Suite is clean under `-W error::ResourceWarning` |
| **Worklog backfill, caught in review** | Every row was recorded with `worklog_logged = 0` whatever `log_work` was set to, and `pending_worklogs()` selected on that flag alone. So the first run after enabling `log_work` would have retroactively logged time against **every sub-task ever created**. Fixed with an explicit `worklog_wanted` column, set from `log_work` at create time rather than inferred. v0.1.0 rows migrate to `0`, the conservative reading. | `test_enabling_log_work_does_not_backfill_old_subtasks` |
| Cap ignored recoveries | `max_creates_per_run` counted created and planned but not recovered, so a run could exceed it. | Covered by the cap test |
| Config validation gaps | Labels containing spaces, unparseable `max_creates_per_run` / `timeout_seconds` / `min_minutes` / `max_minutes`, a blank `subtask_type`, and non-string templates all passed validation and failed later as a Jira 400, an `int()` error, or (for templates) an uncaught `AttributeError` traceback. All now rejected at load time with a message saying what to change. | `test_misconfiguration_is_caught_at_load_time`, `test_numeric_strings_are_still_accepted` |
| State DB failures | `sqlite3.Error` was not handled, so a locked database (a manual run overlapping the scheduled one) or a corrupt one produced a traceback. Both now exit 2 and explain what to do. `State` takes a lock `timeout`. | `StateFailureTests` |
| **Jira Data Center only** | Cloud support removed at the user's request: no `jira.auth`, no `jira.email`, no basic auth, no `accountId` assignee. A leftover `auth: "basic"` or `email` is now a config error rather than a silently ignored key, and `check` fails if `base_url` points at a Cloud site. | `test_authorization_is_always_a_bearer_token`, plus cases in `test_misconfiguration_is_caught_at_load_time` |
| **Tour of duty** | New `tour_of_duty` section classifies each meeting as `inside` / `partial` / `outside` against local working hours, and applies `include` / `label` / `route` / `skip` to wholly-outside ones. Overnight tours and the previous day's window are handled. `Register-MeetingSyncTask.ps1` derives its run time from the end of the tour. | `tests/test_tour_of_duty.py` (15) |
| CLM guardrail widened | The banned-pattern regex only caught `[System.X]::`, so a bare `[math]::Floor` would have passed review and then failed under Constrained Language Mode on the target machine. It now catches any `[Type]::Member` and `New-Object`, while ignoring provider paths like `Registry::`. Mutation-tested. | `test_clm_safe_scripts` |

| **Python discovery was too brittle for a real install** | Found on the target workstation: discovery checked whether a candidate *existed* and never proved it ran. `where py.exe` succeeding returned `py -3` blindly, but the launcher is routinely installed with no 3.x registered. Only PATH was searched, so an SCCM install under `C:\Program Files\Python312` was invisible. There was no version check, and the batch version set `PYCMD` unquoted so a path containing a space broke. Now candidates come from PATH, the registry (`PythonCore\*\InstallPath`, incl. WOW6432Node) and the usual directories, and **each is executed and must report 3.8+**. Every rejected candidate and its reason is reported. | `test_resolve_python_copies_are_identical`; needs a re-run of `doctor` on the workstation |
| **Stale-run age was computed across mismatched time frames** | `Test-Environment.ps1` did `(Get-Date).ToUniversalTime() - [datetime]$when`. PowerShell casts an ISO-8601 `Z` string to a **local** DateTime, so the age was overstated by the UTC offset (5–8 hours here) and a run 3.7 days old could warn falsely. Now compares local to local. Found by reasoning, confirmed on the lab. | check 7 asserts no false warning for a run that just happened, and that a 9-day-old run *is* flagged |

62 unit tests, all passing, and **8/8 checks green on the Windows lab VM** under PowerShell
5.1.20348.5622 (Windows Server 2022, Python 3.12.10). Outlook COM remains the one significant
unverified area — see section 5.

---

## 4. Risk register: where to look first

Ordered roughly by likelihood of breaking on first contact with the real machine.

1. **`Export-OutlookMeetings.ps1`, the `Restrict` filter.** Outlook's Jet date filter is locale-sensitive. We format with `ToString('g')`, which is correct for en-US but can fail silently (0 items) on unusual regional formats. Run with `-Verbose` to see the filter string. The alternative is a DASL `@SQL=` filter in UTC, but check that it still expands recurrences before switching.
2. **Recurrence expansion.** The code relies on `Sort('[Start]')` → `IncludeRecurrences = $true` → `Restrict` → `GetFirst`/`GetNext`. The order matters. Verify that a daily standup shows up once per day in the export.
3. **`credstore.py` DPAPI via ctypes.** ~~The argtypes and `LocalFree` handling were written carefully but never executed.~~ **Verified** on the AWS lab VM (Windows Server 2022, PS 5.1.20348.5622): a round trip through `save_token`/`load_token` works, and the ciphertext does not contain the plaintext token. Still needs a real `set-token` followed by `check` on the actual workstation to prove it end to end with a live Jira PAT.
4. **PowerShell 5.1 `ConvertTo-Json`.** Arrays can serialize as `{"value":[...],"Count":n}`. Both sides guard against this (`Remove-TypeData System.Array` in the exporter, `unwrap_ps_array` in Python). On the lab VM's build (5.1.20348.5622) this did **not** reproduce for 0/1/2-item arrays even without the guard removed manually - the quirk may be specific to a different servicing build, culture, or `ConvertTo-Json` version. Keep both guards regardless; confirm on the actual target workstation by inspecting a `-KeepExport` file with 0, 1, and several categories.
5. **Outlook's object-model guard.** The default fields are believed to be unguarded. If a security prompt appears *without* `-IncludeOrganizer`, find which property triggered it.
6. **`Invoke-MeetingSync.ps1` transcripts under 5.1.** Confirm that Python's console output lands in the `sync_*.log` transcript when run from a hidden scheduled task. If it doesn't, rely on Python's own `Odin.log`.
7. **Python discovery.** `Resolve-Python` prefers `py.exe -3` and skips the Microsoft Store alias. Agency installs sometimes put Python somewhere odd; `-Python <path>` is the escape hatch.
8. **Outlook not running.** `New-Object -ComObject Outlook.Application` may start Outlook headless, or prompt for a profile if there are several. Test it from the scheduled task with Outlook closed.
9. **CSV assumptions.**
   - That the export expands recurring meetings into rows.
   - That the `Show time as` numbering is 0=free … 3=OOF.
   - That the file is cp1252 or UTF-8.

   Verify all three against a real export.
10. **Jira specifics.**
    - Required custom fields on sub-tasks (`extra_fields`).
    - The sub-task type name (`check` lists it).
    - `assignee: {"name": ...}` on Data Center.
    - Whether an SSO proxy intercepts REST calls. The client detects HTML responses and says so.

---

## 5. Target-machine validation checklist (human)

Run these on the federal workstation. For each one, save the output (redact names and keys as needed) and paste it into Kiro.

- [ ] `.\src\windows\Test-Environment.ps1`: full output. Note which path it recommends.
- [ ] `powershell.exe -NoProfile -File tools\Test-PowerShellSyntax.ps1`: the real 5.1 parse check.
- [ ] `py -3 -m unittest discover -s tests -v`: all pass. `test_stdlib_only` skips on Python < 3.10, which is fine.
- [ ] `py -3 -m odin init`, edit the config, then `set-token` and `check`. This proves DPAPI and Jira connectivity.
- [ ] Path A: `.\src\windows\Export-OutlookMeetings.ps1 -Start (Get-Date).Date.AddDays(-7) -End (Get-Date) -Verbose`. Then open the JSON and check:
  - recurring meetings appear once per occurrence
  - times are correct in UTC
  - categories are arrays, not `{"value":...}`
  - no security prompt appeared
- [ ] `.\src\windows\Invoke-MeetingSync.ps1 -DaysBack 7 -DryRun`: the plan looks right (skips, parents, summaries).
- [ ] A real run on one day (`-DaysBack 0`). Check the sub-tasks in Jira, then re-run to confirm everything shows `EXISTS`.
- [ ] Path B: export a CSV for the same week and run `push --csv ... --dry-run`. Meetings already created via COM should show `EXISTS`; this proves cross-source dedupe. Note any rows the loader skipped.
- [ ] If worklogs are wanted: set `log_work: true`, run one real day, and confirm the worklogs in Jira (and in Tempo, if used).
- [ ] `.\src\windows\Register-MeetingSyncTask.ps1`, then `Start-ScheduledTask -TaskName meeting2jira-daily` **with Outlook closed**, then check the logs.
- [ ] Anything surprising: exact error text plus the relevant log lines from `%LOCALAPPDATA%\Asgard\odin\logs\`.

**Path C is dormant** — skip this block unless you have actually been forced onto "new Outlook" *and*
Graph (P2-C) is not yet approved. Otherwise spend the effort on the Graph ticket instead. If you do
need it, for `playwright-app/`:

- [ ] `pip install -r playwright-app\requirements.txt` from the internal mirror, then `playwright install msedge`.
- [ ] `.\odin-owa setup`: a visible browser, sign in including MFA, close it. Confirms Conditional Access accepts the persistent profile.
- [ ] `.\odin-owa discover`: **the important one.** It lists every JSON request the calendar page makes. Confirm a line is flagged `looks like the calendar API`, and that the flagged line is not the telemetry beacon. If nothing is flagged, capture the whole list — that is what the real endpoint pattern has to be derived from.
- [ ] `python tools\sanitize_capture.py` on a real capture, then replace `tests/fixtures/owa_events_sample.json`. The current fixture is **invented**; every mapping test is only as good as it is.
- [ ] `.\odin-owa preview`: the plan should match what Outlook shows for the same week.
- [ ] Confirm `sensitivity`, `showAs`, `responseStatus` and `isOnlineMeeting` are actually present in the real response. `skip_private` and `skip_declined` depend on them, and their absence would silently push meetings that should have been skipped. **Note the casing** in the captured JSON: Graph returns camelCase, the Outlook endpoint returns PascalCase. `mapping._field` handles either, but record which one this tenant serves — it tells us which API the client is really on.
- [ ] Delete the export afterwards, or confirm retention pruning removed it.

---

## 6. Backlog

Priority order: P0 before anything else, then P1s in order.

- Each item should become a Kiro spec.
- Every item must keep all three verification commands passing (see `tech.md`) and update the README where behavior changes.

### P0: Target-machine bring-up fixes
**Goal:** make everything in section 5 pass on the real machine.
**Approach:** driven by pasted results. Prefer small, targeted fixes. If the `Restrict` locale issue shows up, keep `ToString('g')` as the default and add a fallback strategy rather than replacing it outright.
**Done when:** every checklist item passes, and the "Unverified" rows in section 3 are updated with evidence.

### P1-B: Visible health for scheduled runs — DONE
**Problem:** the scheduled task runs hidden. An expired PAT or a moved parent issue will fail silently every day.
**Done:** `last_run.json` (timestamp, counts, exit code, first error) is written by `cmd_push` on every real run, surfaced by `status` and by `Test-Environment.ps1` (which flags a failure or a run older than 4 days), and `Invoke-MeetingSync.ps1` prunes `sync_*.log` older than `-TranscriptRetentionDays` (default 30).

**Also done, closing this item:**
- **Active failure surfacing.** On failure, `ATTENTION-meeting2jira.txt` is written to the Desktop
  (OneDrive relocation handled, data-directory fallback), naming the error and the command that
  diagnoses it, and stating that re-running is safe so the user does not fear duplicates. Removed
  automatically on the next success. Gated on `notify.alert_after_failures` consecutive failures, so
  a single blip need not raise an alarm; the streak lives in `last_run.json`. Failures that happen
  before Python runs — missing config, an export that produced nothing — are alerted by
  `Invoke-MeetingSync.ps1` on the first occurrence, because in that case nothing was attempted at all.
  `doctor` reports an outstanding alert.

  *Researched and rejected:* Windows toast (needs WinRT type loading, which CLM blocks, and the
  orchestrator must stay CLM-safe), BurntToast (PowerShell Gallery unavailable), `mshta.exe` (works
  with no dependencies, but is a well-known living-off-the-land binary that endpoint protection and
  AppLocker commonly block — it would make this tool resemble what those controls exist to stop),
  `Send-MailMessage` (deprecated, needs an SMTP relay), Outlook COM mail (FullLanguage only, new
  scope, risks a security prompt).
- **Token expiry warning.** The endpoint does exist on Data Center: `GET /rest/pat/latest/tokens`
  returns `expiringAt` per token. Surfaced in `check` and as a `TOKEN:` warning during `push`,
  configurable via `jira.warn_token_expiry_days` (default 14, `0` disables). Degrades silently to
  nothing on 401/403/404/405, so older versions are unaffected. One honest limitation encoded in the
  message: the response never echoes the token in use, so with several tokens it reports the
  soonest-expiring by name and says it cannot know which one a request used.

### P2-A: Edits to meetings that were already synced
**Context:** because only *ended* meetings are pushed, this matters less than the README's roadmap implies. It only happens when a past meeting is edited afterwards.
- On the COM path, a subject change keeps the same key, so it is already handled: the meeting shows as `EXISTS` and the sub-task title just goes stale.
- A time change makes a new key and a new content hash, which creates a duplicate.
- On the CSV path, which keys on the content hash, even a subject change creates a duplicate.

**Approach:**
- Add optional `global_id` and `is_recurring` fields to the export. This is additive, so it stays schema v1.
- Add a `global_id` column to state.
- For non-recurring meetings with no key match but a `global_id` match, apply a new config option `on_change: "update" | "new" | "ignore"` (default `"update"`):
  - `"update"` updates the summary and description, and the worklog if `worklog_id` is known.
  - Dry-run shows these as `UPDATE`.
- Recurring exceptions are out of scope unless the user asks: the whole series shares one GlobalAppointmentID.

**Done when:** fixture pairs (before/after) prove update-in-place with no duplicate, for non-recurring meetings.

### P2-B: CSV hardening
- Add a configurable header map for non-English Outlook installs.
- Confirm the `Show time as` numbering from a real export and fix `_CSV_BUSY` if needed.
- Document in `check` or `push` output that response status is unknown on this path.
- Consider deleting the CSV after a successful push, behind a flag, because it contains meeting bodies.

### P2-C: Microsoft Graph source (Path D). **Now the intended primary path.** Blocked on an IT ticket, not on code.

Promoted from "phase 2, nice to have" to the target architecture, because it is the only source that
works on classic *and* new Outlook, needs no browser automation, and raises no terms-of-service
question. Path C (`playwright-app/`) is now a dormant contingency that this supersedes.

**The blocker is the client ID, not the code.** Graph with delegated permissions requires an Entra
application; `msal` changes how you authenticate, not whether you are permitted to. Most federal
tenants disable both non-admin app registration and user consent, so plan on the ticket rather than
hoping. Ask first whether an **existing internal app** already holds a calendar scope and can add
your account — that may skip the queue entirely.

**Needs from IT:** a public-client Entra app (no secret — nothing to rotate or leak), redirect URI
`http://localhost`, delegated calendar consent, admin consent.

**Ask for `Calendars.ReadBasic`, not `Calendars.Read`.** ReadBasic is intended to exclude meeting
bodies and attendee lists, which matches the data-minimization rule the COM path already follows and
lets you tell the ISSO the tool is *incapable* of reading meeting content rather than merely choosing
not to. **Verify the exact field exclusions against current Graph docs before quoting this in the
ticket** — it was not re-confirmed online.

**Do NOT piggyback a first-party client ID** (the Azure PowerShell or Graph PowerShell GUIDs). It is
undocumented reliance on Microsoft's own registrations, commonly blocked by Conditional Access app
filters, and often not consented in GCC High/DoD. On a workstation where ISSO approval is the goal,
it risks the credibility of the whole tool.

**Endpoints by cloud:**

| Cloud | Graph | Login |
|---|---|---|
| Commercial / GCC | `graph.microsoft.com` | `login.microsoftonline.com` |
| GCC High | `graph.microsoft.us` | `login.microsoftonline.us` |
| DoD | `dod-graph.microsoft.us` | `login.microsoftonline.us` |

**Approach:** lives in `graph-app/`, a sibling of `playwright-app/`, so the daily run never depends on msal (packages are allowed when pinned and listed in `MODULES.md`, but a scheduled task shouldn't need one). It writes schema-v1 JSON and shells out to the unmodified `odin push --input`. Nothing under `Asgard/apps/odin/odin/` changes.

- **Use `msal`.** This supersedes the earlier "no MSAL, hand-roll PKCE" plan, which existed only because of the stdlib-only rule, since dropped (Oct 2026). It removes the PKCE dance, the loopback listener, token refresh bookkeeping, and state/nonce validation — the parts of OAuth where hand-written code goes subtly wrong.
- **Auth mode is an open decision.** `enable_broker_on_windows=True` uses the Windows account broker (WAM), so the token comes from the device's existing primary refresh token: silent, no prompt, and the device itself satisfies MFA and device-compliance Conditional Access. That matters a great deal for an unattended scheduled task. But the `msal[broker]` extra pulls `pymsalruntime`, a **native binary** — a bigger approval surface than plain `msal`, which is pure Python. Plain `msal` also works: interactive browser sign-in on first run, then a cached refresh token. Decide deliberately with IT; do not let it default.
- **Token cache via DPAPI.** Serialize MSAL's `SerializableTokenCache` and protect it with the existing `credstore.py` ctypes DPAPI code. Keep it in a separate file; a refresh token can be too large for Credential Manager.
- Call `/me/calendarView` (it expands recurrences), follow `@odata.nextLink` for paging, send `Prefer: outlook.timezone="UTC"`.
- **Reuse the mapping.** `playwright-app/owa/mapping.py` is already "Graph/OWA event → schema v1", Graph's camelCase is its native casing, and it is now case-insensitive either way. The vocabulary it maps (`responseStatus`, `showAs`, `sensitivity`, `isCancelled`, `isOnlineMeeting`/`onlineMeetingProvider`, `iCalUId`/`id`) is Graph's. **Open structural question:** duplicate it into `graph-app/` with a drift guardrail — the precedent this repo already set for the five copies of the Python-discovery logic, see `test_resolve_python_copies_are_identical` — or factor out a shared package and accept that it breaks the "copy this folder and run it" rule. Not yet decided; ask.
- Graph gives a reliable Teams flag, which improves `is_teams`.
- **Switching sources will not duplicate.** `key` changes (`graph:<iCalUId>|<start>` vs COM's `GlobalAppointmentID|start_utc`), but `content_hash` is source-independent — already proven for OWA vs COM, same identical hash. Add the equivalent of `playwright-app/tests/test_parity.py` to prove it for Graph.

**Done when:** tested against a mocked token endpoint and a mocked Graph endpoint, a parity test shows `content_hash` matches the COM export for the same meeting, and nothing in rules/sync/jira changed. Then verified against the real mailbox on the target machine — which is also the trigger to delete `playwright-app/`.

### P3: Nice to have
- **`report` command:** minutes per parent issue per week, from `state.db`. This is useful evidence for the org's tracking requirement.
- **Better Teams detection on the COM path** (research only): find out whether a non-guarded property reliably marks Teams meetings, without triggering the object-model guard.
- **Signing and distribution:** document how to sign the scripts through the agency's code-signing process for `AllSigned` environments, and consider a versioned release zip.
- **DST edge cases on the CSV path:** naive local times in the repeated or skipped hour.

### Won't do (unless the user explicitly asks and the security implications are discussed)
- EWS; published ICS feeds.
- Disabling TLS verification; bypassing execution policy.
- Reading meeting bodies or attendee lists; syncing other people's calendars.
- Deleting Jira issues; two-way sync.

---

## 7. Decision log (short; the README has more)

| Decision | Why |
|---|---|
| Outlook COM first, CSV fallback, Graph later | COM reuses the signed-in session with no approvals. CSV works under Constrained Language Mode. Graph needs an IT app registration. |
| Python using `urllib`, not `requests` | It trusts the Windows cert store, so TLS inspection works; `requests`/certifi usually fails there. Packages are allowed since Oct 2026 when pinned and listed with alternatives in `MODULES.md`; the daily run needs none. |
| PowerShell makes no decisions | One place for logic (Python) that is testable offline. The PowerShell only needs to be correct about data extraction. |
| Dedupe on key OR content_hash | Stable per occurrence on COM, and it prevents duplicates between the COM and CSV sources. |
| Record state immediately after create | A worklog or transition failure can't cause a duplicate sub-task. |
| `only_ended` on by default | Worklogs must reflect time actually spent. It also means post-hoc edits are the only "reschedule" case. |
| Non-GET calls retry only on 429 | An ambiguous 5xx on create must not be blindly retried (see P1-A). |
| Helpers duplicated across .ps1 files | Dot-sourcing can fail across AppLocker trust boundaries. |

---

## 8. Starter prompts

**Orientation (first session):**
> Read HANDOFF.md, README.md, and every file in .kiro/steering. Then read all code under app/ (the package in src/, src/windows, tests, tools). Run the verification commands from tech.md and report the results. Don't change any code yet. Reply with:
> (1) your understanding of the non-negotiables, in your own words,
> (2) anything in the code that contradicts the docs,
> (3) risks you see that aren't in HANDOFF.md section 4,
> (4) questions for me.

**P0, after running the checklist:**
> Here are results from the target-machine checklist (HANDOFF.md section 5): <paste>. Diagnose each failure, propose the smallest fix, and implement it with tests where the logic can be tested off-machine. For anything that can only be verified on the target machine, tell me exactly what to run next. Update HANDOFF.md section 3 with what's now verified.

**Any backlog item:**
> Create a spec for backlog item <P1-A> in HANDOFF.md. Follow the non-negotiables and contracts in the steering files. Include mock-based tests for every acceptance criterion. Update the README and HANDOFF.md status when done.

---

## 9. Keep this document current

When a backlog item ships:
- update section 3 (status)
- remove or amend the relevant risks in section 4
- move the item out of section 6
- add any new decision to section 7
