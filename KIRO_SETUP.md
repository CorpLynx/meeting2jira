# Kiro setup: steering, guardrails and token-saving tooling

This repo is set up so the main model (Opus) does the thinking and editing, while test runs, test
output and code lookups are handled cheaply: by a deterministic script, by hooks that cost no
credits, or by subagents on cheaper models.

It started from a generic Python + PowerShell scaffold and was adapted to this project:

- **Tests are stdlib `unittest`, not pytest.** The runner drives unittest directly, so nothing new
  has to be installed, including on the no-pip workstation.
- **PowerShell calls Python here, not the other way round.** That's why there is no
  `fake_powershell` fixture. PowerShell runtime behavior is verified by `app/tools/Invoke-WindowsChecks.ps1`, not by mocks.
- **Nothing in this file set lives in `app/`.** The deliverable stays self-contained and stdlib-only.

## What's in the box

```
.kiro/
  steering/
    product.md  tech.md  structure.md   always   project rules (scope, non-negotiables, contracts)
    workflow.md          always   edit -> test -> fix loop; where the token savings come from
    powershell.md        .ps1     PowerShell 5.1 / CLM rules
    python-testing.md    tests    unittest conventions, fixtures, what can't be unit-tested
    debug-playbook.md    auto     known gotchas; loads when the agent is stuck (also #debug-playbook)
    handoff.md           manual   /handoff writes .kiro/session-handoff.md for a fresh session
  agents/
    test-runner.md       Haiku    runs tests, returns grouped failures; read-only
    code-scout.md        Sonnet   "where is X used?" lookups, returns file:line; read-only
  hooks/
    lint-on-save.json      ruff (or a stdlib syntax check) on saved .py; PS parse check on saved .ps1
    guard-raw-tests.json   blocks raw `python -m unittest` / pytest so verbose output never floods context
    session-context.json   new session gets branch, changed files, last test result, handoff note
    format-on-stop.json    OFF; leave it off (the code isn't ruff-formatted)
tools/
  run_tests.py           compact unittest runner; full log in .test-output/last-run.log
  hooks/*.py             scripts the hooks call (stdlib only)
.kiroignore              keeps caches, runtime data, exports, tokens and Terraform state away from the agent
ruff.toml                dev lint rules (bug-finding only), Python 3.8 target
```

Root `tools/` is Kiro tooling. `app/tools/` is part of the shipped program. Don't mix them up.

## How the loop runs

1. **Opus** reads the always-on steering and edits code.
2. The **save hook** reports lint and syntax errors right away. A clean save adds nothing to context.
3. While iterating, Opus runs a narrow scope itself, e.g. `python tools/run_tests.py test_state -k worklog` or `--lf`.
   The runner prints about 5-30 lines, with failures grouped by root cause. For example, three
   failing `subTest` cases from one bug show as one group.
4. For **full-suite runs**, Opus delegates to the **test-runner** subagent (Haiku).
5. If Opus runs `python -m unittest` directly anyway, the **guard hook** blocks it and points it at the runner.

What a failing run looks like to the model (illustrative):

```
RESULT: FAIL | 7 passed, 3 failed, 0 errors, 0 skipped | 0.1s
cmd: run_tests.py test_tour_of_duty
full log: .test-output/last-run.log

[1] 3 tests | AssertionError: 'outside' != 'partial'
    at app/tests/test_tour_of_duty.py:66
    test_tour_of_duty.ClassificationTests.test_inside_partial_and_outside (case=1)
    ...
    trace (...):
      <the last few traceback lines>
```

The compact runner and the guard hook do most of the saving on their own. A subagent carries its
own overhead, so it pays off mainly on full-suite runs and messy failures, not on reruns of a
single module.

## One-time setup

1. **Python on PATH.** The hooks, the agent and `workflow.md` call `python`. On a Windows machine
   where only the `py` launcher exists, replace `python` with `py -3` in `.kiro/hooks/*.json`,
   `.kiro/agents/test-runner.md` and `.kiro/steering/workflow.md`.
2. **Optional dev tools** (not needed on the workstation):
   - `python -m pip install ruff` for full linting. Without it, the save hook falls back to a
     syntax check.
   - `Install-Module PSScriptAnalyzer -Scope CurrentUser` for `.ps1` analysis on top of the parse check.
3. **Turn on `.kiroignore`.** In Settings, search "Agent Ignore Files"
   (`kiroAgent.agentIgnoreFiles`) and add `.kiroignore`. Without this step the file is ignored.
4. **Check the model IDs.** Type `/model` in Kiro chat and confirm `claude-haiku-4.5` and
   `claude-sonnet-4.6` exist. If they don't, edit `.kiro/agents/*.md`. **A wrong ID silently falls
   back to the default model, which may be Opus, and you lose the savings.**
5. Select **Opus** in the chat model picker for your normal work.

## Check that it works

1. Run `python tools/run_tests.py` yourself. You should get `RESULT: PASS | 80 passed ...`, and the
   full log in `.test-output/`.
2. Ask Kiro to "run the unit tests with python -m unittest". The guard should block it, and Kiro
   should switch to `tools/run_tests.py`.
3. Ask Kiro to "use the test-runner subagent to run the full suite". Check that the subagent shows
   the Haiku model. Approve the subagent the first time so it doesn't block later runs.
4. Have Kiro add an unused import to a file. The save hook should report `F401` (or nothing,
   without ruff).
5. Start a new session. The first context should include `[session context]` with your branch and
   the last test result.

If a hook doesn't fire, open the **Agent Hooks** panel to confirm it loaded. The trigger names
follow the IDE 1.x / CLI 3.x hook format; older Kiro versions use a different format.

## Token-saving habits

- **Start fresh sessions often.** Every turn re-sends the whole conversation. When a task wraps up,
  type `/handoff` and open a new session; the session hook picks up the note.
  `.kiro/session-handoff.md` is a scratch note. The project's status, backlog and checklist stay in `HANDOFF.md`.
- **Narrow test runs.** Use one module, `-k`, or `--lf` while fixing. Run the full suite once at
  the end, via test-runner. `--changed` runs only the tests that cover your changes.
- **Point at the target.** "Make `test_tour_of_duty.ClassificationTests.test_grace_of_zero_is_strict`
  pass; the bug is in `TourOfDuty.classify()` in `rules.py`" is far cheaper than "fix the failing tests".
- **Use Kiro specs** for backlog items (HANDOFF.md section 6). One planned pass beats several blind
  fix-and-retry loops.
- **Model choice.** Routine fixes, renames, test writing and doc updates usually don't need Opus;
  switch to Sonnet (or Auto) for those.
- **Keep always-on steering short.** It is sent on every turn. `product`, `tech`, `structure` and
  `workflow` are about 225 lines together, above the scaffold's ~150-line guideline, mostly because
  `structure.md` documents contracts. Move detail into `fileMatch`/`auto` files before adding more.
- Kiro CLI ignores inclusion modes and loads every steering file, so keep the conditional files short too.
