# OWA exporter (Playwright)

Reads **your own** calendar out of Outlook on the web and writes meeting2jira's schema-v1 JSON, for
machines where classic Outlook is unavailable — that is, "new Outlook", which supports neither COM
automation nor the Import/Export wizard, so it breaks both existing paths.

## Same commands as the COM app

```powershell
.\meeting2jira-owa setup       # pip, browser, sign-in, then Jira config and token
.\meeting2jira-owa preview     # export from OWA, show what would be created
.\meeting2jira-owa             # do it for real
.\meeting2jira-owa schedule    # weekdays, as you, while logged on
```

| | |
|---|---|
| `meeting2jira-owa [days]` | daily run: export from OWA, push to Jira. Defaults to 1 day back; widening is free because the pipeline dedupes. |
| `preview [days]` | same, creating nothing |
| `setup` | first-run walkthrough |
| `login` | re-authenticate when the browser session expires |
| `export [days]` | export only, keep the JSON (defaults to 7 days, for diffing against a COM export) |
| `discover` | print the calendar API requests OWA makes — diagnosis |
| `check` / `status` / `doctor` / `forget` / `set-token` / `cli` | delegated to the COM app |
| `schedule` / `unschedule` | the weekday scheduled task |
| `selftest` | mapping tests, then the COM app's own tests |

**It is an additional exporter, not a fork.** Everything that is not OWA-specific — the whole Jira
side, config, token storage, state, reporting — is delegated to `app\meeting2jira.cmd` rather than
duplicated. One config file, one token, one state database, so running both paths cannot create
duplicate sub-tasks. Set `M2J_APP_DIR` if the COM app is not at `..\app`.

`tests/test_parity.py` asserts the command surface covers the COM app's, and that the Python
discovery logic in both entry points stays identical — that logic was rewritten after it failed on a
real install, and a copy drifting back to the brittle version would fail the same way while being
harder to notice.

The raw exporter is still there if you want it directly:

```
python export_owa.py --days-back 1 --out week.json
cd ../app && PYTHONPATH=src python -m meeting2jira push --input ../playwright-app/week.json --dry-run
```

## Why this is a separate application

`app/` is standard-library only, and that is what made it approvable on a locked-down machine. This
needs Playwright from pip. Keeping them apart means the working COM app is untouched, its guardrails
stay strict, and you can delete this folder without affecting anything.

They meet where every source meets: **schema-v1 JSON**. Nothing in `rules`, `sync`, `jira` or
`state` changes.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the diagram and the failure modes.

## How it works, and why not the obvious way

Scraping the rendered calendar is the obvious approach and the wrong one. The grid is a virtualised
React view: it renders only visible rows, class names are generated, and the fields the filters
actually depend on — response status, sensitivity, categories, recurrence identity — **are not in
the DOM at all**. A DOM scraper silently loses `skip_declined` and `skip_private`, which is the
worst failure this tool could have.

So the browser is used for one thing: being an authenticated session.

1. Open the calendar in a **persistent profile**, reusing the session you already signed into.
2. Watch for the request OWA makes to its own calendar API, and learn the URL and auth headers.
3. Call that endpoint **directly** for the exact window you asked for, following paging.
4. Map the structured JSON to schema v1.

That gives the same data the web client renders from, including every field the filters need.

### Speed

Roughly in order of how much each matters:

| | |
|---|---|
| **Session reuse** | A persistent profile removes interactive sign-in — the only genuinely slow step, and all the MFA prompts. |
| **Installed Edge** (`--channel msedge`) | No ~150MB browser download, and it uses the browser IT already patches. Falls back to bundled Chromium. |
| **Resource blocking** | Images, media, fonts and stylesheets are aborted. The JSON arrives regardless. |
| **One render, then direct calls** | After the endpoint is known, a 30-day export costs the same page load as a 1-day export. |
| **Headless** | Once a session exists. |
| **No fixed sleeps** | It polls in 250 ms slices and returns the moment the request appears, rather than waiting out a timeout. |

## Setup

```bash
pip install -r requirements.txt
playwright install msedge        # registers the installed Edge; or: playwright install chromium
python export_owa.py --login     # visible browser, sign in incl. MFA, then close it
```

The session is stored in `%LOCALAPPDATA%\meeting2jira\owa-profile`. Later runs are headless and
unattended. **No password is ever handled by this code** — it reuses the browser session Windows
already established, which is also why Conditional Access sees a browser it already trusts.

## Testing it

Four layers, cheapest first. The first two need no browser, no network and no mailbox.

**1. The mapping — run this anywhere, including here**

```bash
cd playwright-app && python -m unittest discover -s tests -v
```

25 tests over a fixture covering recurring instances, declined, private, all-day, cancelled, a
malformed item, and an event returned in the wrong timezone. This is where mapping bugs get caught,
because `mapping.py` imports nothing but the standard library.

**2. End to end against the real pipeline, offline**

Build an export from the fixture and push it with `--dry-run`. Confirms the filters fire on Graph's
field vocabulary and that `load_export` accepts the envelope. Already verified: 7 events in, 3 would
be created, 4 skipped for the right reasons.

**3. On the target machine — does discovery work?**

```bash
python export_owa.py --login                       # once
python export_owa.py --days-back 1 --raw-out raw.json -v
```

`--raw-out` keeps the unmapped events so a field mismatch can be diagnosed without re-running the
browser. If discovery fails:

```bash
python export_owa.py --debug-endpoints             # prints every JSON request the page makes
python export_owa.py --endpoint "<the URL>"        # skip discovery
```

Microsoft changes this endpoint periodically. `--debug-endpoints` plus `--endpoint` is the designed
escape hatch, not a workaround.

**4. The one that actually proves it: diff against the COM export**

You have a COM path that is known to work. Export the same window both ways and compare.

```bash
# on a machine with classic Outlook
.\meeting2jira sync -DaysBack 7 -KeepExport -DryRun
python export_owa.py --days-back 7 --out owa.json
python tools/compare_exports.py com_export.json owa.json
```

It matches on `content_hash` — the same identity the pipeline dedupes on — so "matched" means the
two exports would not create duplicate sub-tasks. It reports meetings missing from either side and
any matched meeting where a decision-affecting field disagrees.

Watch for **equal numbers missing and extra**: that is the signature of a timezone error, where every
meeting shifted by the offset so the originals look missing and the shifted ones look new. The tool
says so explicitly when it sees that pattern.

## Running both paths is safe

`content_hash` is source-independent — normalised subject, start, end — and `state.find()` matches on
key **or** content hash. So a meeting already pushed via COM is recognised here and reported as
`EXISTS`. Verified: the same meeting from both sources produces identical content hashes with
different keys.

## What this gives you that COM cannot

- **Reliable Teams detection.** `isOnlineMeeting` + `onlineMeetingProvider` are explicit, where the
  COM path matches on the location string.
- **`iCalUId`** as a stable per-occurrence identity.
- **Client independence.** Works with new Outlook, classic Outlook, OWA, the phone app.

## Known limits

- **Undocumented endpoint.** This depends on a Microsoft-internal API that can change without notice.
  That is the cost of not having an app registration, and it is why `--debug-endpoints` exists. If
  you get a Graph app registration, prefer it and retire this.
- **Needs an interactive session at least once**, and again whenever the session expires.
- **Not verified against a live mailbox.** Everything above is tested offline against fixtures. The
  browser half — discovery, header replay, paging — has not run against real OWA.
- **Your own calendar only.** No shared, delegate, or other people's calendars.
