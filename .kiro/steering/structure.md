---
inclusion: always
---
# Structure and contracts

**Two deliverables: `Odin/app/` (meeting2jira, the program this file describes) and `Asgard/`
(launcher, Muninn, Baldur). Everything else is a sibling deliverable, documentation, or
development support.**

The repo root holds tooling (`.kiro/`, `tools/`, `infra/`, `pyproject.toml`), the `Odin/` and
`Asgard/` folders, and `context-docs/`. Everything that is Odin - the program, its sibling
exporters, the GUI and the docs - lives under `Odin/`. Paths below are relative to `Odin/` unless
they start with `../` or are one of the root-level tooling names.

**`Asgard/` runs on its own rules.** Before touching it, read `Asgard/HANDOFF.md` (state, decisions,
what's next) and `Asgard/AGENTS.md` (non-negotiables, definition of done). It targets Python 3.9+
stdlib, every app writes through Muninn (`Asgard/asgard/muninn/`), Baldur biases every number down
and never writes to Jira, and changes to estimates, approvals or posting get an independent review
recorded in `Asgard/docs/`. The spec snapshots in `Asgard/docs/` are the current specs; the copy in
`.kiro/specs/` is superseded. Until Odin moves into Muninn, nothing in `Odin/app/` imports
`asgard`, and nothing in `Asgard/` reads Odin's `state.db`. `context-docs/` holds inputs Brandon
dropped in (the 0.3.0 handoff zip, spec drafts, the archive of the retired `munnin-layer/` and
`baldur/`); read them, don't edit them.

Copying `Odin/app/` to a workstation is a complete, runnable install: nothing in it reaches outside
itself, and no other folder is required at runtime. Keep it that way.

```
<repo root>/
  .kiro/  tools/  infra/  pyproject.toml  requirements-dev.txt  KIRO_SETUP.md   dev support, never shipped
  context-docs/           inputs and archives; read-only
  Asgard/                 SECOND DELIVERABLE: Asgard launcher, Muninn, Baldur (see Asgard/AGENTS.md)
  Odin/                   THE PRODUCT: the program, sibling exporters, GUI and docs
    README.md INSTALL.md ARCHITECTURE.md HANDOFF.md   docs; not needed at runtime

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

gui/                      Odin's Tkinter frontend (WORK IN PROGRESS, on-prem origin). stdlib tkinter, so
                          it adds no pip dependency. It is a FRONTEND: no filtering, routing, dedupe
                          or Jira logic belongs in it. See .kiro/specs/odin-gui-guardrails.md for how
                          its guardrail coverage is to be built - until that is done, the guardrail
                          tests do NOT scan it, so every non-negotiable passes vacuously for gui/.
playwright-app/           Path C: an OWA exporter for "new Outlook", which has neither COM nor the
                          Import/Export wizard. A SEPARATE deliverable on purpose - it needs pip
                          (playwright), so folding it into app/ would forfeit the stdlib-only
                          guarantee that test_guardrails.py enforces. It writes schema-v1 JSON and
                          shells out to the unmodified `python -m meeting2jira push --input`;
                          nothing under app/src/meeting2jira/ may change to accommodate it.
                          DORMANT, and not part of the installed deliverable. Superseded by Graph
                          (Path D). Do not add features here; limit changes to things that also
                          protect the Graph path, which in practice means owa/mapping.py. It is
                          deleted once Graph is verified against the real mailbox. See its README.
graph-app/                Path D, PLANNED and the intended primary source: Microsoft Graph
                          /me/calendarView via msal. Separate for the same pip reason. Blocked on an
                          Entra app registration, not on code. See HANDOFF.md P2-C.
                            owa/capture.py  browser session, endpoint discovery, direct fetch
                            owa/mapping.py  OWA JSON -> schema v1. Stdlib only, imports no playwright,
                                            so it is testable with no browser and no mailbox.
                            tests/fake_owa.py  a deliberately noisy local OWA. Keep it noisy: assets,
                                            decoy JSON endpoints and a /owa/telemetry/events beacon
                                            carrying start/end are what catch discovery bugs.
power-platform/           Power Automate / Power BI feasibility only. No runtime code.

(the four blocks above - app/, gui/, playwright-app/, graph-app/, plus power-platform/ - all sit
 inside Odin/, as siblings. Below are the root-level items that stay OUTSIDE it.)

../infra/windows-test-vm/   Terraform for a throwaway Windows host to run those checks on.
                            Dev tooling, never shipped. SSM only, no inbound rules. Syncs Odin/app/ alone.
../tools/                   Kiro dev tooling, never shipped (not Odin/app/tools): run_tests.py, hooks/*.py
../pyproject.toml ../requirements-dev.txt   dev-only pytest/ruff config; not a package definition
../.kiro/steering/          these rules; agents/ (test-runner, code-scout) and hooks/ beside it
../KIRO_SETUP.md            Kiro setup notes
```

## Keeping Odin/app/ self-contained
- Every path inside `app/` is derived from the file's own location (`$PSScriptRoot`,
  `Path(__file__)`), never from the current directory or a repo-relative guess. That is what makes
  the folder portable.
- Nothing in `Odin/app/` may reference `../`, the repo root, `infra/`, or `.kiro/`. (Moving the
  product under `Odin/` did not change this: `app/` reaches nothing outside itself.)
- Runtime data belongs in `%LOCALAPPDATA%\meeting2jira`, never inside `app/`. The folder should stay
  safe to replace wholesale during an upgrade without losing config, token, or state.

## How Python is located
`Odin/app/src` goes on **PYTHONPATH**, and the working directory stays at **`Odin/app/`**. Both entry points
and every PowerShell caller do this. It is what lets `-m meeting2jira` resolve from `src/` while
`unittest discover -s tests` still finds `Odin/app/tests` — cd-ing into `src/` would break the second.
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
