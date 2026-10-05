# Spec: discover Odin's GUI layer, then build its guardrails and steering

**Audience: an AI coding agent running on the on-prem workstation, with the Odin tree in front of
it.** You have no access to the conversation that produced this file. Everything you need is here.

**Why this exists.** The off-prem repo (`meeting2jira`) and the on-prem program (`Odin`) have
diverged. Odin was renamed and gained a Tkinter frontend that ties the `.cmd` and `.ps1` entry
points into a GUI. The off-prem repo's always-on steering still describes the pre-GUI layout, and
its guardrail tests scan paths that may no longer be where the code lives. Nobody off-prem knows
Odin's real shape, so **this spec does not tell you what the structure is — it tells you how to find
out and what to build once you know.**

**The single most important idea in this document:** the guardrail tests assert "no offenders
found". That assertion is **vacuously true against a directory that does not exist**. If the GUI
lives outside the paths those tests scan, every security rule in this project silently *passes* for
the GUI rather than failing. Not a gap you can see in a green test run. That is the primary thing
you are here to close.

---

## 0. How to run this

Two phases with a **mandatory human checkpoint** between them.

- **Phase 1 is read-only.** Discover and report. Write nothing, edit nothing.
- **Stop. Wait for a human to confirm or correct your findings.**
- **Phase 2 builds** the steering file and the guardrail tests, based on what Phase 1 actually found.

Do not collapse the phases. The structural question in Phase 1 (did Odin absorb `app/`, or wrap it?)
changes what correct output looks like in Phase 2, and it is a human's decision to confirm, not
yours to infer.

Usable either as a Kiro spec or pasted directly as a prompt.

---

## 1. Context you need (carried here so you do not have to guess)

### What the program does
A personal tool for one standard (non-admin) user on a locked-down federal Windows 11 workstation.
It reads **that user's own** Outlook calendar and creates one Jira **sub-task** per attended,
already-ended meeting under a configured parent issue. Optionally logs work equal to the meeting
length and transitions the sub-task. One-way: calendar → Jira. Jira **Data Center** only, bearer
token (PAT). The goal is approval by a security reviewer (ISSO).

### The non-negotiables the guardrails enforce
These are the project's existing rules. **You are extending their coverage to the GUI, not
renegotiating them.** If the GUI already violates one, report it — do not "fix" it by loosening a
test, and do not silently refactor the GUI either.

1. **No third-party Python packages in the shipped app** (runtime and shipped tests). The target
   machine has no pip access. Use `urllib`, `ssl`, `sqlite3`, `ctypes`, `json`, `csv` — and
   `tkinter`, which is stdlib and therefore fine.
2. **Never disable TLS verification.** No `CERT_NONE`, no `check_hostname=False`, no unverified
   context, and no config flag that enables any of those. Extra CAs go through a configured CA
   bundle path.
3. **HTTPS only** for Jira. `http://` allowed only for `127.0.0.1`/localhost in tests.
4. **No execution-policy bypass anywhere.** No `-ExecutionPolicy Bypass`, no `Set-ExecutionPolicy`.
5. **Constrained Language Mode (CLM) safety.** Every `.ps1` except the Outlook COM exporter must run
   under CLM: cmdlets, arrays, hashtables and methods on core types only. No `New-Object
   -ComObject`, no `Add-Type`, no `[Type]::Member` statics. Only the COM exporter may require
   FullLanguage.
6. **Outlook data minimization.** The exporter must not read guarded or sensitive properties
   (`Body`, `RequiredAttendees`, `OptionalAttendees`, `Recipients`, …). Reading them triggers
   Outlook's security prompt and widens the data handled. `Organizer` only behind an explicit opt-in.
7. **No admin rights** for any step. Per-user data lives in `%LOCALAPPDATA%\meeting2jira` (or Odin's
   equivalent — discover it), never inside the program folder.
8. **Secrets**: the Jira PAT is stored only via DPAPI. Never logged, printed, or written in plain
   text.
9. **No EWS.** Microsoft Graph is the only acceptable future network source.

### Product rules the GUI can break in ways tests would not notice
- **`--dry-run` / preview must always show exactly what would happen.** If the GUI cannot reach a
  preview, the user loses the only safe way to check before writing to Jira.
- **Never silently drop real work.** Time actually spent must not vanish due to a classification
  edge case.
- **Only meetings that have ended.** Never future meetings.
- **Conservative by default**: skip private, declined, cancelled, all-day and free items; cap creates
  per run.
- **Dedupe, not the scan window, is what prevents duplicates.** Widening the window is always safe.

### Conventions
- Python **3.8**-compatible: no `match`, no runtime `X | Y` unions, no `zoneinfo`, no
  `str.removeprefix`. Use `from __future__ import annotations` and `typing.List`/`Optional`.
- **Windows PowerShell 5.1** is the baseline, not PowerShell 7.
- Tests are stdlib `unittest` (so they run with no pip on the workstation).
- Errors raise a typed exception with a message that says **what to do**, mapped to exit code 2.
- Exit codes: `0` ok, `1` per-item errors or a failed check, `2` config/usage/credential error,
  `130` interrupted.

---

## 2. Phase 1 — Discovery (read-only)

Answer every question below from the actual tree. **Where you cannot determine something, write
`UNKNOWN` and say what you searched.** Do not infer, do not fill gaps with what seems likely. A
wrong "probably" here produces guardrails that test the wrong paths and pass vacuously, which is the
exact failure this spec exists to prevent.

### 2.1 Layout and the decisive structural question
1. Print the directory tree, excluding `.git`, `__pycache__`, `node_modules`, `.venv`, test output.
2. **Is there still a self-contained deliverable folder** (the pre-GUI layout called it `app/`, with
   `src/meeting2jira/` and `src/windows/`)? Which of these is true?
   - **(A) Wrapper** — the old deliverable survives intact and the GUI sits beside/above it, calling
     its documented entry points.
   - **(B) Absorbed** — the GUI moved inside the deliverable folder, or the folder was renamed and
     restructured around the GUI.
   - **(C) Fork** — the GUI calls a *copy* of the logic, so there are now two divergent copies.
   State which, with the paths that prove it. **(C) is the serious one: say so prominently.**
3. Where does the Python package that `python -m <name>` resolves to actually live, and what is it
   called now? Is `PYTHONPATH` still set to a `src` directory by the entry points?
4. Does anything in the deliverable folder reference paths *outside* itself (`..`, the repo root)?
   The folder is supposed to be copyable on its own.

### 2.2 The GUI itself
5. Every file that is part of the GUI, with line counts.
6. The GUI entry point(s): what the user double-clicks or runs.
7. **Every place the GUI invokes an external process.** For each: the file:line, which executable
   (`powershell.exe`, `cmd.exe`, `python.exe`, the `.cmd` dispatcher), and the exact call form —
   `subprocess.run([...])` with an argument list, or a single interpolated string, or `shell=True`.
   Quote the lines. This is the highest-value part of the discovery; be exhaustive.
8. Which values reaching those calls originate from **user input or GUI state** (day counts, file
   paths, issue keys, free text)? Trace each to its widget.
9. Does the GUI import anything outside the standard library? List every non-stdlib import.
10. Does the GUI read, display, hold in a widget, or write to disk any of: the Jira PAT, meeting
    bodies, attendee lists, or full meeting subjects for *private* meetings?
11. Does the GUI write anything inside the program folder (logs, config, state, exports) rather than
    the per-user data directory?
12. Can the user reach a **preview / dry-run** from the GUI without editing config by hand? Quote the
    code path.
13. Does the GUI elevate, or offer to (`runas`, `-Verb RunAs`, a UAC prompt, a manifest)?

### 2.3 Existing tests and what they actually cover
14. Locate the guardrail test file (previously `app/tests/test_guardrails.py`). Print **the path
    constants and glob patterns it uses to decide which files to scan**, verbatim.
15. For each GUI file from §2.2, state **yes/no: is it inside a path that file scans?** A table.
    This is the vacuous-pass audit, and it is the core deliverable of Phase 1.
16. Is there a wiring test that asserts the scanned paths **resolve to real files** (previously
    `GuardrailWiringTests`)? If yes, does it still pass, and does it cover the GUI paths?
17. How are tests run now? Is there a test runner script, a `pyproject.toml`, a pytest config? Do
    the tests still run with plain `python -m unittest` and no pip?
18. Run the full test suite and the PowerShell syntax check. Report the actual commands and results.

### 2.4 Environment
19. `python --version` (and how Python is located by the entry points — there was previously a
    multi-strategy discovery routine that *executes* each candidate and requires 3.8+; is it still
    there, and is the GUI using it or hardcoding an interpreter?).
20. Is `tkinter` importable? (`python -c "import tkinter; print(tkinter.TkVersion)"`)
21. `$ExecutionContext.SessionState.LanguageMode`, and the current execution policy.
22. Is the CLI surface — commands, flags, exit codes — still backward compatible? Scheduled tasks
    depend on it. List any command or flag that was renamed or removed.

### 2.5 Report format

Produce exactly this, then **STOP**:

```
ODIN DISCOVERY REPORT
=====================
STRUCTURE: A-wrapper | B-absorbed | C-fork     (+ the paths that prove it)
DELIVERABLE FOLDER: <path, or "none — GUI and logic are merged">
PACKAGE: <import name> at <path>;  PYTHONPATH handling: <...>
GUI ENTRY POINT(S): <paths>
GUI FILES: <path (lines)>, ...

SUBPROCESS CALLS (the injection and policy surface)
  <file:line>  <exe>  <call form: arg-list | interpolated string | shell=True>
  ...          user-controlled values: <which, from which widget>

NON-STDLIB IMPORTS IN GUI: <list, or "none">
SECRETS / SENSITIVE DATA IN GUI: <findings, or "none found">
WRITES INSIDE PROGRAM FOLDER: <findings, or "none">
PREVIEW/DRY-RUN REACHABLE FROM GUI: yes/no  <code path>
ELEVATION: <findings, or "none">

GUARDRAIL COVERAGE AUDIT
  scanned paths in <guardrail test path>: <verbatim constants>
  | GUI file | inside a scanned path? |
  |---|---|
  VACUOUS-PASS RISK: <which rules currently do not apply to the GUI at all>
  WIRING TEST PRESENT: yes/no; covers GUI paths: yes/no

TESTS: <command> -> <result>
PS SYNTAX CHECK: <command> -> <result>
ENVIRONMENT: python <ver>; tkinter <ver|missing>; LanguageMode <...>; ExecutionPolicy <...>
CLI COMPATIBILITY: <preserved | these changed: ...>

UNKNOWNS: <everything you could not determine, and what you searched>
RULE VIOLATIONS ALREADY PRESENT: <list, or "none found">  (report only; do not fix yet)
```

**Then stop and wait.** Do not proceed to Phase 2 unsolicited.

---

## 3. Phase 2 — Build (only after a human confirms the report)

### 3.1 The steering file

Create a steering file for the GUI layer — `gui.md` alongside the other steering files.

Front matter, with **patterns derived from your discovery**, not from this spec:

```yaml
---
inclusion: fileMatch
fileMatchPattern: ["<the actual GUI paths you found>"]
---
```

An array of globs is valid and is the documented way to match several paths from one steering file.
Front matter must be the very first content in the file, with nothing above it.

Why `fileMatch` and not a separate agent: steering **composes**. A GUI file that shells out to
PowerShell needs the GUI rules *and* the PowerShell/CLM rules at the same time, and overlapping
`fileMatchPattern`s give you both. A per-layer agent gives you whichever one was invoked.

The file must answer these questions **for Odin as it actually is**. Keep it short and specific;
every line should change a decision. No generic Tkinter tutorial content.

1. **Where the seam is.** What belongs in the GUI versus what must stay in the Python logic or the
   PowerShell layer. State plainly that the GUI is a *frontend*: no filtering, routing, dedupe or
   Jira logic in it. If discovery found logic that leaked into the GUI, say where and that it should
   move back.
2. **How the GUI is allowed to invoke things**, written as a rule with the reason attached — see
   §3.3 items 1–3.
3. **Threading rule.** Tkinter's event loop is single-threaded: a long call (export, Jira push) on
   the UI thread freezes the window, and a worker thread touching widgets directly corrupts state.
   Document the pattern Odin actually uses (worker thread plus a queue drained by `after()`, or
   whatever discovery found) and require new work to follow it.
4. **What the GUI must never display or persist**: the PAT, meeting bodies, attendee lists, private
   meeting subjects. Note that "shown in a widget" and "written to a log" are both leaks.
5. **Preview is not optional.** Dry-run must stay reachable from the UI, because it is the only way
   the user can check before anything is written to Jira.
6. **Verification is different here.** There is no useful headless assertion over a widget tree, so
   GUI changes need a human to look at them. Anything touching Outlook COM, DPAPI, Task Scheduler or
   real PS 5.1 runtime behaviour must be labelled **"needs target-machine verification"** and added
   to the handoff checklist unless it was actually run on this machine. State which parts of the GUI
   *can* be tested headlessly (pure functions: command construction, argument quoting, output
   parsing, state mapping) and require those to be unit-tested — see §3.3 item 11.
7. **Python 3.8 and stdlib only**, same as the rest of the shipped code. `tkinter`/`ttk` are fine.
8. **Where runtime data goes** — the per-user directory, never the program folder, so the folder can
   be replaced wholesale on upgrade.

### 3.2 Fix the vacuous-pass gap first

Before adding any new check, make the existing ones actually cover the GUI:

- Extend the guardrail test's scanned paths to include every GUI file and any new `.ps1`.
- Extend or add the **wiring test** that asserts each scanned path resolves to a real file, and that
  each glob matches at least one file. Then deliberately break a path and confirm the wiring test
  fails. A guardrail suite that cannot detect its own misconfiguration is decoration.
- Re-run. **Expect new failures.** Rules 1–8 have never been applied to this code. Report each one;
  fix the code, not the test.

### 3.3 The checks to implement

These derive from the non-negotiables, so they are valid regardless of Odin's shape. Implement each
as a static check over the discovered paths, in the existing guardrail test's style. For each,
**write the check, then prove it works by injecting a violation and confirming it is caught, then
remove the injection.** A guardrail that has never failed has not been tested.

1. **No execution-policy bypass in any GUI launch path.** Detect `-ExecutionPolicy` with `Bypass`/
   `Unrestricted`, and `Set-ExecutionPolicy`, including split across arguments in a list and
   assembled from variables where statically visible. *Why: a frontend launching `powershell.exe` is
   the most tempting place to "just make it run", and it is an explicit non-negotiable.*
2. **No shell interpretation.** Detect `shell=True`, `os.system`, `os.popen`, and
   `subprocess.*` calls whose first argument is a single f-string/concatenation/`%`/`.format()`
   rather than a list. *Why: every user-supplied value — a path, a day count — becomes injectable,
   and this is a GUI, so those values come from text boxes.*
3. **Argument lists, never hand-quoted strings.** Where a command is built, require a list of
   arguments. *Why beyond injection: PS 5.1 mangles embedded double quotes in native-command
   arguments, and paths under `C:\Program Files\...` break on the space. This project has already
   been bitten by both.*
4. **No elevation.** Detect `runas`, `-Verb RunAs`, `ShellExecute` with elevation, UAC manifests.
   *Why: non-negotiable 7 — the tool must work for a standard user, and needing admin fails review.*
5. **No non-stdlib imports** in the shipped GUI. Reuse the existing stdlib-only check if there is
   one; extend its scanned paths rather than writing a second one.
6. **TLS verification never disabled** — extend the existing check's paths to the GUI.
7. **HTTPS only** for Jira URLs — extend the existing check's paths to the GUI.
8. **No guarded Outlook properties** read from the GUI or any new `.ps1`: `Body`,
   `RequiredAttendees`, `OptionalAttendees`, `Recipients`, and the rest of the guarded set.
9. **CLM safety for every new `.ps1`** the GUI calls, except the COM exporter. Reuse the existing
   pattern check — it catches `New-Object`, `Add-Type` and any `[Type]::Member`, while allowing
   provider paths like `Registry::`.
10. **No secret in plaintext anywhere in the GUI**: no PAT written to a log, a config file, a widget
    default, or a window title. Also check the token is never placed in a non-password entry widget
    (`show="*"` or equivalent) and never stored in a plain attribute that gets serialized.
11. **Command construction is unit-tested.** Require pure functions for building the argument list,
    quoting paths and parsing exit codes, with stdlib `unittest` tests covering: a path containing
    spaces, a path containing a double quote, a non-numeric day count, and a non-zero exit code
    surfacing to the UI. *Why: this is the GUI logic that can be tested without a display, and it is
    exactly where the injection and quoting bugs live.*
12. **Exit codes preserved.** The GUI must distinguish `0`/`1`/`2`/`130` and not collapse them into
    "it failed". *Why: `1` means some items failed and others succeeded — telling the user "failed"
    when sub-tasks were created invites a duplicate-creating re-run.*
13. **No writes inside the program folder** from GUI code paths.
14. **If discovery found structure (C) — a forked copy of the logic** — add a drift guardrail that
    fails when the copies diverge, in the style this repo already uses for duplicated code
    (normalize the permitted per-copy differences, compare the rest, and emit a unified diff in the
    failure message). Flag loudly in your report that two copies is a standing liability and should
    be collapsed.

### 3.4 Update the documents that now lie

- The always-on structure steering: correct the layout, name the GUI layer, say where runtime data
  lives, and record which folder is the copyable deliverable.
- The product steering: the program is now called Odin; note the GUI is the primary interface and
  that the CLI remains the contract scheduled tasks depend on.
- The handoff/status document: add a GUI row, and add the GUI items that **need target-machine
  verification** to its checklist.
- If the CLI surface changed (§2.4 q22), that is a **contract break** — scheduled tasks and the
  documented commands depend on it. Document each change explicitly and flag it for a decision
  rather than quietly accepting it.

---

## 4. Verification gate — all of it, before you claim done

1. Full test suite green, using whatever runner discovery found.
2. **Every new guardrail mutation-tested**: inject a violation, confirm that specific test fails,
   remove the injection, confirm green again. Report the list of mutations and that each was caught.
3. The wiring test proven to fail when a scanned path is wrong.
4. PowerShell syntax check passes under Windows PowerShell **5.1** (preferred over 7).
5. Lint, if the repo has it configured, clean on files you changed.
6. The GUI still launches, and preview still reaches a dry-run.
7. Nothing in the deliverable folder reaches outside itself.
8. **Do not commit** unless a human asks. Leave the work in the tree for review.

---

## 5. Anti-goals — do not do these

- **Do not weaken an existing guardrail to get green.** If a rule now fails because it finally
  covers the GUI, that is the guardrail working. Fix the code or report it.
- **Do not refactor Odin.** You are adding guardrails and steering, not redesigning the GUI. If the
  seam is in the wrong place, say so; don't move it.
- **Do not invent structure.** Everything in Phase 2 derives from Phase 1 findings. `UNKNOWN` is an
  acceptable answer; a confident guess is not.
- **Do not add a pip dependency** to the shipped program, and do not add a GUI test framework.
- **Do not create one agent per language or layer.** Steering with overlapping `fileMatchPattern`s is
  the right mechanism, because a GUI file that shells out to PowerShell needs both rule sets at once.
- **Do not write a Tkinter tutorial.** Steering is for decisions specific to this codebase.

---

## 6. What to send back off-prem

The two trees have diverged more than once, and each time the off-prem repo has had to reconstruct
on-prem changes from a verbal description. Close that loop. Produce:

1. The `ODIN DISCOVERY REPORT` from Phase 1, as-is.
2. The new steering file and the guardrail diff.
3. The mutation-test results from §4.2.
4. **A divergence list**: everything on-prem that off-prem does not have — the rename, the GUI, any
   fix made directly on this machine and never pushed back. For each: is it in version control here?
5. Anything still needing target-machine verification, even though you are *on* the target machine —
   e.g. a check that needs Outlook running, a scheduled task under a real interactive logon, or a
   DPAPI round trip under the actual user.

Best outcome: push a branch from this machine so the off-prem repo gets the real tree instead of a
description of it. Until that happens, the always-on steering off-prem describes a program that no
longer exists, and an agent reading it will make confident, wrong edits.
