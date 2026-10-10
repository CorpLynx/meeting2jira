"""Baldur's AI-assisted method: agent estimates in, checked AI-assisted figures out.

The named rules (Baldur spec, "AI-assisted estimates" and "Optional AI review"):
- An AI figure never raises a day; a ticket gains only time moved from another ticket.
- Every adjustment cites evidence that was sent; a review can't add a ticket or go below zero.
- An agent's report without commits is shown and never counted; untracked time is never suggested.
- Reports are facts: never edited, a newer one on the same change withdraws the older.
- Metadata only: no code in a report, no code in a pack, and no pack at all while review_mode is off.
- The person decides: taking AI figures is an approval, recorded with the figure taken, and Odin's
  worklog comment says so.
"""
import contextlib
import datetime as dt
import io
import json
import random
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT, ROOT / "apps" / "baldur", ROOT / "tests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from asgard import muninn  # noqa: E402
from asgard.muninn import baldur as rules  # noqa: E402
from baldur import assist, cli  # noqa: E402
from baldur import report as day_report  # noqa: E402
from baldur import estimate as E  # noqa: E402
from baldur import settings as config  # noqa: E402
from test_baldur import DAY, NOW, DeltaBase, MuninnCase, c, t  # noqa: E402

LATER = dt.datetime(2026, 10, 3, 18, 0, tzinfo=dt.timezone.utc)


def report(**fields):
    base = {"schema": rules.REPORT_SCHEMA, "agent": "kiro", "date": "2026-10-01", "minutes": 45,
            "confidence": "medium", "summary": "Retry with backoff in the poller."}
    base.update(fields)
    return {k: v for k, v in base.items() if v is not None}


class AssistCase(MuninnCase):
    def setUp(self):
        super().setUp()
        self.settings = config.update({"project_keys": ["PROJ", "OPS"], "history_days": 3650})

    def sha(self, cid):
        return self.con.execute("SELECT sha FROM commits WHERE id = ?", (cid,)).fetchone()[0]

    def worked(self):
        """The spec's worked day; returns (PROJ-42 SHAs, PROJ-51 SHAs)."""
        self.worked_example()
        rows = self.con.execute("SELECT c.sha, k.work_item_key FROM commits c JOIN commit_work_items k "
                                "ON k.commit_id = c.id ORDER BY c.id").fetchall()
        return [r[0] for r in rows if r[1] == "PROJ-42"], [r[0] for r in rows if r[1] == "PROJ-51"]

    def record(self, **fields):
        return rules.record_agent_estimate(self.con, report(**fields), now=LATER)

    def suggest(self):
        return assist.suggestions(self.con, self.settings, DAY, now=NOW)


# --------------------------------------------------------------------------
# Intake
# --------------------------------------------------------------------------

class IntakeTests(AssistCase):
    def test_a_report_is_stored_as_a_fact_with_its_commits(self):
        p42, _ = self.worked()
        done = self.record(commits=[s.upper() for s in p42[:2]], key="proj-42",
                           summary="  Retry   with backoff,\n in the poller.  ", started_at="2026-10-01T13:20:00Z",
                           ended_at="2026-10-01T09:55:00-04:00")
        self.assertEqual((done.status, done.replaced), ("recorded", []))
        row = self.con.execute("SELECT * FROM agent_estimates WHERE id = ?", (done.id,)).fetchone()
        self.assertEqual((row["work_item_key"], row["summary"], row["local_date"], row["via"]),
                         ("PROJ-42", "Retry with backoff, in the poller.", "2026-10-01", "cli"))
        self.assertEqual((row["started_at"], row["ended_at"]), ("2026-10-01T13:20:00Z", "2026-10-01T13:55:00Z"))
        cited = [r[0] for r in self.con.execute("SELECT sha FROM agent_estimate_commits WHERE estimate_id = ? "
                                                "ORDER BY sha", (done.id,))]
        self.assertEqual(cited, sorted(p42[:2]))
        self.assertEqual(self.events("agent_estimate.recorded"),
                         [{"local_date": "2026-10-01", "minutes": 45, "agent": "kiro", "commits": 2, "replaced": []}])

    def test_the_same_report_twice_is_stored_once(self):
        p42, _ = self.worked()
        first = self.record(commits=p42)
        again = self.record(commits=list(reversed(p42)))
        self.assertEqual((again.status, again.id), ("duplicate", first.id))
        self.assertEqual(len(self.events("agent_estimate.recorded")), 1)

    def test_a_newer_report_on_the_same_change_withdraws_the_older(self):
        p42, _ = self.worked()
        first = self.record(commits=p42, minutes=45)
        other = self.record(commits=p42, minutes=50, agent="copilot")       # another agent: both stand
        second = self.record(commits=p42, minutes=30, summary="Small retry fix.")
        self.assertEqual(second.replaced, [first.id])
        statuses = dict(self.con.execute("SELECT id, status FROM agent_estimates").fetchall())
        self.assertEqual(statuses, {first.id: "withdrawn", other.id: "recorded", second.id: "recorded"})

    def test_what_isnt_a_report_is_refused(self):
        p42, _ = self.worked()
        cases = [
            (report(minutes=60, extra=1), "Unknown fields extra"),
            (report(schema="baldur.agent_estimate/9"), "schema must be"),
            (report(agent="kiro; rm -rf /"), "agent must name the tool"),
            (report(minutes=0), "from 1 to 1440"), (report(minutes=1441), "from 1 to 1440"),
            (report(minutes=True), "whole number"), (report(minutes="60"), "whole number"),
            (report(minutes=30, minutes_low=45), "can't be more than minutes"),
            (report(confidence="sure"), "high, medium or low"),
            (report(summary=" "), "summary is required"),
            (report(summary="Fixed it:\n```python\nprint(1)\n```"), "no code or diff"),
            (report(summary="diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b"), "no code or diff"),
            (report(summary="x" * 301), "under 300 characters"),
            (report(commits=["not-a-sha"]), "isn't a commit SHA"), (report(commits="abc1234"), "list of commit SHAs"),
            (report(commits=[f"{n:040x}" for n in range(51)]), "at most 50 commits"),
            (report(key="proj 42"), "isn't a Jira key"),
            (report(date="2026-10-05"), "date is in the future"),   # tomorrow even at UTC+14 (it's 3 Oct 18:00 UTC)
            (report(ended_at="2026-10-04T12:00:00Z"), "ended_at is in the future"),
            (report(started_at="2026-10-01T09:00:00"), "needs its time zone"),
            (report(started_at="2026-10-01T11:00:00Z", ended_at="2026-10-01T10:00:00Z"), "before started_at"),
            (report(started_at="2026-09-29T09:00:00Z", ended_at="2026-10-01T10:00:00Z"), "at most one day"),
            ([report()], "is a JSON object"),
        ]
        for bad, message in cases:
            with self.subTest(message):
                with self.assertRaisesRegex(muninn.MuninnError, message):
                    rules.record_agent_estimate(self.con, bad, now=LATER)
        self.assertEqual(self.con.execute("SELECT count(*) FROM agent_estimates").fetchone()[0], 0)

    def test_a_summary_keeps_no_secret(self):
        done = self.record(summary="Rotated the deploy password=hunter2abc in the job config.")
        summary = self.con.execute("SELECT summary FROM agent_estimates WHERE id = ?", (done.id,)).fetchone()[0]
        self.assertNotIn("hunter2abc", summary)
        self.assertIn("***", summary)

    def test_the_day_defaults_to_when_the_work_ended(self):
        done = rules.record_agent_estimate(self.con, report(date=None, ended_at="2026-10-01T03:30:00Z"), now=LATER)
        # 03:30 UTC is still the evening of 2026-09-30 in New York (and earlier wherever tests run west of UTC).
        day = self.con.execute("SELECT local_date FROM agent_estimates WHERE id = ?", (done.id,)).fetchone()[0]
        self.assertEqual(day, muninn.from_ts("2026-10-01T03:30:00Z").astimezone().date().isoformat())

    def test_withdrawing(self):
        done = self.record()
        rules.withdraw_agent_estimate(self.con, done.id)
        with self.assertRaisesRegex(muninn.MuninnError, "already withdrawn"):
            rules.withdraw_agent_estimate(self.con, done.id)
        with self.assertRaisesRegex(muninn.MuninnError, "No agent estimate 999"):
            rules.withdraw_agent_estimate(self.con, 999)
        self.assertEqual(len(self.events("agent_estimate.withdrawn")), 1)

    def test_only_baldur_writes_agent_estimates(self):
        loki = muninn.open_app("loki", supported=(4, 5), path=self.dir / "muninn.db")
        self.addCleanup(loki.close)
        with self.assertRaisesRegex(Exception, "not authorized"):
            rules.record_agent_estimate(loki, report(), now=LATER)
        mine = muninn.open_app("baldur", supported=(4, 5), path=self.dir / "muninn.db")
        self.addCleanup(mine.close)
        self.assertEqual(rules.record_agent_estimate(mine, report(), now=LATER).status, "recorded")


# --------------------------------------------------------------------------
# The check every AI figure passes
# --------------------------------------------------------------------------

class CheckTests(unittest.TestCase):
    BASE = {"PROJ-42": 90, "PROJ-51": 30}
    SHA = "9f3c1a2b4d5e6f708192a3b4c5d6e7f8091a2b3c"
    EVIDENCE = {SHA, "s1", "s2", "r7"}

    def check(self, adjustments, flags=(), day="2026-10-01", raw=None):
        reply = raw if raw is not None else json.dumps({"day": day, "adjustments": adjustments, "flags": list(flags)})
        return rules.check_review("2026-10-01", self.BASE, self.EVIDENCE, reply, step=15)

    def adj(self, ticket, minutes, evidence=("s1",), confidence="medium", reason="why"):
        return {"ticket": ticket, "minutes": minutes, "evidence": list(evidence), "confidence": confidence,
                "reason": reason}

    def test_moving_and_lowering_pass_and_round_down_again(self):
        done = self.check([self.adj("PROJ-42", -20), self.adj("proj-51", 10, evidence=[self.SHA[:9]])])
        self.assertEqual(done.figures, {"PROJ-42": 60, "PROJ-51": 30})      # 70 and 40 round down to 60 and 30
        self.assertEqual(done.adjustments[1].ticket, "PROJ-51")

    def test_each_rule_refuses_the_whole_reply(self):
        cases = [
            ([self.adj("PROJ-42", 15)], "never raise the day"),
            ([self.adj("PROJ-42", -15), self.adj("PROJ-51", 30)], "never raise the day"),
            ([self.adj("PROJ-99", -15)], "can't add a ticket"),
            ([self.adj("PROJ-51", -45)], "below zero"),
            ([self.adj("PROJ-42", -15, evidence=[])], "cites no evidence"),
            ([self.adj("PROJ-42", -15, evidence=["s9"])], "wasn't in the evidence"),
            ([self.adj("PROJ-42", -15, evidence=["deadbeefcafe"])], "wasn't in the evidence"),
            ([self.adj("PROJ-42", -1) for _ in range(11)], "at most 10"),
            ([self.adj("PROJ-42", -15, confidence="certain")], "confidence"),
            ([{"ticket": "PROJ-42", "minutes": "-15", "evidence": ["s1"]}], "whole number"),
        ]
        for adjustments, message in cases:
            with self.subTest(message):
                with self.assertRaisesRegex(rules.ReviewRejected, message):
                    self.check(adjustments)
        with self.assertRaisesRegex(rules.ReviewRejected, "is for '2026-10-02'"):
            self.check([], day="2026-10-02")
        for raw in ("", "Sure! Here you go.", "[1, 2]"):
            with self.assertRaises(rules.ReviewRejected):
                self.check(None, raw=raw)

    def test_a_chat_answer_wrapped_in_a_fence_is_read(self):
        raw = 'Here it is:\n```json\n{"day": "2026-10-01", "adjustments": [], "flags": ["check PROJ-51"]}\n```'
        self.assertEqual(self.check(None, raw=raw).flags, ["check PROJ-51"])

    def test_model_text_is_cleaned_before_anyone_sees_it(self):
        done = self.check([self.adj("PROJ-42", -15, reason="lower\x1b[31m token=sk-live-abcdef123456 " + "x" * 300)],
                          flags=["bearer abcdefghijklmnop1234"])
        self.assertNotIn("\x1b", done.adjustments[0].reason)
        self.assertNotIn("sk-live", done.adjustments[0].reason)
        self.assertLessEqual(len(done.adjustments[0].reason), 200)
        self.assertNotIn("abcdefghijklmnop1234", done.flags[0])

    def test_an_ambiguous_sha_prefix_isnt_evidence(self):
        evidence = {"abcdef1000", "abcdef1999"}
        with self.assertRaisesRegex(rules.ReviewRejected, "wasn't in the evidence"):
            rules.check_review("2026-10-01", self.BASE, evidence, json.dumps({"day": "2026-10-01", "adjustments": [
                self.adj("PROJ-42", -15, evidence=["abcdef1"])]}), step=15)


# --------------------------------------------------------------------------
# Agent estimates turned into figures, in code
# --------------------------------------------------------------------------

class AgentSuggestionTests(AssistCase):
    def test_the_worked_example_moves_time_and_keeps_the_day(self):
        """Named rule: the agent says PROJ-42's six commits were small and PROJ-51's two the real work."""
        p42, p51 = self.worked()
        self.run_store()
        self.record(commits=p42, minutes=45)
        self.record(commits=p51, minutes=90, minutes_low=75, summary="Reworked the form validation.")
        found = self.suggest()
        self.assertEqual((found.method, found.source), ("agent", "agent estimates (kiro, 2 reports)"))
        figures = {k: (s.baseline, s.figure) for k, s in found.tickets.items()}
        self.assertEqual(figures, {"PROJ-42": (90, 45), "PROJ-51": (30, 75)})       # the low end, 75, counts
        self.assertEqual(sum(s.figure for s in found.tickets.values()), 120, "the day keeps its 2h00m")
        self.assertIn("kiro put it at 45m; commits gave 1h30m", found.tickets["PROJ-42"].reason)

    def test_an_agent_can_lower_a_ticket_but_never_raise_the_day(self):
        p42, _ = self.worked()
        self.record(commits=p42, minutes=40)
        found = self.suggest()
        self.assertEqual({k: s.figure for k, s in found.tickets.items()}, {"PROJ-42": 30, "PROJ-51": 30})
        self.record(commits=p42, minutes=600, summary="A much bigger change than git shows.")
        found = self.suggest()
        self.assertEqual({k: s.figure for k, s in found.tickets.items()}, {"PROJ-42": 90, "PROJ-51": 30})
        self.assertTrue(any("can't raise a day" in f for f in found.flags), found.flags)

    def test_a_report_without_commits_is_shown_not_counted(self):
        self.worked()
        self.record(key="PROJ-60", minutes=30, summary="Talked through the export design.")
        found = self.suggest()
        self.assertFalse(found.changed())
        self.assertTrue(any("with no commits, so it isn't counted" in f for f in found.flags), found.flags)

    def test_untracked_commits_are_never_suggested(self):
        self.worked()
        lone = self.sha(self.commit(t(1, 15, 30)))            # no key
        self.record(commits=[lone], minutes=60, summary="An untracked fix.")
        found = self.suggest()
        self.assertFalse(found.changed())
        self.assertTrue(any("has no Jira key" in f for f in found.flags), found.flags)

    def test_where_two_reports_cite_a_commit_the_smaller_counts(self):
        p42, _ = self.worked()
        self.record(commits=p42, minutes=60, agent="kiro")
        self.record(commits=p42, minutes=30, agent="copilot", summary="Small fixes.")
        found = self.suggest()
        self.assertEqual(found.tickets["PROJ-42"].figure, 30)

    def test_a_ticket_partly_covered_keeps_the_engines_share_of_the_rest(self):
        p42, _ = self.worked()
        self.record(commits=p42[:3], minutes=15, summary="Three tiny commits.")
        found = self.suggest()
        # raw 102.5 m over six commits: half of it (51.25) for the three uncovered, plus the agent's 15 m.
        self.assertEqual(found.tickets["PROJ-42"].figure, E.round_down(15 + 102.5 / 2, 15))

    def test_a_decided_ticket_keeps_its_time(self):
        p42, p51 = self.worked()
        self.run_store()
        rules.approve(self.con, self.open_ids()["PROJ-42"])
        self.record(commits=p42, minutes=15)
        self.record(commits=p51, minutes=120, summary="The real work.")
        found = self.suggest()
        self.assertEqual(list(found.tickets), ["PROJ-51"], "an approved ticket gives no time and takes none")
        self.assertEqual(found.tickets["PROJ-51"].figure, 30)

    def test_short_and_unknown_shas(self):
        """A short SHA counts; a report citing a commit Baldur hasn't collected isn't counted until it has (the
        spec: "flagged, not counted"), since its split over that commit would be a guess (review R8)."""
        p42, _ = self.worked()
        short = self.record(commits=[p42[0][:10]], minutes=15)
        found = self.suggest()
        self.assertEqual(found.tickets["PROJ-42"].figure, E.round_down(15 + 102.5 * 5 / 6, 15))
        rules.withdraw_agent_estimate(self.con, short.id)
        self.record(commits=[p42[0][:10], "abcdef0123456"], minutes=15)
        found = self.suggest()
        self.assertFalse(found.changed(), "not counted while one of its commits is missing")
        self.assertTrue(any("abcdef012345, which Baldur hasn't collected" in f and "isn't counted until" in f
                            for f in found.flags), found.flags)

    def test_withdrawn_reports_stop_counting(self):
        p42, _ = self.worked()
        done = self.record(commits=p42, minutes=15)
        rules.withdraw_agent_estimate(self.con, done.id)
        self.assertIsNone(self.suggest())


class AgentDirectionTests(unittest.TestCase):
    """Over random days and random reports: no AI figure raises a day, and every one passes the check."""

    def test_no_day_rises_and_no_ticket_passes_its_target(self):
        rng = random.Random(2026)
        pool = [("PROJ-1",), ("PROJ-2",), ("PROJ-3",), ("PROJ-1", "PROJ-2"), ()]
        checked = 0
        for _ in range(400):
            base = t(1, 7)
            commits = [c(i, base + dt.timedelta(minutes=rng.randint(0, 12 * 60)), *rng.choice(pool),
                         sha=f"{i:040x}") for i in range(1, rng.randint(2, 14))]
            p = E.Params(policy="independent", idle_gap_minutes=rng.choice([30, 60, 120]),
                         round_to_minutes=rng.choice([5, 15, 30]), max_daily_dev_minutes=rng.choice([120, 480]))
            est = E.estimate(commits, [], [], [DAY], p, now=NOW)
            baseline = {x.key: x.minutes_proposed for x in est.proposals_for(DAY) if x.key and x.minutes_proposed}
            reports = []
            for n in range(rng.randint(0, 4)):
                cited = rng.sample(commits, rng.randint(0, min(4, len(commits))))
                minutes = rng.randint(1, 600)
                reports.append(assist.AgentReport(n + 1, rng.choice(["kiro", "copilot"]), None, None, DAY, minutes,
                                                  rng.choice([None, max(1, minutes // 2)]),
                                                  rng.choice(["high", "medium", "low"]), "s", [x.sha for x in cited],
                                                  "2026-10-01T20:00:00Z"))
            reply, _, _ = assist.agent_draft(est, DAY, baseline, reports, commits, p.round_to_minutes)
            if reply is None:
                continue
            evidence = {e for a in reply["adjustments"] for e in a["evidence"]}
            done = rules.check_review(DAY.isoformat(), baseline, evidence, reply, step=p.round_to_minutes)
            checked += 1
            self.assertLessEqual(sum(done.figures.values()), sum(baseline.values()))
            covered = {a["ticket"] for a in reply["adjustments"]}
            for key, minutes in done.figures.items():
                self.assertEqual(minutes % p.round_to_minutes, 0)
                self.assertGreaterEqual(minutes, 0)
                if key not in covered:
                    self.assertEqual(minutes, baseline[key], "a ticket no adjustment names keeps its figure")
        self.assertGreater(checked, 100)


# --------------------------------------------------------------------------
# AI review through the clipboard
# --------------------------------------------------------------------------

class ReviewTests(AssistCase):
    def setUp(self):
        super().setUp()
        self.p42, self.p51 = self.worked()
        self.settings = config.update({"review_mode": "metadata"})

    def pack(self):
        return assist.day_pack(self.con, self.settings, DAY, now=NOW)

    def answer(self, pack, adjustments, flags=()):
        return json.dumps({"day": "2026-10-01", "pack": pack["pack_hash"], "adjustments": adjustments,
                           "flags": list(flags)})

    def test_no_pack_while_review_is_off_and_never_code(self):
        for mode, message in (("off", "AI review is off"), ("content", "rule 6")):
            settings = config.update({"review_mode": mode})
            with self.assertRaisesRegex(assist.AssistError, message):
                assist.day_pack(self.con, settings, DAY, now=NOW)
            # A reply is refused for the same reason, before anything else is looked at (Ysildir's review found a
            # reply to an unestimated day said "make a new pack", which review being off would then refuse).
            with self.assertRaisesRegex(assist.AssistError, message):
                assist.apply_reply(self.con, settings, DAY, {"day": "2026-10-01", "pack": "0" * 16}, now=NOW)

    def test_the_pack_is_metadata_and_names_its_evidence(self):
        self.record(commits=self.p51, minutes=90)
        pack = self.pack()
        self.assertEqual(pack["schema"], assist.PACK_SCHEMA)
        self.assertEqual(pack["baseline"], [{"ticket": "PROJ-42", "minutes": 90}, {"ticket": "PROJ-51", "minutes": 30}])
        self.assertEqual([s["id"] for s in pack["sessions"]], ["s1", "s2"])
        self.assertEqual(sorted(x["sha"] for x in pack["commits"]), sorted(self.p42 + self.p51))
        self.assertEqual(set(pack["commits"][0]), {"sha", "tickets", "at", "repo", "subject", "files", "additions",
                                                    "deletions"})
        self.assertEqual(pack["agent_reports"][0]["id"], "r1")
        self.assertEqual(assist.evidence_ids(pack), set(self.p42 + self.p51) | {"s1", "s2", "r1"})
        self.assertEqual(self.pack()["pack_hash"], pack["pack_hash"], "the same evidence has the same hash")
        self.record(commits=self.p42, minutes=40, summary="Another report.")
        self.assertNotEqual(self.pack()["pack_hash"], pack["pack_hash"])
        text = assist.clipboard_text(pack)
        self.assertIn("Never raise the day's total", text)
        self.assertIn(pack["pack_hash"], text)

    def test_a_good_answer_is_stored_taken_and_named_in_the_worklog(self):
        sid, ctx = self.jira("PROJ-42", "PROJ-51")
        pack = self.pack()
        reply = self.answer(pack, [{"ticket": "PROJ-42", "minutes": -15, "evidence": ["s1"], "confidence": "medium",
                                    "reason": "retry rework is the larger change"},
                                   {"ticket": "PROJ-51", "minutes": 15, "evidence": [self.p51[0]],
                                    "confidence": "medium", "reason": "retry rework is the larger change"}])
        done = assist.apply_reply(self.con, self.settings, DAY, reply, tier="clipboard", model="gpt-4o", now=NOW)
        self.assertEqual(done.figures, {"PROJ-42": 75, "PROJ-51": 45})
        found = self.suggest()
        self.assertEqual((found.method, found.source), ("review", "AI review (clipboard, gpt-4o)"))
        ids = assist.approve_day(self.con, self.settings, DAY, now=NOW, shown=self.suggest().digest())
        rows = {r["work_item_key"]: r for r in self.rows("approved")}
        self.assertEqual({k: r["minutes_final"] for k, r in rows.items()}, {"PROJ-42": 75, "PROJ-51": 45})
        self.assertEqual(sorted(ids), sorted(r["id"] for r in rows.values()))
        note = rules.review_of(rows["PROJ-51"])
        self.assertEqual((note["taken"], note["suggested"], note["baseline"], note["method"]), (True, 45, 30, "review"))
        posted = {p.key: p for p in self.post_all()}
        self.assertIn('Reviewed: AI review (clipboard, gpt-4o) moved 15m to this from the day\'s other tickets '
                      '("retry rework is the larger change")', posted["PROJ-51"].comment)
        self.assertIn("Reviewed: AI review (clipboard, gpt-4o) lowered this from 1h30m to 1h15m",
                      posted["PROJ-42"].comment)
        self.assertTrue(muninn.integrity.check(self.con).ok)

    def test_a_bad_or_stale_answer_changes_nothing(self):
        pack = self.pack()
        with self.assertRaisesRegex(rules.ReviewRejected, "never raise the day"):
            assist.apply_reply(self.con, self.settings, DAY, self.answer(pack, [
                {"ticket": "PROJ-42", "minutes": 15, "evidence": ["s1"], "confidence": "low"}]), now=NOW)
        with self.assertRaisesRegex(assist.AssistError, "different evidence"):
            assist.apply_reply(self.con, self.settings, DAY, json.dumps({"day": "2026-10-01", "pack": "0" * 16,
                                                                         "adjustments": []}), now=NOW)
        self.commit(t(1, 15, 20), "PROJ-51")                   # new work after the pack was made
        with self.assertRaisesRegex(assist.AssistError, "has changed since it was estimated"):
            assist.apply_reply(self.con, self.settings, DAY, self.answer(pack, []), now=NOW)
        self.assertEqual({rules.review_of(r).get("method") for r in self.rows("proposed")}, {None})

    def test_a_stored_review_stops_counting_when_the_day_changes(self):
        pack = self.pack()
        assist.apply_reply(self.con, self.settings, DAY, self.answer(pack, [
            {"ticket": "PROJ-42", "minutes": -30, "evidence": ["s2"], "confidence": "low", "reason": "docs only"}]),
            now=NOW)
        self.assertEqual(self.suggest().method, "review")
        self.record(commits=self.p42, minutes=60)              # new evidence: the pack it saw is out of date
        found = self.suggest()
        self.assertEqual(found.method, "agent")
        self.assertEqual(found.tickets["PROJ-42"].figure, 60)


class ApprovalTests(AssistCase):
    def test_your_figure_wins_over_the_ai_one_and_isnt_marked_taken(self):
        p42, p51 = self.worked()
        self.record(commits=p42, minutes=45)
        self.record(commits=p51, minutes=75, summary="The form.")
        assist.approve_day(self.con, self.settings, DAY, {"proj-51": 60}, now=NOW, shown=self.suggest().digest())
        rows = {r["work_item_key"]: r for r in self.rows("approved")}
        self.assertEqual({k: r["minutes_final"] for k, r in rows.items()}, {"PROJ-42": 45, "PROJ-51": 60})
        self.assertTrue(rules.review_of(rows["PROJ-42"])["taken"])
        self.assertNotIn("taken", rules.review_of(rows["PROJ-51"]))

    def test_nothing_to_take(self):
        self.worked()
        with self.assertRaisesRegex(assist.AssistError, "No AI-assisted figures"):
            assist.approve_day(self.con, self.settings, DAY, now=NOW, shown="")

    def test_a_figure_changed_by_hand_isnt_called_the_ais(self):
        p42, _ = self.worked()
        self.record(commits=p42, minutes=45)
        assist.approve_day(self.con, self.settings, DAY, now=NOW, shown=self.suggest().digest())
        row = [r for r in self.rows("approved") if r["work_item_key"] == "PROJ-42"][0]
        self.assertEqual(rules.review_line(row["review"], row["minutes_final"]),
                         "Reviewed: agent estimates (kiro, 1 report) lowered this from 1h30m to 45m")
        new_id = rules.change_approval(self.con, row["id"], 60)
        new = self.con.execute("SELECT * FROM day_proposals WHERE id = ?", (new_id,)).fetchone()
        self.assertIsNone(rules.review_line(new["review"], new["minutes_final"]))


class OdinCommentTests(DeltaBase):
    def test_an_ordinary_approval_posts_no_reviewed_line(self):
        self.run_store()
        rules.approve_day(self.con, DAY.isoformat())
        for post in self.post_all():
            self.assertNotIn("Reviewed:", post.comment)


# --------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------

class AssistCliTests(AssistCase):
    def cli(self, *args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin is None:
                code = cli.main(list(args))
            else:
                with mock.patch.object(sys, "stdin", io.StringIO(stdin)):
                    code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_an_agents_session(self):
        p42, p51 = self.worked()
        code, out, err = self.cli("ai", "record", "--json", stdin=json.dumps(
            [report(commits=p42, minutes=45), report(commits=p51, minutes=75, summary="The form validation.")]))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), [{"id": "r1", "status": "recorded", "replaced": []},
                                           {"id": "r2", "status": "recorded", "replaced": []}])
        code, out, err = self.cli("ai", "record", "--agent", "copilot", "--minutes", "1h", "--low", "45m",
                                  "--commit", p51[0], "--key", "proj-51", "--confidence", "low", "--date",
                                  "2026-10-01", "--summary", "Looked at the validation.")
        self.assertEqual(code, 0, err)
        self.assertIn("Recorded r3: copilot, 1h00m on PROJ-51 (2026-10-01)", out)
        code, out, _ = self.cli("ai", "list", "--from", "2026-10-01", "--to", "2026-10-01", "--json")
        self.assertEqual([r["id"] for r in json.loads(out)], ["r1", "r2", "r3"])
        code, out, _ = self.cli("ai", "show", "2026-10-01", "--json")
        shown = json.loads(out)
        self.assertEqual(shown["method"], "agent")
        self.assertEqual({x["key"]: (x["estimate"], x["ai_assisted"]) for x in shown["tickets"]},
                         {"PROJ-42": (90, 45), "PROJ-51": (30, 75)})
        self.assertEqual(shown["take"], f"cli.py approve --date 2026-10-01 --ai {shown['id']}")
        code, out, _ = self.cli("ai", "show", "2026-10-01")
        self.assertIn(f"Take them: cli.py approve --date 2026-10-01 --ai {shown['id']}", out)
        self.assertIn("PROJ-42: Reviewed: agent estimates (copilot, kiro, 3 reports) lowered this from 1h30m to 45m",
                      out)
        code, out, _ = self.cli("report", "2026-10-01")
        self.assertIn("AI-assisted figures, from agent estimates (copilot, kiro, 3 reports)", out)
        code, out, err = self.cli("approve", "--date", "2026-10-01", "--ai")
        self.assertEqual(code, 1)
        self.assertIn("Say which AI-assisted figures you're taking", err)
        self.assertIn(f"--ai {shown['id']}", err)
        code, out, err = self.cli("approve", "--date", "2026-10-01", "--ai", shown["id"])
        self.assertEqual(code, 0, err)
        self.assertIn("Approved PROJ-51 ", out)
        code, out, _ = self.cli("ai", "withdraw", "r3")
        self.assertIn("Withdrew r3", out)
        code, out, _ = self.cli("ai", "guide")
        self.assertIn("baldur-agent-2", out)
        code, out, _ = self.cli("ai", "list", "--from", "2026-10-01", "--to", "2026-10-01", "--all")
        self.assertIn("WITHDRAWN", out)

    def test_refusals_are_messages(self):
        self.worked()
        code, _, err = self.cli("ai", "record", stdin="{not json")
        self.assertEqual(code, 1)
        self.assertIn("isn't JSON", err)
        code, _, err = self.cli("ai", "record", stdin=json.dumps(report(summary="```\ncode\n```")))
        self.assertIn("no code or diff", err)
        code, _, err = self.cli("ai", "pack", "2026-10-01")
        self.assertIn("AI review is off", err)
        code, _, err = self.cli("approve", "--date", "2026-10-01", "--ai")
        self.assertIn("No AI-assisted figures", err)
        code, _, err = self.cli("approve", "1", "--ai")
        self.assertIn("--ai goes with --date", err)
        code, _, err = self.cli("ai")
        self.assertIn("needs an action", err)
        code, _, err = self.cli("ai", "withdraw", "x12")
        self.assertIn("isn't an agent estimate id", err)
        self.assertNotIn("Traceback", err)

    def test_the_clipboard_round_trip(self):
        self.worked()
        config.update({"review_mode": "metadata"})
        code, out, err = self.cli("ai", "pack", "2026-10-01", "--out", str(self.dir / "pack.txt"))
        self.assertEqual(code, 0, err)
        text = (self.dir / "pack.txt").read_text(encoding="utf-8")
        pack = json.loads(text[text.rindex("\nInput:\n") + len("\nInput:\n"):])
        answer = json.dumps({"day": "2026-10-01", "pack": pack["pack_hash"], "adjustments": [
            {"ticket": "PROJ-42", "minutes": 45, "evidence": ["s1"], "confidence": "high", "reason": "more"}]})
        code, _, err = self.cli("ai", "review", "2026-10-01", stdin=answer)
        self.assertEqual(code, 1)
        self.assertIn("The answer wasn't used", err)
        answer = answer.replace('"minutes": 45', '"minutes": -15')
        code, out, err = self.cli("ai", "review", "2026-10-01", "--model", "copilot", stdin=answer)
        self.assertEqual(code, 0, err)
        self.assertIn("PROJ-42 1h30m -> 1h15m", out)
        self.assertIn("PROJ-42: Odin's worklog comment will say: Reviewed: AI review (clipboard, copilot) "
                      'lowered this from 1h30m to 1h15m ("more")', out)
        self.assertRegex(out, r"Take them with: cli.py approve --date 2026-10-01 --ai [0-9a-f]{8}")


# --------------------------------------------------------------------------
# Agent files: the guard, the reminder and installing them into a workspace
# --------------------------------------------------------------------------

HOOKS = ROOT / "apps" / "baldur" / "agents" / "hooks"


def load_hook(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, HOOKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_hook(name, command):
    import subprocess
    event = json.dumps({"tool": "shell", "tool_input": {"command": command}})
    return subprocess.run([sys.executable, str(HOOKS / f"{name}.py")], input=event, capture_output=True, text=True,
                          timeout=60)


class AgentGuardTests(unittest.TestCase):
    guard = load_hook("guard_baldur")

    def test_the_persons_decisions_are_blocked(self):
        b = r'py -3 "C:\Users\Dev User\AppData\Local\Asgard\app\apps\baldur\cli.py"'
        for command in (f"{b} approve --date 2026-10-01", f"{b} approve --date 2026-10-01 --ai", f"{b} reject 12",
                        f"{b} change 12 2h", f"{b} actual 2026-10-01 6h", f"{b} calibrate --accept",
                        f"{b} setup --set review_mode=metadata", f"{b} setup --email x@agency.gov",
                        f"{b} repos --off vendor", f"{b} keys 9f3c1a2b PROJ-9", f"{b} schedule --day MON",
                        f"{b} github token", "baldur.cmd approve --date today",
                        r'& "C:\Asgard\app\apps\baldur\baldur.cmd" reject --date 2026-10-01',
                        "python Asgard/apps/baldur/cli.py change 3 1h",
                        "git commit -m x && baldur.cmd approve --date today",
                        r"sqlite3 %LOCALAPPDATA%\Asgard\muninn.db \"UPDATE day_proposals SET status='approved'\"",
                        "python -c \"import sqlite3; sqlite3.connect('muninn.db').execute('DELETE FROM events')\"",
                        "py -3 Asgard.pyw --muninn restore"):
            with self.subTest(command):
                self.assertIsNotNone(self.guard.verdict(command))

    def test_recording_and_reading_are_allowed(self):
        b = r'py -3 "C:\Asgard\app\apps\baldur\cli.py"'
        for command in (f"{b} ai record --agent kiro --minutes 1h --commit abc1234 --confidence low --summary x",
                        f"{b} ai list --json", f"{b} ai show 2026-10-01 --json", f"{b} ai guide", f"{b} report today",
                        f"{b} days", f"{b} estimate", f"{b} collect", f"{b} keys 9f3c1a2b", f"{b} setup",
                        f"{b} calibrate", f"{b} actuals", f"{b} ai withdraw r3", "git commit -m 'approve the docs'",
                        "python tools/run_tests.py", "echo approve"):
            with self.subTest(command):
                self.assertIsNone(self.guard.verdict(command))

    def test_the_packaged_builds_programs_are_read_the_same_way(self):
        """asgard-cli.exe and Asgard.exe run Asgard's scripts as python does (docs/packaging.md)."""
        build = r"C:\Program Files\Asgard"
        for command in (rf'"{build}\asgard-cli.exe" "{build}\apps\baldur\cli.py" approve --date 2026-10-01',
                        r"asgard-cli.exe apps\baldur\cli.py calibrate --accept",
                        rf'cd /d "{build}\apps\baldur" && baldur.cmd approve --date 2026-10-01',
                        rf'"{build}\asgard-cli.exe" "{build}\apps\ysildir\cli.py" tools --on baldur_day',
                        "asgard-cli.exe --muninn restore --yes", r"asgard-cli.exe Asgard.pyw --muninn repair",
                        "asgard-cli.exe --uninstall --yes --purge", rf'"{build}\Asgard.exe" --uninstall',
                        "python Asgard.pyw --uninstall --yes"):
            with self.subTest(command):
                self.assertIsNotNone(self.guard.verdict(command))
        for command in (r"asgard-cli.exe apps\baldur\cli.py report today", "asgard-cli.exe --self-test",
                        "asgard-cli.exe --muninn check", r"asgard-cli.exe apps\ysildir\cli.py check"):
            with self.subTest(command):
                self.assertIsNone(self.guard.verdict(command))

    def test_r6_commands_inside_quotes_bare_cli_py_and_files_are_blocked(self):
        """Review R6 (2026-10-09): each of these got past the guard, and one approved a day."""
        b = r'py -3 "C:\Users\me\AppData\Local\Asgard\app\apps\baldur\cli.py"'
        cfg = r"$env:LOCALAPPDATA\Asgard\settings\baldur.json"
        for command in ('cmd /c "baldur.cmd approve --date 2026-10-01 --ai"',
                        'powershell -NoProfile -Command "baldur.cmd approve --date 2026-10-01"',
                        'bash -lc "python3 Asgard/apps/baldur/cli.py approve --date 2026-10-01"',
                        'Start-Process baldur.cmd -ArgumentList "approve --date 2026-10-01" -Wait',
                        "Start-Process -FilePath baldur.cmd -ArgumentList 'approve','--date','2026-10-01'",
                        'Invoke-Expression "baldur.cmd reject --date 2026-10-01"',
                        r"cd C:\Asgard\app\apps\baldur; py -3 .\cli.py approve --date 2026-10-01",
                        "cd Asgard/apps/baldur && python cli.py actual 2026-10-01 6h",
                        f"{b} calibrate --acc", f"{b} repos --of vendor",
                        f"(Get-Content {cfg}) -replace 'off','metadata' | Set-Content {cfg}",
                        r'sqlite3 $env:LOCALAPPDATA\Asgard\*.db "UPDATE time_actuals SET minutes = 480"'):
            with self.subTest(command):
                self.assertIsNotNone(self.guard.verdict(command))
        event = {"tool": "shell", "tool_input": {"command": ["baldur.cmd", "approve", "--date", "2026-10-01"]}}
        self.assertTrue(any(self.guard.verdict(c) for c in self.guard.commands(event)), "an argv list")

    def test_r6_free_text_and_other_apps_are_not_read_as_commands(self):
        b = r'py -3 "C:\Asgard\app\apps\baldur\cli.py"'
        for command in (f'{b} ai record --agent kiro --minutes 45m --commit 9f3c1a2b4d --confidence low '
                        f'--summary "Moved the muninn.db path check"',
                        'git commit -m "Document where muninn.db lives"',
                        f'{b} ai record --agent kiro --minutes 1h --commit abc1234 --confidence low '
                        f'--summary "a; b & c"',
                        "py -3 Asgard/apps/heimdall/cli.py fill --dry-run", "python Asgard/apps/ysildir/cli.py check"):
            with self.subTest(command):
                self.assertIsNone(self.guard.verdict(command))

    def test_r6_baldur_takes_no_abbreviated_option(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["calibrate", "--acc"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["repos", "--of", "vendor"])

    def test_the_hook_blocks_with_exit_code_2_and_says_why(self):
        done = run_hook("guard_baldur", "baldur.cmd approve --date today")
        self.assertEqual(done.returncode, 2)
        self.assertIn("the person's decision", done.stderr)
        self.assertEqual(run_hook("guard_baldur", "baldur.cmd ai show today").returncode, 0)
        self.assertEqual(run_hook("guard_baldur", "").returncode, 0)

    def test_the_reminder_follows_a_commit_only(self):
        self.assertIn("baldur.cmd ai record", run_hook("after_commit", 'git commit -m "retry"').stdout)
        self.assertIn("ai record", run_hook("after_commit", "git -C repo commit --amend --no-edit").stdout)
        for quiet in ("git status", "git commit --dry-run -m x", "git commit --help", "echo commit"):
            with self.subTest(quiet):
                self.assertEqual(run_hook("after_commit", quiet).stdout, "")


class AgentInstallTests(AssistCase):
    def test_installing_into_a_workspace(self):
        from baldur import agents
        workspace = self.dir / "my service"
        workspace.mkdir()
        done = agents.install(workspace)
        self.assertEqual(sorted(w for _, w in done), ["written"] * 3)
        steering = (workspace / ".kiro" / "steering" / "baldur-estimates.md").read_text(encoding="utf-8")
        self.assertIn(f'"{agents.CLI}" ai record --agent kiro', steering)
        self.assertNotIn("{baldur}", steering)
        self.assertTrue(steering.startswith("---\ninclusion: auto"))
        for name, script, trigger in (("baldur-guard.json", "guard_baldur.py", "PreToolUse"),
                                      ("baldur-after-commit.json", "after_commit.py", "PostToolUse")):
            hook = json.loads((workspace / ".kiro" / "hooks" / name).read_text(encoding="utf-8"))["hooks"][0]
            self.assertEqual(hook["trigger"], trigger)
            self.assertIn(f'"{HOOKS / script}"', hook["action"]["command"])
            self.assertTrue((HOOKS / script).is_file())
        (workspace / ".kiro" / "steering" / "baldur-estimates.md").write_text("my edits", encoding="utf-8")
        again = dict((p.name, w) for p, w in agents.install(workspace))
        self.assertEqual(again["baldur-estimates.md"], "kept")
        self.assertEqual((workspace / ".kiro" / "steering" / "baldur-estimates.md").read_text(), "my edits")
        forced = dict((p.name, w) for p, w in agents.install(workspace, force=True))
        self.assertEqual(forced["baldur-estimates.md"], "replaced")

    def test_the_cli_says_what_it_did(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["ai", "kiro", "--into", str(self.dir)])
            missing = cli.main(["ai", "kiro", "--into", str(self.dir / "nope")])
        self.assertEqual((code, missing), (0, 1), err.getvalue())
        self.assertIn("baldur-guard.json", out.getvalue())
        self.assertIn("isn't a folder", err.getvalue())


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# The independent review of 2026-10-09 (docs/review-2026-10-09.md): one test per finding
# --------------------------------------------------------------------------

class ReviewFindingsTests(AssistCase):
    def taken(self):
        return {r["work_item_key"]: r["minutes_final"] for r in self.rows("approved")}

    def two_reports(self):
        p42, p51 = self.worked()
        self.record(commits=p42, minutes=45)
        self.record(commits=p51, minutes=75, summary="The form validation.")
        return p42, p51

    def test_r1_your_figure_cant_keep_time_the_ai_moved_away(self):
        self.two_reports()
        shown = self.suggest().digest()                # PROJ-42 1h30m -> 45m, PROJ-51 30m -> 1h15m
        with self.assertRaisesRegex(assist.AssistError, "moves time from PROJ-42, but your figure keeps it there"):
            assist.approve_day(self.con, self.settings, DAY, {"PROJ-42": 90}, now=NOW, shown=shown)
        self.assertEqual(self.taken(), {})
        assist.approve_day(self.con, self.settings, DAY, {"PROJ-42": 90, "PROJ-51": 30}, now=NOW, shown=shown)
        self.assertEqual(self.taken(), {"PROJ-42": 90, "PROJ-51": 30}, "your own figures for both are yours")
        self.assertFalse(any(rules.review_of(r).get("taken") for r in self.rows("approved")))

    def test_r2_time_jira_already_holds_never_moves(self):
        p42, p51 = self.worked()
        sid, ctx = self.jira("PROJ-42", "PROJ-51")
        self.by_hand(sid, ctx, 0, 90)                  # PROJ-42's 1h30m is already in Jira
        self.run_store()
        self.record(commits=p42, minutes=45)
        self.record(commits=p51, minutes=75, summary="The form validation.")
        found = self.suggest()
        self.assertFalse(found.changed(), "no time can leave PROJ-42, so none can move to PROJ-51")
        self.assertTrue(any("Jira already holds 1h30m for PROJ-42" in f for f in found.flags), found.flags)
        self.by_hand(sid, ctx, 0, 60, wid="w1")        # Jira holds 1h00m instead: only 30m can move
        found = self.suggest()
        self.assertEqual({k: s.figure for k, s in found.tickets.items()}, {"PROJ-42": 60, "PROJ-51": 60})
        assist.approve_day(self.con, self.settings, DAY, now=NOW, shown=found.digest())
        self.post_all()
        held = rules.held_minutes(self.con, DAY.isoformat(), ["PROJ-42", "PROJ-51"])
        self.assertEqual(sum(held.values()), 120, "Jira holds the engine's 2h00m day, not more")
        with self.assertRaisesRegex(rules.ReviewRejected, "below the 1h00m Jira already holds"):
            rules.check_review(DAY.isoformat(), {"PROJ-42": 90, "PROJ-51": 30}, {"s1"},
                               {"day": DAY.isoformat(), "adjustments": [
                                   {"ticket": "PROJ-42", "minutes": -45, "evidence": ["s1"], "confidence": "low"}]},
                               step=15, floors={"PROJ-42": 60})

    def test_r2_the_pack_tells_the_ai_what_jira_holds(self):
        settings = config.update({"review_mode": "metadata"})
        self.worked()
        sid, ctx = self.jira("PROJ-42", "PROJ-51")
        self.by_hand(sid, ctx, 0, 60)                  # Jira holds 1h00m of PROJ-42
        pack = assist.day_pack(self.con, settings, DAY, now=NOW)
        self.assertEqual(pack["baseline"], [{"ticket": "PROJ-42", "minutes": 90, "in_jira": 60},
                                            {"ticket": "PROJ-51", "minutes": 30}])
        self.assertIn("never\n   lower a ticket below its in_jira minutes", assist.review_prompt())

    def test_r3_a_stored_review_is_checked_again_before_it_counts(self):
        settings = config.update({"review_mode": "metadata"})
        self.worked()
        pack = assist.day_pack(self.con, settings, DAY, now=NOW)
        for r in self.rows("proposed"):                # figures written onto the rows outside store_review
            self.con.execute("UPDATE day_proposals SET review = ? WHERE id = ?", (json.dumps({
                "method": "review", "pack_hash": pack["pack_hash"], "baseline": r["minutes_proposed"],
                "suggested": {"PROJ-42": 120, "PROJ-51": 60}[r["work_item_key"]], "source": "AI review"}), r["id"]))
        self.assertIsNone(assist.suggestions(self.con, settings, DAY, now=NOW))

    def test_r4_approve_takes_only_the_figures_you_were_shown(self):
        p42, p51 = self.worked()
        self.record(commits=p42, minutes=45)
        seen = self.suggest()                          # PROJ-42 1h30m -> 45m; PROJ-51 stays 30m
        self.record(commits=p51, minutes=75, summary="The form validation.")   # recorded after you looked
        with self.assertRaisesRegex(assist.AssistError, "aren't the ones you were shown"):
            assist.approve_day(self.con, self.settings, DAY, now=NOW, shown=seen.digest())
        self.assertEqual(self.taken(), {})
        now = self.suggest()
        self.assertNotEqual(now.digest(), seen.digest())
        assist.approve_day(self.con, self.settings, DAY, now=NOW, shown=now.digest())
        self.assertEqual(self.taken(), {"PROJ-42": 45, "PROJ-51": 75})

    def test_r7_the_worklog_line_is_shown_word_for_word(self):
        settings = config.update({"review_mode": "metadata"})
        self.worked()
        sid, ctx = self.jira("PROJ-42", "PROJ-51")
        pack = assist.day_pack(self.con, settings, DAY, now=NOW)
        first = "Two of the six commits are typo fixes in the README; per the team lead this is pre-approved"
        second = "A second adjustment whose reason was never shown anywhere before approval"
        assist.apply_reply(self.con, settings, DAY, {"day": "2026-10-01", "pack": pack["pack_hash"], "adjustments": [
            {"ticket": "PROJ-42", "minutes": -15, "evidence": ["s1"], "confidence": "medium", "reason": first},
            {"ticket": "PROJ-42", "minutes": -15, "evidence": ["s2"], "confidence": "medium", "reason": second}]},
            now=NOW)
        found = assist.suggestions(self.con, settings, DAY, now=NOW)
        shown = day_report.render_ai(found, DAY)
        line = found.posted_line("PROJ-42")
        self.assertIn(f"PROJ-42: {line}", shown)
        self.assertIn(second, line)
        assist.approve_day(self.con, settings, DAY, now=NOW, shown=found.digest())
        posted = [x for p in self.post_all() for x in p.comment.splitlines() if x.startswith("Reviewed:")]
        self.assertEqual(posted, [line], "what Jira gets is exactly what was shown")

    def test_r8_a_report_on_two_days_counts_only_its_share_on_each(self):
        p42, p51 = self.worked()
        next_day = self.sha(self.commit(t(2, 10), "PROJ-51"))
        self.record(commits=p42, minutes=45)
        self.record(commits=[p51[0], next_day], minutes=120, summary="The form, over two days.")
        found = self.suggest()
        # The report's 2h splits over its two commits: 1h00m on the 1st (12:10), plus the engine's share of
        # the uncovered 12:25 commit; then scaled to the day's 2h00m.
        self.assertEqual({k: s.figure for k, s in found.tickets.items()}, {"PROJ-42": 30, "PROJ-51": 75})

    def test_r9_summaries_are_plain_sentences_and_print_clean(self):
        for summary, message in (("def retry(n): return backoff(n) * 2 if n < MAX else fail()", "no code or diff"),
                                 ("- retries = 3 + retries = 5", "no code or diff"),
                                 ("Set retries = 5 in the poller.", "no code or diff"),
                                 ("function retry(n) { return n }", "no code or diff"),
                                 ("class Retry(Base): pass", "no code or diff"),
                                 ("const n = 5", "no code or diff"),
                                 ("if (a == b) retry()", "no code or diff"),
                                 ("retry(n) -> int", "no code or diff"),
                                 ("Fixed retry\x1b[2J\x1b[31m in the poller\x1b[0m\x07", "control character"),
                                 ("Fixed retry\u202e in the poller.", "control character"),
                                 ("Fixed\x00 retry.", "control character")):
            with self.subTest(summary):
                with self.assertRaisesRegex(muninn.MuninnError, message):
                    self.record(summary=summary)
        for fine in ("Return the retry count to 3 (from 5).", "Moved retry() into the poller.",
                     "Fixed PROJ-42: the poller now retries with jitter.", "Updated README - usage section.",
                     # Prose that names code words stays prose.
                     "Mapped the fn key (F12) to refresh.", "Fixed the retry function (it looped).",
                     "Added the CSS class warning (red) to the banner.", "Fixed typos in the doc(s): README, guide."):
            self.record(summary=fine)
        # A summary stored before this check prints as one clean line.
        self.con.execute("INSERT INTO agent_estimates (agent, local_date, minutes, confidence, summary, report_hash) "
                         "VALUES ('kiro', '2026-10-01', 30, 'low', ?, 'x')", ("Old\x1b[2J summary",))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_ai_list(self.con, self.settings, cli.build_parser().parse_args(
                ["ai", "list", "--from", "2026-10-01", "--to", "2026-10-01"]))
        self.assertNotIn("\x1b", out.getvalue())

    def test_r10_sending_a_withdrawn_report_again_records_it_again(self):
        sha = self.sha(self.commit(t(1, 10), "PROJ-42"))
        first = self.record(commits=[sha], minutes=45)
        second = self.record(commits=[sha], minutes=30)
        third = self.record(commits=[sha], minutes=45)
        self.assertEqual((third.status, third.replaced), ("recorded", [second.id]))
        self.assertNotEqual(third.id, first.id)
        live = [tuple(r) for r in self.con.execute("SELECT id, minutes FROM agent_estimates WHERE status = 'recorded'")]
        self.assertEqual(live, [(third.id, 45)])
        self.assertEqual(self.record(commits=[sha], minutes=45).status, "duplicate", "while it counts, once")
        rules.withdraw_agent_estimate(self.con, third.id)
        self.assertEqual(self.record(commits=[sha], minutes=45).status, "recorded")
        self.assertTrue(muninn.integrity.check(self.con).ok)

    def test_r11_taking_agent_figures_over_a_stale_review_keeps_them_apart(self):
        settings = config.update({"review_mode": "metadata"})
        p42, _ = self.worked()
        pack = assist.day_pack(self.con, settings, DAY, now=NOW)
        assist.apply_reply(self.con, settings, DAY, {"day": "2026-10-01", "pack": pack["pack_hash"], "adjustments": [
            {"ticket": "PROJ-42", "minutes": -45, "evidence": ["s2"], "confidence": "low", "reason": "typo fixes"}]},
            model="gpt-4o", now=NOW)
        self.record(commits=p42, minutes=60)           # new evidence: the review is stale; the agent's figure applies
        found = assist.suggestions(self.con, settings, DAY, now=NOW)
        self.assertEqual(found.method, "agent")
        assist.approve_day(self.con, settings, DAY, now=NOW, shown=found.digest())
        note = rules.review_of([r for r in self.rows("approved") if r["work_item_key"] == "PROJ-42"][0])
        self.assertEqual(note["method"], "agent")
        self.assertFalse({"adjustments", "model", "tier", "prompt_version"} & set(note))
        self.assertEqual(note["earlier"]["model"], "gpt-4o", "the stale review is kept apart, not mixed in")

    def test_r14_a_report_whose_commits_changed_isnt_counted(self):
        p42, p51 = self.worked()
        done = self.record(commits=p42, minutes=45)
        self.assertTrue(self.suggest().changed())
        self.con.execute("INSERT INTO agent_estimate_commits (estimate_id, sha) VALUES (?, ?)", (done.id, p51[0]))
        found = self.suggest()
        self.assertFalse(found.changed())
        self.assertTrue(any(f"r{done.id}'s commits were changed after it was recorded" in f for f in found.flags))
        problems = [f for f in muninn.integrity.check(self.con).findings if f.area == "agents"]
        self.assertTrue(problems and f"r{done.id}" in problems[0].message, problems)
        self.assertIn(f"ai withdraw r{done.id}", problems[0].fix)
        rules.withdraw_agent_estimate(self.con, done.id)
        self.assertEqual([f for f in muninn.integrity.check(self.con).findings if f.area == "agents"], [],
                         "a withdrawn report counts for nothing, so there's nothing to fix")

    def test_r15_a_figure_changed_by_hand_stays_yours_whatever_you_change_it_to(self):
        p42, _ = self.worked()
        self.record(commits=p42, minutes=45)
        assist.approve_day(self.con, self.settings, DAY, now=NOW, shown=self.suggest().digest())
        row = [r for r in self.rows("approved") if r["work_item_key"] == "PROJ-42"][0]
        to_60 = rules.change_approval(self.con, row["id"], 60)
        back = rules.change_approval(self.con, to_60, 45)
        new = self.con.execute("SELECT * FROM day_proposals WHERE id = ?", (back,)).fetchone()
        self.assertIsNone(rules.review_line(new["review"], new["minutes_final"]))

    def test_p6_an_agent_is_named_in_one_word(self):
        with self.assertRaisesRegex(muninn.MuninnError, "one word"):
            self.record(agent="kiro approved by the owner")
        self.record(agent="claude-code")

