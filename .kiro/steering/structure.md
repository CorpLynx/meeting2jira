---
inclusion: always
---
# Structure and contracts

**`app/` is the program. Everything else in the repo is development support.**

Copying `app/` to a workstation is a complete, runnable install: nothing in it reaches outside
itself, and no other folder is required at runtime. Keep it that way.

```
app/                      THE DELIVERABLE. Self-contained; copy this folder and run it.
  meeting2jira.cmd          entry point on Windows. Thin dispatcher only: no logic, no decisions.
  m2j                       same idea for macOS/Linux development. Named differently because the
                            meeting2jira/ package already owns that name and POSIX has no PATHEXT.
  config.example.json       template copied by `init`; also the config reference by example

  src/                    ← the program. Two halves, split at the architectural seam, not by taste.
    meeting2jira/           all logic, stdlib only. Must keep this exact directory name: it is what
                            `python -m meeting2jira` resolves. app/src is put on PYTHONPATH.
      __main__.py   CLI (init, set-token, check, push, status, forget), logging setup, exit codes
      config.py     DEFAULTS, load/merge (keys starting "_" are comments), validate(), parse_hhmm
      models.py     Meeting dataclass, parse_utc/iso_utc, content_hash, unwrap_ps_array
      sources.py    load_export (JSON v1), load_outlook_csv
      rules.py      Router: filters → tour of duty → first-match rules → default_parent
      sync.py       run(): decide → dedupe → render templates → create → worklog → transition
      jira.py       JiraClient over urllib (retries, error hints). Data Center only.
      state.py      sqlite `synced` table (dedupe + worklog bookkeeping)
      credstore.py  DPAPI via ctypes; JIRA_PAT env override
    windows/                the Windows-native layer, thin. No filtering or business logic here.
      Test-Environment.ps1      read-only preflight; recommends Path A (COM) or Path B (CSV). CLM-safe.
      Export-OutlookMeetings.ps1  Outlook COM → export JSON (schema v1). Needs FullLanguage.
      Invoke-MeetingSync.ps1    orchestrator: export (or CSV) → `python -m meeting2jira push`. CLM-safe.
      Register-MeetingSyncTask.ps1  per-user weekday scheduled task. CLM-safe.

  tests/          unittest; fixtures/; test_guardrails.py enforces non-negotiables.
                  Ships deliberately: running them on the target machine proves the install.
  tools/          Test-PowerShellSyntax.ps1   parse check, runs under 5.1 and 7
                  Invoke-WindowsChecks.ps1    Windows-only behavior; `meeting2jira selftest`

infra/windows-test-vm/    Terraform for a throwaway Windows host to run those checks on.
                          Dev tooling, never shipped. SSM only, no inbound rules. Syncs app/ alone.
tools/                    Kiro dev tooling, never shipped (not app/tools): run_tests.py, hooks/*.py
.kiro/steering/           these rules; agents/ (test-runner, code-scout) and hooks/ beside it
README.md INSTALL.md ARCHITECTURE.md HANDOFF.md KIRO_SETUP.md    docs; not needed at runtime
```

## Keeping app/ self-contained
- Every path inside `app/` is derived from the file's own location (`$PSScriptRoot`,
  `Path(__file__)`), never from the current directory or a repo-relative guess. That is what makes
  the folder portable.
- Nothing in `app/` may reference `../`, the repo root, `infra/`, or `.kiro/`.
- Runtime data belongs in `%LOCALAPPDATA%\meeting2jira`, never inside `app/`. The folder should stay
  safe to replace wholesale during an upgrade without losing config, token, or state.

## How Python is located
`app/src` goes on **PYTHONPATH**, and the working directory stays at **`app/`**. Both entry points
and every PowerShell caller do this. It is what lets `-m meeting2jira` resolve from `src/` while
`unittest discover -s tests` still finds `app/tests` — cd-ing into `src/` would break the second.
Save and restore `PYTHONPATH` around the call rather than leaking it.

## Two path traps this layout creates
- The package is three levels down, so `__main__.py` uses `Path(__file__).resolve().parents[2]` to
  find `app/`. Counting parents wrong silently breaks `init`.
- Guardrail tests assert "no offenders found", which passes vacuously against a directory that does
  not exist. `GuardrailWiringTests` asserts the paths resolve to real files for exactly that reason;
  keep it in step when anything moves.

## Entry point rules
- `meeting2jira.cmd` dispatches to `src/windows/*.ps1` and `python -m meeting2jira`. It must stay a
  dispatcher: any new behavior goes in Python, or in PowerShell when it is Windows-native.
- A bare `meeting2jira` (no arguments) is the daily run and must stay safe to repeat.
- Batch traps worth remembering: `shift` does not rewrite `%*`, and `for /f` token-splitting
  mangles quoted paths containing spaces. Collect arguments explicitly.
- Batch has no parse-only mode, so entry-point changes need a case in `Invoke-WindowsChecks.ps1`.

## Contracts (changing any of these is a deliberate, versioned decision)
1. **Export JSON, schema_version 1**, produced by any source and consumed by `sources.load_export`. Documented in README "Export schema".
   - Adding optional fields is backward compatible.
   - Renaming or removing fields, or changing the meaning of `key`, requires bumping `schema_version` and keeping support for the old version.
2. **Dedupe identity.**
   - COM `key` = `GlobalAppointmentID|start_utc`. CSV `key` = `csv:` + content_hash.
   - `content_hash` = sha256(normalized subject | start | end)[:32]. It prevents duplicates across sources.
   - Changing either formula re-creates every past meeting. It needs a state migration plan.
3. **State DB** (`%LOCALAPPDATA%\meeting2jira\state.db`, table `synced`).
   - Schema changes must be additive migrations (`ALTER TABLE ... ADD COLUMN`) that run on open in `State.__init__`.
   - Never drop user state.
   - Rows are written immediately after the issue is created, before the worklog or transition, so a later failure can't cause duplicates.
4. **CLI surface**: commands, flags, and exit codes are used by `Invoke-MeetingSync.ps1` and scheduled tasks. Keep them backward compatible.
5. **Config**: new keys need a default in `config.DEFAULTS`, validation in `validate()`, a row in the README config reference, and an entry in `config.example.json` when useful.

## Where to put new things
- **New calendar source** (e.g. Graph): a new module that writes schema-v1 JSON (preferred) or returns `List[Meeting]`. Wire it into the CLI and the orchestrator. Nothing in rules/sync/jira should change.
- **New filter**: `config.DEFAULTS["filters"]` → `Router.filter_reason` → README → a test in `test_pipeline.py`.
  - A substring-list filter also goes in `config.TEXT_FILTER_KEYS`, which drives both validation and
    `Router.text_filters`. Validation must reject a bare string: `"OOO"` would otherwise be read as
    three one-character needles and skip nearly everything.
  - Filters are evaluated cheapest-first; text matching comes after the structural checks. Tests
    that assert a skip reason have to disable any earlier filter that would fire first.
- **New rule match key**: `rules.MATCH_KEYS` and `_Rule` → README → test.
- **Tour of duty**: `rules.TourOfDuty` compares minutes-since-local-midnight, deliberately not
  datetimes, so it stays a wall-clock question and needs no tz database. `config.parse_hhmm` lives
  in `config.py` because `rules.py` imports `config` and the reverse would be circular.
  `outside_action` of `route`/`skip` is evaluated *before* rules; `partial` always counts as inside.
- **New Jira call**: `JiraClient` method → mock handler in `tests/test_jira_client.py`.
