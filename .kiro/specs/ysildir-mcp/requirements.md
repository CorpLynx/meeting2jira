# Requirements: Ysildir, Asgard's MCP server (teaching agents Baldur and Muninn)

## Read this first (for the implementing agent)

This spec was written off-premises against the `Asgard/` folder of the `meeting2jira` repository,
on branch `claude/baldur-estimation`. That branch has Muninn schema v4, Baldur's AI-assisted
method, and the agent guide `baldur-agent-1`. The on-premises copy may be ahead of it.
Statements about existing code are tagged **(repo)**. Check each one in task 0 and edit the spec
(all three files) to match what you find. Untagged requirements describe intended behaviour;
change them only with the person.

Read `Asgard/AGENTS.md` before anything else; its rules win over this spec. The ones that matter
most here:

- Per-user and no admin. Asgard's floor is Python 3.9, but Ysildir needs 3.10+ because the MCP
  SDK does. On Windows, Muninn already needs 3.11.
- **Use the best module for each job** (Brandon, 2026-10-09: standard-library-only is gone).
  Declare and pin it in `Asgard/requirements.txt`. The comment above each pin says:
  - what it's for;
  - whether it's compiled ("native: needs IT approval", since App Control checks DLLs);
  - its approval status;
  - the alternatives if it isn't available on-premises.

  See `docs/dependency-policy.md`.
- `asgard/muninn/` and the launcher's start-up path stay standard library, because every app and
  Odin import them. Ysildir's own code has no such limit.
- Only Asgard migrates Muninn, and each app writes only its own tables.
- Metadata only.
- The person makes every decision.

Ysildir has **no rules of its own**. Every write it offers is one existing function in
`asgard.muninn.baldur` or Baldur's `assist`. If a rule is missing, add it there, with tests, so
Baldur's window and CLI get it too. Don't add it in Ysildir.

## Introduction

The on-premises steering tells AI coding agents to estimate how long their changes take, but
nothing records those estimates where Baldur can use them. This branch gave Baldur three things:

- **A way in.** `baldur.cmd ai record` stores an agent's estimate in Muninn (`agent_estimates`,
  schema v4).
- **Suggestions.** The AI-assisted method turns those estimates into checked suggestions that the
  person takes or leaves (`Asgard/docs/baldur-spec.md`, "AI-assisted estimates").
- **Kiro files.** A steering file and two hooks (`Asgard/apps/baldur/agents/`) teach the agent
  the command and block the person's decisions.

Ysildir is the MCP route to the same functions. An MCP client (Kiro, Copilot agent mode in VS
Code, or Claude Code) starts it over stdio and gets three things:

1. **Teaching.** Instructions, guides, prompts and tool descriptions tell the agent what Baldur
   and Muninn are, what to do after a change, and what it must never do. They come from the
   server itself, so every workspace and every client gets the same lesson. Nobody has to copy
   steering files around.
2. **Baldur tools.** The agent can record and withdraw its own estimates, show the person's day
   with the AI-assisted figures, and run Baldur's AI review at the MCP tier.
3. **Muninn questions.** Read-only answers from Muninn: what changed, approved against logged
   time, one issue, and search.

Nothing Ysildir offers approves, changes or posts time. Those decisions stay with the person, in
Baldur and Odin.

### Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Implementation | The official MCP Python SDK, `mcp` 2.x (`MCPServer`), stdio transport only | It's the reference implementation. It handles every protocol version (2024-11-05 to 2026-07-28 in 2.3.0) and builds input and output schemas from type hints. It also handles validation, structured results, annotations, prompts and resources, and ships a real client for tests. Ysildir then holds only Asgard's logic. The cost: Python 3.10+ and five compiled wheels for IT to approve. The design's "Modules" table lists the alternatives if it isn't available on-premises |
| Modules | The best module for each job, each pin commented with its alternatives | Brandon's rule (2026-10-09). An on-premises gap then has a known answer rather than a redesign |
| Location | `Asgard/apps/ysildir/`, shipped in the Asgard zip | Setup copies `apps/` **(repo: `install.PAYLOAD`)**. Ysildir and Baldur always ship together, so Ysildir can import Baldur's modules without the two drifting apart |
| Identity | Reads use `open_app("ysildir", readonly=True)`. Each write calls one Baldur function on a connection opened as `baldur` for that call | `docs/integration/ysildir.md` says a write runs under the owning app's identity; the guard then enforces Baldur's ownership as it always does |
| Phase 1 scope | Teaching; Baldur's agent intake; Baldur's day view and the MCP-tier review; Muninn reads | The request: teach agents to use Baldur and Muninn |
| Never a tool | Approving, rejecting or changing a figure; real hours (`actual`); calibration; Baldur's settings, keys and repositories; collecting; posting; anything that reaches Jira, GitHub or the network | The person decides (AGENTS.md rules 4 and 5), and nothing leaves Asgard through Ysildir |
| Default switches | **On:** tools that return Asgard's own text or the agents' own reports. **Off until the person turns them on:** tools that send other Muninn data to the model (commit subjects, Jira summaries, times) | Each tool's data flow is the ISSO's decision (`docs/integration/ysildir.md`). **Proposed: confirm in task 0** |
| Clients | Kiro first (on-premises), then Copilot agent mode in VS Code and Claude Code. One server, and a config writer for each client | Kiro is the on-premises agent. Copilot's use of MCP depends on an org policy |

### Glossary

- **Agent**: the AI coding assistant (Kiro, Copilot or Claude Code) that uses Ysildir through its
  MCP client.
- **Person**: the engineer whose time Baldur estimates. Only they decide.
- **Report**: an agent estimate. It is an `agent_estimates` row with an id like `r12`, in schema
  `baldur.agent_estimate/1` **(repo)**.
- **AI-assisted figure**: Baldur's suggestion beside its own estimate, from checked reports or a
  checked AI review **(repo: `assist.suggestions`)**.
- **Pack**: a day's evidence for an AI review. It holds metadata only and is named by its
  `pack_hash` **(repo: `assist.build_pack`)**.
- **Teaching surface**: everything that tells the agent how to use Asgard: the `initialize`
  instructions, tool descriptions, the guide tool, resources and prompts.
- **Switch**: the person's on/off setting for one tool, in `ysildir.json`.

## Requirements

### Requirement 1: A stdio MCP server built on the MCP SDK

**User Story:** As the person, I want Ysildir built on the official MCP SDK and running only over
stdio, so that it speaks the protocol correctly with any client and opens nothing to the
network.

#### Acceptance Criteria

1. THE server SHALL be built on the official MCP Python SDK (`mcp`, pinned in
   `Asgard/requirements.txt`) and SHALL run only its stdio transport (`MCPServer.run("stdio")`).
   It SHALL NOT start the SDK's HTTP transports (Streamable HTTP, SSE), open a network listener,
   or make a network call.
2. THE protocol SHALL be the SDK's, and Ysildir SHALL NOT patch or reimplement any of it. That
   covers the lifecycle and version negotiation, capabilities, `ping`, cancellation, and JSON-RPC
   errors.
3. THE server SHALL write nothing to stdout itself: the SDK writes the protocol messages there.
   Diagnostics SHALL go to stderr and to `%LOCALAPPDATA%\Asgard\logs\ysildir.log` **(repo:
   `paths.log_dir()`)**.
4. EVERY tool, prompt and resource SHALL be registered through the SDK (`add_tool`, `@prompt`,
   `@resource`). Each uses type hints and pydantic `Field` descriptions, so the SDK builds the
   input and output schemas and validates every call.
5. A tool that takes a report or a reply SHALL take it as one pydantic model with
   `extra="forbid"`, so an unknown field is refused. The SDK drops unknown top-level arguments
   silently, so a nested model is the only way to refuse one.
6. WHEN Baldur or Muninn refuses THEN the tool SHALL raise the SDK's `ToolError` with Baldur's
   message. The agent receives it as an `isError` result, after the SDK's prefix "Error
   executing tool NAME: ".
7. WHEN a tool raises any other exception THEN the agent SHALL get the SDK's generic error, and
   Ysildir SHALL log the traceback without any argument values.
8. A call to a tool that isn't registered SHALL get the SDK's "Unknown tool" error. That covers a
   tool that is switched off, and one that never existed.
9. THE server SHALL need Python 3.10 or newer, the SDK's floor. Ysildir's code SHALL still use
   only Python 3.9 syntax, so vermin stays clean across Asgard. It SHALL find Python the way
   Baldur does: `py -3`, then `py`, then `python` **(repo: `baldur.cmd`, `agents.launcher()`)**.
10. WHEN the SDK isn't installed, or Python is older than 3.10, THEN `ysildir.cmd` SHALL say what
    is missing and what still works, and exit 2. What still works is Baldur's `ai ... --json`
    commands and the clipboard tier.
11. THE SDK and everything it pulls in SHALL ship as reviewed wheels in the payload's `vendor/`
    folder **(repo: `docs/updates.md`)**. Nothing runs pip on the workstation.

### Requirement 2: The server teaches the agent

**User Story:** As the person, I want any agent that connects to learn from Ysildir itself what
Baldur and Muninn are and how to use them, so that I don't copy steering into every workspace and
every client.

#### Acceptance Criteria

1. THE `initialize` result SHALL carry `instructions` of at most 2,000 characters. They SHALL
   cover:
   - what Asgard, Baldur and Muninn are;
   - what to do after a change: record an estimate;
   - which tool answers which kind of question;
   - the rules in requirement 6;
   - the guide versions.

   Any line that names a tool that is switched off SHALL be left out.
2. EVERY tool description SHALL say, in plain sentences, when to use the tool, what it returns,
   and what it never does. Clients always show tool descriptions to the model, but their support
   for instructions, resources and prompts varies.
3. A tool `asgard_guide(topic)` SHALL return the full text of one guide:

   | Topic | Guide |
   | --- | --- |
   | `baldur` | Baldur's agent guide, version `baldur-agent-1` **(repo: `apps/baldur/prompts/agent-guide.md`)** |
   | `muninn` | A new Muninn guide for agents, version `muninn-agent-1` |
   | `review` | Baldur's review prompt **(repo: `apps/baldur/prompts/review.md`)** |
   | `tools` | Every Ysildir tool, whether it is on, and how to turn it on |
4. THE guides SHALL also be MCP resources: `asgard://guides/baldur`, `asgard://guides/muninn` and
   `asgard://guides/review`, all `text/markdown`.
5. THE server SHALL offer MCP prompts for the common tasks:
   - `record-estimate`;
   - `my-day`, which takes a date;
   - `review-day`, which takes a date;
   - `what-changed`, which takes a start time.

   Each prompt SHALL expand into steps that name the tools to call. The same rules apply.
6. THE guide text SHALL be read at run time from the files that ship with each app. Ysildir SHALL
   NOT keep its own copy. A test SHALL check that the versions named in the instructions match
   each guide's first line.
7. THE Baldur and Muninn guides SHALL include worked examples. Each example is a complete sequence
   of tool calls, for:
   - recording an estimate after a commit;
   - explaining a day;
   - reviewing a day;
   - answering "what changed?".
8. WHEN a tool refuses THEN its message SHALL say what happened and what the agent should do next,
   which is usually to show the person the message. The message SHALL NOT invite the agent to
   retry with other values.
9. THE Kiro steering that Baldur installs **(repo: `apps/baldur/agents/kiro-steering.md`)** SHALL
   tell the agent to use `baldur_record_estimate` when that tool is available, and the command
   otherwise. *(Done on this branch.)*

### Requirement 3: Baldur tools for the agent's estimates

**User Story:** As the person, I want the agent to record its estimate of my time on a change
straight into Baldur, so that Baldur can check its own numbers against it.

#### Acceptance Criteria

1. `baldur_record_estimate(report)` SHALL take the report as one object: the same object
   `baldur.cmd ai record --json` takes **(repo: `REPORT_SCHEMA`, `_FIELDS`)**. Its fields are
   `agent`, `model`, `guide`, `date`, `key`, `commits`, `minutes`, `minutes_low`, `confidence`,
   `summary`, `started_at` and `ended_at`. Its pydantic model SHALL refuse any other field
   (`extra="forbid"`). It SHALL call
   `asgard.muninn.baldur.record_agent_estimate(con, report, via="mcp")` on a connection opened as
   `baldur` **(repo)**.
2. THE tool SHALL return:
   - the report's id, like `r12`;
   - its status: `recorded` or `duplicate`;
   - the ids of any reports it replaced;
   - one sentence for the person: nothing changes until they approve.
3. WHEN Baldur refuses the report THEN the tool SHALL raise `ToolError` with Baldur's message,
   unchanged. Baldur refuses, for example, a bad SHA, code in the summary, minutes out of range,
   or a future date.
4. `baldur_withdraw_estimate(id)` SHALL call `withdraw_agent_estimate` **(repo)**. A withdrawn
   report stays in Muninn, marked withdrawn.
5. `baldur_estimates(from, to, include_withdrawn)` SHALL list reports the way
   `baldur.cmd ai list --json` does **(repo: `cmd_ai_list`)**, for at most 31 days at a time.
6. WHEN Muninn isn't set up, or its schema is outside the range Ysildir supports, THEN these tools
   SHALL refuse with Muninn's own message **(repo: `open_app` errors)**.

### Requirement 4: Baldur tools for the day and the AI review (MCP tier)

**User Story:** As the person, I want to ask the agent how my day looks, and have it review the
day's estimate the way the clipboard tier does, so that I can decide faster.

#### Acceptance Criteria

1. `baldur_day(date)` SHALL return Baldur's day view **(repo: `desk.load_day`)** as JSON. It SHALL
   NOT write. The view holds:
   - for each ticket: the estimate, the open or approved figure, what Jira holds, and any
     problem;
   - untracked minutes;
   - the day's flags;
   - the AI-assisted figures, with reason, confidence and evidence;
   - the day report text.
2. WHEN the day has AI-assisted figures that change it THEN `baldur_day` SHALL include the
   command the person runs to take them (`baldur.cmd approve --date DATE --ai`). It SHALL NOT
   offer to run it.
3. `baldur_review_pack(date)` SHALL return the day's pack and the review prompt **(repo:
   `assist.day_pack`, `clipboard_text`)**. Like `baldur.cmd ai pack`, it stores the day's
   estimate first **(repo)**, so it is not read-only.
4. `baldur_review_pack` and `baldur_submit_review` SHALL obey Baldur's `review_mode` as well as
   their own switches **(repo)**:
   - `off`, the default: both refuse;
   - `metadata`: packs are allowed;
   - `content`: refused.
5. `baldur_submit_review(date, reply, model)` SHALL call
   `assist.apply_reply(..., tier="mcp", model=model)` **(repo)** and return the checked figures
   and flags.
6. WHEN the reply's pack hash doesn't match the day's evidence as it is now, or Baldur's check
   refuses the reply, THEN `baldur_submit_review` SHALL return Baldur's message and store nothing.
7. NO tool SHALL approve, reject or change a figure, note real hours, accept a calibration,
   change Baldur's settings, keys or repositories, collect, or post.

### Requirement 5: Muninn questions (read-only)

**User Story:** As the person, I want the agent to answer questions about my work from Muninn, so
that I can ask in chat instead of opening each app.

#### Acceptance Criteria

1. EACH Muninn tool call SHALL open its own connection with
   `open_app("ysildir", supported=SCHEMA, readonly=True)` **(repo)**. On that connection the guard
   and `query_only` refuse every write.
2. `muninn_catalog()` SHALL list what Muninn holds: each table and view with a one-line meaning,
   its owning app and its row count, plus the rules for writing to Muninn. It SHALL NOT return
   rows.
3. `muninn_what_changed(since, kinds, limit)` SHALL return the events after a time **(repo:
   `events`, `ix_events_at`)**: id, time, app, kind, entity type and ref. Payload fields SHALL be
   returned only for event kinds on an allow-list whose payloads hold no free text.
4. `muninn_day_status(from, to)` SHALL return, for at most 31 days at a time **(repo:
   `v_day_status`, `v_unpostable_days`)**:
   - the approved and logged minutes per day and ticket;
   - the days that can't be posted, with the reason.
5. `muninn_issue(key)` SHALL resolve a key through `work_item_aliases` to the issue under its
   current key **(repo)**, and return its key, summary, status, type, epic and update time.
6. `muninn_search(query, kinds, limit)` SHALL search Muninn's FTS5 index **(repo: `search`, with
   entity id `rowid >> 4` and kind `rowid & 15`)**. It SHALL return each hit's kind, entity id
   and title, ranked by bm25. Each term of the query SHALL be quoted, so that FTS5 syntax in the
   query can't change it or make it fail.
7. `v_review_evidence` (Freya's), and Loki's and Freya's prompts, SHALL wait until those apps
   collect data (phase 2).

### Requirement 6: Data handling and safety

**User Story:** As the person, and as the ISSO, I want each tool's data flow visible and
switchable, and the agent unable to make my decisions, so that Ysildir can be approved tool by
tool.

#### Acceptance Criteria

1. EACH tool SHALL have a switch in `%LOCALAPPDATA%\Asgard\settings\ysildir.json`.
   - **On by default:** `asgard_guide`, `muninn_catalog`, `baldur_record_estimate`,
     `baldur_withdraw_estimate` and `baldur_estimates`.
   - **Off by default:** all other tools.

   A tool that is off SHALL NOT be registered with the SDK. So it doesn't appear in `tools/list`,
   and a call to it gets "Unknown tool". `asgard_guide` with topic `tools` SHALL list it as off,
   with the command that turns it on.
2. THE code's tool table SHALL be the allow-list. With every switch on, a test SHALL check that
   `tools/list` matches the documented list exactly, so that adding a tool needs a spec change.
3. Metadata only **(repo: AGENTS.md rule 6)**. No tool SHALL return any of these:
   - code or diffs;
   - commit messages beyond the subject line;
   - Jira descriptions or comments;
   - worklog comments;
   - meeting notes text;
   - `submissions.fields`;
   - review draft text.
4. Text from outside Asgard SHALL be cleaned to one line and capped in length **(repo:
   `clean_line`)**. That covers commit subjects, Jira summaries, pull request titles, agents'
   summaries and AI-review reasons. Every result SHALL list the fields holding such text in
   `untrusted_fields`.
5. No result, log line or error SHALL contain a token, a password or a settings secret. Strings
   from events SHALL go through `scrub` **(repo)**.
6. A result SHALL hold at most 200 items and 64 KB of JSON. A larger answer SHALL be cut, marked
   `truncated`, and say how to narrow the question.
7. Tool annotations SHALL be accurate:
   - `readOnlyHint` is true only for tools that write nothing;
   - `destructiveHint` is true for `baldur_withdraw_estimate`;
   - `idempotentHint` is true for `baldur_review_pack` and for the read-only tools;
   - `openWorldHint` is false for every tool.
8. THE log SHALL record each call's tool name, time and outcome, and the ids it created or
   changed. It SHALL NOT record argument text, summaries or results.
9. Ysildir SHALL NOT read or write any file except:
   - the guides;
   - its own settings;
   - Baldur's settings, read only;
   - Muninn, through `open_app`;
   - its log.

### Requirement 7: Connecting clients

**User Story:** As the person, I want one command that connects Ysildir to my agent, so that I
don't hand-edit JSON on a locked-down machine.

#### Acceptance Criteria

1. `ysildir.cmd setup` SHALL add a `ysildir` server to a client's configuration. It SHALL use the
   client's own command where one exists, since the client then edits its own file, and fall back
   to the file (**check each client's version in task 0**):

   | Option | Client's own command (preferred) | Otherwise, the file |
   | --- | --- | --- |
   | `--kiro DIR` | none known | `DIR\.kiro\settings\mcp.json` |
   | `--kiro-user` | none known | `%USERPROFILE%\.kiro\settings\mcp.json` |
   | `--vscode DIR` | `code --add-mcp` (adds the server to your VS Code profile) | `DIR\.vscode\mcp.json` |
   | `--claude DIR` | `claude mcp add ysildir --scope project -- ...`, run in `DIR` | `DIR\.mcp.json` |

   `--print` SHALL print the commands and snippets instead of running or writing anything.
2. WHEN setup edits a file itself THEN it SHALL:
   - fill in this computer's Python launcher and Ysildir's installed path;
   - keep every other server and setting in the file;
   - not replace an existing `ysildir` entry unless given `--force`;
   - refuse to edit a file it can't parse, and print the snippet to paste instead. VS Code's file
     allows comments, which a JSON parser drops when it writes the file back.
3. Kiro's `autoApprove` SHALL list only the read-only tools that are on. The person may add
   `baldur_record_estimate` themselves, and the README says what that means.
4. `ysildir.cmd check` SHALL start the server, run `initialize` and `tools/list`, and print:
   - the protocol version;
   - Muninn's schema version;
   - each tool, with its switch.
5. WHERE the client can't use MCP, THE same work SHALL stay possible through
   `baldur.cmd ai ... --json` and the clipboard tier, and the guides SHALL say so. That covers
   Copilot's "MCP servers in Copilot" policy being off, VS Code's `ChatMCP` policy blocking it,
   and a Kiro without MCP.

### Requirement 8: Verification

**User Story:** As the maintainer, I want Ysildir tested like the rest of Asgard, and honest about
what only the workstation can show.

#### Acceptance Criteria

1. Tests SHALL be in `Asgard/tests/test_ysildir.py`, written with `unittest`
   (`IsolatedAsyncioTestCase`) so plain `python -m unittest` and pytest both run them. They SHALL
   drive the server with the SDK's own `Client`, against a seeded Muninn in a temporary
   `ASGARD_HOME` **(repo: the test pattern)**. They SHALL skip, saying why, when the SDK isn't
   installed or Python is older than 3.10, the way Heimdall's Playwright tests skip **(repo)**.
2. THE tests SHALL drive the server both in memory (`Client(server)`) and as a subprocess over
   real stdio (`Client(StdioServerParameters(...))`). They SHALL cover:
   - the instructions reaching the client (`Client.instructions`), and the server's name and
     version;
   - stdout carrying only protocol messages, and a clean exit when stdin closes;
   - each tool, including its refusals and their `ToolError` text;
   - an unknown field in a report or reply being refused;
   - the switches, the caps, `untrusted_fields`, the annotations and the output schemas;
   - the read-only connection refusing writes;
   - a recorded report having `via='mcp'`;
   - no tool being able to approve, reject, change or post. The tool table's list proves that,
     and a forged call to `approve` SHALL get the SDK's "Unknown tool" error.
3. A scripted client SHALL replay each worked example from the guides from start to finish. It
   SHALL then approve through the CLI and check the comment line Odin would post **(repo:
   `review_line`)**.
4. THE definition of done in AGENTS.md SHALL apply:
   - the suite passes on Python 3.9 (where Ysildir's tests skip) and on current Python with the
     SDK installed;
   - `tests/test_dependencies.py` passes, with every new import pinned in `requirements.txt`;
   - vermin and ruff are clean;
   - `check_muninn_schema` passes;
   - the smoke script still ends with PROJ-42 1h30m and PROJ-51 30m;
   - the docs match the code;
   - an independent review is done, since Ysildir can change what Baldur suggests.
5. HANDOFF SHALL list what only the workstation can show, as "needs target-machine verification":
   - which MCP features the on-premises Kiro supports (instructions, resources, prompts);
   - the org's Copilot MCP policy and VS Code's `ChatMCP` policy;
   - whether App Control lets the client start `py.exe` with Ysildir's script;
   - whether IT approves the SDK's compiled wheels, and which `mcp` version the agency mirror
     has;
   - how the agent behaves in the acceptance walkthrough (task 10).
