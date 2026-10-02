---
inclusion: always
---
# Tech stack, non-negotiables, and how to verify

## Runtime targets
- **Windows PowerShell 5.1** is the baseline (not PowerShell 7). The scripts must parse and run on 5.1.
- **Python 3.8+, standard library only** (the code currently needs 3.7+; keep it ≤ 3.8).
  - Don't use `match`, runtime `X | Y` unions, `zoneinfo` (Windows has no tz database without the `tzdata` package), or `str.removeprefix`.
  - Use `from __future__ import annotations` and `typing.List`/`Optional`.

## Non-negotiables (enforced by tests/test_guardrails.py; don't weaken the tests)
1. **No third-party Python packages**: no requests, msal, pywin32, keyring, dateutil. The target machine has no pip access, and every package would need approval. Use `urllib`, `ssl`, `sqlite3`, `ctypes`, `json`, `csv`.
2. **Never disable TLS verification.** No `CERT_NONE`, `check_hostname=False`, or unverified contexts, and no config flag for it. Extra CAs go through `jira.ca_bundle`. Python on Windows already trusts the Windows cert store (this is why it's urllib and not requests).
3. **HTTPS only** for Jira (http is allowed only for localhost in tests).
4. **No execution-policy bypass** anywhere: no `-ExecutionPolicy Bypass`, no `Set-ExecutionPolicy`.
5. **Constrained Language Mode safety.**
   - `Invoke-MeetingSync.ps1`, `Test-Environment.ps1`, and `Register-MeetingSyncTask.ps1` must run under CLM: cmdlets, arrays, hashtables, and methods on core types only. No `New-Object -ComObject`, `Add-Type`, or `[System.X]::` statics.
   - Only `Export-OutlookMeetings.ps1` may require FullLanguage.
6. **Outlook data minimization.** The exporter must not read guarded or sensitive properties (`Body`, `RequiredAttendees`, `OptionalAttendees`, `Recipients`, …).
   - `Organizer` is read only behind `-IncludeOrganizer`.
   - Reading guarded properties triggers Outlook's security prompt, and it widens what data is handled.
7. **No admin rights** required for any step. Per-user data lives in `%LOCALAPPDATA%\meeting2jira`.
8. **Secrets**: the Jira PAT is stored only via DPAPI (`credstore.py`). Never log it, print it, or write it in plain text.
9. **No EWS.** Exchange Online is disabling it starting Oct 2026. Microsoft Graph is the only acceptable future network source, and it needs an IT-registered app.

## Verify every change (all must pass)
```
python tools/run_tests.py                                    # agent: compact unittest run (see workflow.md)
python -m unittest discover -s tests -v                      # human, from app/ with app/src on PYTHONPATH
powershell.exe -NoProfile -File tools\Test-PowerShellSyntax.ps1   # from app/; Windows PowerShell 5.1 (preferred)
pwsh -NoProfile -File tools/Test-PowerShellSyntax.ps1             # if only PowerShell 7 is available
```
`tools/run_tests.py` (repo root) and the plain unittest command run the same suite; the agent uses the
runner because a hook blocks raw unittest output. Lint rules for dev are in `ruff.toml`.
Optional: `vermin -t=3.8- --violations meeting2jira tests` to confirm Python-version compatibility.

## Conventions
- Logging: use module loggers (`logging.getLogger(__name__)`) under the `meeting2jira` namespace. The CLI prints through logging, not `print`.
- Errors: raise `ConfigError`, `JiraError`, or `CredentialError` with a message that says **what to do**. `main()` maps them to exit code 2.
- Exit codes:
  - `0` ok
  - `1` push finished with per-item errors, or a check failed
  - `2` config, usage, or credential error
  - `130` interrupted
- Tests: stdlib `unittest` only. No real network; the Jira tests use a local `http.server` on 127.0.0.1. Add fixtures under `tests/fixtures/`.
- Keep `README.md` (config reference, troubleshooting, roadmap) in sync with behavior changes.
- **Be explicit about verification.** This repo is often edited away from the target Windows machine. Anything touching Outlook COM, DPAPI, Task Scheduler, or PS 5.1 runtime behavior must be reported as "needs target-machine verification" unless it was actually run there. Add those items to the checklist in `HANDOFF.md`.

## PowerShell specifics
- Test for the language mode with `$ExecutionContext.SessionState.LanguageMode`.
- PS 5.1 `ConvertTo-Json` quirks: arrays can serialize as `{"value":[...],"Count":n}`.
  - The exporter calls `Remove-TypeData System.Array` before `ConvertTo-Json`, and Python calls `unwrap_ps_array()`. Keep both.
- Write files as UTF-8 without a BOM (`[IO.File]::WriteAllText` with `UTF8Encoding($false)`) in FullLanguage scripts. Python reads with `utf-8-sig` anyway.
- PS 5.1 mangles embedded double quotes in native-command arguments. Avoid `"` inside arguments passed to `python.exe`.
