---
inclusion: always
---
# Tech stack, non-negotiables, and how to verify

`Asgard/AGENTS.md` holds the rules for everything under `Asgard/`, Odin included; read it first. This
file adds Odin's own non-negotiables and the commands to verify a change.

## Runtime targets
- **Windows PowerShell 5.1** is the baseline (not PowerShell 7). The scripts must parse and run on 5.1.
- **Python 3.9+** (Asgard's rule; vermin `-t=3.9-`). No `match`, no runtime `X | Y` unions, no
  `zoneinfo` (Windows has no tz database without `tzdata`). Muninn needs Python's bundled SQLite 3.37+
  with FTS5, which on Windows means Python 3.11+.
- **Packages**: Odin's daily run is standard library (`Asgard/tests/test_dependencies.py`), because the
  Python it runs on may carry none; its window uses PySide6 like every Asgard window. Odin's packages
  are Asgard's (`Asgard/requirements.txt`, `Asgard/MODULES.md`); the exporters' are in `Odin/MODULES.md`.
  Why `jira`, `requests`, `truststore` and `keyring` aren't used: `Asgard/docs/dependency-policy.md`.

## Odin's non-negotiables (enforced by Asgard/tests/test_odin_guardrails.py; don't weaken the tests)
1. **Never disable TLS verification.** No `CERT_NONE`, `check_hostname=False` or unverified contexts,
   and no config flag for it. Extra CAs go through `jira.ca_bundle`. urllib on Windows trusts the
   Windows certificate store, which is why it's urllib.
2. **HTTPS only** for Jira (http only for localhost in tests).
3. **No execution-policy bypass**: no `-ExecutionPolicy Bypass`, no `Set-ExecutionPolicy`.
4. **Constrained Language Mode.** `Invoke-MeetingSync.ps1`, `Test-Environment.ps1` and
   `Register-MeetingSyncTask.ps1` use cmdlets, arrays, hashtables and core-type methods only: no
   `New-Object -ComObject`, `Add-Type` or `[System.X]::` statics. Only `Export-OutlookMeetings.ps1`
   may need FullLanguage. Scripts are ASCII with CRLF.
5. **Outlook data minimization.** The exporter never reads guarded or sensitive properties (`Body`,
   `RequiredAttendees`, `OptionalAttendees`, `Recipients`, ...); `Organizer` only behind `-IncludeOrganizer`.
6. **No admin rights.** Per-user data in `%LOCALAPPDATA%\Asgard\odin`.
7. **Secrets**: the PAT is stored only via DPAPI (`credstore.py`). Never log, print or store it in plain text.
8. **No EWS.** Microsoft Graph is the only acceptable future network source for the calendar.
9. **Jira writes are crash-safe.** A sub-task's record is written right after the create; every
   worklog goes through Muninn's `sending` row and marker; a write is retried only on 429. A change
   to anything that writes to Jira gets an independent review (`Asgard/AGENTS.md`).

## Verify every change (all must pass)
```
python tools/run_tests.py                       # dev/agent, from the repo root: compact pytest run (workflow.md)
cd Asgard && python -m unittest discover -s tests   # the no-dev-tools check
cd Asgard && python tools/check_muninn_schema.py    # when the schema changed
cd Asgard && vermin -t=3.9- --no-tips --eval-annotations --violations apps asgard tests
ruff check .                                    # dev lint (bug-finding rules)
powershell.exe -NoProfile -File Asgard\apps\odin\tools\Test-PowerShellSyntax.ps1   # Windows PowerShell 5.1
pwsh -NoProfile -File Asgard/apps/odin/tools/Test-PowerShellSyntax.ps1             # if only PowerShell 7 is there
bash Asgard/tools/baldur_smoke.sh               # when Baldur changes: must end PROJ-42 1h30m, PROJ-51 30m
```
Dev tools (pytest, pytest-timeout, pytest-cov, ruff, vermin, actionlint-py) are pinned in `requirements-dev.txt`, which CI
(`.github/workflows/checks.yml`) installs too, and are configured
in `pyproject.toml`; they are never needed to run the apps. The exporters have their own suites:
`python -m unittest discover -s tests` from `Odin/graph-app` and `Odin/playwright-app`.

## Conventions
- Logging: module loggers (`logging.getLogger(__name__)`) under the `odin` namespace. The CLI prints
  through logging, not `print`.
- Errors: raise `ConfigError`, `JiraError`, `CredentialError`, `HistoryError` or `MuninnError` with a
  message that says **what to do**. Exit codes: `0` ok, `1` finished with per-item errors or a failed
  check, `2` config, usage, credential or Muninn error, `130` interrupted.
- Tests: stdlib `unittest`, no real network. Flows use `fake_jira.FakeJira` against a temporary Muninn
  (`fake_jira.OdinTestCase`); `test_odin_jira_client.py` uses a local `http.server` on 127.0.0.1.
- Keep `Odin/README.md` (config reference, troubleshooting), `Odin/INSTALL.md` and
  `Asgard/docs/integration/odin.md` in sync with behavior changes.
- **Be explicit about verification.** Anything touching Outlook COM, DPAPI, Task Scheduler or PS 5.1
  runtime behavior is "needs target-machine verification" unless it was run there; add it to
  `Asgard/HANDOFF.md`.

## PowerShell specifics
- Test for the language mode with `$ExecutionContext.SessionState.LanguageMode`.
- PS 5.1 `ConvertTo-Json` can serialize arrays as `{"value":[...],"Count":n}`: the exporter calls
  `Remove-TypeData System.Array`, and Python calls `unwrap_ps_array()`. Keep both.
- FullLanguage scripts write UTF-8 without a BOM (`[IO.File]::WriteAllText` with `UTF8Encoding($false)`);
  Python reads with `utf-8-sig` anyway.
- PS 5.1 mangles embedded double quotes in native-command arguments. Avoid `"` inside arguments to `python.exe`.
