# Refinements spec: moved meetings, Muninn hardening, settling posts, Baldur's tests, Ysildir's tile (2026-10-10)

Branch `claude/ci-and-odin-tests`, from `main` after PR #2 and #3. Brandon asked for the four refinements a survey of the code found, specified here first and then built in this order: 1b, 1a, 2, 3, 4a, 4b. Each step ends with tests, the docs it touches, and CI green. Steps 1a, 1b and 2 change how Odin writes to Jira, what counts as an agent estimate, or posting, so they get an independent review before they're called done (`AGENTS.md`).

Decision taken (Brandon, Oct 10, "unless you say otherwise"): Odin keeps its rule of never editing a Jira issue. A moved meeting is recognised and reported; its sub-task and worklog are left as they are.

## 1. Before 0.4.0 freezes the migrations

Migrations 0004 and 0005 haven't shipped (`HANDOFF.md`), so they may still change; after 0.4.0 they're frozen.

### 1a. Odin recognises a meeting that moved

**Why.** Checked on `main`: a meeting pushed at 14:00 and then moved to 15:00 by its organizer got a second sub-task. The calendar key is `<calendar id>|<start>` and the content hash covers the start, so both change. On the CSV path a renamed meeting gets a second one too (the key is the content hash). A renamed meeting from Outlook is already handled: the key doesn't change.

**What changes.**

| Part | Change |
| --- | --- |
| Export schema v1 | Two optional fields, so the version stays 1: `global_id` (the calendar's id for the meeting; every occurrence of a series shares it) and `is_recurring` (true for an occurrence or exception of a series). Absent means unknown, and Odin behaves as today |
| `Export-OutlookMeetings.ps1` | `global_id = GlobalAppointmentID`, `is_recurring = [bool]$item.IsRecurring`. Neither is a guarded property. Needs verification on the workstation |
| `Odin/graph-app` | `global_id = iCalUId`; `is_recurring` from Graph's `type` (`singleInstance` false, `occurrence` or `exception` true, otherwise unknown) |
| `Odin/playwright-app`, CSV | OWA's JSON as mapped gives no series type and the CSV no id: both stay as today |
| `models.Meeting`, `sources.load_export` | Carry the two fields; anything but a real boolean for `is_recurring` reads as unknown |
| `sync.py` | When the existing checks (Muninn's record by key or hash, the journal, `state.db`) find nothing, a meeting with a `global_id` and `is_recurring` false is looked up by that id: a record whose `meeting_key` starts with `<global_id>|`. Found means it is the same meeting before it moved: nothing is created, the run reports `MOVED` with the issue, both times, and both lengths when they differ, and says the sub-task and worklog were left as they are |
| The record's calendar link | If the record's calendar event is gone (the export swept it) or it has none, the record is linked to the moved meeting's event, as `store.link_event` already does for history from `state.db`. The v5 trigger allows exactly this change |
| Results | `RunResult.moved`, the summary line, `last_run.json` (`moved`) and a dry run (`odin preview` shows `MOVED` too) |

**Out of scope.** A moved occurrence of a recurring meeting (an exception) still gets its own sub-task, as today: the whole series shares one id. CSV renames: without an id, matching by time slot could merge two meetings booked at the same time. Updating the old sub-task's title or worklog: Odin doesn't edit Jira issues (the decision above); it could become a setting later.

**Done when.** Tests show: a moved one-off meeting gets no second sub-task and is reported as moved, its record linked to the new event; a moved occurrence of a series is created as today; an export without the fields behaves as today; Graph's mapping emits both fields; the COM exporter reads `IsRecurring` and still no guarded property (guardrail test). The README's export schema lists the fields.

### 1b. Muninn prevents what the Oct 9 review could only detect (R14, P7)

**Why.** `docs/review-2026-10-09.md` left two items for "the next migration". R14: commits could be added to a recorded agent estimate through Baldur's connection, changing its figures; today that is detected by `report_digest` and the report stops counting, but not prevented. P7: a raw insert could back-date `agent_estimates.recorded_at`.

**What changes.** Two triggers, added to the unshipped `0004_agent_estimates.sql` because neither needs a table change:

| Trigger | Rule |
| --- | --- |
| `agent_estimate_commits_with_their_report` | `BEFORE INSERT ON agent_estimate_commits`: refused unless `new.estimate_id` is `last_insert_rowid()`. `muninn.baldur.record_agent_estimate` inserts the report and then its commits on one connection, with nothing in between, and an insert into a `WITHOUT ROWID` table leaves `last_insert_rowid()` alone. Any later insert (another connection, or after any other row) is refused |
| `agent_estimates_recorded_now` | `BEFORE INSERT ON agent_estimates`: `recorded_at` must be within two minutes of now. It defaults to now, so only an explicit value can trip it |

A database already at v4 or v5 from an earlier copy reports the two triggers as missing in `--muninn check`, and `--muninn repair` adds them (`integrity.repair_schema` restores triggers). R14's detection stays, for such databases until they're repaired. P5 stays open: it's provenance, not a number.

**Done when.** `tools/check_muninn_schema.py` checks both triggers; the R14 test shows the insert refused, and its detection test still runs on a database whose trigger was dropped; a back-dated report is refused and a normal one recorded.

## 2. A post in doubt can be settled

**Why.** When Odin can't tell whether an interrupted post reached Jira, the row stays `sending` and the next run asks Jira again. Since the Oct 10 review, a 404 while asking (a deleted issue, or browse permission lost) leaves it `sending` until Jira answers, which may be never. `odin status` shows only a count, and nothing lets a person settle one.

**What changes.**

| Command | Does |
| --- | --- |
| `odin settle` | Lists the posts in doubt: an id, the issue, the start, the length, how long ago, and the marker to search for in Jira |
| `odin settle ID` | Asks Jira again by the marker. Found: recorded as posted with Jira's id. Jira answered and it isn't there: recorded as not posted, and the time is offered again. Jira couldn't answer (404, 403, no connection): nothing changes, and it says why and what you can do |
| `odin settle ID --posted WORKLOG_ID` | You found it in Jira: recorded as posted with that id |
| `odin settle ID --not-posted` | You checked Jira and it isn't there: recorded as not posted, so its time is offered again. The help and the confirmation say that a post that did land would then be posted twice |

It takes Odin's run lock, so it never races a run. `odin status` lists each post in doubt with the command to settle it. `odin.cmd` passes `settle` through. The Muninn side is `asgard.muninn.odin.resolve_stuck`, unchanged.

**Done when.** Tests cover each outcome, including a 404 that leaves the row alone, an unknown id, a row that isn't in doubt, `--posted` and `--not-posted`, the lock, and the status lines.

## 3. Baldur's command line and GitHub sync, tested

**Why.** On `main`, Baldur's `cli.py` was at 70% and `github.py` at 80%, its largest modules after the estimator. Never run in a test: `collect` (27 of 31 lines), `github` (35 of 36), `reject` (13 of 14), most of `setup`, `estimate` and `repos`, `schedule`, and the GitHub token's save, load and delete.

**What changes.** Tests through `main()`, on real temporary git repositories (`AGENTS.md`), for: `collect`; `estimate`; `approve` and `reject`; `setup`; `repos`; `github` (token status, set and delete, with Credential Manager stood in for off Windows); `schedule` (the schtasks command it would run); `main`'s error messages; the token functions off Windows; `_sync_repo`'s untested branches. Each test checks behaviour, not lines. A bug found is fixed with a regression test.

**Done when.** `cli.py` is at 85% or more and `github.py` at 90% or more, and anything the tests found is fixed.

## 4. Smaller items

### 4a. Ysildir's tile opens a status page

**Why.** Ysildir is built and documented, but its tile still says "coming soon": it has no window, because it runs inside an AI client.

**What changes.** The tile is available and opens a page in Asgard's shared window, like Odin's and Heimdall's (`apps/ysildir/ui/`, a plain-Python backend in `ysildir/ui_backend.py`, `ysildir.pyw`):

- whether the MCP SDK is installed and which version, or what's missing (it needs Python 3.10+);
- which tools are switched on (read-only here; `ysildir.cmd tools` changes them, a deliberate step because the switches decide what reaches the AI client);
- which AI clients are connected;
- buttons that run `ysildir check` and `ysildir setup` as a child process, with their output shown.

**Done when.** The backend has tests, the page loads offscreen in `tests/test_ui_qt.py`, and the catalog test expects the tile available.

### 4b. The live specs catch up with their snapshots

**Why.** `docs/baldur-spec.md` and `docs/muninn-design.md` are snapshots of live Claude artifacts. Their headers list edits made in the repo since Oct 4 that never reached the live pages, which are meant to be the source of truth.

**What changes.** Each live artifact is read first. If it hasn't changed since the snapshot was taken, the snapshot's current text is published to it. If it has edits of its own, they're merged by hand with the snapshot's, and both are kept; nothing is lost. The snapshot headers then say the live pages are current. If an artifact can't be read or written from this session, it's left for Brandon and the hand-off says so.

**Done when.** Both live pages match their snapshots, or the reason they don't is written down.

## Verification for every step

The suite on Python 3.9, 3.11 and 3.13, with and without the optional packages; the Windows file-lock emulation; ruff, vermin, the schema check, actionlint; CI green (`checks.yml`, `odin-checks.yml`). What only the workstation can prove (the Outlook exporter's new fields, the window at real DPI) is listed in `HANDOFF.md`.
