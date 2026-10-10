"""Ysildir, Asgard's MCP server, driven through the MCP SDK's own client: in memory and over stdio.

The named rules (Ysildir spec, .kiro/specs/ysildir-mcp/, requirement 8):
- The protocol is the SDK's. The instructions, tools, prompts and resources are what a real client
  sees; stdout carries only protocol messages; the server exits cleanly when stdin closes.
- The tool table is the allow-list. With every switch on, tools/list is exactly the documented
  set; a tool that's switched off, or a decision tool that never existed, gets "Unknown tool".
- Ysildir has no rules of its own. Baldur's refusals come back as the tool's error, unchanged;
  reads run on a read-only connection; a report recorded through Ysildir has via='mcp'.
- Metadata only. Payloads come back only with fields that hold no free text, and text from
  outside Asgard is cleaned and named in untrusted_fields.
- The guides' worked examples replay from start to finish over stdio, and approving through
  Baldur's CLI afterwards gives the line Odin posts.

These need Python 3.10+ and the MCP SDK (requirements.txt: mcp, pydantic), and skip without them,
saying why. The rest (the missing-SDK message, the static checks, the guard) always run.
"""
import ast
import contextlib
import importlib.util
import io
import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
YSILDIR = ROOT / "apps" / "ysildir"
PACKAGE = YSILDIR / "ysildir"
for folder in (ROOT, ROOT / "apps" / "baldur", YSILDIR, ROOT / "tests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import asgard  # noqa: E402
import ysildir  # noqa: E402   (standard library only: SCHEMA, reader, Refused)
from asgard import muninn  # noqa: E402
from asgard.muninn import baldur as rules  # noqa: E402
from asgard.muninn import guard, tables  # noqa: E402
from baldur import assist  # noqa: E402
from baldur import cli as baldur_cli  # noqa: E402
from baldur import settings as baldur_settings  # noqa: E402
from test_baldur import MuninnCase, t  # noqa: E402
from test_baldur_assist import LATER, AssistCase, report, run_hook  # noqa: E402

SKIP = None
if sys.version_info < (3, 10):
    SKIP = "Ysildir needs Python 3.10 or newer (the MCP SDK does)"
else:
    try:
        from mcp import Client, MCPError, StdioServerParameters
        from ysildir import clients, config, models, muninn_tools, results, server, teach
    except ImportError as exc:                 # pragma: no cover - depends on what's installed
        SKIP = f"the MCP SDK isn't installed ({exc.name})"
needs_sdk = unittest.skipIf(SKIP is not None, SKIP or "")

DEFAULT_TOOLS = ["asgard_guide", "muninn_catalog", "baldur_record_estimate", "baldur_withdraw_estimate",
                 "baldur_estimates"]
# (readOnlyHint, destructiveHint, idempotentHint): the spec's annotation table. openWorldHint is always false.
ANNOTATIONS = {
    "asgard_guide": (True, False, True), "muninn_catalog": (True, False, True),
    "baldur_record_estimate": (False, False, False), "baldur_withdraw_estimate": (False, True, False),
    "baldur_estimates": (True, False, True), "baldur_day": (True, False, True),
    "baldur_review_pack": (False, False, True), "baldur_submit_review": (False, False, False),
    "muninn_what_changed": (True, False, True), "muninn_day_status": (True, False, True),
    "muninn_issue": (True, False, True), "muninn_search": (True, False, True)}
DAY = "2026-10-01"


def rules_day():
    import datetime as dt
    return dt.date.fromisoformat(DAY)


def load_cli():
    """apps/ysildir/cli.py as a module (Baldur has a cli.py too, so not by name)."""
    spec = importlib.util.spec_from_file_location("ysildir_cli", YSILDIR / "cli.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def text(result):
    return "\n".join(c.text for c in result.content if getattr(c, "text", None))


def baldur(*args):
    """Baldur's command line, as the person runs it."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = baldur_cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


def a_report(shas, **fields):
    body = {"agent": "kiro", "guide": "baldur-agent-2", "date": DAY, "commits": shas, "minutes": 60,
            "minutes_low": 45, "confidence": "medium", "summary": "Added jittered retry to the poller and its tests."}
    body.update(fields)
    return {k: v for k, v in body.items() if v is not None}


class YsildirCase(AssistCase, unittest.IsolatedAsyncioTestCase):
    """The smoke day's Muninn in a temporary ASGARD_HOME (test_baldur_assist.AssistCase), every tool on."""

    def setUp(self):
        super().setUp()
        self.switches = config.everything_on()

    def tearDown(self):
        results.close_log()
        super().tearDown()

    async def call(self, name, args=None, switches=None):
        async with Client(server.build(switches or self.switches)) as client:
            return await client.call_tool(name, args or {})

    def ok(self, result):
        self.assertFalse(result.is_error, text(result))
        return result.structured_content

    def refused(self, result):
        self.assertTrue(result.is_error, result.structured_content)
        return text(result)

    def counts(self):
        names = [r[0] for r in self.con.execute("SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT "
                                                "LIKE 'sqlite%'")]
        return {n: self.con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}

    def log(self):
        path = self.dir / "logs" / "ysildir.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""


# --------------------------------------------------------------------------
# The protocol, the allow-list and the teaching
# --------------------------------------------------------------------------

@needs_sdk
class ProtocolTests(YsildirCase):
    async def test_a_client_sees_the_server_its_instructions_and_only_whats_on(self):
        async with Client(server.build(config.load())) as client:          # no switches file: the defaults
            info = client.server_info
            instructions = client.instructions
            names = [x.name for x in (await client.list_tools()).tools]
            prompts = [p.name for p in (await client.list_prompts()).prompts]
            resources = sorted(str(r.uri) for r in (await client.list_resources()).resources)
        self.assertEqual((info.name, info.version), ("ysildir", asgard.__version__))
        self.assertEqual(names, DEFAULT_TOOLS)
        self.assertEqual(prompts, ["record-estimate"])
        self.assertEqual(resources, ["asgard://guides/baldur", "asgard://guides/muninn", "asgard://guides/review"])
        self.assertLessEqual(len(instructions), teach.MAX_INSTRUCTIONS)
        self.assertIn("baldur_record_estimate", instructions)
        self.assertIn("The person decides.", instructions)
        for off in ("baldur_day", "baldur_review_pack", "baldur_submit_review", "muninn_what_changed",
                    "muninn_day_status", "muninn_issue", "muninn_search"):
            self.assertNotIn(off, instructions)

    async def test_the_allow_list_is_exactly_the_documented_tools(self):
        async with Client(server.build(self.switches)) as client:
            tools = {x.name: x for x in (await client.list_tools()).tools}
        self.assertEqual(list(tools), list(config.NAMES))
        self.assertEqual([x.name for x in server.TOOLS], list(config.NAMES), "a tool needs a switch, and the reverse")
        for name, tool in tools.items():
            with self.subTest(name):
                a = tool.annotations
                self.assertEqual((a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint),
                                 ANNOTATIONS[name] + (False,))
                self.assertEqual(tool.output_schema is None, name == "asgard_guide", "every data tool has a schema")
                self.assertGreater(len(tool.description), 120, "the description is the lesson every client shows")
        self.assertFalse(tools["baldur_record_estimate"].input_schema["$defs"]["AgentReport"]["additionalProperties"])
        self.assertFalse(tools["baldur_submit_review"].input_schema["$defs"]["ReviewReply"]["additionalProperties"])

    async def test_decisions_were_never_tools_and_a_tool_thats_off_isnt_one(self):
        for name in ("approve", "baldur_approve", "approve_day", "baldur_change", "baldur_actual", "muninn_sql",
                     "odin_post"):
            with self.subTest(name):
                self.assertIn(f"Unknown tool: {name}", self.refused(await self.call(name, {"date": DAY})))
        self.assertIn("Unknown tool: baldur_day",
                      self.refused(await self.call("baldur_day", {"date": DAY}, switches=config.load())))

    async def test_every_guide_is_a_tool_topic_and_a_resource(self):
        async with Client(server.build(self.switches)) as client:
            for topic, path in teach.GUIDES.items():
                with self.subTest(topic):
                    want = path.read_text(encoding="utf-8")
                    self.assertEqual(text(await client.call_tool("asgard_guide", {"topic": topic})), want)
                    read = (await client.read_resource(f"asgard://guides/{topic}")).contents[0]
                    self.assertEqual((read.text, read.mime_type), (want, "text/markdown"))
            bad = await client.call_tool("asgard_guide", {"topic": "secrets"})
        self.assertTrue(bad.is_error)
        self.assertIn("baldur_record_estimate", teach.GUIDES["baldur"].read_text(encoding="utf-8"))

    def test_the_instructions_name_each_guides_version(self):
        lines = teach.instructions(config.everything_on()).splitlines()
        self.assertEqual(lines[-1], f"Guides: {teach.version(teach.GUIDES['baldur'])}, "
                                    f"{teach.version(teach.GUIDES['muninn'])}.")
        self.assertEqual(teach.version(teach.GUIDES["baldur"]), assist.guide_version())
        self.assertEqual(teach.version(teach.GUIDES["baldur"]), "baldur-agent-2")
        self.assertFalse(any(line.startswith("<!--") for line in lines))
        self.assertTrue(all(len(teach.named(line)) <= 2 for line in lines))

    def test_the_tools_topic_says_whats_off_and_who_turns_it_on(self):
        guide = teach.guide("tools", config.load())
        self.assertIn("| `baldur_day` | off |", guide)
        self.assertIn("| `baldur_record_estimate` | on |", guide)
        self.assertIn("ysildir.cmd tools --on NAME", guide)
        self.assertIn("Never a tool", guide)

    async def test_prompts_name_only_tools_that_are_on(self):
        some = config.Config(dict(config.defaults(), muninn_what_changed=True), self.dir / "x.json")
        async with Client(server.build(some)) as client:
            names = [p.name for p in (await client.list_prompts()).prompts]
            changed = (await client.get_prompt("what-changed", {"since": "2026-10-08"})).messages[0].content.text
        self.assertEqual(names, ["record-estimate", "what-changed"])
        self.assertIn("muninn_what_changed from 2026-10-08", changed)
        self.assertNotIn("muninn_issue", changed, "its tool is off, so its step is left out")
        async with Client(server.build(self.switches)) as client:
            day = (await client.get_prompt("my-day", {"date": DAY})).messages[0].content.text
            record = (await client.get_prompt("record-estimate", {"key": "proj-42"})).messages[0].content.text
            review = (await client.get_prompt("review-day", {})).messages[0].content.text
            with self.assertRaisesRegex(MCPError, "isn't a day; give one like 2026-10-01"):
                await client.get_prompt("my-day", {"date": "October 1"})
            with self.assertRaisesRegex(MCPError, "isn't a Jira key"):
                await client.get_prompt("record-estimate", {"key": "not a key"})
        self.assertIn(f"Call baldur_day for {DAY}.", day)
        self.assertIn("key PROJ-42", record)
        self.assertIn("guide baldur-agent-2", record)
        self.assertIn("baldur_review_pack", review)
        for prompt in (day, record, review):
            self.assertTrue(set(teach.named(prompt)) <= set(config.NAMES))


@needs_sdk
class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)   # after the test's own cleanups (open files)
        self.path = Path(self.tmp.name) / "settings" / "ysildir.json"


    def test_no_file_means_the_defaults(self):
        found = config.load(self.path)
        self.assertEqual(found.on_names(), DEFAULT_TOOLS)
        self.assertIsNone(found.problem)
        self.assertFalse(self.path.exists(), "reading the switches never writes them")

    def test_a_broken_file_fails_closed_and_names_what_is_wrong(self):
        self.path.parent.mkdir(parents=True)
        for content, message in (('{"version": 1, "tools": {"baldur_day": "yes"}}', "tools.baldur_day"),
                                 ("{not json", "isn't JSON"), ("[]", "JSON object"),
                                 ('{"version": 7}', "version")):
            with self.subTest(content):
                self.path.write_text(content, encoding="utf-8")
                found = config.load(self.path)
                self.assertEqual(found.on_names(), ["asgard_guide"])
                self.assertIn(message, found.problem)
                self.assertNotIn('"yes"', found.problem, "the message names the entry, not its value")
                self.assertIn(message, teach.guide("tools", found))

    def test_unknown_names_are_ignored_and_listed(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"tools": {"baldur_day": True, "approve_everything": True}}), encoding="utf-8")
        found = config.load(self.path)
        self.assertTrue(found.on("baldur_day"))
        self.assertFalse(found.on("approve_everything"))
        self.assertEqual(found.unknown, ["approve_everything"])
        self.assertIn("approve_everything", teach.guide("tools", found))

    def test_switching_keeps_the_rest_and_writes_whole_files(self):
        found = config.switch(["baldur_day", "muninn_issue"], True, self.path)
        self.assertEqual(found.on_names(), DEFAULT_TOOLS[:5] + ["baldur_day", "muninn_issue"])
        found = config.switch(["baldur_estimates"], False, self.path)
        self.assertFalse(found.on("baldur_estimates"))
        self.assertTrue(found.on("baldur_day"))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(data["tools"]), list(config.NAMES))
        self.assertIn("ISSO", data["_help"])
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["ysildir.json"], "no temporary file left")
        with self.assertRaisesRegex(ysildir.Refused, "muninn_sql isn't a Ysildir tool"):
            config.switch(["muninn_sql"], True, self.path)
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ysildir.Refused, "has a mistake in it"):
            config.switch(["baldur_day"], True, self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken", "a broken file is never overwritten")

    def test_the_tools_command(self):
        cli = load_cli()
        with mock.patch.dict(os.environ, {"ASGARD_HOME": self.tmp.name}):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["tools", "--on", "baldur_day", "--off", "baldur_estimates"])
            self.assertEqual(code, 0, err.getvalue())
            self.assertIn("on   baldur_day", out.getvalue())
            self.assertIn("off  baldur_estimates", out.getvalue())
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["tools", "--on", "baldur_day", "--off", "baldur_day"]), 1)
                self.assertEqual(cli.main(["tools", "--on", "muninn_sql"]), 1)
        self.assertIn("can't be switched both on and off", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


# --------------------------------------------------------------------------
# Baldur tools
# --------------------------------------------------------------------------

@needs_sdk
class RecordTests(YsildirCase):
    async def test_a_report_is_recorded_through_baldurs_rules_with_via_mcp(self):
        p42, _ = self.worked()
        body = a_report(p42, key="PROJ-42")
        got = self.ok(await self.call("baldur_record_estimate", {"report": body}))
        self.assertEqual((got["id"], got["status"], got["replaced"]), ("r1", "recorded", []))
        self.assertIn("Recorded r1: kiro, 45m to 1h00m on PROJ-42 (2026-10-01)", got["message"])
        self.assertIn("nothing changes until you approve", got["message"])
        row = self.con.execute("SELECT via, guide_version, minutes, minutes_low FROM agent_estimates").fetchone()
        self.assertEqual(tuple(row), ("mcp", "baldur-agent-2", 60, 45))
        again = self.ok(await self.call("baldur_record_estimate", {"report": dict(body, schema=rules.REPORT_SCHEMA)}))
        self.assertEqual((again["id"], again["status"]), ("r1", "duplicate"))
        newer = self.ok(await self.call("baldur_record_estimate", {"report": dict(body, minutes=50)}))
        self.assertEqual((newer["id"], newer["replaced"]), ("r2", ["r1"]))
        log = self.log()
        self.assertIn("baldur_record_estimate ok", log)
        self.assertIn("r2 recorded replaced r1", log)
        self.assertNotIn("jittered", log, "the log never holds a report's text")

    async def test_baldurs_refusals_come_back_through_the_tool(self):
        """Baldur's own refusal cases (test_baldur_assist.IntakeTests), through the tool: the schema refuses the
        wrong shapes, Baldur the wrong meanings, and nothing is stored either way."""
        p42, _ = self.worked()
        by_schema = [
            (dict(extra=1), "Extra inputs are not permitted"), (dict(schema="baldur.agent_estimate/9"), "schema"),
            (dict(minutes=0), "greater than or equal to 1"), (dict(minutes=1441), "less than or equal to 1440"),
            (dict(minutes=True), "valid integer"), (dict(minutes="60"), "valid integer"),
            (dict(confidence="sure"), "'high', 'medium' or 'low'"), (dict(summary="x" * 301), "at most 300"),
            (dict(commits="abc1234"), "valid list"), (dict(commits=[f"{n:040x}" for n in range(51)]), "at most 50"),
            (dict(date="Oct 1"), "pattern")]
        by_baldur = [
            (dict(agent="kiro; rm -rf /"), "agent must name the tool"),
            (dict(minutes=30, minutes_low=45), "can't be more than minutes"),
            (dict(summary=" "), "summary is required"),
            (dict(summary="Fixed it:\n```python\nprint(1)\n```"), "no code or diff"),
            (dict(summary="diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b"), "no code or diff"),
            (dict(commits=["not-a-sha"]), "isn't a commit SHA"), (dict(key="proj 42"), "isn't a Jira key"),
            (dict(date="2999-10-05"), "date is in the future"),
            (dict(ended_at="2999-10-04T12:00:00Z"), "ended_at is in the future"),
            (dict(started_at="2026-10-01T09:00:00"), "needs its time zone"),
            (dict(started_at="2026-10-01T11:00:00Z", ended_at="2026-10-01T10:00:00Z"), "before started_at"),
            (dict(started_at="2026-09-29T09:00:00Z", ended_at="2026-10-01T10:00:00Z"), "at most one day")]
        for fields, message in by_schema + by_baldur:
            with self.subTest(message):
                said = self.refused(await self.call("baldur_record_estimate", {"report": a_report(p42, **fields)}))
                self.assertIn(message, said)
                self.assertTrue(said.startswith("Error executing tool baldur_record_estimate: "), said)
        for fields, message in by_baldur:
            with self.subTest(f"Baldur's own words: {message}"):
                with self.assertRaisesRegex(muninn.MuninnError, message):
                    rules.record_agent_estimate(self.con, a_report(p42, **fields), now=LATER)
        self.assertEqual(self.counts()["agent_estimates"], 0)
        self.assertIn("baldur_record_estimate refused", self.log())
        # The SDK drops arguments it doesn't know at the top level (the reason a report is one model): a stray
        # "approve" does nothing, and the minutes recorded are the report's own.
        got = self.ok(await self.call("baldur_record_estimate", {"report": a_report(p42), "approve": True,
                                                                 "minutes": 5}))
        self.assertEqual(got["status"], "recorded")
        self.assertEqual(self.con.execute("SELECT minutes FROM agent_estimates").fetchone()[0], 60)
        self.assertEqual(self.rows("approved"), [])

    async def test_withdrawing_a_report(self):
        p42, _ = self.worked()
        done = rules.record_agent_estimate(self.con, report(commits=p42), now=LATER)
        got = self.ok(await self.call("baldur_withdraw_estimate", {"id": f"r{done.id}"}))
        self.assertEqual((got["id"], got["status"]), (f"r{done.id}", "withdrawn"))
        self.assertEqual(self.con.execute("SELECT status FROM agent_estimates").fetchone()[0], "withdrawn")
        self.assertIn("already withdrawn", self.refused(await self.call("baldur_withdraw_estimate", {"id": "1"})))
        self.assertIn("No agent estimate 99", self.refused(await self.call("baldur_withdraw_estimate", {"id": "r99"})))
        self.assertIn("pattern", self.refused(await self.call("baldur_withdraw_estimate", {"id": "x12"})))

    async def test_the_list_is_baldurs_own(self):
        p42, p51 = self.worked()
        self.record(commits=p42, minutes=45)
        gone = self.record(commits=p51, minutes=75, summary="The form validation, token=abcdef1234567890abcdef.")
        rules.withdraw_agent_estimate(self.con, gone.id)
        got = self.ok(await self.call("baldur_estimates", {"date_from": DAY, "date_to": DAY,
                                                           "include_withdrawn": True}))
        code, out, err = baldur("ai", "list", "--from", DAY, "--to", DAY, "--all", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(got["estimates"], json.loads(out), "the CLI and Ysildir share one query")
        self.assertEqual(got["untrusted_fields"], ["estimates[].summary"])
        self.assertNotIn("abcdef1234567890abcdef", json.dumps(got), "credential-shaped text stays masked")
        mine = self.ok(await self.call("baldur_estimates", {"date_from": DAY, "date_to": DAY}))
        self.assertEqual([r["id"] for r in mine["estimates"]], ["r1"], "withdrawn reports only when asked")
        for args, message in (({"date_from": "2026-09-01", "date_to": "2026-10-09"}, "at most 31 days"),
                              ({"date_from": "2026-10-02", "date_to": DAY}, "after date_to"),
                              ({"date_to": "2026-02-30"}, "isn't a day on the calendar")):
            with self.subTest(message):
                self.assertIn(message, self.refused(await self.call("baldur_estimates", args)))

    async def test_a_muninn_that_isnt_ready_says_so_in_its_own_words(self):
        p42, _ = self.worked()
        self.con.execute("PRAGMA user_version = 6")
        self.assertIn("newer than ysildir understands", self.refused(await self.call("baldur_estimates")))
        self.assertIn("newer than baldur understands",
                      self.refused(await self.call("baldur_record_estimate", {"report": a_report(p42)})))
        self.con.execute("PRAGMA user_version = 3")
        self.assertIn("ysildir needs version 4 or newer", self.refused(await self.call("muninn_catalog")))
        self.con.execute("PRAGMA user_version = 4")
        with mock.patch.dict(os.environ, {"ASGARD_HOME": str(self.dir / "elsewhere")}):
            self.assertIn("Muninn isn't set up yet", self.refused(await self.call("muninn_catalog")))
        self.assertEqual(self.counts()["agent_estimates"], 0)


@needs_sdk
class DayTests(YsildirCase):
    async def test_the_smoke_day_with_two_agent_reports(self):
        """The guides' example C: r12 put PROJ-42 at 45m, r13 PROJ-51 at 75m to 1h30m."""
        p42, p51 = self.worked()
        self.run_store()
        self.record(commits=p42, minutes=45)
        self.record(commits=p51, minutes=90, minutes_low=75, summary="Reworked the form validation.")
        before = self.counts()
        got = self.ok(await self.call("baldur_day", {"date": DAY}))
        self.assertEqual(self.counts(), before, "baldur_day writes nothing (it runs on the read-only connection)")
        tickets = {x["key"]: x for x in got["tickets"]}
        self.assertEqual({k: (x["estimate"], x["ai_assisted"], x["open"]) for k, x in tickets.items()},
                         {"PROJ-42": (90, 45, True), "PROJ-51": (30, 75, True)})
        self.assertIn("kiro put it at 45m; commits gave 1h30m", tickets["PROJ-42"]["reason"])
        self.assertEqual(tickets["PROJ-42"]["evidence"][0], "r1")
        shown = got["ai"].pop("id")
        self.assertEqual(got["ai"], {"method": "agent", "source": "agent estimates (kiro, 2 reports)",
                                     "reports": ["r1", "r2"], "flags": []})
        self.assertEqual(shown, assist.suggestions(self.con, baldur_settings.load(), rules_day()).digest())
        self.assertEqual((got["take"], got["keep"]), (f"baldur.cmd approve --date 2026-10-01 --ai {shown}",
                                                      "baldur.cmd approve --date 2026-10-01"))
        self.assertEqual(tickets["PROJ-42"]["worklog_line"],
                         "Reviewed: agent estimates (kiro, 2 reports) lowered this from 1h30m to 45m")
        self.assertIsNone(got["report"])
        self.assertEqual(got["untrusted_fields"], ["tickets[].reason", "tickets[].worklog_line", "flags[]",
                                                   "ai.source", "ai.flags[]"])
        full = self.ok(await self.call("baldur_day", {"date": DAY, "include_report": True}))
        self.assertIn("PROJ-42", full["report"])
        self.assertIn("report", full["untrusted_fields"])

    async def test_a_day_without_ai_figures_offers_no_command(self):
        self.worked()
        got = self.ok(await self.call("baldur_day", {"date": DAY}))
        self.assertEqual({x["key"]: x["estimate"] for x in got["tickets"]}, {"PROJ-42": 90, "PROJ-51": 30})
        self.assertEqual((got["ai"], got["take"], got["keep"]), (None, None, None))

    async def test_baldurs_settings_are_read_never_made(self):
        self.worked()
        path = baldur_settings.settings_path()
        path.unlink()
        self.assertIn("open Baldur once", self.refused(await self.call("baldur_day", {"date": DAY})))
        self.assertFalse(path.exists(), "Ysildir never creates baldur.json")
        path.write_text("{broken", encoding="utf-8")
        self.assertIn("Baldur's settings have a problem", self.refused(await self.call("baldur_review_pack")))
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


@needs_sdk
class ReviewTests(YsildirCase):
    def setUp(self):
        super().setUp()
        self.p42, self.p51 = self.worked()

    def reply(self, pack, adjustments, flags=()):
        return {"day": DAY, "pack": pack["pack_hash"], "adjustments": adjustments, "flags": list(flags)}

    async def test_review_mode_is_obeyed(self):
        for mode, message in (("off", "AI review is off"), ("content", "rule 6")):
            with self.subTest(mode):
                baldur_settings.update({"review_mode": mode})
                self.assertIn(message, self.refused(await self.call("baldur_review_pack", {"date": DAY})))
                self.assertIn(message, self.refused(await self.call(
                    "baldur_submit_review", {"date": DAY, "reply": {"day": DAY, "pack": "0" * 16}})))
        self.assertEqual(self.rows(), [], "with review off, asking for a pack stores nothing")

    async def test_a_review_at_the_mcp_tier_then_the_persons_approval(self):
        baldur_settings.update({"review_mode": "metadata"})
        self.record(commits=self.p42, minutes=45)
        self.jira("PROJ-42", "PROJ-51")
        got = self.ok(await self.call("baldur_review_pack", {"date": DAY}))
        pack = got["pack"]
        self.assertEqual(got["reply_to"], "baldur_submit_review")
        self.assertTrue(got["prompt"].startswith("You adjust a development-time estimate"))
        self.assertNotIn("baldur-review-", got["prompt"], "the version line is for maintainers")
        self.assertEqual([r["work_item_key"] for r in self.rows("proposed")], ["PROJ-42", "PROJ-51"],
                         "the pack stored the day's estimate first")
        self.assertIn("pack.commits[].subject", got["untrusted_fields"])
        reply = self.reply(pack, [
            {"ticket": "PROJ-42", "minutes": -45, "confidence": "medium", "evidence": ["r1"],
             "reason": "The retry change was small."},
            {"ticket": "PROJ-51", "minutes": 45, "confidence": "medium", "evidence": [self.p51[0]],
             "reason": "The form validation was the real work."}])
        done = self.ok(await self.call("baldur_submit_review", {"date": DAY, "reply": reply, "model": "test-model"}))
        self.assertEqual((done["baseline"], done["figures"]), ({"PROJ-42": 90, "PROJ-51": 30},
                                                               {"PROJ-42": 45, "PROJ-51": 75}))
        self.assertRegex(done["take"], r"^baldur\.cmd approve --date 2026-10-01 --ai [0-9a-f]{8}$")
        self.assertIn("Reviewed: AI review (mcp, test-model) lowered this from 1h30m to 45m",
                      done["worklog_lines"]["PROJ-42"])
        self.assertEqual(self.rows("approved"), [], "a review approves nothing")
        code, out, err = baldur("approve", "--date", DAY, "--ai", done["take"].split()[-1])   # the person's decision
        self.assertEqual(code, 0, err)
        rows = {r["work_item_key"]: r for r in self.rows("approved")}
        self.assertEqual({k: r["minutes_final"] for k, r in rows.items()}, {"PROJ-42": 45, "PROJ-51": 75})
        posted = {p.key: p for p in self.post_all()}
        self.assertIn('Reviewed: AI review (mcp, test-model) lowered this from 1h30m to 45m ("The retry change was '
                      'small.")', posted["PROJ-42"].comment)

    async def test_a_stale_raising_or_misspelled_reply_changes_nothing(self):
        baldur_settings.update({"review_mode": "metadata"})
        pack = self.ok(await self.call("baldur_review_pack", {"date": DAY}))["pack"]
        raising = self.reply(pack, [{"ticket": "PROJ-42", "minutes": 15, "confidence": "low", "evidence": ["s1"]}])
        said = self.refused(await self.call("baldur_submit_review", {"date": DAY, "reply": raising}))
        self.assertIn("The reply wasn't used: The adjustments add 15 minutes", said)
        self.assertIn("never raise the day", said)
        self.assertIn("different evidence", self.refused(await self.call(
            "baldur_submit_review", {"date": DAY, "reply": dict(raising, pack="0" * 16, adjustments=[])})))
        for typo in ({"day": DAY, "pack": pack["pack_hash"], "adjustment": []},
                     self.reply(pack, [{"ticket": "PROJ-42", "minutes": -15, "confidence": "low",
                                        "evidense": ["s1"]}])):
            with self.subTest(typo):
                self.assertIn("Extra inputs are not permitted", self.refused(await self.call(
                    "baldur_submit_review", {"date": DAY, "reply": typo})))
        self.assertIn("is for '2026-10-02', not 2026-10-01", self.refused(await self.call(
            "baldur_submit_review", {"date": DAY, "reply": dict(self.reply(pack, []), day="2026-10-02")})))
        self.commit(t(1, 15, 20), "PROJ-51")                   # new work after the pack was made
        self.assertIn("has changed since it was estimated", self.refused(await self.call(
            "baldur_submit_review", {"date": DAY, "reply": self.reply(pack, [])})))
        self.assertEqual({rules.review_of(r).get("method") for r in self.rows("proposed")}, {None})


# --------------------------------------------------------------------------
# Muninn tools
# --------------------------------------------------------------------------

@needs_sdk
class MuninnToolTests(YsildirCase):
    async def test_the_catalog_says_what_is_there_without_rows(self):
        self.worked()
        got = self.ok(await self.call("muninn_catalog"))
        entries = {e["name"]: e for e in got["entries"]}
        self.assertEqual(got["schema_version"], muninn.SCHEMA_VERSION)
        self.assertFalse(set(entries) & guard.FTS_SHADOW, "the search index's insides aren't listed")
        self.assertEqual((entries["commits"]["owner"], entries["commits"]["rows"]), ("baldur", 8))
        self.assertEqual((entries["events"]["owner"], entries["work_items"]["owner"]), ("shared", "odin"))
        self.assertEqual((entries["v_day_status"]["type"], entries["v_day_status"]["rows"]), ("view", None))
        self.assertEqual({e["meaning"] for e in entries.values()} & {"(no description yet)"}, set())
        self.assertEqual(got["rules"], tables.RULES)
        self.assertNotIn("change 1", json.dumps(got), "no rows, so no commit subjects")

    async def test_what_changed_keeps_only_payload_fields_without_free_text(self):
        self.jira("PROJ-42")                                   # work_item.created, with its summary in the payload
        muninn.emit(self.con, "odin", "work_item.updated", "work_items", 1, "PROJ-42",
                    {"summary": ["Old words", "New words"], "status": ["To Do", "In Progress"]})
        muninn.emit(self.con, "odin", "worklog.failed", "worklogs", 3, "PROJ-42",
                    {"origin": "baldur", "proposal_id": 7, "calendar_event_id": None, "error": "Jira said no"})
        muninn.emit(self.con, "odin", "work_item.reopened", "work_items", 1, "PROJ-42",
                    {"status": "Reopened password=hunter2abc"})
        muninn.emit(self.con, "loki", "meeting.created", "meetings", 1, None, {"notes": "private notes"})
        got = self.ok(await self.call("muninn_what_changed", {"since": "2026-01-01"}))
        by_kind = {e["kind"]: e for e in got["events"]}
        self.assertEqual(by_kind["work_item.created"]["payload"], {"status": "In Progress"})
        self.assertEqual(by_kind["work_item.updated"]["payload"], {"changed": ["status", "summary"]})
        self.assertEqual(by_kind["worklog.failed"]["payload"], {"origin": "baldur", "proposal_id": 7})
        self.assertEqual(by_kind["meeting.created"]["payload"], {}, "a kind that isn't on the allow-list")
        said = json.dumps(got)
        for secret in ("Work on PROJ-42", "Old words", "Jira said no", "private notes", "hunter2abc"):
            self.assertNotIn(secret, said)
        self.assertIn("Reopened", by_kind["work_item.reopened"]["payload"]["status"])
        only = self.ok(await self.call("muninn_what_changed", {"since": "2026-01-01", "kinds": ["worklog"]}))
        self.assertEqual([e["kind"] for e in only["events"]], ["worklog.failed"])
        cut = self.ok(await self.call("muninn_what_changed", {"since": "2026-01-01", "limit": 2}))
        self.assertEqual((len(cut["events"]), cut["truncated"]), (2, True))
        self.assertIn("later time", cut["narrow"])
        self.assertIn("since must be a time", self.refused(await self.call("muninn_what_changed", {"since": "lately"})))
        later = self.ok(await self.call("muninn_what_changed", {"since": "2999-01-01T00:00:00Z"}))
        self.assertEqual(later["events"], [])

    async def test_approved_against_logged_time(self):
        self.worked()
        sid, ctx = self.jira("PROJ-42", "PROJ-51")
        self.by_hand(sid, ctx, 0, 30)
        self.run_store()
        rules.approve_day(self.con, DAY)
        got = self.ok(await self.call("muninn_day_status", {"date_from": DAY, "date_to": DAY}))
        self.assertEqual({r["key"]: (r["approved_minutes"], r["logged_minutes"], r["to_post"], r["unpostable"])
                          for r in got["rows"]}, {"PROJ-42": (90, 30, 60, None), "PROJ-51": (30, 0, 30, None)})
        self.assertIn("at most 31 days", self.refused(await self.call(
            "muninn_day_status", {"date_from": "2026-01-01", "date_to": DAY})))

    async def test_an_issue_by_any_of_its_keys(self):
        sid, ctx = self.jira("PROJ-42")
        got = self.ok(await self.call("muninn_issue", {"key": "proj-42"}))
        self.assertEqual((got["found"], got["key"], got["summary"], got["status"], got["type"]),
                         (True, "PROJ-42", "Work on PROJ-42", "In Progress", "Issue"))
        self.assertEqual(got["untrusted_fields"], ["summary", "status", "type", "resolution"])
        from asgard.muninn import odin
        with muninn.Run(self.con, "odin", sid, "issues") as run:      # the issue moved to another project
            odin.upsert_issue(run, {"id": "1000", "key": "OPS-7", "fields": {
                "summary": "Work on PROJ-42", "status": {"name": "Done", "statusCategory": {"key": "done"}},
                "project": {"key": "OPS"}, "created": "2026-09-01T08:00:00.000-0400",
                "updated": "2026-10-02T08:00:00.000-0400", "resolutiondate": "2026-10-02T08:00:00.000-0400",
                "resolution": {"name": "Done"}}}, ctx)
        moved = self.ok(await self.call("muninn_issue", {"key": "PROJ-42"}))
        self.assertEqual((moved["asked"], moved["key"], moved["status"]), ("PROJ-42", "OPS-7", "Done"))
        missing = self.ok(await self.call("muninn_issue", {"key": "PROJ-999"}))
        self.assertFalse(missing["found"])
        self.assertIn("Odin collects Jira issues", missing["message"])
        self.assertIn("isn't a Jira key", self.refused(await self.call("muninn_issue", {"key": "not a key"})))

    async def test_search_reads_every_query_as_plain_words(self):
        self.worked()
        self.commit(t(2, 10), "PROJ-42", subject="Make the poller retry with jitter")
        self.commit(t(2, 11), "PROJ-42", subject="Poller notes from a colleague", mine=0)
        self.jira("PROJ-42")
        got = self.ok(await self.call("muninn_search", {"query": "poller", "kinds": ["commits"]}))
        self.assertEqual([(h["kind"], h["title"]) for h in got["hits"]], [("commits", "Make the poller retry with "
                                                                                       "jitter")])
        self.assertEqual(len(got["hits"][0]["ref"]), 40)
        self.assertEqual(got["query"], '"poller"')
        self.assertEqual(got["untrusted_fields"], ["hits[].title"])
        issues = self.ok(await self.call("muninn_search", {"query": "Work PROJ", "kinds": ["issues"]}))
        self.assertEqual([h["ref"] for h in issues["hits"]], ["PROJ-42"])
        for query in ('"', '*', "NEAR(poller", "poller OR", "-poller", 'poller"', "AND", "^", "title:poller", ")("):
            with self.subTest(query):
                result = await self.call("muninn_search", {"query": query})
                if result.is_error:
                    self.assertIn("Give at least one word", text(result))
                else:
                    self.assertTrue(all("syntax" not in h["title"] for h in result.structured_content["hits"]))
        self.assertEqual(self.ok(await self.call("muninn_search", {"query": "-poller"}))["hits"][0]["kind"], "commits")

    async def test_text_from_git_comes_back_as_one_clean_line(self):
        self.commit(t(2, 9), "PROJ-42", subject="Fixed the poller\x1b[2J\x07 token=abcdef1234567890abcdef "
                                                + "x" * 300)
        hit = self.ok(await self.call("muninn_search", {"query": "poller"}))["hits"][0]
        self.assertEqual(hit["title"][:16], "Fixed the poller")
        self.assertFalse(any(not ch.isprintable() for ch in hit["title"]), hit["title"])
        self.assertNotIn("abcdef1234567890abcdef", hit["title"])
        self.assertLessEqual(len(hit["title"]), 200)

    async def test_every_read_is_on_the_read_only_connection(self):
        self.worked()
        opened = []
        real = muninn.open_app

        def spy(app, **kwargs):
            opened.append((app, kwargs.get("readonly", False)))
            return real(app, **kwargs)

        with mock.patch.object(muninn, "open_app", spy):
            for name, args in (("muninn_catalog", {}), ("muninn_what_changed", {"since": "2026-01-01"}),
                               ("muninn_day_status", {}), ("muninn_issue", {"key": "PROJ-1"}),
                               ("muninn_search", {"query": "change"}), ("baldur_estimates", {}),
                               ("baldur_day", {"date": DAY})):
                with self.subTest(name):
                    self.ok(await self.call(name, args))
        self.assertEqual(set(opened), {("ysildir", True)})
        with ysildir.reader() as con:
            for sql in ("UPDATE day_proposals SET status = 'approved'", "DELETE FROM agent_estimates",
                        "INSERT INTO events (app, kind, entity_type) VALUES ('ysildir', 'a.b', 'x')"):
                with self.subTest(sql), self.assertRaises(sqlite3.DatabaseError):
                    con.execute(sql)


# --------------------------------------------------------------------------
# Caps, errors and the log
# --------------------------------------------------------------------------

@needs_sdk
class ResultTests(YsildirCase):
    async def test_a_long_answer_is_cut_and_says_how_to_narrow_it(self):
        for n in range(250):
            muninn.emit(self.con, "baldur", "estimate_run.created", "estimate_runs", n, None,
                        {"date_from": DAY, "date_to": DAY, "proposals": 2, "withdrawn": 0})
        got = self.ok(await self.call("muninn_what_changed", {"since": "2026-01-01", "limit": 200}))
        self.assertEqual((len(got["events"]), got["truncated"]), (200, True))
        self.assertEqual(got["narrow"], models.Events.NARROW)

    def test_fit_keeps_answers_under_the_caps(self):
        row = dict(id="r1", date=DAY, agent="kiro", key="PROJ-42", minutes=60, minutes_low=None, confidence="low",
                   commits=1, summary="x" * 300, status="recorded")
        big = models.Estimates(date_from=DAY, date_to=DAY, estimates=[models.EstimateRow(**row)] * 190)
        cut = results.fit(big)
        self.assertLessEqual(len(cut.model_dump_json().encode("utf-8")), results.MAX_BYTES)
        self.assertTrue(cut.truncated)
        self.assertGreater(len(cut.estimates), 100)
        many = models.Estimates(date_from=DAY, date_to=DAY,
                                estimates=[models.EstimateRow(**dict(row, summary="x"))] * 250)
        self.assertEqual(len(results.fit(many).estimates), results.MAX_ITEMS)
        small = models.Estimates(date_from=DAY, date_to=DAY, estimates=[models.EstimateRow(**row)])
        self.assertIs(results.fit(small), small)

    async def test_a_bug_is_logged_without_its_arguments_and_reported_plainly(self):
        with mock.patch.object(muninn_tools, "match_expression", side_effect=KeyError("secret-argument")):
            said = self.refused(await self.call("muninn_search", {"query": "secret-argument"}))
        self.assertIn("Ysildir hit a problem it didn't expect (KeyError)", said)
        self.assertNotIn("secret-argument", said)
        log = self.log()
        self.assertIn("muninn_search error", log)
        self.assertIn("KeyError", log)
        self.assertNotIn("secret-argument", log, "the log never holds an argument")


# --------------------------------------------------------------------------
# Over stdio, as an AI client runs it
# --------------------------------------------------------------------------

def stdio(home, *args):
    return StdioServerParameters(command=sys.executable, args=[str(YSILDIR / "cli.py"), "serve", *args],
                                 env={"ASGARD_HOME": str(home)})


@needs_sdk
class StdioTests(YsildirCase):
    async def test_a_real_client_over_stdio(self):
        with self.assertNoLogs("mcp.client.stdio", "ERROR"):        # a stray line on stdout would be logged here
            async with Client(stdio(self.dir), read_timeout_seconds=60) as client:
                self.assertEqual(client.server_info.name, "ysildir")
                names = [x.name for x in (await client.list_tools()).tools]
                guide = text(await client.call_tool("asgard_guide", {"topic": "muninn"}))
                catalog = (await client.call_tool("muninn_catalog", {})).structured_content
        self.assertEqual(names, DEFAULT_TOOLS)
        self.assertTrue(guide.startswith("<!-- muninn-agent-1 -->"))
        self.assertEqual(catalog["schema_version"], muninn.SCHEMA_VERSION)
        self.assertIn("serve started 5 tools on", self.log())

    def test_stdout_is_protocol_only_and_closing_stdin_ends_it(self):
        env = dict(os.environ, ASGARD_HOME=str(self.dir))
        proc = subprocess.Popen([sys.executable, str(YSILDIR / "cli.py"), "serve"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True, encoding="utf-8")
        lines: "queue.Queue[str]" = queue.Queue()
        threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout], daemon=True).start()

        def send(message):
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()

        def answer():
            return json.loads(lines.get(timeout=60))

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
            hello = answer()
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "asgard_guide", "arguments": {"topic": "review"}}})
            guide = answer()
            send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "approve", "arguments": {}}})
            forged = answer()
            proc.stdin.close()
            code = proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            err = proc.stderr.read()
            proc.stderr.close()
        self.assertEqual(hello["result"]["serverInfo"]["name"], "ysildir")
        self.assertEqual(hello["result"]["protocolVersion"], "2025-06-18")
        self.assertTrue(guide["result"]["content"][0]["text"].startswith("<!-- baldur-review-"))
        self.assertTrue(forged["result"]["isError"])
        self.assertEqual(code, 0, err)
        self.assertTrue(lines.empty(), "nothing but answers on stdout")
        self.assertNotIn("Traceback", err)


@needs_sdk
class WalkthroughTests(YsildirCase):
    """The guides' worked examples A to F, replayed by a real client over stdio, then the person's approval."""

    async def test_the_worked_examples(self):
        p42, p51 = self.worked()
        sid, ctx = self.jira("PROJ-42", "PROJ-51")
        config.save({n: True for n in config.NAMES})           # the person switched every tool on
        baldur_settings.update({"review_mode": "metadata"})    # and AI review, once their ISSO agreed
        since = muninn.utcnow()
        async with Client(stdio(self.dir), read_timeout_seconds=60) as client:
            # A. Recording an estimate after a commit
            self.assertIn("baldur-agent-2", text(await client.call_tool("asgard_guide", {"topic": "baldur"})))
            a = (await client.call_tool("baldur_record_estimate", {"report": a_report(p42, key="PROJ-42")}))
            r12 = a.structured_content["id"]
            self.assertIn("nothing changes until you approve", a.structured_content["message"])
            # B. A refused report, and the form fixed (never the numbers)
            b = await client.call_tool("baldur_record_estimate", {"report": a_report(
                p51, key="PROJ-51", minutes=90, minutes_low=75, summary="Changed ```retry(n=3)``` to a jittered retry.")})
            self.assertTrue(b.is_error)
            self.assertEqual(text(b), "Error executing tool baldur_record_estimate: summary must be one plain sentence, "
                                      "with no code or diff: Muninn keeps metadata only.")
            b = await client.call_tool("baldur_record_estimate", {"report": a_report(
                p51, key="PROJ-51", minutes=90, minutes_low=75, summary="Reworked the form validation.")})
            r13 = b.structured_content["id"]
            # C. Explaining a day
            c = (await client.call_tool("baldur_day", {"date": DAY})).structured_content
            self.assertEqual({x["key"]: (x["estimate"], x["ai_assisted"]) for x in c["tickets"]},
                             {"PROJ-42": (90, 45), "PROJ-51": (30, 75)})
            self.assertEqual((c["ai"]["source"], c["ai"]["reports"]), ("agent estimates (kiro, 2 reports)", [r12, r13]))
            self.assertEqual(c["take"], f"baldur.cmd approve --date 2026-10-01 --ai {c['ai']['id']}")
            # D. Reviewing the day at the MCP tier
            d = (await client.call_tool("baldur_review_pack", {"date": DAY})).structured_content
            reply = {"day": DAY, "pack": d["pack"]["pack_hash"], "flags": [], "adjustments": [
                {"ticket": "PROJ-42", "minutes": -45, "confidence": "medium", "evidence": [r12],
                 "reason": "The retry change was small."},
                {"ticket": "PROJ-51", "minutes": 45, "confidence": "medium", "evidence": [r13],
                 "reason": "The form validation was the real work."}]}
            done = (await client.call_tool("baldur_submit_review", {"date": DAY, "reply": reply,
                                                                    "model": "test-model"})).structured_content
            self.assertEqual(done["figures"], {"PROJ-42": 45, "PROJ-51": 75})
            take = done["take"]
            # E. Being asked to approve: there is no tool for it
            self.assertIn("Unknown tool: approve", text(await client.call_tool("approve", {"date": DAY})))
        code, out, err = baldur(*take.split()[1:])                             # the person runs the command
        self.assertEqual(code, 0, err)
        posted = {p.key: p for p in self.post_all()}
        self.assertIn('Reviewed: AI review (mcp, test-model) moved 45m to this from the day\'s other tickets ("The '
                      'form validation was the real work.")', posted["PROJ-51"].comment)
        # F. What changed
        async with Client(stdio(self.dir), read_timeout_seconds=60) as client:
            f = (await client.call_tool("muninn_what_changed", {"since": since})).structured_content
        approved = sorted((e["ref"], e["payload"]) for e in f["events"] if e["kind"] == "day_proposal.approved")
        self.assertEqual(approved, [("PROJ-42", {"local_date": DAY, "minutes": 45}),
                                    ("PROJ-51", {"local_date": DAY, "minutes": 75})])
        recorded = [e for e in f["events"] if e["kind"] == "agent_estimate.recorded"]
        self.assertEqual([e["payload"]["minutes"] for e in recorded], [60, 90])


# --------------------------------------------------------------------------
# Connecting clients, and check
# --------------------------------------------------------------------------

FAKE = """import json, os, sys
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
"""


@needs_sdk
class SetupTests(unittest.TestCase):
    read_only = [x.name for x in server.TOOLS if x.read_only] if SKIP is None else []

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)   # after the test's own cleanups (open files)
        self.dir = Path(self.tmp.name).resolve()
        self.ws = self.dir / "ws"
        self.ws.mkdir()
        self.config = config.Config(config.defaults(), self.dir / "ysildir.json")


    def setup(self, **kwargs):
        return clients.setup(self.config, self.read_only, **kwargs)

    def test_kiro_keeps_every_other_server_and_auto_approves_reads_only(self):
        path = self.ws / ".kiro" / "settings" / "mcp.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}), encoding="utf-8")
        self.assertEqual(self.setup(kiro=str(self.ws)), [f"Kiro, this workspace: written {path}"])
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((data["keep"], data["mcpServers"]["other"]), (1, {"command": "x"}))
        entry = data["mcpServers"]["ysildir"]
        self.assertEqual((entry["command"], entry["args"]), (sys.executable, [str(clients.CLI), "serve"]))
        self.assertEqual(entry["autoApprove"], ["asgard_guide", "muninn_catalog", "baldur_estimates"],
                         "only the read-only tools that are on; recording stays the person's click")
        self.assertEqual(self.setup(kiro=str(self.ws)), [f"Kiro, this workspace: kept {path}"])
        self.assertEqual(self.setup(kiro=str(self.ws), force=True), [f"Kiro, this workspace: replaced {path}"])
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["mcp.json"])

    def test_a_file_it_cant_parse_is_left_alone_with_the_snippet_to_paste(self):
        path = self.ws / ".vscode" / "mcp.json"
        path.parent.mkdir(parents=True)
        path.write_text('// my servers\n{"servers": {}}\n', encoding="utf-8")
        with self.assertRaisesRegex(ysildir.Refused, "may have comments") as caught:
            self.setup(vscode=str(self.ws))
        self.assertIn('"servers"', str(caught.exception))
        self.assertIn(json.dumps(str(clients.CLI)), str(caught.exception))
        self.assertEqual(path.read_text(encoding="utf-8"), '// my servers\n{"servers": {}}\n')

    def test_print_shows_everything_and_changes_nothing(self):
        out = "\n".join(self.setup(kiro=str(self.ws), vscode=str(self.ws), vscode_user=True, claude=str(self.ws),
                                   print_only=True))
        for want in ("mcpServers", '"servers"', "code --add-mcp", "claude mcp add --scope project ysildir --"):
            self.assertIn(want, out)
        self.assertEqual(list(self.ws.iterdir()), [])
        with self.assertRaisesRegex(ysildir.Refused, "Say which client"):
            self.setup()

    def fakes(self):
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        (bin_dir / "fake.py").write_text(FAKE, encoding="utf-8")
        for name in ("code", "claude"):
            if os.name == "nt":
                (bin_dir / f"{name}.cmd").write_text(f'@"{sys.executable}" "{bin_dir / "fake.py"}" %*\r\n')
            else:
                path = bin_dir / name
                path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fake.py"}" "$@"\n')
                path.chmod(0o755)
        log = self.dir / "fake.log"
        return {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "FAKE_LOG": str(log)}, log

    def test_the_clients_own_commands_are_used_when_they_are_there(self):
        env, log = self.fakes()
        with mock.patch.dict(os.environ, env):
            self.assertEqual(self.setup(vscode_user=True), ["VS Code, your profile: added with code --add-mcp"])
            self.setup(claude=str(self.ws))
            self.setup(claude=str(self.ws), force=True)
        calls = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(calls[0]["argv"][0], "--add-mcp")
        self.assertEqual(json.loads(calls[0]["argv"][1]),
                         {"name": "ysildir", "command": sys.executable, "args": [str(clients.CLI), "serve"]})
        self.assertEqual(calls[1]["argv"], ["mcp", "add", "--scope", "project", "ysildir", "--", sys.executable,
                                            str(clients.CLI), "serve"])
        self.assertEqual(Path(calls[1]["cwd"]).resolve(), self.ws)
        self.assertEqual([c["argv"][:2] for c in calls[2:]], [["mcp", "remove"], ["mcp", "add"]], "--force replaces")

    def test_without_claudes_command_the_project_file_is_written(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.dir / "nothing-here")}):
            self.setup(claude=str(self.ws))
            with self.assertRaisesRegex(ysildir.Refused, "isn't on PATH"):
                self.setup(vscode_user=True)
        data = json.loads((self.ws / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["ysildir"]["type"], "stdio")


@needs_sdk
class CheckTests(MuninnCase):
    def tearDown(self):
        results.close_log()
        super().tearDown()

    def test_check_lists_what_an_agent_sees(self):
        lines = server.check()
        self.assertIn("protocol", lines[0])
        self.assertIn(f"Muninn: version {muninn.SCHEMA_VERSION} (Ysildir understands 4 to 5)", lines)
        self.assertIn("Tools an agent sees: 5 of 12", lines)
        self.assertTrue(any(line.startswith("  off  baldur_day") for line in lines))
        self.assertIn("Prompts: record-estimate", lines)
        everything = server.check(config.everything_on())
        self.assertIn("Tools an agent sees: 12 of 12", everything)


# --------------------------------------------------------------------------
# What always runs: the missing-SDK message, the static checks, the guard
# --------------------------------------------------------------------------

class MissingSdkTests(unittest.TestCase):
    def run_cli(self, *args):
        cli = load_cli()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_without_the_sdk_it_says_what_is_missing_and_what_still_works(self):
        with mock.patch.dict(sys.modules, {"mcp": None}):
            code, out, err = self.run_cli("serve")
        self.assertEqual(code, 2)
        self.assertEqual(out, "", "nothing on stdout: a client would read it as protocol")
        self.assertRegex(err, r"needs Python 3\.10|isn't installed")
        self.assertIn("baldur.cmd ai record", err)
        self.assertIn("baldur.cmd ai pack", err)

    def test_no_abbreviated_options(self):
        cli = load_cli()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["tools", "--of", "baldur_day"])

    def test_an_old_python_is_named(self):
        cli = load_cli()
        with mock.patch.object(sys, "version_info", (3, 9, 18, "final", 0)):
            self.assertIn("Python 3.10 or newer", cli.missing())

    def test_ysildir_cmd_finds_python_310_and_exits_2_without_it(self):
        raw = (YSILDIR / "ysildir.cmd").read_bytes()
        self.assertTrue(raw.isascii())
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), "CRLF line endings")
        cmd = raw.decode("ascii")
        self.assertIn("sys.version_info >= (3, 10)", cmd)
        self.assertIn("exit /b 2", cmd)
        self.assertIn("baldur.cmd ai record", cmd)


def ysildir_modules():
    return sorted(PACKAGE.glob("*.py"))


def calls(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            yield f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""


def imported(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module if node.level == 0 else "." * node.level + node.module
            if node.level == 0:
                yield from (f"{node.module}.{a.name}" for a in node.names)


class StaticTests(unittest.TestCase):
    """Checks on Ysildir's source, which need no SDK: the AST, not the text (take strings mention approve)."""

    DECISIONS = {"approve", "approve_day", "reject", "reject_day", "change_approval", "change_figure", "accept",
                 "forget", "begin_post", "finish_post", "fail_post", "save"}

    def test_the_schema_range_is_baldurs(self):
        self.assertEqual(ysildir.SCHEMA, baldur_cli.SCHEMA)

    def test_nothing_writes_to_stdout_but_the_cli(self):
        for path in ysildir_modules():
            with self.subTest(path.name):
                self.assertNotIn("print", set(calls(path)))
                self.assertNotIn("sys.stdout", path.read_text(encoding="utf-8"))

    def test_no_decision_is_ever_called(self):
        for path in ysildir_modules():
            with self.subTest(path.name):
                found = set(calls(path)) & self.DECISIONS
                if path.name == "config.py":
                    found -= {"save"}                  # its own switches file, which only `tools` writes
                self.assertEqual(found, set())
                self.assertFalse({m for m in imported(path) if "odin" in m or "calibrate" in m or "desk.approve"
                                  in m or m.endswith("store")})

    def test_the_sdk_and_baldur_are_imported_where_the_docs_say(self):
        where = {"mcp": {"server.py", "results.py"}, "pydantic": {"models.py", "config.py"},
                 "baldur": {"baldur_tools.py"}}
        for package, files in where.items():
            with self.subTest(package):
                found = {p.name for p in ysildir_modules()
                         if any(m == package or m.startswith(package + ".") for m in imported(p))}
                self.assertEqual(found, files)

    def test_every_table_and_view_says_what_it_holds(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db = Path(tmp.name) / "muninn.db"
            muninn.prepare(db, backups=Path(tmp.name) / "b")
            con = muninn.connect(db)
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_schema WHERE type IN ('table', 'view') "
                                               "AND name NOT LIKE 'sqlite%'")} - guard.FTS_SHADOW
            con.close()
        finally:
            tmp.cleanup()
        self.assertEqual(sorted(names - set(tables.MEANINGS)), [], "say what each new table holds (tables.py)")
        self.assertEqual(sorted(set(tables.MEANINGS) - names), [], "a meaning for a table that isn't there")

    def test_baldurs_settings_can_be_read_without_being_made(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = Path(tmp.name) / "baldur.json"
            found = baldur_settings.load(path, create=False)
            self.assertFalse(found.broken)
            self.assertFalse(path.exists())
            baldur_settings.load(path)
            self.assertTrue(path.exists(), "Baldur itself still creates it the first time")
        finally:
            tmp.cleanup()


class GuardTests(unittest.TestCase):
    """Kiro's guard hook keeps an agent's hands off Ysildir's switches as well as Baldur's decisions."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "guard_baldur", ROOT / "apps" / "baldur" / "agents" / "hooks" / "guard_baldur.py")
        self.guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.guard)

    def test_switches_and_setup_are_the_persons(self):
        y = r'py -3 "C:\Users\Dev User\AppData\Local\Asgard\app\apps\ysildir\cli.py"'
        for command in ("ysildir.cmd tools --on baldur_day", "ysildir tools --off muninn_search",
                        "ysildir.cmd tools --on=baldur_review_pack", f"{y} tools --on muninn_issue",
                        f"{y} setup --kiro .", "ysildir.cmd setup --vscode-user --force",
                        "git commit -m x && ysildir.cmd setup --claude .",
                        r"notepad %LOCALAPPDATA%\Asgard\settings\ysildir.json",
                        "python -c \"open('ysildir.json','w').write('{}')\""):
            with self.subTest(command):
                self.assertIn("Ysildir", self.guard.verdict(command) or "")

    def test_reading_ysildir_is_allowed(self):
        for command in ("ysildir.cmd tools", "ysildir.cmd tools --list", "ysildir.cmd check", "ysildir.cmd help",
                        "baldur.cmd ai record --agent kiro --minutes 1h --commit abc1234 --confidence low --summary x"):
            with self.subTest(command):
                self.assertIsNone(self.guard.verdict(command))

    def test_the_hook_blocks_with_exit_code_2(self):
        done = run_hook("guard_baldur", "ysildir.cmd tools --on baldur_day")
        self.assertEqual(done.returncode, 2)
        self.assertIn("approved by their ISSO", done.stderr)


if __name__ == "__main__":
    unittest.main()
