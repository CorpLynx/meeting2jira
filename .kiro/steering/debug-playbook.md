---
inclusion: auto
name: debug-playbook
description: Debugging playbook and known meeting2jira gotchas. Use when a test keeps failing after two fix attempts, or when a failure involves the PowerShell-to-Python export handoff, encoding, PYTHONPATH/paths, JSON parsing, Jira HTTP calls, or the state DB.
---
# Debug playbook

## Narrow it down
1. Rerun just the one test with more traceback:
   `python tools/run_tests.py test_module.Class.test_name --trace-lines 60`
2. Find the deepest frame in OUR code (`app/src/meeting2jira`), not the stdlib; look there.
3. State one hypothesis, check it with the smallest possible change or a temporary log line, then
   remove it. Never print or log the Jira token while debugging.

## Known gotchas in this project
- `ModuleNotFoundError: meeting2jira` or `tests`: the cwd must be `app/` with `app/src` on
  PYTHONPATH. `tools/run_tests.py` does this; a hand-rolled command from the repo root won't.
- `init` can't find files: `__main__.py` uses `Path(__file__).resolve().parents[2]` to reach `app/`.
  Count the parents again if anything moved.
- Guardrail tests passing suspiciously after a move: they assert "no offenders found", which passes
  on an empty glob. `GuardrailWiringTests` must fail first; if it doesn't, its paths are stale.
- Export has `{"value":[...],"Count":n}` instead of a list: PS 5.1 `ConvertTo-Json` array quirk.
  The exporter's `Remove-TypeData System.Array` and Python's `unwrap_ps_array()` both stay.
- JSON starts with a BOM / mojibake: Python reads exports with `utf-8-sig`; FullLanguage scripts
  write UTF-8 without BOM via `[IO.File]::WriteAllText`.
- The orchestrator picked up the wrong "export path": a called script wrote data with
  `Write-Output` instead of `Write-Host`. It takes the last output object as the path.
- Quotes vanish in arguments to `python.exe`: PS 5.1 mangles embedded `"` in native-command args.
- A CLM-safe script works in your shell but fails on the workstation: a `[Type]::Member` static,
  `New-Object` or `Add-Type` slipped in. `test_guardrails.test_clm_safe_scripts` should catch it.
- Outlook `Restrict` returns nothing: date strings must use the machine's short format
  (`ToString('g')`), and `Sort('[Start]')` must come before `IncludeRecurrences = $true`.
- Duplicate sub-tasks: check the dedupe key (`GlobalAppointmentID|start_utc` vs `csv:` + hash) and
  that the state row is written right after create, before worklog/transition.
- Text filter skips almost everything: a bare string like `"OOO"` was treated as a list of
  one-character needles. Validation must reject non-list values.
<!-- Add one line each time you or the agent burn more than a couple of turns on something. -->
