# Ysildir and Muninn

Identity `ysildir` · not built yet · an MCP server (stdio) so an approved AI client can use Asgard

Ysildir lets an AI client use Asgard: Kiro, Copilot agent mode in VS Code, or Claude Code. It
does three things:

- **Teaches.** It tells the agent what Baldur and Muninn are and how to use them.
- **Takes estimates.** It takes the agent's estimates of your time into Baldur.
- **Answers questions.** It answers questions from Muninn.

It never approves, changes or posts anything; those stay with you. Whether a client may call it
at all depends on that client's MCP settings, and for Copilot also on GitHub's "MCP servers in
Copilot" policy (off by default) and VS Code's `ChatMCP` policy.

The full spec, for the agent that builds it, is the Kiro spec in the repository at
`.kiro/specs/ysildir-mcp/` (requirements, design, tasks).

## Built on

The official MCP Python SDK (`mcp` 2.3.0, `MCPServer`), running only its stdio transport.

- **Why the SDK.** It handles the protocol, every protocol version, schemas from type hints,
  validation, prompts and resources, and ships a real client for tests. Ysildir holds only
  Asgard's logic.
- **What it needs.** Python 3.10+, and IT approval for five compiled wheels: `pydantic-core`,
  `cryptography`, `cffi`, `rpds-py`, and `pywin32` on Windows.
- **If it isn't available on-premises**, in order:
  1. `mcp` 1.x (`FastMCP`, with the same decorators);
  2. `fastmcp`;
  3. a standard-library JSON-RPC server over the same handlers;
  4. no MCP: Baldur's `ai ... --json` commands and the clipboard tier.

The design's "Modules" table has the details. When the code lands, `mcp` and `pydantic` get their
sections in `Asgard/MODULES.md`; the design drafts both.

## Connections

- **Reads:** `muninn.open_app("ysildir", supported=(4, 4), readonly=True)`, one connection per
  call. `query_only` is on and the guard refuses every write, so a tool bug can't change data.
- **Writes:** each write tool calls one Baldur function on a connection opened as `baldur`, never
  as `ysildir`. Recording an agent's estimate is
  `asgard.muninn.baldur.record_agent_estimate(con, report, via="mcp")`. Ysildir owns no tables
  and writes no SQL of its own, so Baldur's rules hold whichever way a report arrives.

## Teaching

Each lesson ships with the app it describes, and Ysildir serves it in every way a client can
take it:

| Way | Where it reaches the model |
| --- | --- |
| `initialize` instructions (`apps/ysildir/instructions.md`) | Clients that pass server instructions on to the model |
| Tool descriptions | Every client, always |
| `asgard_guide(topic)` | Any client with tools. Topics: `baldur` (`apps/baldur/prompts/agent-guide.md`), `muninn` (`asgard/muninn/agent-guide.md`), `review` (Baldur's review prompt), `tools` |
| Resources `asgard://guides/...` | Clients that attach MCP resources |
| Prompts `record-estimate`, `my-day`, `review-day`, `what-changed` | Slash commands you type |

Kiro also gets Baldur's steering file and guard hooks (`baldur.cmd ai kiro --into DIR`). Those
work without MCP.

## Tools

| Tool | On by default | Calls or reads | Writes |
| --- | --- | --- | --- |
| `asgard_guide` | yes | The guide files | — |
| `muninn_catalog` | yes | `sqlite_schema`, `guard.OWNERS`, row counts | — |
| `baldur_record_estimate` | yes | `muninn.baldur.record_agent_estimate(via="mcp")` | `agent_estimates`, as `baldur` |
| `baldur_withdraw_estimate` | yes | `muninn.baldur.withdraw_agent_estimate` | `agent_estimates`, as `baldur` |
| `baldur_estimates` | yes | `agent_estimates` | — |
| `baldur_day` | no | Baldur's `desk.load_day`, on the read-only connection | — |
| `baldur_review_pack` | no | Baldur's `assist.day_pack` (obeys `review_mode`) | the day's estimate, as `baldur` |
| `baldur_submit_review` | no | Baldur's `assist.apply_reply(tier="mcp")` | the review on the day's proposals, as `baldur` |
| `muninn_what_changed` | no | `events` (`ix_events_at`); allow-listed payload fields only | — |
| `muninn_day_status` | no | `v_day_status`, `v_unpostable_days` | — |
| `muninn_issue` | no | `work_item_aliases` → `work_items` | — |
| `muninn_search` | no | `search` (FTS5, bm25); entity id `rowid >> 4`, kind `rowid & 15` | — |

There is never a tool to approve, reject or change time, note real hours, accept a calibration,
change settings, keys or repositories, collect, post, or reach Jira or the network. An agent gives
you the command instead, for example `baldur.cmd approve --date 2026-10-01 --ai`.

Freya's `v_review_evidence`, and Loki's and Freya's prompts, come later, once those apps collect
data.

## Data handling

- **Your ISSO approves each tool.** Everything Ysildir returns goes to the AI client, and so to
  its model. You switch each tool on or off in `%LOCALAPPDATA%\Asgard\settings\ysildir.json`
  (`ysildir.cmd tools --on NAME`). Tools that only return Asgard's own text, or the agents' own
  reports, start on. Tools that send commit subjects, Jira summaries or times start off.
  `baldur_review_pack` also needs Baldur's `review_mode` set to `metadata`.
- **Metadata only.** Ysildir never returns code, diffs, commit bodies, Jira descriptions,
  worklog comments, meeting notes, `submissions.fields` or review draft text.
- **Untrusted text.** Commit subjects, Jira summaries and agents' summaries can carry
  instructions aimed at the model. Ysildir cleans them to one capped line, and each result lists
  them in `untrusted_fields` so the model treats them as data.
- **Stdio only.** Nothing listens on a port, and Ysildir makes no network calls.
- **The log.** `logs\ysildir.log` records each call's tool, time, outcome and ids, never its
  arguments or results.

## Tests Ysildir needs

- Each tool against a seeded Muninn.
- A write attempt on the read-only connection fails.
- A report recorded through Ysildir is stored with `via='mcp'` under Baldur's rules.
- A payload with a credential-shaped string comes back masked (events are also scrubbed when
  they are written).
- `tools/list` matches the documented allow-list exactly.
- A scripted client replays the guides' worked examples from start to finish.
