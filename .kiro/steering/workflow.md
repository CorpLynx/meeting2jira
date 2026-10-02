---
inclusion: always
---
# Workflow: edit -> test -> fix

## Running tests
- Never run `python -m unittest` (or pytest) directly; a hook blocks it. Use
  `python tools/run_tests.py [test_module[.Class.test]] [-k pattern] [--lf] [-x]` from the repo root.
  It sets up `app/` + PYTHONPATH itself, prints a compact grouped summary, and keeps the full log
  in `.test-output/last-run.log`.
- While fixing, run the narrowest scope: one module, `-k`, or `--lf`. `--changed` runs the tests
  that cover git-changed files (it always adds `test_guardrails` for changes under `app/src`).
- Full-suite runs (before saying you're done, or after a multi-file change): delegate to the
  `test-runner` subagent rather than running the full suite yourself.
- If you need more detail on one failure, rerun that test id with `--trace-lines 60`, or read only
  its section of the log.

## Fixing failures
- Failures are grouped by root cause. Fix group [1] first, then rerun with `--lf`.
- An import error in a test module hides every test in it; fix it before anything else.
- If the same failure survives two fix attempts, stop and state your hypothesis and what you'd check
  next instead of trying a third variant. Use `#debug-playbook` for known gotchas.
- Never weaken `test_guardrails.py` to get green, and don't change any other test to make it pass
  unless the user asked or the test is clearly wrong; say so explicitly.

## Context discipline
- Trust `structure.md` and `tech.md`; don't re-explore what they already describe.
- For lookups across many files ("where is X used?"), delegate to the `code-scout` subagent.
- Read only the functions you need (search or line ranges) for files over ~300 lines.
- Fix lint findings reported by the save hook before running tests.

## Done means
- Full suite green via `test-runner`, no lint findings on files you changed.
- If any `.ps1` or `.cmd` changed: the PowerShell syntax check from `tech.md` passed, and anything
  needing Outlook, DPAPI, Task Scheduler or real 5.1 runtime is labeled
  "needs target-machine verification" and added to the checklist in `HANDOFF.md`.
- Reply briefly: what changed, the RESULT line, next step. `/handoff` follows `handoff.md`.
