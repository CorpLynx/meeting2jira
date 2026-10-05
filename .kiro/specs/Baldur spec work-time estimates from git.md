# Baldur spec: work-time estimates from git

Oct 3, 2026 · @Brandon

Baldur turns your git activity and calendar into per-ticket time proposals that you review and approve. It never writes hours on its own, every number carries the evidence behind it, and every rounding goes down.

## Principles

Logged time is always an estimate. On a day of back-to-back calls, commits, reviews and messages, nobody can split the hours accurately from memory. Baldur doesn't measure time. It produces an approximation that is open about its basis, biased the same way every time, and closer than typing round numbers into a form on Friday.

1. **Every number carries its basis.** "2h15m from 7 commits in 3 sessions, 09:12–16:40" can be audited. "2h15m" can't. The basis is stored with the estimate, not reconstructed later.
2. **Bias down, consistently.** Every cap, rounding and tie-break goes down. A slightly low estimate is defensible; one that swings unpredictably can't be calibrated.
3. **Baldur proposes, you approve.** Nothing reaches Jira or a timesheet without an explicit confirmation, and Muninn's checks enforce that order (Muninn rule 7).
4. **Say what can't be seen.** Design talks, code review without commits and thinking leave no trace. The day report names that gap instead of implying the total is the whole day.
5. **Metadata only by default.** Baldur reads commit times, subjects, branch names and line counts, never diffs. Code content reaches a model only through the opt-in review mode (see Optional AI review).

## How Baldur fits into Asgard

&#91;embedded content: Baldur in Asgard · sources, Muninn, estimate, review, output\]

Baldur opens from its Asgard tile and writes everything through Muninn, so Freya can cite the same commits and the hours you confirmed. Collection, estimation and review are separate steps: you can re-estimate a week with new settings without collecting again, and nothing leaves Baldur until you approve it.

## What changed from Odin's gitwork spec

The old spec's model holds up. These changes move it into Asgard, fix three logic gaps, and make its defaults measurable.

| Area | Old spec | This spec | Why |
| --- | --- | --- | --- |
| Home | `meeting2jira/gitwork.py` inside Odin | Asgard app in `apps/baldur/`, porting that module and its 34 tests | Odin stays focused on Jira; Baldur gets its own tile |
| State | `git_worklogs` table in Odin's `state.db` | Muninn tables shared with Freya and Loki | Freya cites the same commits and confirmed hours |
| Authors | `authors` list in config | Muninn `identities`, which every app uses for `is_mine`; empty is still refused | One definition of "me" across Asgard |
| Collection | Scan repos on every run | Incremental sync into Muninn, collected weekly at minimum | Git expires reflog entries after 90 days |
| Sessions | Built per repo (unstated) | One timeline across all repos | Two repos worked in parallel no longer count twice |
| Meeting overlap | Overlap fraction, plus ambient weighting as a "second pass" | Three exclusive policies; `ambient` by default, `overlap` when a day has no meeting times | Applying both discounted meeting time twice |
| Lead-in | Fixed 30m before the first commit | Reflog checkout time when present, 30m otherwise | Evidence replaces an assumption |
| Issue keys | Branch name only | Branch, then commit message, filtered by a `project_keys` allowlist | `UTF-8` and `SHA-256` look like Jira keys |
| Defaults | Hand-tuned guesses | Calibrated against hours you log during a trial period; you accept each change | The dials get measured, not guessed |
| AI review | Could raise the total by up to 25%, contradicting its own first rule | May only move time between issues or lower it; rules checked in code | A model's output is checked, not trusted |
| Agent files | `.kiro/agents` and `.kiro/steering` | Prompt ships with Baldur and runs through Asgard's AI tiers: API, MCP or clipboard | Works with whatever AI access is approved |
| Output | Push through Odin's `JiraClient` | Report and clipboard first; posting later | Report-only answers the "should a tool write official hours" question safely |

## The estimation pipeline

Each run estimates a date range with one frozen set of settings, recorded as an `estimate_runs` row. The same commits and settings always give the same numbers.

1. **Collect.** Read your commits from Muninn (`is_mine = 1`), using author time, not commit time, because rebases rewrite commit time. Skip merge commits. Count a cherry-picked change once by matching `git patch-id`.
2. **Find issue keys.** Take the key from the branch name, then from the commit message. Keep only keys whose project is in `project_keys`. A commit with two keys splits evenly; a commit with none is **untracked**.
3. **Build sessions on one timeline.** Sort commits from all repos together. A gap longer than `idle_gap_minutes` starts a new session. Reflog entries inside a session extend it, but reflog alone never creates a session.
4. **Set each session's span.** The start is the first reflog checkout in the session if there is one, otherwise the first commit minus `lead_in_minutes`. The end is the last commit. Clamp to `min_session_minutes` and `max_session_minutes`. Split at local midnight.
5. **Apply the meeting policy.** Under `ambient`, minutes that overlap a busy calendar event count at `ambient_weight`. Under `overlap` and `independent`, they count in full and the day cap does the work (next section).
6. **Cap the day.** If the day's session minutes exceed the policy's cap, scale every session down by the same factor.
7. **Attribute.** Split each session's minutes across its issues by commit count. Minutes for untracked commits stay untracked: reported, never logged.
8. **Round down.** Sum per issue per local day, then round down to `round_to_minutes`.
9. **Write the proposal.** Store sessions, per-issue `day_proposals` with their basis text, and the delta against time already logged.

Commit count, not lines changed, is the attribution weight. A vendored dependency or bulk reformat dwarfs an afternoon of debugging in line counts. Keep that reason as a comment in the code so nobody "optimises" it later.

## Meetings and concurrency

Meetings and coding overlap; treating them as mutually exclusive was the old module's central bug. Exactly one policy applies to each day, so meeting time is never discounted twice.

| Policy | When it applies | Rule | Dev cap |
| --- | --- | --- | --- |
| `ambient` (default) | The day has meeting start and end times | Minutes that overlap a busy meeting count at `ambient_weight` | `max_daily_dev_minutes` |
| `overlap` | Meeting totals are known but not their times | All minutes count in full | `max_daily_dev_minutes − meeting minutes × (1 − concurrent_fraction)` |
| `independent` | No calendar data, or you choose it | All minutes count in full; the day is flagged if meetings plus dev exceed your tour of duty | `max_daily_dev_minutes` |

Every calendar event counts as a meeting except declined, cancelled, all-day, free and out-of-office ones. Tentative events count, which keeps the estimate low.

### Worked example

Meetings run 09:00–12:00 and 12:30–16:30, 7 hours in all. PROJ-42 gets commits at 09:50, 10:20, 10:55, 11:40, 14:30 and 15:05; PROJ-51 at 12:10 and 12:25. With a 120m gap and 30m lead-in that makes two sessions:

- **Session 1**, 09:20–12:25: 185 minutes, 160 of them during meetings; 4 commits on PROJ-42, 2 on PROJ-51.
- **Session 2**, 14:00–15:05: 65 minutes, all during a meeting; 2 commits on PROJ-42.

| Policy | Day cap | Dev estimate | PROJ-42 | PROJ-51 |
| --- | --- | --- | --- | --- |
| Subtractive (old, removed) | 480 − 420 = 60m | 45m | 45m | 0m |
| `overlap`, fraction 0.5 | 480 − 210 = 270m | 4h00m | 3h00m | 1h00m |
| `ambient`, weight 0.5 | 480m | 2h00m | 1h30m | 30m |
| `independent` | 480m | 4h00m, flagged: 7h + 4h exceeds the day | 3h00m | 1h00m |

Under `ambient`, session 1 counts 160 × 0.5 + 25 = 105 minutes and session 2 counts 32.5, for 137.5 in total. Commit shares give PROJ-42 102.5 minutes and PROJ-51 35, which round down to 1h30m and 30m.

`ambient` is the default because it uses when the commits happened: a morning of calls and a focused afternoon aren't averaged together. Both `ambient_weight` and `concurrent_fraction` start at 0.5 as guesses, and calibration replaces them with measured values.

## Muninn: what Baldur reads and writes

Baldur is Muninn's first real client: it owns the git and time tables from the Muninn design and adds four tables, two views and a few columns to the schema. Muninn hasn't shipped yet, so fold these into v1; if it ships first, they apply unchanged as migration 0002. The SQL below passed its checks against `muninn_schema.sql` v1.

| Table | Baldur | Notes |
| --- | --- | --- |
| `identities`, `sources`, `meta` | Reads | Your git emails decide `is_mine`; no identity means no run |
| `repos`, `commits`, `commit_work_items`, `reflog_entries` | Writes (owner) | Incremental, via `sync_cursors`; metadata only |
| `pull_requests`, `pr_reviews` | Writes (owner), later phase | Needs GitHub access; shown in the day report, not logged |
| `calendar_events` (new) | Writes (owner) | The schedule; Loki's `meetings` holds recaps |
| `estimate_runs` (new) | Writes | Frozen settings for each run |
| `work_sessions`, `session_commits` (new), `session_allocations` | Writes | Sessions, their commits, and minutes per ticket |
| `day_proposals` (new) | Writes | The unit you review: minutes per ticket per local day |
| `calibration_runs`, `time_actuals` | Writes | Trial-period hours and fitted settings |
| `worklogs` | Writes `origin = 'baldur'`, posting phase only | Needs the ticket in `work_items`, which only Odin can supply |
| `work_items` | Reads | Empty until Odin syncs to Muninn; until then keys come from git |
| `events` | Appends | `estimate_run.created`, `day_proposal.approved`, `worklog.posted`, `calibration.accepted` |

Three checks in `day_proposals` carry the principles into the database: rounding can't raise a number, approval needs a decision time and final minutes, and untracked minutes can never be approved.

```sql
-- Baldur additions to Muninn. Fold into v1, or apply as migration 0002.
BEGIN;

ALTER TABLE commits ADD COLUMN patch_id TEXT;  -- count a cherry-picked change once
CREATE INDEX ix_commits_patch_id ON commits (patch_id) WHERE patch_id IS NOT NULL;

CREATE TABLE calendar_events (
    id             INTEGER PRIMARY KEY,
    source_id      INTEGER NOT NULL REFERENCES sources(id),
    external_id    TEXT NOT NULL,
    title          TEXT NOT NULL,
    starts_at      TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', starts_at) IS starts_at),
    ends_at        TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', ends_at) IS ends_at),
    is_all_day     INTEGER NOT NULL DEFAULT 0 CHECK (is_all_day IN (0,1)),
    show_as        TEXT NOT NULL DEFAULT 'busy' CHECK (show_as IN ('busy','tentative','free','oof','unknown')),
    response       TEXT NOT NULL DEFAULT 'none' CHECK (response IN ('organizer','accepted','tentative','declined','none')),
    is_cancelled   INTEGER NOT NULL DEFAULT 0 CHECK (is_cancelled IN (0,1)),
    logged_by      TEXT,  -- 'odin' when Odin already logs this meeting in Jira
    meeting_id     INTEGER REFERENCES meetings(id) ON DELETE SET NULL,
    first_seen_at  TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', first_seen_at) IS first_seen_at),
    last_seen_at   TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_seen_at) IS last_seen_at),
    deleted_at     TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', deleted_at) IS deleted_at),
    run_id         INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL,
    UNIQUE (source_id, external_id),
    CHECK (ends_at >= starts_at)
) STRICT;
CREATE INDEX ix_calendar_events_starts ON calendar_events (starts_at) WHERE deleted_at IS NULL;

CREATE VIEW v_busy_meetings AS
    SELECT * FROM calendar_events
     WHERE deleted_at IS NULL AND is_all_day = 0 AND is_cancelled = 0
       AND response <> 'declined' AND show_as IN ('busy', 'tentative');

CREATE TABLE estimate_runs (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                    CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    date_from       TEXT NOT NULL CHECK (date(date_from) IS date_from),
    date_to         TEXT NOT NULL CHECK (date(date_to) IS date_to),
    model_version   TEXT NOT NULL,  -- 'baldur-1'
    params          TEXT NOT NULL CHECK (json_valid(params) AND json_type(params) = 'object'),
    params_hash     TEXT NOT NULL,
    calibration_id  INTEGER REFERENCES calibration_runs(id),
    CHECK (date_to >= date_from)
) STRICT;

ALTER TABLE work_sessions ADD COLUMN estimate_run_id INTEGER REFERENCES estimate_runs(id) ON DELETE CASCADE;
ALTER TABLE work_sessions ADD COLUMN local_date TEXT CHECK (date(local_date) IS local_date);
ALTER TABLE work_sessions ADD COLUMN policy TEXT CHECK (policy IN ('ambient','overlap','independent'));
ALTER TABLE work_sessions ADD COLUMN focused_minutes REAL CHECK (focused_minutes >= 0);
ALTER TABLE work_sessions ADD COLUMN ambient_minutes REAL CHECK (ambient_minutes >= 0);
CREATE INDEX ix_sessions_run_day ON work_sessions (estimate_run_id, local_date);

CREATE TABLE session_commits (
    session_id  INTEGER NOT NULL REFERENCES work_sessions(id) ON DELETE CASCADE,
    commit_id   INTEGER NOT NULL REFERENCES commits(id),
    PRIMARY KEY (session_id, commit_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE day_proposals (
    id                INTEGER PRIMARY KEY,
    estimate_run_id   INTEGER NOT NULL REFERENCES estimate_runs(id) ON DELETE CASCADE,
    local_date        TEXT NOT NULL CHECK (date(local_date) IS local_date),
    work_item_key     TEXT,  -- NULL = untracked
    work_item_id      INTEGER REFERENCES work_items(id),
    minutes_raw       REAL NOT NULL CHECK (minutes_raw >= 0),
    minutes_proposed  INTEGER NOT NULL CHECK (minutes_proposed >= 0),
    minutes_final     INTEGER CHECK (minutes_final >= 0),
    basis             TEXT NOT NULL,
    basis_hash        TEXT NOT NULL,
    review            TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(review) AND json_type(review) = 'object'),
    status            TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','approved','rejected','superseded')),
    decided_at        TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', decided_at) IS decided_at),
    CHECK (minutes_proposed <= minutes_raw),
    CHECK (status <> 'approved' OR (decided_at IS NOT NULL AND minutes_final IS NOT NULL)),
    CHECK (work_item_key IS NOT NULL OR status IN ('proposed','superseded'))
) STRICT;
CREATE UNIQUE INDEX ux_day_proposals ON day_proposals (estimate_run_id, local_date, coalesce(work_item_key, ''));
CREATE INDEX ix_day_proposals_day ON day_proposals (local_date, work_item_key, status);

ALTER TABLE calibration_runs ADD COLUMN ambient_weight REAL CHECK (ambient_weight BETWEEN 0 AND 1);

ALTER TABLE worklogs ADD COLUMN proposal_id INTEGER REFERENCES day_proposals(id);
ALTER TABLE worklogs ADD COLUMN basis_hash TEXT;

-- Agreed and posted minutes per day and ticket: the base for delta logging.
CREATE VIEW v_day_status AS
    SELECT p.local_date, p.work_item_key,
           (SELECT a.minutes_final FROM day_proposals a
             WHERE a.local_date = p.local_date AND a.work_item_key = p.work_item_key
               AND a.status = 'approved'
             ORDER BY a.decided_at DESC, a.id DESC LIMIT 1) AS approved_minutes,
           coalesce((SELECT sum(w.seconds) FROM worklogs w JOIN day_proposals q ON q.id = w.proposal_id
                      WHERE q.local_date = p.local_date AND q.work_item_key = p.work_item_key
                        AND w.state = 'posted'), 0) / 60 AS posted_minutes
      FROM day_proposals p
     WHERE p.work_item_key IS NOT NULL
     GROUP BY p.local_date, p.work_item_key;

PRAGMA user_version = 2;
COMMIT;
```

## Settings

Baldur's settings live in `%LOCALAPPDATA%\Asgard\settings\baldur.json`, created with these defaults on first run. A file that won't parse falls back to the defaults with a warning, as `apps.local.json` does. Each run copies its settings into `estimate_runs.params`, so changing a default never changes past numbers.

Your git identities aren't a Baldur setting: they live in Muninn's `identities`, shared with every app. On first run Baldur offers the email from `git config user.email` and refuses to estimate with none. That prevents logging the whole team's commits to one timesheet.

| Key | Default | Meaning |
| --- | --- | --- |
| `repo_roots` | `[]` | Folders to search for repositories. Found repos go into Muninn `repos`, where each can be switched off |
| `max_depth` | `3` | How deep to search under each root; never the whole disk |
| `project_keys` | `[]` | Jira projects to recognise, such as `["PROJ", "OPS"]`. Empty is refused |
| `idle_gap_minutes` | `120` | A gap longer than this starts a new session; the most influential setting |
| `lead_in_minutes` | `30` | Time before a session's first commit, used only when there's no reflog checkout |
| `min_session_minutes` | `15` | Floor, so a lone commit isn't zero |
| `max_session_minutes` | `240` | Ceiling, so commits at 09:00 and 17:00 aren't one 8-hour session |
| `max_daily_dev_minutes` | `480` | Development ceiling per day |
| `policy` | `"ambient"` | `ambient`, `overlap` or `independent`; see Meetings and concurrency |
| `ambient_weight` | `0.5` | Share of an overlapping minute that counts, under `ambient` |
| `concurrent_fraction` | `0.5` | Share of meeting time assumed usable for coding, under `overlap` |
| `round_to_minutes` | `15` | Each ticket's daily total rounds down to this |
| `tour_of_duty` | `{"start": "07:30", "end": "16:00", "unpaid_minutes": 30}` | Used only to flag days in the report |
| `calendar_source` | `"odin"` | `odin` reads Odin's synced meetings read-only; `ics` reads an exported calendar file; `none` |
| `output` | `"report"` | `report` copies the approved day to the clipboard; `post` writes Jira worklogs (posting phase) |
| `review_mode` | `"off"` | AI review: `off`, `metadata` or `content` |

Two old settings are gone on purpose. Approval can't be switched off, and untracked minutes can't be logged; the database refuses both rather than trusting a flag.

## Review, approval and logging

You review in Baldur's window, opened from its Asgard tile. Each day shows a reconciliation report and one row per ticket. You can edit any figure, then approve or reject the day. **Copy for timesheet** puts the approved day on the clipboard; posting to Jira comes in a later phase.

### The day report

The report matters more than the per-ticket numbers. It shows what was measured, what was estimated, and what Baldur can't see. Here is the worked example day:

```
Thu 2026-10-01                                      policy: ambient
  Meetings (calendar)                    7h00m   logged by Odin
  Development (estimated from git)       2h00m   PROJ-42 1h30m, PROJ-51 30m
  Untracked commits (no Jira key)          0m
  Pull request reviews (not logged)      1       asgard#7
  ------------------------------------------------
  Accounted                              9h00m
  Tour of duty                           8h00m   07:30-16:00, 30m unpaid
  Overlap assumed                        1h00m   coding during calls

  Not visible to Baldur: design talk, reading code, messages, planning.
  If the day felt fuller than this, that is the gap.
```

### The basis

Every approved figure stores its derivation in `day_proposals.basis`. That text also becomes the Jira worklog comment, so the number can be defended months later without re-deriving it:

```
Baldur estimate: 1h30m for PROJ-42 on 2026-10-01
Basis: 6 commits in 2 sessions (09:20-12:25, 14:00-15:05), repo asgard
Model: baldur-1, policy ambient 0.5, gap 120m, lead-in 30m, rounded down to 15m (run 41)
Meetings that day: 7h00m, logged separately
Reviewed: AI review moved 15m to PROJ-51 ("retry rework is the larger change")
Approved: 2026-10-01 17:02
```

`basis_hash` covers the model version, settings, commit SHAs and meeting IDs. A re-run with the same hash has nothing new to say; a different hash shows whether the evidence or the settings changed.

### Logging only the difference

- **Approving** a day records the total you agreed for each ticket and supersedes earlier approvals for that day.
- **Re-running** compares against the agreed total, so new commits show up as an addition to approve, never as a second copy.
- **Posting** sends only *agreed minus already posted*, read from `v_day_status`. If the new figure is lower, Baldur reports the gap and never deletes posted time.
- The `worklogs` row is written as soon as Jira confirms, before anything else, so a crash can't post the same time twice.

### Calibration

The 0.5 weights and 120-minute gap are guesses until measured. Calibration measures them:

1. During a trial of 2–4 weeks, note your real hours each day in Baldur, as a day total and per ticket where you can. They go into `time_actuals`.
2. Baldur searches gap 60–180m, lead-in 0–60m and ambient weight 0.3–0.8, looking for the lowest daily error **among settings that estimate low on average**, which keeps the bias downward.
3. It shows the old and new error side by side. Accepting creates an active `calibration_runs` row and updates `baldur.json`; nothing changes automatically, and past runs keep their settings.

## Optional AI review

Commit counts can't tell five typo fixes from one hard debugging session; a model reading commit subjects can. The review only moves time between tickets or lowers it, and Baldur checks every reply in code before showing it. It is off by default and never posts anything.

### How it runs

The review uses whichever of Asgard's AI tiers you have:

- **API:** Baldur sends the day's evidence to an approved endpoint and uses its smallest model.
- **MCP:** Ysildir exposes the day's evidence and the review prompt; your AI client returns the reply.
- **Clipboard:** Baldur copies evidence and prompt; you paste the reply back into Baldur.

### Privacy gate

`review_mode` decides what leaves the machine, and setting it is a data-handling decision, not a convenience:

- **`metadata`** (recommended): commit subjects, file paths, line counts, times, ticket keys and session IDs. No code.
- **`content`:** adds diff hunks. Raise this with your ISSO before turning it on.

Commit subjects carry most of the signal: "rework retry logic" shows a commit was substantial without shipping the retry logic.

### The reply, checked in code

The model must return JSON. Baldur rejects the whole reply, shows why, and keeps the baseline if any check fails:

1. The adjustments sum to zero or less: the day's total never rises.
2. Every adjustment names a ticket already in the baseline, and none goes below zero.
3. Every adjustment cites at least one commit SHA or session ID from the evidence sent.
4. At most 10 adjustments; the result is rounded down again.

Accepted adjustments appear as suggestions you take or leave per ticket, stored in `day_proposals.review` with the prompt version.

### The prompt

It ships as `apps/baldur/prompts/review.md`, and its version is recorded with each review.

```markdown
You adjust a development-time estimate that was calculated from git history.
You don't estimate from scratch and you can't post anything.

Input: for one day, the baseline minutes per Jira ticket, the sessions (id, start,
end, commit SHAs), each commit's subject and line counts, and which session
minutes overlapped meetings.

Return only this JSON:
{"day": "YYYY-MM-DD",
 "adjustments": [{"ticket": "KEY-1", "minutes": -15, "confidence": "high|medium|low",
                  "evidence": ["<sha or session id>"], "reason": "<one sentence>"}],
 "flags": ["<anything the person should check before approving>"]}

Rules, in priority order:
1. Never raise the day's total. Move minutes between tickets, or lower them.
2. Cite evidence for every adjustment: a commit SHA or session id from the input.
3. Prefer moving time to removing it. Commit counts are a crude weight: five trivial
   commits on one ticket and one hard commit on another is the usual error.
4. When two readings are equally plausible, choose the smaller.
5. Use low confidence freely, and flag instead of guessing.
6. Never invent work. Note in flags if the day looks under-represented, but add no time.
```

## Edge cases

Each rule errs low, matching the principles.

| Case | Rule |
| --- | --- |
| Rebased or amended commits | Use author time; count each SHA once, then each `patch-id` once |
| Squash merges | Skip a squash commit when its message cites a pull request of yours whose branch commits are already collected |
| Two repos worked in parallel | One timeline across repos, so overlapping work isn't counted twice |
| A commit naming two tickets | Split its share evenly |
| Co-authored commits | Count only commits you authored; list `Co-authored-by` ones in the report |
| A session across midnight | Split at local midnight; each part belongs to its own day |
| Time zones and daylight saving | Convert with Windows' local time through the standard library; no time-zone package needed |
| Dates older than 90 days | Reflog is gone, so lead-in falls back to 30m; the report says so |
| Days with no calendar data | Use `independent` and flag the day |
| Meetings Odin already logs | Used only to weight overlap; Baldur never logs meeting time |
| Vendored or reformatted code | No effect: attribution counts commits, not lines |
| Future-dated or skewed commit times | Excluded and flagged |
| Weekends and leave days | Estimated like any day and flagged against your tour of duty; nothing is approved automatically |
| Odin is running while Baldur reads its database | Open Odin's `state.db` read-only and wait up to 5 seconds for a lock |

## Build order

Steps 1–6 deliver the report-only product. Posting and AI review come after a trial shows the numbers are sound.

1. **Muninn writer module** (`asgard/muninn/`) with schema v1 and the Baldur additions folded in. Every later step writes through it.
2. **Port and fix.** Move `gitwork.py` and its 34 tests into `apps/baldur/`, then replace the subtractive cap with the three policies. The worked example becomes a named test.
3. **Collector.** Repo discovery, commits with `patch-id`, ticket keys, reflog and first-run identity setup, synced incrementally into Muninn. It runs from the window or a weekly per-user scheduled task, because reflog expires.
4. **Calendar importer.** Odin's synced meetings, read-only, or an `.ics` export, into `calendar_events`.
5. **Estimator.** Writes `estimate_runs`, sessions and `day_proposals`, with basis text and `basis_hash` from the first version, so the audit trail is never retrofitted.
6. **Baldur window.** Week view, day report, edit, approve, **Copy for timesheet**. `apps/baldur/baldur.pyw` adds `ASGARD_APP` to its import path to reach `asgard.muninn`. Its tile in `apps.json` changes to `"status": "available"` with `"target": "{app}\\apps\\baldur\\baldur.pyw"`.
7. **Trial and calibration.** Two to four weeks of real hours, then fitted settings you accept.
8. **Posting to Jira.** Delta logging through `v_day_status`. It needs your tickets in Muninn's `work_items`, which means Odin syncing to Muninn first.
9. **Pull request reviews** in the day report, once GitHub access is settled.
10. **AI review**, last: an optional refinement on a baseline that already works.

## Tests

Tests check the direction of bias, not just exact values. Proving a cap or rounding never raises a number is worth more than pinning one minute count. Use real temporary git repositories, as the existing 34 tests do, rather than a mocked `subprocess`.

- **Direction:** every cap, rounding and scaling step leaves each number equal or lower, over randomised inputs.
- **Worked example:** the meetings-plus-commits day gives 2h00m under `ambient`, 4h00m under `overlap`, and never the old 45 minutes.
- **No double discount:** for any day, `ambient` minutes never fall below what a single discount would give, so the policies can't compound.
- **One timeline:** commits interleaved across two repos produce one set of sessions.
- **Deltas:** approve 2h, re-estimate 2h30m, and only 30m is offered; re-run with no new commits and nothing is offered.
- **Never retract:** a lower re-estimate reports the gap and leaves posted worklogs alone.
- **Hash:** `basis_hash` changes when settings change and stays the same when nothing does.
- **Schema:** the database rejects a rounded-up proposal, an approval without decision time, and an approved untracked row.
- **AI review:** replies that raise the total, invent a ticket, or cite missing evidence are rejected.
- **Identity:** an empty identity list or empty `project_keys` refuses to run.

## Open decisions

- [ ] **Report only, or post to Jira?** A report you paste into your own timesheet sidesteps a tool writing inferred hours into an official system, and may be the better product. Nothing before step 8 depends on the answer.
- [ ] **Will Odin sync to Muninn?** Posting needs your tickets in `work_items`. Without that, Baldur stays report-only and reads Odin's meetings directly.
- [ ] **Odin's meeting fields.** Confirm that `state.db` holds start, end, response and whether Odin logged each meeting, so the importer can map them.
- [ ] **Schema timing.** Fold the Baldur additions into Muninn v1 (and its design doc), or ship them as migration 0002.
- [ ] **AI review.** Is an AI tool on the workstation approved for this use, and in `metadata` or `content` mode?
- [ ] **Pull request reviews.** Show them in the day report only, or let them become loggable time later?
- [ ] **Trial length.** Two weeks or four of real hours before the first calibration.
