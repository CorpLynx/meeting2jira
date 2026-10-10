# Platform consolidation spec

Oct 6, 2026 · status: proposed · owner: Asgard core

Asgard's apps were built one at a time, so each carries its own copy of things every app needs: where files live, writing a JSON file safely, logging, finding Python, scheduling, HTTP, secrets, versions, diagnostics. The copies have started to drift, and two of the drifts are bugs today. This spec says how to find that duplication, what moves into Asgard's core (`asgard/`), what deliberately stays in the apps, and the order to do it in. Updates are the headline case: Asgard updates every app; no app updates itself.

## Principles

1. **Core owns the cross-cutting; apps own their domain.** Paths, files, logs, processes, scheduling, HTTP, secrets, versions, install and update, diagnostics and UI chrome live in `asgard/`. What counts as your commit, how an estimate rounds, what a SeCcHm field means, stay in the app.
2. **Move a thing to core when a second app needs it, or when correctness needs exactly one copy** (atomic writes, TLS, secrets, the schema). Not before: a core module with one caller is a guess about the second.
3. **One rule, one place, one test.** A consolidated module comes with the test that would have caught the drift, and a guardrail that stops a new copy appearing.
4. **Policies may differ; mechanisms may not.** Two apps can want different behaviour for a broken settings file (refuse vs. reset); they should share the code that reads it and pass the policy in.
5. **Never centralize what Odin depends on.** Odin must run with no Asgard present (its guardrail). Its Jira write path, DPAPI token, exit codes and PowerShell `Resolve-Python` copies stay Odin's. Its data folder is `%LOCALAPPDATA%\Asgard\odin` (Brandon, Oct 10), found the way `asgard.paths` finds Asgard's, with no import of Asgard. Core may offer equivalents that Odin adopts later by choice, never a dependency Odin must take.
6. **Heimdall's on-prem fork is out of scope.** The copy here is a pattern sample; nothing in core is shaped around it.

## How to interrogate the codebase

Run this before each release and whenever an app is added; it is how the inventory below was made.

1. **Walk the concerns.** For each row of the checklist, find every implementation:

| Concern | Search for |
| --- | --- |
| Where files live | `LOCALAPPDATA`, `ASGARD_HOME`, `Path.home()`, `paths.` |
| Writing files safely | `os.replace(`, `.tmp`, `NamedTemporaryFile`, `write_text(` |
| Logging | `log_dir()`, `.log`, `logging.getLogger`, `print(..., file=sys.stderr)` |
| Errors and exit codes | `sys.exit(`, `return 2`, `raise SystemExit`, `class .*Error` |
| Secrets | `CredRead`, `CredWrite`, `CryptProtectData`, `_TOKEN`, `keyring` |
| HTTP | `urllib.request`, `ssl.create_default_context`, `Authorization`, `ca_bundle`, `proxy` |
| Time | `strftime`, `astimezone`, `timezone.utc`, `parse_hhmm`, `tour` |
| Processes | `subprocess.`, `CREATE_NO_WINDOW`, `encoding=`, `errors=` |
| Scheduling | `schtasks`, `Register-ScheduledTask`, `sys.executable` in a command line |
| Finding Python | `Resolve-Python`, `py -3`, `pythonw`, `sys.executable` |
| UI chrome | `LIGHT`, `DARK`, `messagebox`, DPI, fonts |
| Versions and updates | `VERSION`, `__version__`, `"version":`, `SCHEMA =` |
| Install and uninstall | `install-ledger`, `USER_DATA`, `HKCU`, `.lnk` |
| Diagnostics | `preflight`, `check`, `status`, `doctor`, About box text |

2. **For each copy, record** where it is, how it differs from the others, whether the difference is intended (a policy) or drift, and what breaks if it stays.
3. **Classify:** consolidate, keep separate (say why, in the table), or delete.
4. **Check the claim with a test** where you can: a failing test for the drift (see "Two drifts that are bugs") is the strongest reason to consolidate.
5. **Update this document's inventory**, then the order of work.

## Inventory (Oct 6, 2026)

| # | Concern | Copies today | Verdict |
| --- | --- | --- | --- |
| 1 | Data folder | `asgard.paths.data_dir()` (ASGARD_HOME > `%LOCALAPPDATA%\Asgard`); the launcher exports `ASGARD_DATA` to apps, which nothing reads; Odin's `%LOCALAPPDATA%\Asgard\odin` (moved from `%LOCALAPPDATA%\meeting2jira`, Oct 10) | Apps use `asgard.paths` only; drop `ASGARD_DATA` or make it the one override children read. Odin computes the same folder itself (principle 5) |
| 2 | Atomic JSON writes | `baldur/settings.save`, `catalog._write_json`, Heimdall `templates.save`, the install ledger in `install.py` | Consolidate: `asgard.jsonfile`. None has the Windows `PermissionError` retry Muninn's backup needed in the lab |
| 3 | Reading a broken JSON file | The catalog raises with the line and column; Baldur returns defaults marked `broken` with a warning; the ledger readers fall back silently | Keep the policies, share the reader (principle 4) |
| 4 | Ledger loading | `install.load_ledger` (empty dict) and `valhalla.load_ledger` (None) | Consolidate into the lifecycle module |
| 5 | App logs | `logs\baldur.log` has two writers (`cli._log`, `window.log_problem`) besides the runner's per-app capture; the runner rotates at 1 MB, the others never | Consolidate: `asgard.applog` with one rotation rule and a lock-free append |
| 6 | Exit codes | Odin: 2 = config or credential error (a contract with its task). Baldur, Heimdall: 1 for everything | Asgard apps adopt Odin's meanings: 0 ok, 1 ran but found problems, 2 can't run, 130 interrupted |
| 7 | Secrets | Odin: DPAPI file + `JIRA_PAT`. Baldur: Credential Manager via ctypes + `BALDUR_GITHUB_TOKEN` | Consolidate Asgard's into `asgard.secrets` (Credential Manager; env override for tests); Odin keeps DPAPI |
| 8 | HTTP | Odin `jira.py`: retries, ambiguous-write handling, `ca_bundle`, proxy. Baldur `github.py`: ETags; no `ca_bundle`, proxy or retry | Consolidate: `asgard.http`. Bifrost, Loki and Mímir would otherwise each write a third and fourth |
| 9 | Time helpers | `muninn.db` (`to_ts`, `from_ts`), `muninn.odin.parse_time`, Baldur's local-day maths; tour of duty parsed in Odin and Baldur | Muninn's helpers are the core; Baldur's estimator keeps its own pure maths |
| 10 | Dead code | Odin `gitwork.py` (wired to nothing; Baldur superseded it) | Delete, with its tests, in Odin's next change |
| 11 | Child processes | `CREATE_NO_WINDOW` defined in `runner.py` and `gitread.py`; `_tolerant_output` in Baldur and Heimdall CLIs | Consolidate: `asgard.proc` (`run`, `NO_WINDOW`, `tolerant_output`) |
| 12 | Scheduling | Baldur's `schedule_command` bakes `sys.executable` into the task (a Python upgrade breaks it); Valhalla doesn't remove the task; `run_at_logon` and `commit_hook` settings are validated but unused | Consolidate: `asgard.scheduler`, one recorded task (see [integration/huginn.md](integration/huginn.md)); remove the unused settings |
| 13 | Finding Python | Four PowerShell `Resolve-Python` copies in Odin (a guardrail compares three); `setup-Asgard.cmd` and `baldur.cmd` try `py -3`, `py`, `python` | Asgard: one locator that reads the ledger's recorded Python. Odin's copies stay |
| 14 | Python floors | Odin 3.8, Asgard 3.9, Muninn effectively 3.11 on Windows (SQLite 3.37) | Setup must check Muninn's floor (`sqlite_problems()`), not just 3.9 |
| 15 | UI chrome | Baldur's window imports `LIGHT`/`DARK` from `launcher.py`, pulling in the whole launcher | Consolidate: `asgard.ui` (palette, fonts, DPI, dialogs, worker thread) |
| 16 | Versions | `VERSION` (Asgard), Odin `__version__ = "0.1.0"`, no VERSION file; `apps.json` `"version": 1` read by nothing; Baldur's `SCHEMA` hardcoded | The manifest (below) |
| 17 | Updates | None: no mechanism, no version comparison, no downgrade guard | [updates.md](updates.md) |
| 18 | Diagnostics | `asgard_preflight.ps1`, Odin's `Test-Environment.ps1`, Odin `check`/`status` (`last_run.json`), the launcher's About box, now `--muninn status/check` | Consolidate: `Asgard.pyw --doctor` gathering each app's checks |

### Two drifts that are bugs

- **Atomic writes on Windows (2).** Muninn's backup failed on the Windows lab VM when four processes replaced files at once (`PermissionError`, often an antivirus scan of a new file); it now retries for up to 2 s. The four JSON writers have no retry, so a settings save can fail the same way.
- **The scheduled task (12).** Baldur's weekly task runs a fixed `python.exe` path. Upgrading Python from the catalog leaves the task pointing at nothing, and uninstalling leaves the task behind.

## Target core modules

| Module | Replaces | API sketch | Size |
| --- | --- | --- | --- |
| `asgard.manifest` | `apps.json` entries, Baldur's `SCHEMA`, scattered versions | `load() -> List[AppManifest]`; per app: id, name, version, entry points (`window`, `cli`, `collect`), `muninn` (identity, `supported`), `settings` file, `user_data` paths, `scheduled` jobs, `requires` (Python floor, packages) | M |
| `asgard.lifecycle` | `install.py`/`valhalla.py` ledger code | `ledger()`, `record(item)`, `forget(item)`, `stage(payload) -> Staged`, `swap_in(staged)`, `user_data(app)` | M |
| `asgard.doctor` | the four diagnostics | `checks() -> List[Check]` from core plus each manifest's `doctor` entry; `Asgard.pyw --doctor [--json]` | S–M |
| `asgard.jsonfile` | item 2, 3 | `read(path, *, on_broken="raise"|"default"|"warn", default=...)`, `write_atomic(path, data)` with the Windows retry | S |
| `asgard.applog` | item 5 | `log(app, text)`, rotation at 1 MB, one `.1` kept, timestamps in local time, `scrub()` applied | S |
| `asgard.proc` | item 11 | `run(argv, *, timeout, no_window=True) -> Done`; `tolerant_output()` | S |
| `asgard.scheduler` | item 12 | `ensure(job)`, `remove(job)`, `list()`: per-user tasks via `schtasks`, command resolved at run time through the ledger's Python, every task recorded in the ledger | M |
| `asgard.http` | item 8 | `Client(base_url, auth, *, ca_bundle=None, proxy=None, retries=3)`; `get_json`, `post_json`; ETag cache; retry only idempotent calls; a non-idempotent call that times out raises `Ambiguous` so the caller uses the outgoing protocol | M |
| `asgard.secrets` | item 7 | `get(service, account)`, `put`, `delete`; Credential Manager; `ASGARD_SECRET_<SERVICE>` env override for tests and CI only | M |
| `asgard.ui` | item 15 | palette, fonts, DPI, `Worker` (thread + `after()` polling), standard error and confirm dialogs | M |
| `asgard.pylocate` | item 13, 14 | `recorded()` (ledger), `check_floor()` (including Muninn's SQLite needs) | S–M |

With the stdlib-only rule lifted ([dependency-policy.md](dependency-policy.md)), `asgard.http` may wrap `truststore` (pure Python; injects the Windows certificate store) and `asgard.secrets` may wrap `keyring` (pure Python on Windows). Both are optional: the stdlib implementations stay the fallback, so an app works where the packages aren't approved.

## Order of work

Ranked by risk removed per unit of effort; each step ships alone and leaves everything working.

1. **`asgard.manifest`** (M). Everything below reads it. Move Baldur's `SCHEMA` into its manifest entry; `open_app` callers read `supported` from it.
2. **`asgard.lifecycle`** (M). One ledger; scheduled tasks and every app's user data recorded, so Valhalla removes exactly what was installed.
3. **`asgard.doctor`** (S–M). One place to ask "why doesn't it work" before support calls.
4. **Retire Odin's `gitwork.py`** (S). Dead code with tests that still run.
5. **`asgard.pylocate`** (S–M). Setup refuses a Python Muninn can't use, instead of passing on 3.9 and failing later.
6. **`asgard.jsonfile` and `asgard.applog`** (S). Fixes the Windows replace race in four places.
7. **`asgard.scheduler`** (M). Fixes the stale task; Huginn builds on it.
8. **`asgard.http`** (M). Before Bifrost, Loki or Mímir write a third client.
9. **`asgard.secrets`** (M). Before a second Asgard app stores a token.
10. **`asgard.ui`** (M). Before Freya's window copies Baldur's.

## Guardrails that keep it consolidated

Add each with its module, as tests in `tests/test_platform.py`:

- No `os.replace(` outside `asgard/jsonfile.py`, `asgard/lifecycle.py` and `asgard/muninn/db.py`.
- No `CREATE_NO_WINDOW` or `schtasks` outside `asgard/proc.py` and `asgard/scheduler.py`.
- No app imports `asgard.launcher` (UI comes from `asgard.ui`).
- Every app in `apps.json` has a manifest with `muninn.supported` that includes `muninn.SCHEMA_VERSION`, or says why not.
- Every path an app writes under the data folder is in its manifest's `user_data`, and therefore in Valhalla's purge set.
- No `ssl` context without verification anywhere (as Odin's guardrail).

## Open decisions

- [ ] Exit codes: adopt Odin's meanings for every Asgard app (proposed), and Baldur's and Heimdall's 1-for-everything becomes 1 or 2.
- [ ] `ASGARD_DATA`: remove, or make it the single override child processes read (proposed: remove; `ASGARD_HOME` already exists for tests).
- [ ] Optional packages (`truststore`, `keyring`): request approval, or stay on the stdlib implementations.
- [ ] When Odin moves into Muninn, whether it adopts `asgard.http` and `asgard.secrets` (its choice; principle 5).
