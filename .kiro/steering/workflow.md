---
inclusion: always
---
# Workflow: edit -> test -> fix

## Running tests
- Never run pytest or `python -m unittest` directly; a hook blocks it. Use
  `python tools/run_tests.py [test_module | path::Class::test] [-k expr] [--lf] [-x] [--cov]` from the
  repo root. It runs pytest (config in `pyproject.toml`), prints a compact grouped summary, and keeps
  the full log in `.test-output/last-run.log`. Use its `--lf`, not pytest's: it also catches subTests.
- While fixing, run the narrowest scope: one module, `-k`, or `--lf`. `--changed` runs the tests
  that cover git-changed files (it always adds `test_guardrails` for changes under `Odin/app/src`).
- Full-suite runs (before saying you're done, or after a multi-file change): delegate to the
  `test-runner` subagent rather than running the full suite yourself.
- If you need more detail on one failure, rerun that test id with `--tb long --trace-lines 60`, or
  read only its section of the log. `--cov` adds coverage with the five least-covered modules.

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
