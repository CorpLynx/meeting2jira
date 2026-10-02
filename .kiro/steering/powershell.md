---
inclusion: fileMatch
fileMatchPattern: ["**/*.ps1", "**/*.psm1", "**/*.psd1"]
---
# Editing PowerShell in this repo

- Target **Windows PowerShell 5.1**. There is no `?:`, `??`, `?.`, `&&`/`||`, or `ForEach-Object -Parallel`. Run `tools/Test-PowerShellSyntax.ps1` after edits.
- Only `Export-OutlookMeetings.ps1` may use COM or .NET statics. Every other script must stay **CLM-safe** (see tech.md #5; enforced by `tests/test_guardrails.py`).
- Shared helpers are **duplicated on purpose** (e.g. `Resolve-Python`), because dot-sourcing can fail across AppLocker trust boundaries. If you change one copy, change the other.
- Never add `-ExecutionPolicy Bypass`, `Set-ExecutionPolicy`, `Unblock-File` calls, or anything that needs elevation.
- In the exporter:
  - Don't read `Body`, attendee, or recipient properties.
  - With `IncludeRecurrences`, iterate with `GetFirst`/`GetNext` (never `.Count` or `foreach`), and call `Sort('[Start]')` **before** enabling recurrences.
  - `Restrict` date strings must use the machine's short date/time format (`ToString('g')`).
- Scripts called by other scripts return data on the output stream and chat to the host with `Write-Host`. Keep that separation, because the orchestrator takes the last output object as the export path.
