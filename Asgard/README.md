# Asgard

Asgard is a launcher for the Asgard suite: one window with a tile for each app, like the Microsoft My Apps portal. It runs on the Python your agency already provides. It installs per user, so you don't need admin rights.

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

You need Python 3.9 or newer with Tcl/Tk (tkinter). Most catalog Python installs include it. `tools\asgard_preflight.ps1` checks this, along with the policies that affect the other apps.

## Using Asgard

- **Click a tile** to open the app. Tiles marked **Coming soon** aren't built yet.
- **Odin** lives outside Asgard, so its tile says **Set up** the first time. Click it and pick the file you normally start Odin with (`.pyw`, `.py`, `.exe`, `.ps1`, `.cmd` or a shortcut). Asgard remembers the choice. If that file's folder, or the folder above it, holds a virtual environment (`.venv`, `venv` or `env`), Asgard runs Odin with that environment's Python.
- **Right-click a tile** to change its location, open its folder, or view its log.
- **Type to search.** Arrow keys move between tiles, Enter opens one, and F5 reloads your tiles.
- If an app closes right after starting, Asgard shows the last lines of its log. Logs are in `%LOCALAPPDATA%\Asgard\logs`.
- **Counts on tiles** come from Muninn, the database the apps share: Odin's tile counts worklogs waiting to post, Baldur's counts reviews requested of you. Point at a tile, or move to it with the arrow keys, to see what the count means. Counts refresh every minute and when you come back to Asgard.

## Baldur from the command line

Baldur's window isn't built yet, so its tile still says **Coming soon**, but its collector and estimator work today from Command Prompt:

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

`baldur.cmd` tries `py -3`, then `py`, then `python`. If your computer blocks `.cmd` files, run `py -3 cli.py` (or `python cli.py`) from the same folder instead. Settings are in `%LOCALAPPDATA%\Asgard\settings\baldur.json`; the Baldur spec explains each one.

## Heimdall from the command line

Heimdall fills a SeCcHm request in Edge from a saved template, then stops so you can review it and click **Submit** yourself. Its tile says **Coming soon** until its window is built; the commands work today:

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

## Uninstall

Use the **Valhalla** tile, **Settings > Apps > Asgard > Uninstall**, or **... > Uninstall Asgard**. Valhalla removes what setup recorded: the app folder, the shortcuts and the Settings entry. It asks before deleting your data (tile settings, logs, the Muninn database and its backups). It never deletes anything outside Asgard's own folders, and it doesn't touch apps that live elsewhere, such as Odin.

## Where things live

| Path | What |
| --- | --- |
| `%LOCALAPPDATA%\Asgard\app` | The installed code, replaced on upgrade |
| `%LOCALAPPDATA%\Asgard\apps.local.json` | Your tile settings |
| `%LOCALAPPDATA%\Asgard\logs` | One log per app, plus `launcher.log` |
| `%LOCALAPPDATA%\Asgard\muninn.db` | Muninn, the database the apps share (with `-wal` and `-shm` files beside it) |
| `%LOCALAPPDATA%\Asgard\backups` | Muninn's last 7 daily copies, the last 3 taken before a schema upgrade, and the last 5 taken with **... > Back up Muninn now** |
| `%LOCALAPPDATA%\Asgard\install-ledger.json` | What setup created, so Valhalla can undo it |
| `%LOCALAPPDATA%\Asgard\settings\baldur.json` | Baldur's settings |
| `%LOCALAPPDATA%\Asgard\settings\heimdall.json` | Heimdall's form file: the SeCcHm catalog item and its fields |
| `%LOCALAPPDATA%\Asgard\settings\heimdall-templates.json` | Your Heimdall templates. Heimdall won't overwrite this file if it can't read it |
| `%LOCALAPPDATA%\Asgard\heimdall\edge-profile` | The Edge profile Heimdall signs in with; delete it to sign out. Traces from `--trace` go in `heimdall\traces` |

To upgrade, download the new zip and run setup again. Your tile settings stay. Close Asgard first.

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

`open_app` raises `muninn.NotReady` if Asgard hasn't created Muninn yet, and `muninn.VersionError` if the schema is outside the app's range. Both messages say what to do.

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
| After a crash | `odin.stuck_posts(con)`: search each issue's worklogs for the marker, then `odin.resolve_stuck()` |

Three rules keep the data right:

- **Write only your own tables**, through the module. Every app may read any table.
- **Fetch, then write.** Never hold a write transaction across a network call: fetch a page from Jira, then write it in `with run.batch():`. `begin_post()` commits its row before you call Jira, and `finish_post()` writes again afterwards. If one item fails, catch it and call `run.problem()`; the run ends `partial` and keeps its old cursor, so the next run retries that item.
- **After a timeout, don't call `fail_post()`.** The worklog may exist in Jira. Leave the row `sending`; `stuck_posts()` finds it and the marker settles it. Until then nothing more is posted for that issue and day. Keep the stuck threshold (120 seconds by default) well above your HTTP timeout.

## For maintainers

- **Layout.** `setup-Asgard.cmd` finds Python and runs `asgard/install.py`. `Asgard.pyw` starts `asgard/launcher.py`. Default tiles are in `asgard/apps.json`. Put bundled apps in `apps/<id>/` and point their tile at `{app}\apps\<id>\<entry>.pyw`.
- **Rules.** Use the Python standard library only, so Asgard runs anywhere the catalog Python runs. Keep `setup-Asgard.cmd` ASCII with CRLF line endings; `.gitattributes` enforces CRLF.
- **Heimdall's one exception.** `apps/heimdall/heimdall/browser.py` uses Playwright and imports it only when `fill` runs; nothing else in Asgard imports it. Whether that exception stands is an open decision (see `HANDOFF.md`).
- **Tests.** Run `py -3 -m unittest discover -s tests`. Heimdall's browser tests (`tests/test_heimdall_browser.py`, against `tests/fake_servicenow.py`) skip unless Playwright and a browser are installed; set `HEIMDALL_TEST_CHANNEL=msedge` to use Edge. The schema's own checks (`tools/check_muninn_schema.py`) switch time zones, so they run on Linux, macOS or WSL and are skipped on Windows.
- **Schema changes.** Add `asgard/muninn/migrations/000N_<name>.sql`, numbered one past the last. The runner wraps each file in a transaction and sets `user_version`. A migration that rebuilds a table starts with `-- muninn: foreign_keys=off`. Never edit a migration that has shipped.
- **Releases.** Bump `VERSION`, tag (`git tag v0.2.0`), push the tag, and publish a release from it. GitHub attaches the zip automatically. `.gitattributes` keeps `tests/` and other maintainer files out of downloaded zips.
- **Icon.** `tools/make_icon.py` regenerates `asgard/asgard.ico`. It needs Pillow.
