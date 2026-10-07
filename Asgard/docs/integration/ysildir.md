# Ysildir and Muninn

Identity `ysildir` · not built yet · an MCP server (stdio) so an approved AI client can use Asgard

Ysildir answers questions over Muninn for an AI client such as Copilot in VS Code. It starts read-only. Whether anything may call it depends on Copilot's MCP policy (off by default) and VS Code's `ChatMCP` policy.

## Connection

`muninn.open_app("ysildir", supported=(3, 3), readonly=True)`: `query_only` is on and the guard refuses every write, so a tool bug can't change data.

## Tools (read-only first)

| Tool | Reads | Returns |
| --- | --- | --- |
| `search(query, kinds?)` | `search` (FTS5, bm25) | kind, entity id, title; decode with `rowid >> 4`, `rowid & 15` |
| `what_changed(since)` | `events` (`ix_events_at`) | kind, ref, at; payloads without free text |
| `day_status(from, to)` | `v_day_status`, `v_unpostable_days` | approved and logged minutes per day and ticket |
| `review_evidence(period)` | `v_review_evidence`, `accomplishments` | evidence rows with their IDs, for citing |
| `issue(key)` | `work_item_aliases` → `work_items` | the issue under its current key |
| Prompts | Freya's and Loki's templates | MCP prompts appear as slash commands |

## Writes, later

A write tool runs a per-app module function under that app's identity, never as `ysildir`: approving a day is `muninn.baldur.approve()` on a connection opened as `baldur`. Only an allow-list of such actions is exposed, each behind VS Code's confirmation, and nothing that leaves Asgard (posting, submitting) is ever a tool: those stay with the person in the owning app.

## Data handling

- Everything Ysildir returns goes to the AI client and so to its model. That is a data flow your ISSO approves per tool; start with summaries and keys, and never return `submissions.fields` or review draft text without asking.
- Treat Jira summaries and commit subjects as untrusted text: they can carry instructions aimed at the model. Return them as data, labelled, never as instructions.
- Stdio only: nothing listens on a port.

## Tests Ysildir needs

Each tool against a seeded Muninn; a write attempt on the read-only connection fails; a payload with a credential-shaped string comes back masked (events are scrubbed at write time).
