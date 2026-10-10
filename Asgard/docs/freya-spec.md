# Freya spec: review drafts from what you actually did

Oct 9, 2026 (schema numbering updated Oct 10) · **work in progress** · for Asgard 0.5 · builds on Muninn v5 on main: v4 added Baldur's agent estimates, v5 Odin's `meeting_subtasks`; Ysildir has merged

Freya keeps a durable record of your work all year and drafts your mid-year and yearly reviews from it. The model only puts the evidence into words; it never decides what you did. Every sentence in a draft must cite facts Freya sent, every number must appear in those facts, and code checks both before you see the draft. Nothing is final until you mark it so. Freya never sends anything anywhere itself. You paste the draft into the appraisal system.

## Principles

1. **Evidence first, words last.** Freya collects, totals and groups the evidence in code. The model writes sentences from a closed pack and can't read Muninn.
2. **Every claim cites; every number checks.** A sentence with a fact ID that wasn't sent, a number that isn't in its cited facts, or a name that wasn't in the pack is refused, not shown.
3. **Gaps are shown, never filled.** An element with thin evidence says so, and the model is told to write less, not to pad.
4. **Your words outrank its words.** Your impact notes are the strongest evidence. Sentences you write yourself are allowed and marked as yours.
5. **Minimum data leaves the machine.** Summaries, keys, counts, dates and your notes, through an allow-list. Never descriptions, transcripts, code, or other people's names.
6. **You decide.** Marking final, closing a period, notes and tags are yours. An AI agent can draft through Ysildir; it can't decide.
7. **Same pattern as Baldur's AI review.** A pack with a `pack_hash`, a strict reply parser, a check in `asgard.muninn` that refuses with a reason, a privacy setting, and the three tiers (clipboard first). Someone who knows `baldur.cmd ai pack` already knows Freya's.

## What Freya reads, from whom

Freya writes only its own tables. Everything else it reads, through the views where they exist.

| App | Tables and views | Becomes evidence of | Fact kind |
| --- | --- | --- | --- |
| Odin | `work_items` (done, resolution, epic, points), `work_item_aliases`, `work_item_transitions` (`by_me`) | Delivered work; scope by epic; cycle time (first `by_me` transition into progress, then done) | `W` work, `X` totals |
| Odin | meeting `worklogs`, `calendar_events` (organizer = you) | Meeting load; recurring sessions you ran | `MT` meeting time, `M` meeting |
| Baldur | `v_day_status`, `v_time_by_item_month` (approved days only) | Approved development hours per ticket and quarter, finished or not | `T` time |
| Baldur | `pull_requests`, `pr_reviews` (`is_mine`) | Changes shipped; reviews you gave others | `PR`, `RV` |
| Baldur | `time_actuals` (calibration trials) | Real hours you recorded, the strongest time evidence | `T` (marked "recorded") |
| Baldur v4 | `agent_estimates` | **Not cited by default.** An agent's one-line summary of a change is AI-written text; off unless you switch it on (`use_agent_summaries`), and then only as a description of a change, never of impact | `AG` |
| Loki | `blufs` (posted, `sections.decisions`), `action_items` (yours, done) | Decisions documented and shared; follow-through | `B`, `AI` |
| Heimdall, Bifrost | `submissions` (accepted; returned then accepted), `submission_status_history` | Security and compliance deliverables | `S` |
| Freya | `accomplishments` (impact note, highlight), manual wins, element tags | Your words: the result no data source has | `W`, `N` |
| All | `events` (`work_item.done`, `.reopened`, `.deleted`) | The timeline, with dates; a win that reopened is flagged, never claimed as done | — |
| All | `sync_runs` | How fresh each source is (pre-flight) | — |

Commit counts and lines changed are left out of the pack. They're context Baldur uses, not merit, and putting them in a review invites the wrong reading.

### Until Odin's issues are in Muninn

Freya's ledger fills from Odin's `work_item.done` events, and Odin doesn't sync issues into Muninn yet (its move is in progress elsewhere). Meanwhile, `freya.cmd import FILE.csv` reads a CSV export of a Jira filter, such as `resolution changed by currentUser()` or `resolved >= -365d AND assignee was currentUser()`. Each row becomes an accomplishment: issue id, key, summary, type, resolution, resolved date, epic link. `accomplishments` is Freya's table and the real Jira ID is its key, so when Odin's events start arriving they update the same rows. Baldur's approved time attaches by key already, so the time totals work without Odin.

## Fact IDs

Every fact in a pack has an ID that a claim cites, and that a person can read:

| ID | Fact | Built from |
| --- | --- | --- |
| `W:PROJ-42` | A finished issue, under its current key | `accomplishments` (+ `work_items` when present) |
| `N:3` | A manual win (training, mentoring, an incident) | `manual_wins` |
| `T:PROJ-42@FY26Q2` | Approved minutes on a ticket in a quarter, with the approval IDs it sums | `v_day_status` |
| `MT:FY26Q2` | Meeting hours in a quarter | Odin's meeting worklogs |
| `PR:csb/asgard#7` | A merged pull request of yours | `pull_requests` |
| `RV:FY26Q2` | Reviews you gave in a quarter: count and repositories | `pr_reviews` |
| `M:9f3c` | A meeting you organized or recapped (hash of its ID) | `calendar_events`, `meetings` |
| `B:31`, `AI:12` | A posted BLUF; an action item you closed | `blufs`, `action_items` |
| `S:secchm/CHG1` | An accepted submission | `submissions` |
| `X:cycle-PROJ-42` | A computed number, with its formula and inputs | code, never the model |

A fact carries its kind, dates, a cleaned one-line title, its numbers, its element tags, and the source rows behind it (`table:id` list). The source rows are what make a citation checkable later.

## The pipeline

### 1. Pre-flight (before any pack)

`freya.cmd preflight PERIOD` and the Drafts page list what would weaken the draft:
- days in the period not yet reviewed in Baldur;
- wins with no element tag, and wins with no note;
- keys Odin couldn't resolve;
- each source's last successful sync;
- `--muninn check` errors.

You fix them or accept them. The draft records what was accepted, so the record says it was written on that basis.

### 2. Collect and total (deterministic)

SQL builds the facts for the period. Code computes every total: hours, counts, cycle times, quarter spreads. Each total becomes an `X` fact carrying its inputs. The same Muninn state always produces the same facts.

### 3. Map to elements

The rubric is stored on the period: each element's name, standard text, rating-level descriptions and character limit. A fact belongs to an element by your confirmed tag. For untagged facts, Freya suggests a tag from the epic, labels, and a full-text search of the element's wording against titles; you confirm with a click. The Coverage page shows elements by quarter with their facts. That's the mid-year early warning.

### 4. Select and budget

Per element, rank highlights first, then wins with notes, then hours, keeping the quarters spread. Keep what fits the model's input budget and the element's character limit. If fewer than the minimum facts remain (setting `thin_element`, default 3), the pack marks the element thin and the prompt says to write briefly.

### 5. Input gate (what may leave)

`draft_mode` decides whether a pack may leave the machine at all, like Baldur's `review_mode`:
- **`off`** (default): no packs. The Coverage page and the evidence list still work.
- **`metadata`**: the allow-list below.
- Nothing more is ever offered.

The allow-list policy is `freya.review/1`:
- **Fields:** fact IDs, kinds, dates, cleaned titles (one line, 120 characters), numbers, element tags, your notes. Never Jira descriptions (not stored), worklog comments, meeting notes, `submissions.fields`, code or file paths.
- **People become roles.** Assignees, reporters, reviewers, organizers and action-item owners become "a teammate", "the team lead" or "another team". The role map lives in Freya's settings. The pack holds no person's name except yours, and only if you allow it.
- **Untrusted text.** Titles and summaries go through `clean_line` and `scrub`, sit in data fields named in `untrusted_fields`, and the prompt tells the model they are data, never instructions.

### 6. The pack and the prompt

The pack (`freya.review_pack/1`) holds:
- the element and its standard;
- the period and whether it's a mid-year or yearly draft;
- the facts;
- the character limit;
- what the mid-year draft said about this element, for a yearly draft;
- the thin flag.

Its `pack_hash` names exactly this evidence. The prompt is versioned on its first line (`<!-- freya-review-1 -->`), like Baldur's, and asks for JSON only:

```json
{"element": "E2", "pack": "<pack_hash>",
 "claims": [{"text": "Delivered the retry service for nightly imports, cutting failed runs.",
             "cites": ["W:PROJ-42", "X:failures-PROJ-42"]}],
 "gaps": ["No evidence of mentoring in Q3."]}
```

Freya assembles the claims into the element's paragraph; the model never returns free prose. The tiers:
- **Clipboard** (first): `freya.cmd pack PERIOD ELEMENT` prints the prompt and pack; `freya.cmd draft PERIOD ELEMENT ANSWER.json` takes the reply.
- **MCP:** Ysildir tools, off by default.
- **API:** Mímir, once an endpoint is approved.

### 7. The check (`asgard.muninn.freya.check_draft`, standard library)

The reply is refused whole, with a reason in plain words, if any hard rule fails:

1. It carries the `pack_hash` it was given, and the evidence hasn't changed since. A new approval or a new note means a new pack.
2. The element is the one asked for; at most `max_claims` claims (default 8).
3. Every claim cites at least one fact ID from this pack.
4. **Numbers:** every number in a claim's text appears among its cited facts' numbers. Rounding to the unit shown is allowed (93.5 h may read "about 90 hours"; 40 % only if a fact holds it). Hours from Baldur must be phrased "approved" or "about", never "worked".
5. **Names and references:** no Jira key, PR reference, repository, system name or known person (any name in Muninn's people fields) appears unless it is in a cited fact. Roles are fine.
6. **Fitting evidence:** claim verbs need a fitting fact kind, through a verb map in the policy. "Led", "organized" or "ran" needs `M` as organizer, `B` or `N`. "Delivered", "shipped" or "completed" needs `W` done or a merged `PR`. "Reviewed" or "mentored" needs `RV` or `N`. A reopened win can't be "completed".
7. **Dates:** cited facts fall inside the period.
8. **Length:** the assembled paragraph fits the element's character limit.

Soft rules warn, and the draft is still shown: phrases from the policy's list ("single-handedly", "always", "world-class"), superlatives without a cited number, two claims citing the same facts, a thin element with more than two claims.

Gaps are kept as notes for you and never become text.

### 8. You, then final

- **The draft page:** each element shows its sentences. Point at one to see the facts it cites; soft warnings are marked.
- **Editing:** you may edit anything. An edited sentence is re-checked (rules 3 to 8). A sentence you add without citations is marked "your words": allowed, visible, never silently counted as evidence.
- **Mark final** (per element): refused while any cited fact no longer matches Muninn, for example an approval changed since. A database trigger stops `status = 'final'` while the draft has unverified citations.
- **Close the period:** freezes accomplishments (`frozen_at`, already in the schema) and writes the period's time snapshot. Notes and highlights can't change after that (trigger).

### 9. The record

Every draft version stores:
- the element, the pack and its hash;
- the tier, model and prompt version;
- the check's result and the soft warnings;
- the claims with their citations;
- what pre-flight items you accepted.

A paragraph that changed can be traced to new evidence, a new prompt or a new model.

## Mid-year and yearly

- **Mid-year** (`kind = 'mid_year'`): per element, progress so far, the gaps from the Coverage page, and a short plan for the second half. Fewer claims, a lower character budget, and "planned" claims that cite nothing are allowed, marked as plans.
- **Yearly** (`kind = 'yearly'`): a narrative per element in the rubric's language. The pack includes the mid-year draft's claims, so the year reads as one story. Freya shows what changed since mid-year.
- **The same evidence, other prompts (later):** award nominations, a brag-doc export, and a self-assessment for a new supervisor. Each is a new prompt with the same checks.

## Muninn changes (Freya's migration)

Numbered after Odin's v5: `0006_freya.sql`, which takes schema v6 (check `asgard/muninn/migrations/` for the next free number before writing it). All of it is additive except one rebuild of a table that is still empty.

| Change | Why |
| --- | --- |
| `review_periods`: add `kind` (`mid_year`, `yearly`), `char_limits` JSON, `accepted_preflight` JSON | Two kinds of review; limits per element |
| `element_tags (period_id, fact_ref, element, by)`, with `by` = `you` or `suggested`, kept until confirmed | Mapping facts to elements |
| `manual_wins` (`id`, `period_id`, `on_date`, `title`, `impact_note`, `element`) | Wins that aren't Jira issues |
| `review_packs (id, period_id, element, kind, pack_hash UNIQUE, pack JSON, created_at)` | What was sent, so every citation resolves to what the model saw |
| `review_drafts`: add `pack_id`, `check` JSON, `warnings` JSON | Ties a draft to its pack and its check |
| `review_claims (draft_id, n, text, cites JSON, origin)`, origin = `model`, `you` or `plan` | Claims stay structured, so edits re-check per claim |
| `period_time (period_id, work_item_key, quarter, minutes, proposal_ids JSON)` | The time snapshot written at close |
| Rebuild `citations` to widen `evidence_type` (`time`, `pr_review`, `submission`, `action_item`, `manual_win`, `meeting_time`, `total`). The CHECK lists six kinds today, and SQLite can't change a CHECK in place. The table is empty because neither Loki nor Freya is built, so the rebuild is free now and costly later | Every fact kind can be cited |
| Triggers: no `final` draft with unverified citations; notes, highlights and tags frozen after `closed_at`; packs, claims of final drafts and the time snapshot are facts (never updated or deleted) | The gates hold even for plain SQL |
| `guard.OWNERS`: Freya owns all of the above; `guard.IDENTITY_KINDS` unchanged (Freya writes no identities) | Rule 2 |

The rules go in `asgard/muninn/freya.py` (standard library), beside `muninn.baldur`, so the window, the CLI and Ysildir follow one set of rules. These are:
- `record_finish`, `refresh`, `import_csv`;
- `tag`, `note`, `add_manual_win`;
- `build_pack`, `parse_reply` (shared with Baldur's), `check_draft`, `store_draft`;
- `mark_final`, `close_period`.

## Events

- **Consumes**, in one `consume()` call: `work_item.done`, `work_item.reopened`, `work_item.deleted`. Later also `bluf.posted` and `submission.status_changed`, to refresh coverage.
- **Emits:** `accomplishment.recorded`, `review_draft.created`, `review_draft.final`, `review_period.closed`. Payloads hold IDs and states, never draft text.

## The app

- **Shared Qt window pages** (manifest under `apps/freya/ui/`, plain-Python backend `freya/ui_backend.py`, Heimdall's pattern):
  - **Wins:** filter untagged or without notes; note, highlight, tag.
  - **Coverage:** elements by quarter.
  - **Time:** approved and meeting hours by ticket and quarter, including unfinished work.
  - **Periods:** rubric, limits, close.
  - **Drafts:** per element Pack, Paste answer and Check; sentence-level evidence; Mark final.
  - **Dashboard cards:** wins without notes, thin elements, citations to check.
- **CLI** (`freya.cmd`): `import`, `wins`, `tag`, `note`, `win add`, `coverage`, `preflight`, `pack`, `draft`, `show`, `final`, `close`, `export`. `--json` where a program reads it. Exit codes 0, 1, 2 and 130, per the app contract.
- **Settings** (`settings\freya.json`):
  - `draft_mode`;
  - `period_defaults`, with the fiscal year from October to September;
  - the role map;
  - phrase and verb lists;
  - `max_claims`, `thin_element`;
  - `use_agent_summaries` (default off);
  - which resolutions count as wins.

## Ysildir and agents

- **Ysildir tools** (`.kiro/specs/ysildir-mcp/`), each switch in `ysildir.json`:
  - **`freya_coverage`, on by default:** elements and counts only.
  - **`freya_pack`, off:** sends titles and notes.
  - **`freya_submit_draft`, off:** stores a checked draft, never final.

  It connects as `freya` for writes, with `supported` covering Freya's version.
- **Never a tool for:** marking final, closing a period, editing notes, tags or highlights, or changing settings. An agent prints the command for you instead.
- **The Kiro guard hook** gets Freya's decisions added (`freya final`, `close`, `note`, `tag`, `win add`), so an agent with a shell can't make them either.

## Tests

- **Facts:** a seeded Muninn produces the same facts and the same `pack_hash` every time. Moved keys count once. A reopened win is flagged. Unfinished work appears under Time.
- **The check:** a fixed set of good and bad replies, each with its expected reason:
  - an invented fact ID;
  - a number not in a fact, and "worked 400 hours";
  - a teammate's name;
  - "led" citing only a `W`;
  - a date outside the period;
  - over the character limit;
  - a stale `pack_hash`.

  These tests come before any model is used.
- **Gates:** `final` is refused with an unverified citation, by both the API and plain SQL. Notes freeze after close. An edited sentence is re-checked. The time snapshot doesn't change when an approval changes after close.
- **Data:** no pack contains a person's name, a description field, or a credential-shaped string (`scrub`). `untrusted_fields` lists every outside string.
- **End to end:** the CSV import, then tags, coverage, pack, reply, check, edit, final and close. `integrity.check()` is clean at the end.

## Build order

1. `asgard.muninn.freya` with the migration: ledger, CSV import, events, tags, manual wins, time totals.
2. CLI `wins`, `tag`, `note`, `coverage`, `preflight`; the Wins, Coverage and Time pages. Useful with no AI.
3. Facts and pack (`draft_mode`, policy, roles, `pack_hash`), and `check_draft` with its reply fixtures.
4. Clipboard drafts, the claims store, editing, Mark final, Close.
5. Ysildir's Freya tools and the guard hook; Mímir's API tier when approved.
6. The yearly draft using mid-year as its anchor; then other prompts.

Steps 3 and 4 change what a person submits as their review, so they get an independent review, as Baldur's estimate changes do.

## Open decisions

- [ ] The rubric: your performance elements, standards, rating levels, and the appraisal system's character limits.
- [ ] Periods: fiscal year (Oct–Sep) and the mid-year date.
- [ ] Which resolutions count as wins (proposed: all but Won't Do, Duplicate, Cannot Reproduce).
- [ ] Whether your own name may appear in packs, or "I" only.
- [ ] Whether agent summaries may be used as evidence (proposed: off).
- [ ] Which AI tier is approved for review drafts, and whether the ISSO accepts `metadata` for `freya_pack`.
- [ ] Keep every draft version, or prune superseded ones once a period closes (already open in the Muninn doc).
