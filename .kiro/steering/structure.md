---
inclusion: always
---
# Structure and contracts

**One deliverable: `Asgard/`** (the launcher, Muninn, and its apps: Baldur, Heimdall, Odin, Ysildir).
Odin, the program this steering grew up around, has been an Asgard app since Oct 10, 2026:
`Asgard/apps/odin`, package `odin`, with its records in Muninn. **`Odin/`** keeps only Odin's two
optional calendar exporters, the Power Platform material and Odin's docs and history.

**Asgard runs on its own rules, and they win.** Before touching anything under `Asgard/`, read
`Asgard/HANDOFF.md` (state, decisions, what's next) and `Asgard/AGENTS.md` (non-negotiables,
definition of done). Odin's contract with Muninn, the order of its daily run and what protects Jira
are in `Asgard/docs/integration/odin.md`. The spec snapshots in `Asgard/docs/` are the current
specs. `context-docs/` holds inputs Brandon dropped in (the 0.3.0 handoff zip, spec drafts, the
archive of the retired `munnin-layer/` and `baldur/`); read them, don't edit them.

```
<repo root>/
  .kiro/  tools/  infra/  pyproject.toml  requirements-dev.txt  KIRO_SETUP.md   dev support, never shipped
  context-docs/             inputs and archives; read-only
  Asgard/                   THE DELIVERABLE (see Asgard/AGENTS.md and Asgard/README.md)
    asgard/                   launcher, installer, Valhalla, the shared window (asgard.ui), Muninn (asgard/muninn/)
      muninn/odin.py            Odin's flows in Muninn: calendar, meeting_subtasks, the posting protocol
    apps/odin/                ODIN
      odin.cmd                  Windows dispatcher: no logic, only routing. A bare `odin` is the daily run.
      cli.py                    Python entry point (daily, push, sync, post, status, forget, report, init, set-token, check)
      odin.pyw, ui/             the window: pages in Asgard's shared window (backend: odin/ui_backend.py)
      config.example.json       template copied by `init`; also the config reference by example
      odin/                     all logic; the daily run is standard library (tests/test_dependencies.py)
        cli.py        commands, logging, exit codes, last_run.json and the Desktop alert
        config.py     DEFAULTS, load/merge (keys starting "_" are comments), validate, parse_hhmm
        models.py     Meeting, parse_utc/iso_utc, content_hash, unwrap_ps_array
        sources.py    load_export (JSON v1), load_outlook_csv, read_export
        rules.py      Router: filters -> tour of duty -> first-match rules -> default_parent
        sync.py       meetings to sub-tasks: decide -> dedupe -> create -> record -> worklog -> transition
        store.py      Muninn: connection, sources, calendar, meeting_subtasks, the journal, the run lock
        collect.py    Jira into Muninn: issues, tracked parents, key lookups, worklogs
        posting.py    worklogs to Jira: meeting time, approved Baldur days, posts nobody saw finish
        history.py    state.db imported into Muninn once, then retired
        jira.py       JiraClient over urllib (retries, error hints); the only network code. Data Center only.
        credstore.py  DPAPI via ctypes; JIRA_PAT env override
      windows/                  the Windows-native layer, thin. No filtering or business logic here.
        Test-Environment.ps1        read-only preflight (`odin doctor`). CLM-safe.
        Export-OutlookMeetings.ps1  Outlook COM -> export JSON (schema v1). Needs FullLanguage.
        Invoke-MeetingSync.ps1      orchestrator: export (or CSV) -> `cli.py daily`. CLM-safe.
        Register-MeetingSyncTask.ps1  the "Asgard Odin daily" task. CLM-safe.
      tools/                    Test-PowerShellSyntax.ps1 (5.1 and 7), Invoke-WindowsChecks.ps1 (`odin selftest`)
    tests/                    plain unittest. Odin's are test_odin_*.py, with fake_jira.py (an in-memory
                              Jira and OdinTestCase, a temporary Muninn) and fixtures/odin/.
  Odin/
    README.md INSTALL.md ARCHITECTURE.md MODULES.md HANDOFF.md   docs; HANDOFF.md is history
    graph-app/                Path D, PLANNED and the intended primary source: Microsoft Graph via msal.
                              Separate so the daily run never depends on msal. Blocked on an Entra app
                              registration, not on code. Hands its export to Asgard's odin.cmd.
    playwright-app/           Path C, DORMANT contingency: OWA through a browser for "new Outlook".
                              Needs playwright (native: bundles node.exe). Superseded by Graph; don't
                              add features, limit changes to what also protects Graph (owa/mapping.py).
                                owa/mapping.py  OWA JSON -> schema v1, stdlib, testable with no browser
                                tests/fake_owa.py  a deliberately noisy local OWA; keep it noisy
    power-platform/           Power Automate / Power BI material (power-bi/report.ps1 wraps `odin report`)

infra/windows-test-vm/      Terraform for a throwaway Windows host for the Windows checks. Never shipped.
tools/                      Kiro dev tooling (not Asgard/apps/odin/tools): run_tests.py, hooks/*.py
.kiro/steering/             these rules; agents/ (test-runner, code-scout) and hooks/ beside it
```

## Paths and Python
- Every path is derived from the file's own location (`$PSScriptRoot`, `Path(__file__)`) or from
  `asgard.paths`, never from the current directory. Runtime data lives in `%LOCALAPPDATA%\Asgard\odin`
  (`ASGARD_HOME\odin` when set); records live in Muninn (`%LOCALAPPDATA%\Asgard\muninn.db`).
- `cli.py` puts `Asgard/` and `apps/odin/` on `sys.path` itself, like Baldur's. The `.cmd` and the
  PowerShell scripts find Python the way Asgard does: `asgard-cli.exe` in a packaged build, else the
  ledger's Python (`install-ledger.json`), else a probe for 3.9+ with SQLite 3.37+ and FTS5.
  `Resolve-Python` is copied into each script on purpose; `test_odin_guardrails` keeps the copies identical.
- Guardrail tests assert "no offenders found", which passes vacuously against a path that doesn't
  exist. `GuardrailWiringTests` asserts the paths resolve to real files; keep it in step when anything moves.

## Entry point rules
- `odin.cmd` dispatches to `windows/*.ps1` and `cli.py`. It must stay a dispatcher: new behavior goes
  in Python, or in PowerShell when it is Windows-native. A bare `odin` must stay safe to repeat.
- `.cmd` and `.ps1` are ASCII with CRLF and Constrained Language Mode safe (Asgard's rule).
- Batch traps: `shift` does not rewrite `%*`, and `for /f` token-splitting mangles quoted paths with
  spaces. Batch has no parse-only mode, so entry-point changes need a case in `Invoke-WindowsChecks.ps1`.

## Contracts (changing any of these is a deliberate, versioned decision)
1. **Export JSON, schema_version 1**, produced by any source and consumed by `sources.load_export`
   (`Odin/README.md`, "Export schema"). Adding optional fields is compatible; renaming or removing
   fields, or changing the meaning of `key`, bumps `schema_version` and keeps the old one working.
2. **Dedupe identity.** COM `key` = `GlobalAppointmentID|start_utc`; CSV `key` = `csv:` + content_hash.
   `content_hash` = sha256(normalized subject | start | end)[:32], which prevents duplicates across
   sources. The `m2j-<hash>` label finds an ambiguously created sub-task. Changing any of these
   re-creates every past meeting.
3. **Muninn's `meeting_subtasks`** (v5, owned by Odin) replaced `state.db`. The record is written the
   moment Jira accepts a create, before the worklog and transition, or goes to `unrecorded.jsonl` and
   the run stops creating. Schema changes are new migrations under Asgard's rules (`Asgard/AGENTS.md` 3).
4. **CLI surface**: commands, flags and exit codes are used by `Invoke-MeetingSync.ps1`, the window
   and the scheduled task. Keep them backward compatible.
5. **Config**: new keys need a default in `config.DEFAULTS`, validation, a row in `Odin/README.md`'s
   config reference, and an entry in `config.example.json` when useful.

## Where to put new things
- **New calendar source** (Graph): something that writes schema-v1 JSON and hands it to `odin.cmd`
  (as `Odin/graph-app` does). Nothing in rules, sync or jira should change.
- **New filter**: `config.DEFAULTS["filters"]` -> `Router.filter_reason` -> `Odin/README.md` -> a test
  in `test_odin_pipeline.py`. A substring-list filter also goes in `config.TEXT_FILTER_KEYS`; validation
  must reject a bare string (`"OOO"` would be three one-character needles). Filters run cheapest-first.
- **New rule match key**: `rules.MATCH_KEYS` and `_Rule` -> README -> test.
- **Tour of duty**: `rules.TourOfDuty` compares minutes since local midnight, not datetimes, so it
  needs no tz database. `outside_action` `route`/`skip` runs before rules; `partial` counts as inside.
- **New Jira call**: `JiraClient` method -> a case in `test_odin_jira_client.py` (local HTTP server)
  and, if the flows use it, `fake_jira.FakeJira`.
- **New Muninn write**: through `asgard.muninn.odin` or `muninn.transaction()`, never across a Jira call.
