# Asgard

Asgard is a launcher for the Asgard suite: one window with a tile for each app, like the Microsoft My Apps portal. It runs on the Python your agency already provides, or as a packaged build that brings its own. It installs per user, so you don't need admin rights.

There are two ways to install it. Both hold the same apps, keep your data in `%LOCALAPPDATA%\Asgard`, and are uninstalled the same way:

- **With Python** (below): the zip of the code, on your agency's Python. It needs no programs approved, so it's the default.
- **The packaged build** (programs made with PyInstaller): it needs no Python, but IT has to allow its programs. See [Install the packaged build](#install-the-packaged-build).

## Install

1. Download the zip: **Code > Download ZIP** on the GitHub page, or the **Source code (zip)** of a release.
2. **Unblock it before extracting.** Right-click the zip, choose **Properties**, tick **Unblock**, then **OK**. This stops Windows from warning about every extracted file.
3. Right-click the zip and choose **Extract All**.
4. Open the extracted folder and double-click **setup-Asgard.cmd**.

Setup copies Asgard to `%LOCALAPPDATA%\Asgard\app`, adds an **Asgard** shortcut to the Start menu, lists Asgard in **Settings > Apps**, and opens it. To keep it handy, right-click Asgard in the Start menu and choose **Pin to taskbar**.

Setup options: `setup-Asgard.cmd --desktop` also adds a desktop shortcut, and `--no-launch` skips opening Asgard at the end.

### If setup is blocked

Some agency policies block `.cmd` files, and Windows then says the app "has been blocked by your system administrator". Run the same installer through Python instead. Open Command Prompt or PowerShell in the extracted folder and run:

```
py -3 asgard\install.py
```

If `py` isn't found, use `python asgard\install.py`.

You need Python 3.9 or newer with Tcl/Tk (tkinter); Muninn needs 3.11 or newer on Windows, for its SQLite. Most catalog Python installs include Tcl/Tk. `tools\asgard_preflight.ps1` checks this, along with the policies that affect the other apps.

### Install the packaged build

The packaged build is a folder with `Asgard.exe`, `asgard-cli.exe`, Python 3.12 and the packages the apps use (the shared window's Qt and Ysildir's MCP SDK). It comes as `Asgard-VERSION-windows-x64.zip` from a GitHub release, or from the **Package Asgard** workflow's artifact.

1. **Check the download.** Compare `certutil -hashfile Asgard-0.4.0-windows-x64.zip SHA256` with the `.sha256` file beside the zip.
2. **Unblock it and extract it** into a folder your policy allows programs to run from. Ask IT which; they may put it in place for you. It runs where it is: setup doesn't copy it, and it never writes into its own folder, so a read-only folder is fine.
3. **Check it.** Open Command Prompt in the folder and run `asgard-cli.exe --self-test`. Each line should say `[ok]`. A blocked DLL or program means App Control needs to allow this folder's files; show IT the output.
4. **Double-click `setup-Asgard.cmd`** in the folder. If `.cmd` files are blocked, run `asgard-cli.exe asgard\install.py` instead. Setup adds the Start menu shortcut and the **Settings > Apps** entry, and opens Asgard.

Keep the folder where it is: the shortcut points to it. To upgrade, close Asgard, put the new folder in place of the old one, and run setup again. Heimdall's `fill` isn't in the packaged build, because it needs Playwright's own programs; `fill --dry-run` still lists every value to type. [docs/packaging.md](docs/packaging.md) has the details.

## Using Asgard

- **Click a tile** to open the app. Tiles marked **Coming soon** aren't built yet.
- **Odin** lives outside Asgard, so its tile says **Set up** the first time. Click it and pick the file you normally start Odin with (`.pyw`, `.py`, `.exe`, `.ps1`, `.cmd` or a shortcut). Asgard remembers the choice. If that file's folder, or the folder above it, holds a virtual environment (`.venv`, `venv` or `env`), Asgard runs Odin with that environment's Python.
- **Right-click a tile** to change its location, open its folder, or view its log.
- **Type to search.** Arrow keys move between tiles, Enter opens one, and F5 reloads your tiles.
- If an app closes right after starting, Asgard shows the last lines of its log. Logs are in `%LOCALAPPDATA%\Asgard\logs`.
- **Counts on tiles** come from Muninn, the database the apps share: Odin's tile counts worklogs waiting to post, Baldur's counts reviews requested of you. Point at a tile, or move to it with the arrow keys, to see what the count means. Counts refresh every minute and when you come back to Asgard.

## Baldur from the command line

Baldur's window opens from its tile. Everything Baldur does also works from Command Prompt. In the packaged build, `cd` to its `apps\baldur` folder instead; `baldur.cmd` uses the build's own `asgard-cli.exe`, and every command below works the same:

```
cd /d "%LOCALAPPDATA%\Asgard\app\apps\baldur"
baldur.cmd setup --from-git --project PROJ --root C:\src
baldur.cmd collect
baldur.cmd estimate
baldur.cmd days
baldur.cmd report 2026-10-01
baldur.cmd approve --date 2026-10-01
```

- `setup` records your git email in Muninn (only your email decides which commits are yours), the Jira projects you work in, and the folders your repositories are in. Run it alone to see what's still missing. If you commit with more than one email (one set per folder with `includeIf`, say), add each with `--email`.
- `collect` reads your commits and each repository's reflog into Muninn: metadata only, never code. Git deletes reflog entries after 90 days, or 30 for commits no longer on any branch (such as ones a rebase replaced), so collect at least weekly; `baldur.cmd schedule` adds a weekly task for you, with no admin rights.
- `estimate` turns the last 14 days into one proposal per ticket per day. `report` shows a day with the basis of every number. Nothing reaches Jira until you `approve` it, and only Odin posts.
- `keys SHA PROJ-123` gives a commit a Jira key by hand, and `repos --off NAME` leaves a repository out of estimates.
- `setup --set github_api=github.agency.gov` names your GitHub Enterprise Server (the host is enough; `github.com` works too). Only remotes on that host count as GitHub repositories, and the squash commits it writes are skipped as copies. Pull request alerts aren't built yet.

### Calibrating the estimate

Baldur's settings start as guesses. To measure them, note your real hours during a trial of two
to four weeks:

```
baldur.cmd actual 2026-10-01 6h15m
baldur.cmd actual 2026-10-01 1h45m --key PROJ-42
baldur.cmd actual 2026-10-01 --remove
baldur.cmd actuals
baldur.cmd calibrate
baldur.cmd calibrate --accept
```

- `actual 2026-10-01 6h15m` records a day's total. Add `--key PROJ-42` to record one ticket's time
  instead, and `--remove` to take a note back.
- `actuals` lists what you noted.
- `calibrate` searches three settings for the lowest daily error against your notes:
  - the idle gap, from 60 to 180 minutes;
  - the lead-in, from 0 to 60 minutes;
  - the ambient weight, from 0.3 to 0.8.

  It only picks settings that estimate low on average, then shows the old and new error side by
  side.
- `calibrate --accept` writes the fit into `baldur.json`. Nothing changes until you accept, and
  past estimates keep their settings.

### AI-assisted figures

Baldur has two ways to estimate. The manual engine above uses only git, your calendar and your
settings. The AI-assisted method puts suggested figures beside the engine's numbers, from two
sources.

**An AI coding agent's estimate.** After a change made with an agent (Kiro, Copilot, Claude Code),
the agent records its estimate of your working time on it:

```
baldur.cmd ai record --agent kiro --minutes 1h --low 45m --confidence medium --commit <SHA> --key PROJ-42 --summary "One sentence."
baldur.cmd ai list
baldur.cmd ai withdraw r12
```

To teach Kiro to do this in a repository, run `baldur.cmd ai kiro --into C:\src\my-service`. That
writes a steering file and two hooks into the repository's `.kiro` folder. One hook blocks the
agent from approving, rejecting or changing your time. `baldur.cmd ai guide` prints the full
guide for any agent.

**An AI review of the day.** Paste the day's evidence into your approved AI chat, then give
Baldur its answer:

```
baldur.cmd ai pack 2026-10-01 --out pack.txt
baldur.cmd ai review 2026-10-01 answer.json
```

The review needs `setup --set review_mode=metadata` first. That sends commit subjects, times, line
counts and keys to the chat, never code.

**Seeing and taking the figures.**

```
baldur.cmd ai show 2026-10-01
baldur.cmd approve --date 2026-10-01 --ai 3f2a9c1d
```

- `ai show` lists the suggestions, what Odin's worklog comment will say for each, and the command
  that takes them. `report` shows them under the day.
- `approve --date ... --ai ID` takes them. The ID names exactly the figures you saw: if a new agent
  report or review changes them first, nothing is approved and Baldur shows the new ones. Add
  `--set PROJ-42=1h` to give your own figure for a ticket; for a ticket the AI raised, give your own
  figure for the ticket it took the time from too, or approve without `--ai`.

Every suggestion is checked in code. A suggestion never raises a day, adds a ticket, or goes
without cited evidence. An agent's estimate can move time between tickets or lower it, never add
time git doesn't show, and time Jira already holds for a ticket never moves (Odin never takes time
back out of Jira). Nothing changes until you approve. When you take a figure, Odin's worklog
comment says so, in exactly the words `ai show` showed you.

`baldur.cmd` tries `py -3`, then `py`, then `python`. If your computer blocks `.cmd` files, run `py -3 cli.py` (or `python cli.py`) from the same folder instead. Settings are in `%LOCALAPPDATA%\Asgard\settings\baldur.json`; the Baldur spec explains each one.

## Ysildir: Asgard for your AI client

Ysildir is an MCP server: an AI client (Kiro, Copilot agent mode in VS Code, Claude Code) starts it and gets tools that teach it Baldur and Muninn, take its estimates of your time into Baldur, and answer questions from Muninn. It never approves, changes or posts anything; it gives you the command instead. It needs Python 3.10+ and the MCP SDK (`mcp` and `pydantic` from `requirements.txt`, with IT's approval of their compiled parts). Without them, `ysildir.cmd` says what's missing, and agents still use `baldur.cmd ai record` and the clipboard review. The packaged build has both already: `ysildir.cmd setup` there points your AI client at the build's `asgard-cli.exe`.

```
ysildir.cmd setup --kiro C:\src\my-service     connect Kiro in that workspace (--vscode DIR, --claude DIR,
                                               --kiro-user, --vscode-user; --print shows it first)
ysildir.cmd check                              what an agent will see: tools, switches, Muninn's version
ysildir.cmd tools                              each tool, on or off, and what it sends to the AI client
ysildir.cmd tools --on baldur_day              turn a tool on, then restart the MCP server in your client
```

Each tool's data flow needs your ISSO's approval, so each has a switch in `%LOCALAPPDATA%\Asgard\settings\ysildir.json`. Tools that send Muninn data (commit subjects, Jira summaries, times) start off. [docs/integration/ysildir.md](docs/integration/ysildir.md) lists every tool, what it sends and its command-line equivalent.

## Heimdall from the command line

Heimdall fills a SeCcHm request in Edge from a saved template, then stops so you can review it and click **Submit** yourself. Its tile opens Heimdall's window (see below), which does everything these commands do; the commands also work on their own:

```
cd /d "%LOCALAPPDATA%\Asgard\app\apps\heimdall"
heimdall.cmd init
heimdall.cmd fields
heimdall.cmd template save "Monthly scan" --set "Short description=Monthly scan $today" --set "Category=Hardware" --attachment C:\Reports\scan.xlsx
heimdall.cmd fill -t "Monthly scan" --dry-run
heimdall.cmd fill -t "Monthly scan"
```

- `init` creates the form file, `%LOCALAPPDATA%\Asgard\settings\heimdall.json`, from the example. Edit it once to match the SeCcHm form: `instance_url`, the catalog item's `catalog_sys_id` (the 32 characters after `sysparm_id=` or `sys_id=` in its address), and one entry per field with its label exactly as the form shows it and its `kind`: `text`, `select`, `reference` or `checkbox`. A field can also have `"required": true`, a `default`, or a `selector` when its label isn't enough to find it. `fields` shows what Heimdall read.
- **Templates** are saved combinations of values for those fields. `template save NAME --set "LABEL=VALUE"` creates one; `template list`, `show`, `edit` (`--set`, `--unset`, `--attachment`, `--no-attachment`, `--rename`) and `delete` manage them. Names and labels ignore capitals. Checkboxes take `true` or `false`. Values may use `$today` (2026-10-04) or `$today_us` (10/04/2026); write `$$` for a dollar sign. In PowerShell, put such values in single quotes, or PowerShell replaces `$today` before Heimdall sees it.
- `fill` uses, for each field, the form's default, then the template's value, then any `--set` you add for this run. A field with no value anywhere is left as it is. `--attachment` or `--no-attachment` changes the attachment for this run only, and `--dry-run` shows the plan without opening Edge.
- **What a fill does.** Heimdall opens Edge with its own profile and signs in the normal way: pick your certificate and enter your PIN if Edge asks. After the first time, the profile usually keeps you signed in. It opens the catalog item, fills each field, checks that each value took, attaches the file, and brings Edge to the front. Review the form, click **Submit**, then close the window.
- **What it never does.** It never clicks Submit, and it never reads, copies or stores your session, cookies or tokens; they stay in Heimdall's Edge profile.
- **When it stops.** Heimdall stops with a message instead of guessing: a template that names a field the form no longer has, a required field with no value, a missing attachment, a label that matches no field or two fields, or a field that didn't keep its value. If two fields share a label, give one a `selector` in the form file.
- **If the form won't open,** run `fill --trace` and open the trace with `playwright show-trace`. The trace holds your session and screenshots of the form; delete it afterwards and don't share it. Set `"ui": "portal"` in the form file to use the Service Portal page instead of the classic form.

`fill` needs **Playwright for Python**, installed for the same Python that `heimdall.cmd` finds, and Edge's `RemoteDebuggingAllowed` policy must not be off (check `edge://policy`). Playwright runs a bundled `node.exe`, so if your computer only allows programs from approved folders, ask IT to install it in one. Every other command works without Playwright. `heimdall.cmd` finds Python the same way `baldur.cmd` does; if `.cmd` files are blocked, run `py -3 cli.py` from the same folder. Add `--json` to `fields`, `template list`, `template show` or `fill --dry-run` for output a program can read.

## The shared window

Heimdall's tile opens Asgard's shared window: a sidebar with **Dashboard**, the app's own pages and **Settings**. Every app that adds pages uses the same window, so they look and work alike. `py -3 -m asgard.ui` (run from `%LOCALAPPDATA%\Asgard\app`) opens one window with every app's pages.

- **Heimdall's pages.** **Templates** lists your templates; pick one to see each field's value and where it comes from, then **Fill in Edge**, **Dry run**, **Edit** or **Delete**. **New template** shows every field on the form; leave a field empty to leave it as it is. A fill runs `heimdall fill` in the background and shows its progress; **Stop** ends it. **Form** shows what Heimdall read from the form file and can create it the first time.
- **Dashboard** shows a card for each thing that needs you (templates that need fixing, a form not set up yet, Muninn's counts). Click one to go there.
- **Settings** has Light, Dark or Match Windows, text size, and an accent colour for each app. Changes apply straight away. Text in an accent colour is adjusted if needed so it stays readable. Settings also lists each app's settings files with **Open** buttons.
- **Keys.** Tab moves between controls, Enter or Space opens a sidebar page, Alt+Left goes back, F5 reloads the page.

The window needs **PySide6-Essentials** (approved, pinned in `requirements.txt`) for the Python the tile uses. Without it the tile says what to install; the command-line tools don't need it.

Your colour and text-size choices are in `%LOCALAPPDATA%\Asgard\settings\ui.json`, which you can also edit by hand:

```json
{
  "mode": "dark",
  "theme": {"fontSize": 14},
  "apps": {"heimdall": {"accent": "#6B4FBF"}, "odin": {"dark": {"accent": "#7FA7E8"}}}
}
```

Any token can be set for every app (`theme`) or one app (`apps`), in both modes or just `light` or `dark`. Colours: `background`, `surface`, `surfaceHigh`, `sidebar`, `border`, `borderSoft`, `text`, `textMuted`, `textDim`, `accent`, `success`, `warning`, `error`. Sizes: `fontSize`, `radiusSmall`, `radiusMedium`, `radiusLarge`, `sidebarWidth`, `spacing`. Fonts: `fontFamily`, `monoFamily`. A bad value is ignored with a warning on the Settings page; if the file can't be read at all, the window uses the defaults and won't overwrite it.

## Changing your tiles

Your tile settings live in `%LOCALAPPDATA%\Asgard\apps.local.json`. Open it from the **...** menu with **Edit my tiles**. Setup never overwrites it. Save the file, then press F5 in Asgard.

```json
{
  "apps": {
    "odin": {
      "launch": {
        "type": "python",
        "target": "C:\\Tools\\Odin\\odin.pyw",
        "interpreter": "C:\\Tools\\Odin\\.venv\\Scripts\\pythonw.exe"
      }
    },
    "jira": {
      "name": "Jira",
      "description": "Our Jira",
      "monogram": "Ji",
      "color": "#0B5CAD",
      "launch": { "type": "url", "target": "https://jira.example.gov" }
    }
  },
  "hidden": ["valkyrie"],
  "order": ["odin", "jira"]
}
```

In JSON, each backslash in a Windows path is written twice.

| Launch type | Starts | Notes |
| --- | --- | --- |
| `python` | A `.py` or `.pyw` with `pythonw.exe` | Set `interpreter` for an app with its own environment; `"console": true` shows a console window |
| `powershell` | `powershell.exe -NoProfile -File <target>` | Your execution policy still applies |
| `exe` | A program | Optional `args` list |
| `open` | Anything, as a double-click would | Shortcuts, `.cmd` files, documents, folders |
| `url` | A web page in your browser | `http://` or `https://` only |

Paths can use `{app}` (Asgard's code folder), `{data}` (Asgard's data folder) and environment variables such as `%USERPROFILE%`.

## Looking after Muninn

Asgard checks Muninn each time it starts. It backs it up once a day and tidies it: it refreshes statistics, folds the write-ahead log in, and rebuilds a damaged search index. If the file itself is damaged, Asgard says so and names the newest backup. From a console, in `%LOCALAPPDATA%\Asgard\app`, using `python.exe` (`pythonw.exe` prints nothing). In the packaged build, run the same commands in its folder as `asgard-cli.exe --muninn status` and so on:

```
python Asgard.pyw --muninn prepare           create Muninn or bring it up to date, as opening Asgard does
python Asgard.pyw --muninn status            version, sizes, backups, last housekeeping
python Asgard.pyw --muninn check             look for damage and anything that should never happen (exit 1 if found)
python Asgard.pyw --muninn repair            rebuild the search index, put back missing protections
python Asgard.pyw --muninn backup            a copy now
python Asgard.pyw --muninn restore [FILE]    put a backup back (the newest if FILE is left out); close every app first
python Asgard.pyw --muninn retention on|off  prune old run records daily (off until you confirm the periods)
```

[docs/muninn-operations.md](docs/muninn-operations.md) explains each message and what to do about it.

## Uninstall

Use the **Valhalla** tile, **Settings > Apps > Asgard > Uninstall**, or **... > Uninstall Asgard**. Valhalla removes what setup recorded: the app folder, the shortcuts and the Settings entry. It asks before deleting your data (tile settings, logs, the Muninn database and its backups). It never deletes anything outside Asgard's own folders, and it doesn't touch apps that live elsewhere, such as Odin. So the packaged build's folder stays: Valhalla says where it is, and you (or IT) delete it once Asgard has closed.

## Where things live

| Path | What |
| --- | --- |
| `%LOCALAPPDATA%\Asgard\app` | The installed code, replaced on upgrade (the Python install only: the packaged build runs from its own folder) |
| `%LOCALAPPDATA%\Asgard\apps.local.json` | Your tile settings |
| `%LOCALAPPDATA%\Asgard\logs` | One log per app, plus `launcher.log` |
| `%LOCALAPPDATA%\Asgard\muninn.db` | Muninn, the database the apps share (with `-wal` and `-shm` files beside it) |
| `%LOCALAPPDATA%\Asgard\backups` | Muninn's last 7 daily copies, the last 3 taken before a schema upgrade, and the last 5 taken with **... > Back up Muninn now** |
| `%LOCALAPPDATA%\Asgard\muninn.before-restore-*.db` | The database as it was before a restore, kept so the restore can be undone |
| `%LOCALAPPDATA%\Asgard\install-ledger.json` | What setup created, so Valhalla can undo it |
| `%LOCALAPPDATA%\Asgard\settings\baldur.json` | Baldur's settings |
| `%LOCALAPPDATA%\Asgard\settings\ysildir.json` | Which Ysildir tools your AI client may use (`ysildir.cmd tools`). No file means the defaults; a file Ysildir can't read leaves only `asgard_guide` on |
| `%LOCALAPPDATA%\Asgard\settings\ui.json` | Your choices for the shared window: mode, text size, colours |
| `%LOCALAPPDATA%\Asgard\settings\heimdall.json` | Heimdall's form file: the SeCcHm catalog item and its fields |
| `%LOCALAPPDATA%\Asgard\settings\heimdall-templates.json` | Your Heimdall templates. Heimdall won't overwrite this file if it can't read it |
| `%LOCALAPPDATA%\Asgard\heimdall\edge-profile` | The Edge profile Heimdall signs in with; delete it to sign out. Traces from `--trace` go in `heimdall\traces` |

To upgrade, download the new zip and run setup again. Your tile settings stay. Close Asgard first. For the packaged build, replace its folder with the new one, then run its setup.

## Muninn, for app authors

Asgard creates Muninn the first time it starts, upgrades it when a new Asgard needs a newer schema, and copies it to `backups` once a day. Only Asgard upgrades it. Every app reads and writes through the `asgard.muninn` package, which ships inside Asgard, so there is one copy of the schema and its rules.

An app that lives outside Asgard, such as Odin, loads the package from Asgard's install folder. Asgard sets `ASGARD_APP` when it starts an app; the fallback covers starting the app on its own:

```python
import os
import sys
from pathlib import Path


def load_muninn():
    root = os.environ.get("ASGARD_APP") or str(Path(os.environ["LOCALAPPDATA"]) / "Asgard" / "app")
    if root not in sys.path:
        sys.path.insert(0, root)
    from asgard import muninn
    return muninn


muninn = load_muninn()
con = muninn.open_app("odin", supported=(1, 3))   # schema versions Odin was written for; never upgrades
```

`open_app` has no default range: give the versions your app was tested against. It raises `muninn.NotReady` if Asgard hasn't created Muninn yet, `muninn.VersionError` if the schema is outside the app's range, `muninn.CorruptError` if the file is damaged (the message names the newest backup and the restore command), and `muninn.BusyError` if another app held the write lock for 30 s. Each message says what to do.

The connection it returns enforces the rules below. A write to another app's table, a schema change, or a change to protections such as foreign keys is refused with "not authorized". `muninn.guard.describe(exc)` says which table and why. Store Jira keys through `muninn.normalize_key()`. From schema v3 the database refuses a key that isn't in capitals like `PROJ-123`. Each app's contract is in [docs/integration/](docs/integration/README.md).

What Odin calls, by job (`from asgard.muninn import odin`):

| Job | Calls |
| --- | --- |
| Sync your issues (your existing JQL, `expand=changelog`, `updated >= odin.jql_time(run.cursor)`) | `muninn.Run(con, "odin", jira_source, "issues")`; fetch a page, then `with run.batch():` call `odin.upsert_issue(run, issue_json, ctx)` for each issue. Pass `mine=True` for results of `assignee was currentUser()` |
| Assigned to Me view, tracked parents | `odin.assigned_to_me(con)`, `odin.children_of(con, parent_key)` |
| Keys Baldur and the other apps mention | `odin.unknown_keys(con)`, then `GET /rest/api/2/issue/{key}` and `odin.record_lookup(run, key, json_or_None, ctx)`; `odin.refresh_batches()` keeps their status current |
| Calendar | `odin.event_from_graph()` or `odin.event_from_outlook()`, `odin.upsert_calendar_event(run, event)`, then `odin.sweep_calendar(run, start, end)` after a whole window |
| Your worklogs | `/rest/api/2/worklog/updated` and `/worklog/list`, then `odin.upsert_worklog(run, worklog_json, ctx)`; `/worklog/deleted` and `odin.mark_worklog_deleted()` |
| Post approved Baldur days | `odin.posts_due(con)`, then `odin.begin_post()`, the Jira call, and `odin.finish_post()` or `odin.fail_post()` |
| Log a meeting, or time typed into Odin | `odin.begin_meeting_post()` or `odin.begin_manual_post()`, then the same finish calls |
| After a crash | `odin.stuck_posts(con)`: search each issue's worklogs for the marker, then `odin.resolve_stuck(con, id, found_id_or_None, searched=True)`. Without `searched=True` a missing id is refused, because marking a post that reached Jira as failed would post it twice |

Three rules keep the data right:

- **Write only your own tables**, through the module. Every app may read any table.
- **Fetch, then write.** Never hold a write transaction across a network call: fetch a page from Jira, then write it in `with run.batch():`. `begin_post()` commits its row before you call Jira, and `finish_post()` writes again afterwards. If one item fails, catch it and call `run.problem()`; the run ends `partial` and keeps its old cursor, so the next run retries that item.
- **After a timeout, don't call `fail_post()`.** The worklog may exist in Jira. Leave the row `sending`; `stuck_posts()` finds it and the marker settles it. Until then nothing more is posted for that issue and day. Keep the stuck threshold (120 seconds by default) well above your HTTP timeout.

## For maintainers

- **Layout.** `setup-Asgard.cmd` finds Python (or the packaged build's `asgard-cli.exe`) and runs `asgard/install.py`. `Asgard.pyw` starts `asgard/launcher.py`. Default tiles are in `asgard/apps.json`. Put bundled apps in `apps/<id>/` and point their tile at `{app}\apps\<id>\<entry>.pyw`.
- **Rules.** The launcher, setup and Muninn use the Python standard library only, so they run anywhere the catalog Python runs. Keep `setup-Asgard.cmd` ASCII with CRLF line endings; `.gitattributes` enforces CRLF.
- **Adding pages to the shared window.** Create `apps/<id>/ui/manifest.json` (`app`, `name`, `subtitle`, `theme`, `backend` as `package.module:Class`, and `views`: `id`, `title`, two-letter `icon`, `qml`), the QML files beside it, and the backend class. QML pages `import AsgardUI` for `ScrollPage`, `PageHeader`, `Card`, `MetricCard`, `AppButton`, `NavItem`, `Pill` and `EmptyState`, use only `theme.*` for colours and sizes, and declare `property var bridge: null` to get the backend: `bridge.call("method", [args])` returns a dict, with `error` set when it raised a `ValueError`. Optional backend methods: `dashboard()` (cards with `label`, `value`, `detail`, `tone`, `view`) and `settings_files()`. Heimdall (`apps/heimdall/ui/`, `heimdall/ui_backend.py`) is the worked example. Nothing in `asgard/ui` changes when an app is added.
- **Packages.** The best module for each job, declared and pinned in `requirements.txt` (`docs/dependency-policy.md`), and imported only where needed: Playwright only in Heimdall's `browser.py` when `fill` runs, PySide6 only in `asgard/ui/shell.py` when a window opens. [MODULES.md](MODULES.md) lists each package, where it's used, and what to use instead if it isn't available on-prem. `tests/test_dependencies.py` enforces all of this.
- **Tests.** Run `py -3 -m unittest discover -s tests`. Heimdall's browser tests (`tests/test_heimdall_browser.py`, against `tests/fake_servicenow.py`) skip unless Playwright and a browser are installed; set `HEIMDALL_TEST_CHANNEL=msedge` to use Edge. The window's tests (`tests/test_ui_qt.py`) skip unless PySide6 is installed and run offscreen (`QT_QPA_PLATFORM=offscreen`). The schema's own checks (`tools/check_muninn_schema.py`) switch time zones, so they run on Linux, macOS or WSL and are skipped on Windows.
- **Schema changes.** Add `asgard/muninn/migrations/000N_<name>.sql`, numbered one past the last. The runner wraps each file in a transaction and sets `user_version`. A migration that rebuilds a table starts with `-- muninn: foreign_keys=off`. Never edit a migration that has shipped. Add new tables to `guard.OWNERS` (a test fails until you do) and checks to `tools/check_muninn_schema.py`; `--muninn check` compares every file with what the migrations make, so the schema must come only from migrations.
- **Releases.** Bump `VERSION`, then tag `asgard-vX.Y.Z` (matching `VERSION`) and push the tag. The **Package Asgard** workflow (`.github/workflows/asgard-package.yml`) builds and checks the packaged build on Windows and publishes the release with its zip and `.sha256`; GitHub adds the source zip. `.gitattributes` keeps `tests/` and other maintainer files out of downloaded zips.
- **The packaged build.** `py -3.12 packaging\build.py` makes it locally, by the same steps as the workflow: pinned packages in `build\venv`, the tests, PyInstaller (`packaging/asgard.spec`), checks through the built programs, then the zip. Add `--skip-tests` or `--keep-venv` to go faster; on macOS or Linux it makes that system's build, to try the steps. [docs/packaging.md](docs/packaging.md) explains the decisions (one folder that runs in place; `Asgard.exe` and `asgard-cli.exe` standing in for `pythonw` and `python`; Asgard's code shipped as `.py` files) and what IT needs on the workstation.
- **Icon.** `tools/make_icon.py` regenerates `asgard/asgard.ico`. It needs Pillow.
