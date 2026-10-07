# Heimdall and Muninn

Identity `heimdall` · developed on-prem and diverging; the copy in `apps/heimdall/` is a pattern sample only · this page is the contract it should meet if it writes Muninn

Heimdall prepares SeCcHm submissions and drives the browser to fill them, stopping before Submit. It shares `submissions` with Bifrost (`system = 'secchm'` for Heimdall).

## Tables

| Owns (shared with Bifrost) | Reads |
| --- | --- |
| `submissions` (`system = 'secchm'` rows), `submission_status_history` (filled by trigger only) | `work_items`, `work_item_aliases` (the related issue), `identities` |

Keep to your own `system` value: the guard is table-level, so the module (proposed `asgard.muninn.submissions`, shared with Bifrost) filters every write by system.

## The state machine

`draft → approved → submitted → in_review → accepted | returned`, and `withdrawn` from any state. The schema enforces the order's evidence: any state past draft needs `approved_at`; submitted or later needs `submitted_at`. Every state change adds a history row by trigger, so no code path can forget it.

| Step | Call | Notes |
| --- | --- | --- |
| Prepare | `submissions.create(con, "secchm", title, fields, work_item_key)` | `fields` is the values by field name; emits `submission.created` |
| Approve | `submissions.approve(con, id)` | Your approval of the content; emits `submission.approved` |
| Fill | browser automation, no Muninn lock held | Stops on the review page |
| Submit | the person clicks Submit; then `submissions.mark_submitted(con, id, external_id, url)` | Security submissions carry an attestation in your name, so the click stays human |
| Track | poll queue `next_poll_at` (`ix_submissions_poll`); `submissions.record_status(con, id, state, raw)` | `last_status_raw` keeps the status text as shown; emits `submission.status_changed` |

## Rules for the diverging copy

1. Open with `muninn.open_app("heimdall", supported=(3, 3))`; never migrate. If Heimdall needs columns, they come in an Asgard migration.
2. Never hold a transaction while the browser works.
3. `fields` holds the same sensitive detail as SeCcHm itself: same handling as its exports; never in events (put the id and state in payloads, not field values).
4. Templates and browser profiles stay in Heimdall's own folders (`settings\heimdall\`, the Edge profile); they are not Muninn data. Valhalla must list the Edge profile folder in its purge set (open item from the inventory).
5. Playwright is allowed under the dependency policy (declared and pinned), but stays out of `asgard.muninn`: only the `fill` command imports it, as today.

## Tests the contract needs

A submission taken through every state produces one history row per change; withdrawn from approved keeps `approved_at`; a Bifrost connection can't change a `secchm` row through the module.
