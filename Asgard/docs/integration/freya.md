# Freya and Muninn

Identity `freya` · not built yet · module to add: `asgard.muninn.freya` (rules) and `asgard.muninn.citations` (shared with Loki)

Freya keeps a durable record of every issue you finish and drafts your yearly review from it. Freya is mostly queries: code groups the facts, then one or two model passes (through Mímir) write the text, and every claim must cite an ID that resolves to a real row.

## Tables

| Owns | Reads |
| --- | --- |
| `accomplishments`, `review_periods`, `review_drafts`, `citations` (owner_type `review_draft`) | `events` (`work_item.done`, `.reopened`, `.deleted`), `work_items`, `work_item_aliases`, `v_day_status`, `v_time_by_item_month`, `v_review_evidence`, `commit_work_items`, `pull_requests`, `blufs` |

## The flows

**1. A finished issue becomes an accomplishment.**

```python
with muninn.consume(con, "freya", ["work_item.done", "work_item.reopened", "work_item.deleted"]) as batch:
    for event in batch:
        freya.on_work_item_event(con, event)     # upsert accomplishments by jira_id; commits with the cursor
```

- Keep it when the issue is yours (`is_mine`, or your commits or approved minutes on any of its keys) and its resolution counts. Won't Do, Duplicate and Cannot Reproduce don't (an open decision says which count).
- Key by `jira_id`, not the key: an issue that moves projects stays one accomplishment.
- `work_item.reopened` sets `state = 'reopened'` and counts it; `work_item.deleted` keeps the row (the copy is the point) and notes the source is gone.

**2. Stats refresh until the period closes.** `freya.refresh(con, jira_id)` recomputes `approved_minutes` (from `v_day_status`, across all the issue's keys), `commit_count` (distinct patch ids, `is_merge = 0`), `merged_pr_count` and activity bounds. It never touches `impact_note` or `highlight`, and does nothing once `frozen_at` is set. `freya.close_period(con, period_id)` sets `closed_at` and freezes that period's accomplishments in one transaction.

**3. A draft, and the citation gate.**

1. Build the evidence pack from `v_review_evidence` and the period's accomplishments; ask Mímir for the text, asking for IDs in brackets.
2. `freya.add_draft(con, period_id, element, text, tier, model, prompt_version)` writes a new `review_drafts` version and parses the bracketed IDs into `citations` (`verified = 0`).
3. `citations.verify(con, "review_draft", draft_id)` resolves each `evidence_ref` against its table and sets `evidence_id`, `verified = 1`.
4. `freya.mark_final(con, draft_id)` refuses while any citation of that draft is unverified.

## What protects Freya's data

- `accomplishments.jira_id` is UNIQUE; `last_done_at >= first_done_at`; counts are ≥ 0.
- `citations`: `verified = 1` requires `evidence_id`; one row per (owner, evidence).
- `review_drafts`: every regeneration is a new version, UNIQUE (period, element, version).
- **To add in the migration that ships Freya:** a trigger that refuses `status = 'final'` while the draft has unverified citations (today only the module enforces it), and a trigger that keeps `impact_note` and `highlight` unchanged once `frozen_at` is set.

## Events

Emits (planned) `accomplishment.recorded`, `review_draft.created`, `review_draft.final`. Consumes `work_item.done`, `.reopened`, `.deleted` in one `consume()` call.

## Data handling

Review drafts are your words about your work and may be sensitive; they stay in Muninn and leave only by your paste. Tier 1 (API) sends the evidence pack to an approved endpoint: summaries, keys and counts, never ticket descriptions (rule 9).

## Tests Freya needs

- A done → reopened → done issue is one accomplishment with `reopened_count = 1`.
- A key move between finish and refresh keeps one row and the right minutes.
- An invented `XYZ-999` stays unverified and blocks `mark_final`; a real key verifies.
- `integrity.check()` clean after each scenario.

## Open decisions

Which resolutions count; whether Jira descriptions may be stored (default no); keeping every draft version or pruning superseded ones once a period closes.
