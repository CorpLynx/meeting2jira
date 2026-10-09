# Tasks: Ysildir, Asgard's MCP server

Work through these in order. Each task ends with its tests passing. The commands, run from
`Asgard/`, are:

```
python -m unittest discover -s tests
python ../tools/run_tests.py tests/test_ysildir.py
```

The second is the pytest runner, from the repository root's `tools/`. Each task's requirements
are cited as R*n*.*m*.

Use the best module for each job, and note the alternatives in case it isn't on-premises (design,
"Modules").

- [ ] 0. Reconcile this spec with the on-premises code and tools. *This task is read-only, and
  ends at a checkpoint with the person.*
  - Compare every **(repo)** statement with the code in front of you: Muninn's schema version,
    `open_app`, the guard, and Baldur's `record_agent_estimate`, `assist`, `desk.load_day`,
    `settings.load` and `cli.SCHEMA`. Edit all three spec files to match.
  - Find the on-premises steering and guardrail documents that tell agents to estimate how long
    changes take. Write down, for the person:
    - what they ask the agent to estimate: the person's time spent, or a "without AI" sizing
      figure, which doesn't belong in Baldur;
    - whether they produce a range;
    - where the answer goes today;
    - the edits that would route the answer to `baldur_record_estimate` (or to
      `baldur.cmd ai record`), keeping their estimating method where it's stricter. See
      `Asgard/apps/baldur/agents/README.md`, "Reconciling".
  - Check the MCP SDK on-premises:
    - Which `mcp` versions does the agency mirror or software catalog have? The pin is 2.3.0; a
      1.x line means the FastMCP import (design, "Modules").
    - Is the workstation's Python 3.10 or newer? It should be, since Muninn needs 3.11 on Windows.
    - Will IT approve the compiled wheels: `pydantic-core`, `cryptography`, `cffi`, `rpds-py` and
      `pywin32`?
  - Record the on-premises Kiro's version, and test what its MCP support covers:
    - Does `.kiro/settings/mcp.json` have the keys in the design (`command`, `args`, `env`,
      `disabled`, `autoApprove`)?
    - Is `instructions` passed to the model?
    - Are MCP prompts and resources supported?
    - Do hooks see MCP tool calls?
  - Check the other clients' own setup commands: VS Code's `code --add-mcp` and Claude Code's
    `claude mcp add`.
  - Check the Copilot policy ("MCP servers in Copilot"), VS Code's `ChatMCP` policy, and whether
    App Control lets Kiro start `py.exe` with a script under `%LOCALAPPDATA%\Asgard\app\`
    (`tools/asgard_preflight.ps1` covers part of this).
  - Bring the SDK's approval, the default switches (R6.1), and open questions 4 and 7 to the
    person, and through them to the ISSO.
  - **Stop. Report what you found and the spec edits you made, and wait for the person to
    confirm.**

- [ ] 1. Add the SDK (`Asgard/requirements.txt`, `vendor/`, `ysildir.cmd`)
  - Pin `mcp` and `pydantic` with the comment block from the design ("Pins to add"). It says what
    each is for, why the standard library isn't enough, that it's native, and its approval status.
    Use the version task 0 found.
  - Paste the two sections from the design ("Sections to add to `Asgard/MODULES.md`") into
    `MODULES.md`'s "Packages", and remove Ysildir's row from "Planned". Each section gives where
    the package is used, what happens without it, and the standard-library and package
    alternatives. Keep each "Used in" row matching the files that import it.
  - Download the wheel set for the workstation's Python (`pip download ... --platform win_amd64`)
    and list it, marking the native wheels, in the release notes. The payload carries the wheels
    in `vendor/` with their hashes **(repo: `docs/updates.md`)**.
  - `ysildir.cmd`, modelled on `baldur.cmd`, finds Python 3.10+. When `mcp` won't import, it
    prints what's missing and what still works (`baldur.cmd ai ... --json`, the clipboard tier),
    and exits 2.
  - Tests: `tests/test_dependencies.py` passes with the new pins, imports and `MODULES.md`
    sections. The missing-SDK message runs with `mcp` hidden (a `sys.modules` stub).
  - _R1.9–R1.12_

- [ ] 2. The server on the SDK (`server.py`, `results.py`, `models.py`)
  - `server.build(config)`: `MCPServer("ysildir", title=..., version=..., instructions=...,
    log_level="WARNING")`. Then `add_tool(..., annotations=ToolAnnotations(...),
    structured_output=True)` for each tool that's on. `serve()` runs `run("stdio")` and nothing
    else.
  - `models.py`: `AgentReport` and `ReviewReply` (`extra="forbid"`), and a result model per tool
    carrying `truncated`, `narrow` and `untrusted_fields`.
  - `results.py`:
    - the `refusals` wrapper: Asgard's errors become `ToolError` with their message (design,
      "Errors");
    - the caps;
    - the call log in `logs/ysildir.log` (`RotatingFileHandler`, about 1 MB): tool, time,
      outcome and ids, never arguments.
  - Tests, through the SDK's `Client` in memory:
    - `client.instructions`;
    - the annotations and the input and output schemas;
    - a `ToolError`'s text;
    - an unknown field in a report refused;
    - "Unknown tool" for a name that isn't registered;
    - a structured result.

    Over stdio: one session under `assertNoLogs("mcp.client.stdio", "ERROR")`, and a clean exit
    on EOF.
  - _R1.1–R1.8, R6.6–R6.8_

- [ ] 3. Switches (`config.py`) and the `tools` command
  - Read `settings/ysildir.json` through a pydantic model, with its defaults. A broken file fails
    closed, and the message names the bad entry. Unknown names are logged.
  - A tool that is off isn't registered, so it isn't in `list_tools`, and calling it gets
    "Unknown tool". Its instruction lines and prompts are left out too. The `tools` topic of
    `asgard_guide` lists it as off, with the command that turns it on.
  - `cli.py tools --list | --on NAME | --off NAME` writes the file through a temporary file and a
    rename.
  - Tests: the defaults, a broken file, unknown names, and `list_tools` with all switches on
    equal to the design's table.
  - _R6.1, R6.2_

- [ ] 4. The teaching surface (`teach.py`, `instructions.md`, `asgard/muninn/agent-guide.md`)
  - Write `instructions.md` and the Muninn guide from the design's drafts.
  - `asgard_guide(topic)` covers `baldur`, `muninn`, `review` and `tools`. The `tools` topic is
    generated: each tool's switch, what it sends, and its command-line equivalent.
  - Resources, through `@server.resource`: `asgard://guides/baldur`, `asgard://guides/muninn` and
    `asgard://guides/review` (`text/markdown`).
  - The four prompts, through `@server.prompt`, with argument checks. A date defaults to today.
  - Add worked examples A to E to `apps/baldur/prompts/agent-guide.md`, and bump it to
    `baldur-agent-2`: the Kiro steering, `guide_version()`, the tests and the docs follow. The
    guide's MCP line becomes "call `baldur_record_estimate` with the report as `report`".
  - Tests:
    - instructions under 2,000 characters;
    - no line naming a tool that is off;
    - the versions named equal each guide's first line;
    - every guide reachable as a tool and as a resource (`read_resource`);
    - `get_prompt` text names only tools that are on.
  - _R2.1–R2.8_

- [x] 4a. The Kiro steering prefers the MCP tool when it is there (`apps/baldur/agents/kiro-steering.md`).
  *Done on this branch.*
  - _R2.9_

- [ ] 5. Baldur tools: the agent's estimates (`baldur_tools.py`)
  - `baldur_record_estimate(report: AgentReport)` calls `record_agent_estimate(..., via="mcp")`
    on `as_baldur()`, and returns id, status, replaced and the message.
    `baldur_withdraw_estimate` calls `withdraw_agent_estimate`.
  - `baldur_estimates`: move `cmd_ai_list`'s query into
    `asgard.muninn.baldur.list_agent_estimates(con, first, last, include_withdrawn)`, and have the
    CLI and Ysildir share it. The range is at most 31 days.
  - Load Baldur's settings without creating them: add `settings.load(create=False)` or similar,
    with a test.
  - Tests:
    - one per Baldur refusal case (as in `test_baldur_assist.IntakeTests`), run through the tool;
    - `via='mcp'` on the stored row;
    - the duplicate and replace paths;
    - a Muninn that isn't set up;
    - a Muninn at version 3 or 5.
  - _R3.1–R3.6_

- [ ] 6. Baldur tools: the day and the MCP-tier review (`baldur_tools.py`)
  - `baldur_day`: `desk.load_day` on the read-only `ysildir` connection, mapped to the design's
    output. `take` and `keep` appear only when the AI-assisted figures change the day.
    `include_report` adds the report text.
  - `baldur_review_pack`: `assist.day_pack` on `as_baldur()`, plus the prompt and `reply_to`.
    `review_mode` is obeyed through `assist._check_mode`; don't duplicate it.
  - `baldur_submit_review(date, reply: ReviewReply, model)`:
    `assist.apply_reply(..., tier="mcp", model=...)`. A stale pack, a changed day and a rejected
    reply each come back as Baldur's message.
  - Tests:
    - on the smoke day, after reports r12 (PROJ-42 at 45m) and r13 (PROJ-51 at 75–90m),
      `baldur_day` returns PROJ-42 90→45 and PROJ-51 30→75;
    - `review_mode` off, and `content`;
    - a stale pack hash;
    - a reply that raises the day;
    - a misspelled reply key refused by `ReviewReply`;
    - approving afterwards stores the `review` note and gives Odin's `Reviewed:` line.
  - _R4.1–R4.7_

- [ ] 7. Muninn tools (`muninn_tools.py`)
  - `muninn_catalog`: tables and views from `sqlite_schema`, owners from `guard.OWNERS` and
    `SHARED`, a one-line meaning per table, and row counts. Keep the meanings in
    `asgard/muninn/agent-guide.md` or beside `guard.OWNERS`, not in Ysildir.
  - `muninn_what_changed`, with the payload allow-list from the design.
  - `muninn_day_status`, `muninn_issue` (through the aliases) and `muninn_search` (each term
    quoted, kinds filtered by `rowid & 15`, bm25 order).
  - Tests:
    - each tool on seeded data;
    - FTS5 syntax in a query (`"`, `*`, `NEAR(`, `OR`, `-`) searches as plain words and never
      fails;
    - a credential-shaped string in an event comes back masked;
    - a `worklog.failed` error and a `work_item.created` summary are never returned;
    - every query runs on a read-only connection.
  - _R5.1–R5.7, R6.3–R6.5_

- [ ] 8. Connecting clients (`clients.py`, `cli.py setup | check`) and the guard
  - `setup --kiro DIR | --kiro-user | --vscode DIR | --claude DIR | --print [--force]`. Use the
    client's own command where task 0 found one (`code --add-mcp`,
    `claude mcp add --scope project`); otherwise use the design's merge rules. `autoApprove` lists
    only the read-only tools that are on.
  - `check`: build the server, connect the SDK's `Client` to it in memory, and print the protocol
    version, Muninn's version and each tool with its switch.
  - Extend `apps/baldur/agents/hooks/guard_baldur.py` (or add a guard for all of Asgard) so an
    agent can't run `ysildir.cmd tools --on|--off` or `ysildir.cmd setup`. Add a test for each.
  - Tests:
    - merging into a file that has other servers;
    - refusing a file with comments;
    - `--force`;
    - the client commands built correctly, run against a fake `code` and `claude` on `PATH`;
    - the launcher on Windows and on POSIX;
    - `check`'s output.
  - _R7.1–R7.5_

- [ ] 9. The scripted walkthrough and the stdio tests
  - A scripted client (the SDK's `Client` over stdio) replays examples A to F against the smoke
    data. It then approves through the CLI and checks the `Reviewed:` line.
  - Check that the forged decision tools get "Unknown tool", and run the static AST checks from
    the design's testing table (no decision calls; no stray `print`).
  - Run the mutation checks: disable the switch check, flip a `readOnlyHint`, drop a cap. A test
    must fail each time; restore the code after each one.
  - _R8.1–R8.3_

- [ ] 10. On-premises acceptance with the real agent. *This is a checkpoint with the person.*
  - Install into a test workspace with `ysildir.cmd setup --kiro`, then confirm Kiro lists the
    server and its tools.
  - The person runs three scenarios with Kiro, and you note what landed in Muninn
    (`baldur.cmd ai list --json`):
    1. Make a small change and commit it. The agent records an estimate on its own (the steering
       and the instructions), with full SHAs and a range when unsure.
    2. Ask "how does today look?" with `baldur_day` on. The agent explains the estimate against
       the AI-assisted figures, and gives the command without running it.
    3. Ask the agent to approve the day. It refuses and gives the command; if it tries the
       terminal anyway, the guard hook blocks it.
  - Record the results, and anything the agent got wrong, in HANDOFF. Fix the teaching text, not
    the rules.
  - _R8.5_

- [ ] 11. Docs, and the definition of done
  - Update:
    - `docs/integration/ysildir.md`: built status, the tool table, switches and data flows;
    - `docs/integration/README.md`: Ysildir's row, and the events it reads;
    - `docs/security-and-data.md`: a row per tool's data flow, and the SDK in the dependency
      list;
    - `README.md`: the `ysildir.cmd` commands, and what to install;
    - `HANDOFF.md`: status and "needs target-machine verification";
    - `asgard/apps.json`: the tile can stay `coming_soon` until it has a window (the GUI comes
      later).
  - Run the definition of done from AGENTS.md:
    - the full suite on Python 3.9 (Ysildir's tests skip) and on current Python with the SDK;
    - `check_muninn_schema`, vermin and ruff;
    - the smoke script, which still ends with PROJ-42 1h30m and PROJ-51 30m;
    - an independent review of anything that can change what Baldur suggests.
  - _R8.4, R8.5_

- [ ] 12. *Only if task 0 finds `mcp` 2.x can't be used on-premises.* Take the first alternative
  from the design's "Modules" table that IT accepts, and change only `server.py` and
  `results.py`:
  1. the newest `mcp` 1.x: `from mcp.server.fastmcp import FastMCP`, with the same decorators,
     annotations and `ToolError`;
  2. `fastmcp` 4.x;
  3. the standard-library JSON-RPC server over the same handlers, designed in this spec's first
     revision (commit `d510709`);
  4. no MCP: say so in the guides, and keep the CLI and clipboard tier as the route.

  Update the pin, its comment, its `MODULES.md` section and the tests to match.
  - _R1.1–R1.11_
