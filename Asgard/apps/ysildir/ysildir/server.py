"""Ysildir's MCP server: the SDK's MCPServer, with the tools, prompts and resources that are switched on.

    python apps\\ysildir\\cli.py serve      what an AI client starts; stdio only, never a port

The SDK owns the protocol: framing, the handshake and version negotiation, ping, cancellation,
the schemas and the argument checks. Nothing here patches it. Ysildir adds:
- TOOLS, the allow-list: each tool's handler, title, description and annotations. A tool that's
  switched off is never registered, so it isn't in tools/list and a call to it gets the SDK's
  "Unknown tool". Adding a tool means a switch (config.SWITCHES) and a spec change.
- results.tool around every handler: refusals as ToolError, the caps, the call log.
- teach: the instructions, the guides, the prompts and the resources.
This module and results.py are the only ones that import the SDK (MODULES.md, "mcp"); moving to
another SDK changes only them.
"""
import asyncio
import importlib.metadata
from typing import Any, Callable, List, NamedTuple, Optional

from mcp.server import MCPServer        # mcp 1.x: from mcp.server.fastmcp import FastMCP as MCPServer
from mcp.types import ToolAnnotations

from asgard import __version__

from . import SCHEMA, baldur_tools, muninn_tools, reader, results, teach
from . import config as switches


class Tool(NamedTuple):
    name: str
    fn: Optional[Callable[..., Any]]    # None: made per server from the switches (asgard_guide)
    title: str
    description: str
    read_only: bool
    idempotent: bool
    destructive: bool = False
    structured: bool = True             # False: the answer is plain text (a guide)


TOOLS = (
    Tool("asgard_guide", None, "Asgard guide",
         "Read one of Asgard's guides. Read the baldur guide before you first record an estimate or explain a day, "
         "and the muninn guide before you answer questions about the person's work. The tools topic lists "
         "Ysildir's tools, which are switched on, and the command-line way to do the same. Returns the guide as "
         "markdown. Changes nothing.", read_only=True, idempotent=True, structured=False),
    Tool("muninn_catalog", muninn_tools.muninn_catalog, "What Muninn holds",
         "What Muninn, Asgard's local database, holds: each table and view with what it holds, the app that writes "
         "it and how many rows it has, plus the rules for writing to it. Use it to learn what you can ask about. "
         "Returns no rows of data. Changes nothing.", read_only=True, idempotent=True),
    Tool("baldur_record_estimate", baldur_tools.baldur_record_estimate, "Record your estimate in Baldur",
         "Record your estimate of the person's working time on a change you made with them, after the commit. "
         "Baldur keeps it in Muninn and uses it to check its own estimate: it may lower figures or move minutes "
         "between tickets, never raise a day. Recording never approves anything; the person decides. Recording the "
         "same commits again replaces your earlier report. Returns the report's id and a message for the person. If "
         "Baldur refuses the report, show the person its message; don't retry with other numbers.",
         read_only=False, idempotent=False),
    Tool("baldur_withdraw_estimate", baldur_tools.baldur_withdraw_estimate, "Withdraw a report",
         "Withdraw a report you recorded that was wrong, by its id (r12). It stays in Muninn, marked withdrawn, and "
         "stops counting. To correct a report you can instead record it again with the same commits, which replaces "
         "it. Never approves or changes time.", read_only=False, idempotent=False, destructive=True),
    Tool("baldur_estimates", baldur_tools.baldur_estimates, "Reports recorded in Baldur",
         "List the agents' reports recorded in Baldur, for at most 31 days (default: the last 14). Returns each "
         "report's id, day, agent, ticket, minutes, confidence, commit count, summary and status. Changes nothing.",
         read_only=True, idempotent=True),
    Tool("baldur_day", baldur_tools.baldur_day, "How a day looks in Baldur",
         "Show how one day looks in Baldur: each ticket's estimate from git, what the person approved, what Jira "
         "holds, and Baldur's AI-assisted figures with their reasons and evidence. When the AI-assisted figures "
         "change the day, take is the command that takes them and keep the one that approves Baldur's estimate "
         "instead: give them to the person, never run them. Changes nothing.", read_only=True, idempotent=True),
    Tool("baldur_review_pack", baldur_tools.baldur_review_pack, "Start a day's AI review",
         "Start an AI review of one day's estimate, when the person asks for one. Stores the day's current estimate "
         "first, as Baldur does before any review, then returns the day's evidence (the pack, metadata only) and "
         "Baldur's review prompt. Follow the prompt exactly, with the pack as its input, then send your JSON reply "
         "to baldur_submit_review. Needs Baldur's review_mode set to metadata by the person.",
         read_only=False, idempotent=True),
    Tool("baldur_submit_review", baldur_tools.baldur_submit_review, "Submit a day's AI review",
         "Submit your reply to Baldur's review prompt for a day. Baldur checks it against the day's evidence as it "
         "is now: the day's total never rises, every adjustment cites evidence from the pack, and no ticket is "
         "added. A checked reply is stored as AI-assisted figures beside the estimate; it approves nothing. Returns "
         "the figures and the command that takes them, for the person to run. If Baldur refuses the reply, show "
         "the person its message.", read_only=False, idempotent=False),
    Tool("muninn_what_changed", muninn_tools.muninn_what_changed, "What changed",
         "What changed in Asgard since a time: one event per change any app made (Jira issues, approvals, "
         "estimates, agents' reports, worklogs), oldest first. Payload fields come back only for kinds whose "
         "payloads hold no free text. Changes nothing.", read_only=True, idempotent=True),
    Tool("muninn_day_status", muninn_tools.muninn_day_status, "Approved against logged time",
         "Approved time against time logged in Jira, per day and ticket, for at most 31 days (default: the last 7): "
         "the minutes the person approved in Baldur, the development time Jira holds, what Odin still has to post, "
         "and why Odin can't post a day when it can't. Changes nothing.", read_only=True, idempotent=True),
    Tool("muninn_issue", muninn_tools.muninn_issue, "One Jira issue",
         "One Jira issue as Odin last collected it, by key; an old key finds the issue after it moved. Returns its "
         "key, summary, status, type, epic and when it last changed, never its description or comments. Changes "
         "nothing.", read_only=True, idempotent=True),
    Tool("muninn_search", muninn_tools.muninn_search, "Search Muninn",
         "Search the person's Jira issues, their own commits and their pull requests by words, best matches first. "
         "Returns each hit's kind, id, ref (key, SHA or repo#number), title and time. Each word is matched as "
         "written. Changes nothing.", read_only=True, idempotent=True),
)


def annotations(tool: Tool) -> ToolAnnotations:
    return ToolAnnotations(title=tool.title, readOnlyHint=tool.read_only, destructiveHint=tool.destructive,
                           idempotentHint=tool.idempotent, openWorldHint=False)


def build(config: Optional[switches.Config] = None) -> MCPServer:
    """The server, with only what the person has switched on."""
    config = config or switches.load()
    server = MCPServer("ysildir", title="Ysildir (Asgard)", version=__version__,
                       instructions=teach.instructions(config), log_level="WARNING")
    for tool in TOOLS:
        if config.on(tool.name):
            fn = tool.fn or teach.guide_tool(config)
            server.add_tool(results.tool(tool.name, fn), name=tool.name, title=tool.title,
                            description=tool.description, annotations=annotations(tool),
                            structured_output=tool.structured)
    teach.register(server, config, wrap=results.prompt)
    return server


def serve() -> None:
    config = switches.load()
    results.log_call("serve", "started", None, f"{len(config.on_names())} tools on" +
                     (", switches file broken" if config.problem else ""))
    build(config).run("stdio")          # stdio only: no port, ever


def check(config: Optional[switches.Config] = None) -> List[str]:
    """What `ysildir.cmd check` prints: the server as an agent sees it, through the SDK's own client in memory."""
    from mcp import Client

    config = config or switches.load()

    async def look() -> Any:
        async with Client(build(config)) as client:
            return (client.protocol_version, client.instructions or "", await client.list_tools(),
                    await client.list_prompts(), await client.list_resources())

    protocol, instructions, tools, prompts, resources = asyncio.run(look())
    out = [f"Ysildir {__version__} on the MCP SDK {importlib.metadata.version('mcp')}, protocol {protocol}"]
    try:
        with reader() as con:
            version = int(con.execute("PRAGMA user_version").fetchone()[0])
        out.append(f"Muninn: version {version} (Ysildir understands {SCHEMA[0]} to {SCHEMA[1]})")
    except Exception as exc:                   # noqa: BLE001 - check reports every problem it finds
        out.append(f"Muninn: {exc}")
    try:
        baldur_tools.settings()
        out.append("Baldur's settings: found")
    except Exception as exc:                   # noqa: BLE001
        out.append(f"Baldur's settings: {exc}")
    out.append(f"Switches: {config.path}" + ("" if config.path.exists() else " (no file yet: the defaults)"))
    if config.problem:
        out.append(f"  The file can't be used, so only {switches.ALWAYS_ON} is on: {config.problem}")
    if config.unknown:
        out.append(f"  Ignored, not Ysildir tools: {', '.join(config.unknown)}")
    listed = {t.name for t in tools.tools}
    out.append(f"Tools an agent sees: {len(listed)} of {len(TOOLS)}")
    for s in switches.SWITCHES:
        out.append(f"  {'on ' if s.name in listed else 'off'}  {s.name:<26} sends: {s.sends}")
    out.append("Prompts: " + (", ".join(p.name for p in prompts.prompts) or "none"))
    out.append("Resources: " + (", ".join(str(r.uri) for r in resources.resources) or "none"))
    out.append(f"Instructions: {len(instructions)} characters")
    return out
