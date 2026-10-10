# Ysildir and Muninn

Identity `ysildir` · built Oct 9, 2026, not yet tried on the workstation · an MCP server (stdio) so an approved AI client can use Asgard

Ysildir lets an AI client use Asgard: Kiro, Copilot agent mode in VS Code, or Claude Code. It
does three things:

- **Teaches.** It tells the agent what Baldur and Muninn are and how to use them.
- **Takes estimates.** It takes the agent's estimates of your time into Baldur.
- **Answers questions.** It answers questions from Muninn.

It never approves, changes or posts anything; those stay with you. Whether a client may call it
at all depends on that client's MCP settings, and for Copilot also on GitHub's "MCP servers in
Copilot" policy (off by default) and VS Code's `ChatMCP` policy.

The code is `apps/ysildir/`; its tests are `tests/test_ysildir.py`. The spec it was built from is
the Kiro spec in the repository at `.kiro/specs/ysildir-mcp/` (requirements, design, tasks).

## Built on

The official MCP Python SDK (`mcp` 2.3.0, `MCPServer`), running only its stdio transport.

- **Why the SDK.** It handles the protocol, every protocol version, schemas from type hints,
  validation, prompts and resources, and ships a real client for tests. Ysildir holds only
  Asgard's logic. Only `server.py` and `results.py` import it, and only `models.py` and
  `config.py` import `pydantic`, so moving to another SDK changes those files alone.
- **What it needs.** Python 3.10+, and IT approval for five compiled wheels: `pydantic-core`,
  `cryptography`, `cffi`, `rpds-py`, and `pywin32` on Windows. `Asgard/MODULES.md` has both
  packages' sections.
- **If it isn't available on-premises**, in order:
  1. `mcp` 1.x (`FastMCP`, with the same `add_tool`, decorators and `ToolError`);
  2. `fastmcp`;
  3. a standard-library JSON-RPC server over the same handlers;
  4. no MCP: Baldur's `ai ... --json` commands and the clipboard tier.

  Without the SDK, or on a Python older than 3.10, `ysildir.cmd` says what's missing and what
  still works, and exits 2.
- **The packaged build** ([packaging.md](../packaging.md)) includes Python 3.12, `mcp` and
  `pydantic`, so it needs nothing installed. `ysildir.cmd setup` there writes
  `asgard-cli.exe apps\ysildir\cli.py serve` as the client's command (`clients.launch`), and the
  build's checks drive Ysildir over stdio from it.

## Commands

`%LOCALAPPDATA%\Asgard\app\apps\ysildir\ysildir.cmd` (in the repository, `py -3 Asgard\apps\ysildir\cli.py`):

| Command | What it does |
| --- | --- |
| `ysildir.cmd check` | Starts the server in memory and lists what an agent would see: the protocol version, Muninn's version, each tool with its switch, the prompts and resources. `--all` shows it as if every tool were on |
| `ysildir.cmd tools` | Each tool, on or off, and what it sends to the AI client |
| `ysildir.cmd tools --on NAME ...` / `--off NAME ...` | Switches tools. You run this, never an agent (Kiro's guard hook blocks it). Restart the MCP server in your client afterwards |
| `ysildir.cmd setup --kiro DIR` | Adds Ysildir to a Kiro workspace (`DIR\.kiro\settings\mcp.json`); `--kiro-user` for every workspace |
| `ysildir.cmd setup --vscode DIR` | Adds it to a VS Code workspace (`DIR\.vscode\mcp.json`); `--vscode-user` adds it to your profile with `code --add-mcp` |
| `ysildir.cmd setup --claude DIR` | Adds it to a Claude Code workspace with `claude mcp add --scope project`, or writes `DIR\.mcp.json` when `claude` isn't on PATH |
| `ysildir.cmd serve` | What the client starts. You don't run it by hand |

`setup --print` shows the commands and snippets without changing anything; `--force` replaces an
entry that's already there. Setup keeps every other server in a file, refuses a file it can't
parse (VS Code's may have comments) and prints the snippet to paste instead. The client starts the
Python that ran setup, with `cli.py serve` (in the packaged build, its `asgard-cli.exe`); run setup
again after changing Python or moving the build.

Kiro's `autoApprove` lists only the read-only tools that are on. Adding `baldur_record_estimate`
to it saves a click after every commit, and a report never changes a figure on its own; that's
your choice to make.

## Connections

- **Reads:** `muninn.open_app("ysildir", supported=(4, 5), readonly=True)`, one connection per
  call. `query_only` is on and the guard refuses every write, so a tool bug can't change data.
  `baldur_day` runs Baldur's own `desk.load_day` on that connection, which proves it writes
  nothing.
- **Writes:** each write tool calls one Baldur function on a connection opened as `baldur`, never
  as `ysildir`. Recording an agent's estimate is
  `asgard.muninn.baldur.record_agent_estimate(con, report, via="mcp")`. Ysildir owns no tables
  and writes no SQL of its own, so Baldur's rules hold whichever way a report arrives.
- **Baldur's settings** are read, never created or changed: if `baldur.json` isn't there, the
  Baldur tools say to open Baldur once.

## Teaching

Each lesson ships with the app it describes, and Ysildir serves it in every way a client can
take it:

| Way | Where it reaches the model |
| --- | --- |
| `initialize` instructions (`apps/ysildir/instructions.md`) | Clients that pass server instructions on to the model. Under 2,000 characters; lines naming a tool that's off are left out |
| Tool descriptions | Every client, always |
| `asgard_guide(topic)` | Any client with tools. Topics: `baldur` (`apps/baldur/prompts/agent-guide.md`, `baldur-agent-2`, with worked examples), `muninn` (`asgard/muninn/agent-guide.md`, `muninn-agent-1`), `review` (Baldur's review prompt), `tools` (written from your switches) |
| Resources `asgard://guides/baldur`, `muninn`, `review` | Clients that attach MCP resources |
| Prompts `record-estimate`, `my-day`, `review-day`, `what-changed` | Slash commands you type. A prompt whose tools are off isn't offered |

Kiro also gets Baldur's steering file and guard hooks (`baldur.cmd ai kiro --into DIR`). Those
work without MCP.

## Tools

| Tool | On by default | Arguments | Calls or reads | Writes |
| --- | --- | --- | --- | --- |
| `asgard_guide` | yes | `topic` | The guide files | — |
| `muninn_catalog` | yes | — | `sqlite_schema`, `guard.OWNERS`, `asgard/muninn/tables.py`, row counts | — |
| `baldur_record_estimate` | yes | `report` (one object; unknown fields refused) | `muninn.baldur.record_agent_estimate(via="mcp")` | `agent_estimates`, as `baldur` |
| `baldur_withdraw_estimate` | yes | `id` (r12) | `muninn.baldur.withdraw_agent_estimate` | `agent_estimates`, as `baldur` |
| `baldur_estimates` | yes | `date_from`, `date_to`, `include_withdrawn` | `muninn.baldur.list_agent_estimates`, the same query as `ai list --json` | — |
| `baldur_day` | no | `date`, `include_report` | Baldur's `desk.load_day`, on the read-only connection | — |
| `baldur_review_pack` | no | `date` | Baldur's `assist.day_pack` (obeys `review_mode`) | the day's estimate, as `baldur` |
| `baldur_submit_review` | no | `date`, `reply` (one object; unknown fields refused), `model` | Baldur's `assist.apply_reply(tier="mcp")` | the review on the day's open proposals, as `baldur` |
| `muninn_what_changed` | no | `since` (included), `kinds`, `limit` | `events` (`ix_events_at`); allow-listed payload fields only | — |
| `muninn_day_status` | no | `date_from`, `date_to` | `v_day_status`, `v_unpostable_days` | — |
| `muninn_issue` | no | `key` | `work_item_aliases` → `work_items` | — |
| `muninn_search` | no | `query`, `kinds`, `limit` | `search` (FTS5, bm25; each word quoted); your own commits only | — |

Date ranges cover at most 31 days. There is never a tool to approve, reject or change time, note
real hours, accept a calibration, change settings, keys or repositories, collect, post, or reach
Jira or the network; a test checks the tool list, and that no Ysildir module calls a decision. An
agent gives you the command instead, for example `baldur.cmd approve --date 2026-10-01 --ai 3f2a9c1d`:
the id names exactly the figures `baldur_day` showed, so a later report can't change what you approve.

Freya's `v_review_evidence`, and Loki's and Freya's prompts, come later, once those apps collect
data.

## Data handling

- **Your ISSO approves each tool.** Everything Ysildir returns goes to the AI client, and so to
  its model. You switch each tool on or off in `%LOCALAPPDATA%\Asgard\settings\ysildir.json`
  (`ysildir.cmd tools --on NAME`). Tools that only return Asgard's own text, or the agents' own
  reports, start on. Tools that send commit subjects, Jira summaries or times start off.
  `baldur_review_pack` and `baldur_submit_review` also need Baldur's `review_mode` set to
  `metadata`. A switches file that can't be read fails closed: only `asgard_guide` stays on, and
  its `tools` topic says what's wrong.
- **Metadata only.** Ysildir never returns code, diffs, commit bodies, Jira descriptions,
  worklog comments, meeting notes, `submissions.fields` or review draft text. Event payloads come
  back only with fields known to hold no free text (`work_item.created` without its summary,
  `worklog.failed` without its error).
- **Untrusted text.** Commit subjects, Jira summaries and statuses, pull request titles and
  agents' summaries can carry instructions aimed at the model. Ysildir cleans them to one capped
  line with credential-shaped text masked, and each result lists them in `untrusted_fields` so
  the model treats them as data.
- **Caps.** At most 200 items and 64 KB of JSON per answer. A cut answer says `truncated` and how
  to ask for less. A day's review pack over 60 KB is refused with the clipboard command instead.
- **Stdio only.** Nothing listens on a port, and Ysildir makes no network calls.
- **The log.** `logs\ysildir.log` records each call's tool, time, outcome and ids, never its
  arguments or results. An unexpected error logs its traceback without the exception's message,
  which might repeat an argument.

## Tests

`tests/test_ysildir.py` drives the server with the SDK's own client, in memory and as a subprocess
over real stdio, against the smoke day in a temporary `ASGARD_HOME`. It covers:

- the instructions, tools, annotations, schemas, prompts and resources a client sees, and the
  allow-list with every switch on;
- each tool, including Baldur's own refusal cases through the tool, with nothing stored;
- the read-only connection for every read, and `via='mcp'` on a recorded report;
- payload allow-listing, masking, caps, and a bug logged without its arguments;
- stdout carrying only protocol messages, and a clean exit when stdin closes;
- the guides' worked examples replayed over stdio, then approval through Baldur's CLI and the
  `Reviewed:` line Odin posts.

It also includes the switches file, `setup` against fake `code` and `claude` commands, `check`,
the missing-SDK message, static checks on the source (no `print`, no decision calls, imports where
`MODULES.md` says) and Kiro's guard. The SDK tests skip, saying why, without the SDK or on Python
older than 3.10. Breaking a guarantee on purpose (the switch check, a `readOnlyHint`, a cap, the
report's `extra="forbid"`, the read-only connection, the payload allow-list, cleaning, the
instructions' filtering, the figures' id in `take`, a ticket's `worklog_line`) fails a test each time.

## Needs the workstation

- Which MCP features the on-premises Kiro supports (instructions, resources, prompts), and the
  keys in its `mcp.json`.
- The org's Copilot MCP policy and VS Code's `ChatMCP` policy.
- Whether App Control lets the client start Python with `cli.py serve` from
  `%LOCALAPPDATA%\Asgard\app\`.
- Whether IT approves the SDK's compiled wheels, and which `mcp` version the agency mirror has.
- How a real agent behaves in the acceptance walkthrough (the spec's task 10).
