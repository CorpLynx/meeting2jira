# OWA exporter (Playwright)

Reads **your own** calendar out of Outlook on the web and writes meeting2jira's schema-v1 JSON, for
machines where classic Outlook is unavailable — that is, "new Outlook", which supports neither COM
automation nor the Import/Export wizard, so it breaks both existing paths.

> ## Status: dormant contingency. Not installed, not under active development.
>
> **This folder is not part of the deliverable.** The program is `app/` — copy that folder and it
> runs. Nothing here is ever copied to the workstation, and `app/` does not reference it.
>
> **Prefer Microsoft Graph (Path D).** Graph works on classic *and* new Outlook, needs no browser
> automation, and raises no terms-of-service question. This exporter reads OWA's **undocumented
> internal APIs**; an independent project doing the same thing
> ([outlook-cli](https://github.com/yusufaltunbicak/outlook-cli)) carries an explicit warning that
> such use may breach Microsoft's terms or an organization's acceptable-use policy. Raise it with
> your ISSO before this is used anywhere real.
>
> **Why it still exists.** It covers exactly one scenario, and covers nothing else: you are forced
> onto new Outlook *and* Graph has not been approved. In that situation nothing else works. Today
> it is insurance, not infrastructure — the workstation runs classic Outlook and Path A (COM) is
> verified against a real mailbox, which this has never been.
>
> **Delete it when** the Graph path is working and verified against the real mailbox on the target
> machine. Not when Graph is merely approved, and not when it merely passes mocked tests. At that
> point this costs nothing to lose and carries review risk to keep.
>
> **Until then:** no new features, no further hardening. Changes are limited to things that also
> protect the Graph path — `owa/mapping.py` is shared ground, since Graph's field vocabulary is what
> it maps.

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
   Three things have to agree: the URL pattern, the body shape, and — among weakly-named candidates —
   which payload is most calendar-like. The page fetches several decoys first, and each one defeats a
   single check on its own: `/owa/telemetry/events` has `start`/`end` as integers, while Graph's
   `insights/events` has a real `subject` and `dateTime` and passes any shape test.
3. Call that endpoint **directly** for the exact window you asked for, following paging. One retry on
   a transient answer; paging stops if a `nextLink` repeats; page and event caps are announced on
   stderr rather than truncating silently.
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
| **No fixed sleeps** | It blocks on the response event and returns the moment the request appears, rather than waiting out a timeout. The budget is a monotonic deadline, so a slow mailbox is still found. |
| **Telemetry aborted, discovery page closed** | Beacon hosts are refused at the route handler, and the discovery page is closed as soon as it has served its purpose instead of holding a renderer. |

Against the fake OWA server, warm: **~505ms** end to end — 266ms Playwright start, 121ms browser
launch, 101ms discovery, 9ms fetch.

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

**0. Replace the invented fixture with a real one — do this first**

The shipped fixture was written from what Graph is *believed* to return. If your tenant uses
different field names, every mapping test passes and the export still produces nothing. This is the
weakest link and the biggest single jump in confidence available:

```bash
python export_owa.py --days-back 7 --raw-out raw.json
python tools/sanitize_capture.py raw.json -o tests/fixtures/owa_real_capture.json
python -m unittest discover -s tests
```

The sanitizer replaces subjects, names, addresses, body previews, join URLs and identifiers with
synthetic values while preserving *shape* — which fields exist, which are null, how enums are spelled.
It also prints the real field names, which is the diagnostic worth reading. Verified: on a sample
carrying names, emails, a phone number and a Teams join URL, nothing recognisable survived, and
`sensitivity` / `showAs` / `responseStatus` / `isOnlineMeeting` / `categories` came through intact.

Read the output before committing it. If the tests then fail, the real field names differ from the
assumed ones — which is exactly what you wanted to find out.

**1. The mapping — run this anywhere, including here**

```bash
cd playwright-app && python -m unittest discover -s tests -v
```

28 tests over a fixture covering recurring instances, declined, private, all-day, cancelled, a
malformed item, and an event returned in the wrong timezone. This is where mapping bugs get caught,
because `mapping.py` imports nothing but the standard library.

Three of those cover **property casing**, and they exist because of a bug research found rather than a
failing run. Microsoft Graph returns camelCase (`subject`, `showAs`); the Outlook endpoint returns
PascalCase (`Subject`, `ShowAs`). Since the endpoint here is *discovered* rather than chosen, either
is possible, and the mapping was camelCase-only. Lookup is now case-insensitive and the same fixture
in both casings must map to identical output. See ARCHITECTURE.md, "Prior art, and what it settled".

**2. End to end against the real pipeline, offline**

Build an export from the fixture and push it with `--dry-run`. Confirms the filters fire on Graph's
field vocabulary and that `load_export` accepts the envelope. Already verified: 7 events in, 3 would
be created, 4 skipped for the right reasons.

**2b. The browser layer, offline, with no mailbox**

`tests/test_capture.py` drives a real browser against `tests/fake_owa.py`, a local server that
behaves like OWA rather than like a convenient stub. It serves a page that fetches its calendar from
JavaScript with an `Authorization` header and pages with `@odata.nextLink`, but it also fetches CSS,
JS, images and fonts, and calls five other JSON endpoints — presence, mail folders, user config, and
a `/owa/telemetry/events` beacon whose records carry `start` and `end`. It can add latency, inject
failures per request, return a self-referential `nextLink`, and switch into sign-in, HTML,
server-error, POST-only and empty modes.

That realism is the point: the noise is what catches bugs a clean fake cannot. It covers the half
otherwise only testable against a live mailbox — endpoint discovery against decoys, header replay,
window rewriting, paging and paging loops, transient-failure retry, page and event caps, resource
blocking (asserted by counting what the server was actually asked for), renderer cleanup, expired
sessions, an SSO page served in place of JSON, a 500, and a POST-shaped endpoint.

```bash
pip install -r requirements.txt && playwright install chromium
python -m unittest discover -s tests -v
```

Skipped automatically when Playwright is absent. Verified: **66 tests pass** with a real browser.

Building the noisy fake found seven real defects in `capture.py`: discovery locking onto the telemetry
beacon; the wait loop charging a full slice per wakeup and so abandoning slow discovery early; the
discovery page never being closed; `debug_endpoints` referencing a deleted constant; an unbounded
`nextLink` loop; the expired-session diagnosis degrading to the generic message because it questioned
a page that had already been closed; and first-weak-wins candidate selection, which would have
exported Graph's "insights" list the day the calendar endpoint was renamed.

**Mutation-tested.** Two of these tests were originally passing for the wrong reason — the only decoy
was `/owa/telemetry/events`, which resource blocking aborts before the browser ever issues it, so the
matcher was never exercised; and the sign-in test asserted only that `--login` appeared in the
message, which is true of both failure messages. Nine deliberate breakages are now each caught by at
least one test: dropping the URL tiering, the shape check, the weak-candidate ranking, the
closed-page fix, telemetry blocking, the paging-loop guard, the transient retry, the page close, and
the event cap.

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
