"""Baldur: Jira keys, the estimator, the collector on real git repositories, the store, the report and the CLI.

The spec's worked example is a named test, and the property tests check the
direction of bias: no cap, weight, scale or rounding raises a number. On
Linux and macOS the tests run in America/New_York, so a daylight-saving day
can be tested; elsewhere they use the computer's own time zone.
"""
import contextlib
import dataclasses
import datetime as dt
import io
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT, ROOT / "apps" / "baldur"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from asgard import muninn  # noqa: E402
from asgard.muninn import baldur as approvals  # noqa: E402
from asgard.muninn import odin  # noqa: E402
from baldur import cli, collect, desk, github, gitread, keys, report, store  # noqa: E402
from baldur import estimate as E  # noqa: E402
from baldur import settings as config  # noqa: E402

GIT = shutil.which("git")
NOW = dt.datetime(2026, 10, 3, 18, 0, tzinfo=dt.timezone.utc)
DAY = dt.date(2026, 10, 1)
NEXT = dt.date(2026, 10, 2)
WORKED_42 = [(9, 50), (10, 20), (10, 55), (11, 40), (14, 30), (15, 5)]
WORKED_51 = [(12, 10), (12, 25)]
MEETINGS = [((9, 0), (12, 0)), ((12, 30), (16, 30))]
_saved_tz = None


def setUpModule():
    global _saved_tz
    if hasattr(time, "tzset"):
        _saved_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()


def tearDownModule():
    if hasattr(time, "tzset"):
        if _saved_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = _saved_tz
        time.tzset()


def t(day, hh, mm=0, month=10):
    """A local time in 2026."""
    return dt.datetime(2026, month, day, hh, mm).astimezone()


def c(n, at, *ks, repo="asgard", branch=None, subject=None, patch=None, sha=None):
    return E.Commit(n, sha or f"{n:040x}", at, tuple(ks), repo, subject or f"change {n}", patch, branch)


def worked_commits():
    out = [c(n + 1, t(1, h, m), "PROJ-42") for n, (h, m) in enumerate(WORKED_42)]
    return out + [c(n + 10, t(1, h, m), "PROJ-51") for n, (h, m) in enumerate(WORKED_51)]


def worked_meetings():
    return [E.Meeting(str(n), t(1, *a), t(1, *b)) for n, (a, b) in enumerate(MEETINGS)]


# --------------------------------------------------------------------------
# Jira keys
# --------------------------------------------------------------------------

class KeyTests(unittest.TestCase):
    P = ["PROJ", "OPS"]

    def test_messages_need_a_known_upper_case_project(self):
        self.assertEqual(keys.find_keys("PROJ-123 retry; see OPS-7 and PROJ-123 again", self.P), ["PROJ-123", "OPS-7"])
        self.assertEqual(keys.find_keys("UTF-8 and SHA-256 and ABC-1", self.P), [])
        self.assertEqual(keys.find_keys("proj-123 retry", self.P), [])
        self.assertEqual(keys.find_keys("PROJ-0 PROJ-012 XPROJ-5", self.P), [])

    def test_branches_match_in_any_case(self):
        self.assertEqual(keys.branch_keys("feature/proj-42-retry", self.P), ["PROJ-42"])
        self.assertEqual(keys.branch_keys("refs/remotes/origin/bugfix/OPS-7", self.P), ["OPS-7"])
        self.assertEqual(keys.branch_keys("main", self.P), [])

    def test_the_first_source_with_a_key_wins(self):
        self.assertEqual(keys.choose(self.P, "feature/PROJ-1-a", ["feature/PROJ-2"], ["feature/PROJ-3"], "PROJ-4"),
                         (["PROJ-1"], "reflog"))
        self.assertEqual(keys.choose(self.P, "main", ["feature/PROJ-2"], ["feature/PROJ-3"], "PROJ-4"),
                         (["PROJ-2"], "branch"))
        # The branch was deleted after merge; the pull request's head branch still has the key.
        self.assertEqual(keys.choose(self.P, None, [], ["feature/PROJ-3-x"], "fix tests"), (["PROJ-3"], "pr"))
        self.assertEqual(keys.choose(self.P, None, [], [], "PROJ-4 and OPS-9"), (["PROJ-4", "OPS-9"], "message"))
        self.assertEqual(keys.choose(self.P, None, [], [], "ABC-1 isn't ours"), ([], None))


# --------------------------------------------------------------------------
# The estimator
# --------------------------------------------------------------------------

class WorkedExampleTests(unittest.TestCase):
    """The spec's day: meetings 09:00-12:00 and 12:30-16:30, eight commits on two tickets."""

    def run_policy(self, policy):
        return E.estimate(worked_commits(), [], worked_meetings(), [DAY], E.Params(policy=policy),
                          calendar=(DAY, DAY), now=NOW)

    def minutes(self, est):
        return {x.key: x.minutes_proposed for x in est.proposals}

    def test_two_sessions(self):
        est = self.run_policy("ambient")
        self.assertEqual([(x.start, x.end, x.minutes, x.ambient) for x in est.parts],
                         [(t(1, 9, 20), t(1, 12, 25), 185, 160), (t(1, 14), t(1, 15, 5), 65, 65)])

    def test_ambient_gives_2h00m(self):
        est = self.run_policy("ambient")
        self.assertAlmostEqual(est.days[DAY].counted, 137.5)
        self.assertEqual({x.key: round(x.minutes_raw, 6) for x in est.proposals}, {"PROJ-42": 102.5, "PROJ-51": 35})
        self.assertEqual(self.minutes(est), {"PROJ-42": 90, "PROJ-51": 30})
        self.assertEqual(est.days[DAY].flags, [])

    def test_overlap_gives_4h00m_under_a_270_minute_cap(self):
        est = self.run_policy("overlap")
        self.assertEqual(est.days[DAY].cap, 270)
        self.assertEqual(self.minutes(est), {"PROJ-42": 180, "PROJ-51": 60})

    def test_independent_counts_in_full_and_flags_the_day(self):
        est = self.run_policy("independent")
        self.assertEqual(self.minutes(est), {"PROJ-42": 180, "PROJ-51": 60})
        self.assertIn("Meetings (7h00m) plus development (4h00m) exceed your tour of duty (8h00m)",
                      est.days[DAY].flags)

    def test_never_the_old_45_minutes(self):
        for policy in config.POLICIES:
            self.assertGreaterEqual(sum(self.minutes(self.run_policy(policy)).values()), 120, policy)


class SessionTests(unittest.TestCase):
    P = E.Params()

    def test_one_timeline_across_repositories(self):
        commits = [c(1, t(1, 9), "PROJ-1", repo="a"), c(2, t(1, 9, 20), "PROJ-2", repo="b"),
                   c(3, t(1, 9, 40), "PROJ-1", repo="a"), c(4, t(1, 10), "PROJ-2", repo="b")]
        self.assertEqual([(s.start, s.end) for s in E.build_sessions(commits, [], self.P)], [(t(1, 8, 30), t(1, 10))])
        est = E.estimate(commits, [], [], [DAY], self.P, now=NOW)
        self.assertEqual(sum(x.minutes_raw for x in est.proposals), 90, "two repositories in parallel count once")

    def test_a_gap_longer_than_the_idle_gap_starts_a_new_session(self):
        commits = [c(1, t(1, 9)), c(2, t(1, 11)), c(3, t(1, 13, 1))]
        self.assertEqual([len(s.commits) for s in E.build_sessions(commits, [], self.P)], [2, 1])

    def test_the_checkout_onto_the_branch_starts_the_session(self):
        first = c(1, t(1, 10), branch="feature/PROJ-1")
        marks = [E.Checkout(t(1, 7, 59), "asgard", "feature/PROJ-1"),    # more than the idle gap before
                 E.Checkout(t(1, 9, 5), "asgard", "feature/PROJ-1"),
                 E.Checkout(t(1, 9, 10), "asgard", "main"),              # another branch
                 E.Checkout(t(1, 9, 15), "asgard", "feature/PROJ-1"),    # the latest onto it wins
                 E.Checkout(t(1, 9, 50), "other", "feature/PROJ-1")]     # another repository
        s = E.build_sessions([first], marks, self.P)[0]
        self.assertEqual((s.start, s.start_basis), (t(1, 9, 15), "reflog"))
        s = E.build_sessions([first], [marks[0], marks[2], marks[4]], self.P)[0]
        self.assertEqual((s.start, s.start_basis), (t(1, 9, 30), "lead_in"))

    def test_reflog_alone_never_makes_a_session(self):
        est = E.estimate([], [E.Checkout(t(1, 9), "asgard", "feature/PROJ-1")], [], [DAY], self.P, now=NOW)
        self.assertEqual((est.parts, est.proposals), ([], []))

    def test_clamps(self):
        lone = E.build_sessions([c(1, t(1, 10))], [], E.Params(lead_in_minutes=0))[0]
        self.assertEqual((lone.start, lone.end), (t(1, 9, 45), t(1, 10)), "a lone commit gets the 15-minute floor")
        long = [c(n, t(1, 8) + dt.timedelta(minutes=100 * n)) for n in range(5)]    # 08:00 to 14:40
        s = E.build_sessions(long, [], self.P)[0]
        self.assertEqual((s.start, s.end), (t(1, 10, 40), t(1, 14, 40)), "keeps the 4 hours nearest the end")

    def test_a_session_across_midnight_is_split(self):
        est = E.estimate([c(1, t(1, 23, 50), "PROJ-1"), c(2, t(2, 0, 40), "PROJ-2")], [], [], [DAY, NEXT],
                         self.P, now=NOW)
        self.assertEqual([(x.local_date, x.start, x.end, [k.id for k in x.commits]) for x in est.parts],
                         [(DAY, t(1, 23, 20), t(2, 0), [1]), (NEXT, t(2, 0), t(2, 0, 40), [2])])
        self.assertEqual({(x.local_date, x.key): x.minutes_proposed for x in est.proposals},
                         {(DAY, "PROJ-1"): 30, (NEXT, "PROJ-2"): 30})

    def test_a_lead_in_before_midnight_borrows_the_commits(self):
        est = E.estimate([c(1, t(2, 0, 10), "PROJ-1")], [], [], [DAY, NEXT], self.P, now=NOW)
        first = est.parts[0]
        self.assertEqual((first.local_date, first.borrowed, [k.id for k in first.commits]), (DAY, True, [1]))
        self.assertEqual([(x.local_date, x.minutes_proposed) for x in est.proposals], [(DAY, 15), (NEXT, 0)])

    def test_two_keys_split_evenly_and_untracked_stays_untracked(self):
        commits = [c(1, t(1, 9), "PROJ-1", "PROJ-2"), c(2, t(1, 9, 30)), c(3, t(1, 10), "PROJ-1")]
        est = E.estimate(commits, [], [], [DAY], self.P, now=NOW)
        self.assertEqual({x.key: x.minutes_raw for x in est.proposals}, {"PROJ-1": 45, "PROJ-2": 15, None: 30})
        self.assertIsNone(est.proposals[-1].key, "untracked comes last")

    def test_each_change_counts_once_and_future_commits_are_left_out(self):
        commits = [c(1, t(1, 9), "PROJ-1", sha="a" * 40, patch="p1", subject="retry"),
                   c(2, t(1, 9), "PROJ-1", sha="a" * 40, repo="clone"),       # the same commit in another clone
                   c(3, t(1, 9), "PROJ-1", sha="b" * 40, patch="p1"),         # cherry-picked
                   c(4, t(1, 9), "PROJ-1", sha="c" * 40, subject="retry"),    # amended
                   c(5, NOW + dt.timedelta(hours=2), "PROJ-1", sha="d" * 40)]
        kept, dropped = E.clean(commits, NOW)
        self.assertEqual(([k.id for k in kept], sorted(k.id for k, _ in dropped)), ([1], [2, 3, 4, 5]))
        its_day = E.local_day(commits[-1].at)   # the flag goes on the commit's own day, which in some zones is tomorrow
        est = E.estimate(commits, [], [], [its_day], self.P, now=NOW)
        self.assertTrue(any("dated in the future" in f for f in est.days[its_day].flags))

    def test_the_day_cap_scales_every_session(self):
        commits = [c(1, t(1, 9), "PROJ-1"), c(2, t(1, 10), "PROJ-1"), c(3, t(1, 13), "PROJ-2"), c(4, t(1, 14), "PROJ-2")]
        est = E.estimate(commits, [], [], [DAY], E.Params(max_daily_dev_minutes=120), now=NOW)
        self.assertEqual({x.key: round(x.minutes_raw, 6) for x in est.proposals}, {"PROJ-1": 60, "PROJ-2": 60})
        self.assertIn("Scaled down to the day cap of 2h00m", est.days[DAY].flags)

    def test_a_day_without_calendar_data_is_independent_and_flagged(self):
        for calendar in (None, (NEXT, NEXT)):
            est = E.estimate(worked_commits(), [], worked_meetings(), [DAY], self.P, calendar=calendar, now=NOW)
            day = est.days[DAY]
            self.assertEqual((day.policy, day.meeting_minutes), ("independent", 0))
            self.assertIn("No calendar data for this day, so meetings aren't taken into account", day.flags)

    def test_old_days_say_the_reflog_is_gone(self):
        est = E.estimate([c(1, t(1, 10), "PROJ-1")], [], [], [DAY], self.P, reflog_since=t(2, 9), now=NOW)
        self.assertIn("Git's reflog doesn't reach back to this day, so sessions start 30m before their first commit",
                      est.days[DAY].flags)

    def test_weekends_are_flagged(self):
        saturday = dt.date(2026, 10, 3)
        est = E.estimate([c(1, t(3, 10), "PROJ-1")], [], [], [saturday], self.P, now=t(3, 18))
        self.assertIn("A weekend day", est.days[saturday].flags)

    @unittest.skipUnless(hasattr(time, "tzset"), "needs a time zone the tests can set")
    def test_the_25_hour_day(self):
        fall_back = dt.date(2026, 11, 1)
        first = dt.datetime(2026, 11, 1, 5, 30, tzinfo=dt.timezone.utc)    # 01:30 EDT
        second = dt.datetime(2026, 11, 1, 6, 30, tzinfo=dt.timezone.utc)   # 01:30 EST, an hour later
        est = E.estimate([c(1, first, "PROJ-1"), c(2, second, "PROJ-1")], [], [], [fall_back], self.P,
                         now=first + dt.timedelta(days=1))
        self.assertEqual([x.minutes for x in est.parts], [90])
        self.assertEqual(E.local_midnight(dt.date(2026, 11, 2)) - E.local_midnight(fall_back), dt.timedelta(hours=25))

    def test_rounding_forgives_float_noise_but_never_rounds_up(self):
        self.assertEqual(E.round_down(102.5, 15), 90)
        self.assertEqual(E.round_down(29.999999999999, 15), 30)
        self.assertEqual(E.round_down(29.9, 15), 15)
        self.assertEqual(E.fmt(135), "2h15m")
        self.assertEqual(E.fmt(32.5), "32m")


class DirectionTests(unittest.TestCase):
    """No cap, weight, scale or rounding raises a number, over random days."""

    def random_day(self, rng):
        base = t(1, 7)
        pool = [("PROJ-1",), ("PROJ-2",), ("PROJ-1", "PROJ-2"), ()]
        commits = [c(i, base + dt.timedelta(minutes=rng.randint(0, 14 * 60)), *rng.choice(pool), repo=rng.choice("ab"))
                   for i in range(rng.randint(1, 14))]
        meetings = []
        for i in range(rng.randint(0, 6)):
            start = base + dt.timedelta(minutes=rng.randint(0, 12 * 60))
            meetings.append(E.Meeting(str(i), start, start + dt.timedelta(minutes=rng.choice([15, 30, 60, 90, 180]))))
        p = E.Params(policy=rng.choice(config.POLICIES), idle_gap_minutes=rng.choice([30, 60, 120, 180]),
                     lead_in_minutes=rng.choice([0, 15, 30, 60]), max_daily_dev_minutes=rng.choice([120, 240, 480]),
                     ambient_weight=rng.choice([0.0, 0.3, 0.5, 1.0]), concurrent_fraction=rng.choice([0.0, 0.5, 1.0]))
        return commits, meetings, p

    def run_day(self, commits, meetings, p):
        return E.estimate(commits, [], meetings, [DAY], p, calendar=(DAY, DAY), now=NOW)

    def test_every_step_keeps_or_lowers(self):
        rng = random.Random(1001)
        for _ in range(300):
            commits, meetings, p = self.random_day(rng)
            est = self.run_day(commits, meetings, p)
            day = est.days[DAY]
            self.assertLessEqual(day.weighted, sum(x.minutes for x in est.parts) + 1e-9)
            self.assertLessEqual(day.counted, min(day.weighted, day.cap) + 1e-9)
            for x in est.parts:
                self.assertLessEqual(x.counted, x.minutes + 1e-9)
                self.assertAlmostEqual(sum(x.shares.values()), x.counted)
            for prop in est.proposals:
                self.assertLessEqual(prop.minutes_proposed, prop.minutes_raw)
                self.assertEqual(prop.minutes_proposed % p.round_to_minutes, 0)
            self.assertAlmostEqual(sum(x.minutes_raw for x in est.proposals), day.counted, places=6)

    def test_ambient_discounts_meeting_time_once(self):
        rng = random.Random(7)
        for _ in range(200):
            commits, meetings, p = self.random_day(rng)
            p = dataclasses.replace(p, policy="ambient")
            est = self.run_day(commits, meetings, p)
            single = sum(x.minutes - x.ambient * (1 - p.ambient_weight) for x in est.parts)
            self.assertAlmostEqual(est.days[DAY].weighted, single)
            self.assertAlmostEqual(est.days[DAY].counted, min(single, p.max_daily_dev_minutes))

    def test_another_meeting_never_raises_the_day(self):
        rng = random.Random(42)
        for _ in range(200):
            commits, meetings, p = self.random_day(rng)
            extra = E.Meeting("x", t(1, rng.randint(7, 18)), t(1, 19))
            before = self.run_day(commits, meetings, p).days[DAY].counted
            after = self.run_day(commits, meetings + [extra], p).days[DAY].counted
            self.assertLessEqual(after, before + 1e-9)


# --------------------------------------------------------------------------
# Muninn: the store, approvals and posting
# --------------------------------------------------------------------------

class MuninnCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)   # after the test's own cleanups (open files)
        # Resolved, because Baldur stores resolved paths: macOS's temp folder is under /var, a link
        # to /private/var, and Windows can hand out an 8.3 short name such as C:\Users\BRANDO~1.
        self.dir = Path(self.tmp.name).resolve()
        self._home = os.environ.get("ASGARD_HOME")
        os.environ["ASGARD_HOME"] = str(self.dir)
        muninn.prepare(self.dir / "muninn.db", backups=self.dir / "backups")
        self.con = muninn.connect(self.dir / "muninn.db")
        # A cleanup, not tearDown: it also runs when a subclass's setUp skips, and before the folder goes.
        self.addCleanup(lambda: self.con.close())
        self.settings = config.Settings(config.validate({"project_keys": ["PROJ", "OPS"], "history_days": 3650}))
        self.source = muninn.ensure_source(self.con, "git", "local-git")
        muninn.add_identity(self.con, "git_email", "brandon@agency.gov")
        self.repo = self.add_repo("asgard")
        self.n = 0

    def tearDown(self):
        if self._home is None:
            os.environ.pop("ASGARD_HOME", None)
        else:
            os.environ["ASGARD_HOME"] = self._home

    def add_repo(self, name, active=1):
        return int(self.con.execute("INSERT INTO repos (source_id, name, local_path, active) VALUES (?, ?, ?, ?) "
                                    "RETURNING id", (self.source, name, str(self.dir / name), active)).fetchone()[0])

    def commit(self, at, *ks, repo=None, subject=None, branch=None, mine=1):
        self.n += 1
        ts = muninn.to_ts(at)
        cid = int(self.con.execute(
            "INSERT INTO commits (repo_id, sha, author_name, author_email, authored_at, committed_at, subject, "
            "is_mine, branch_hint, first_seen_at) VALUES (?, ?, 'Brandon', 'brandon@agency.gov', ?, ?, ?, ?, ?, ?) "
            "RETURNING id", (repo or self.repo, f"{self.n:040x}", ts, ts, subject or f"change {self.n}", mine, branch,
                             ts)).fetchone()[0])
        for k in ks:
            self.con.execute("INSERT INTO commit_work_items (commit_id, work_item_key, method) VALUES (?, ?, 'message')",
                             (cid, k))
        return cid

    def calendar(self, *spans):
        cal = muninn.ensure_source(self.con, "calendar", "outlook")
        with muninn.Run(self.con, "odin", cal, "calendar") as run:
            for n, (start, end) in enumerate(spans):
                odin.upsert_calendar_event(run, {
                    "external_id": f"m{n}", "title": f"Meeting {n}", "starts_at": muninn.to_ts(start),
                    "ends_at": muninn.to_ts(end), "is_all_day": 0, "show_as": "busy", "response": "accepted",
                    "is_cancelled": 0})

    def worked_example(self):
        for h, m in WORKED_42:
            self.commit(t(1, h, m), "PROJ-42")
        for h, m in WORKED_51:
            self.commit(t(1, h, m), "PROJ-51")
        self.calendar(*[(t(1, *a), t(1, *b)) for a, b in MEETINGS])

    def run_store(self, first=DAY, last=DAY):
        return store.run(self.con, self.settings, first, last, now=NOW)

    def rows(self, status=None):
        sql = "SELECT * FROM day_proposals" + (" WHERE status = ?" if status else "") + " ORDER BY local_date, work_item_key, id"
        return self.con.execute(sql, (status,) if status else ()).fetchall()

    def open_ids(self):
        return {r["work_item_key"]: r["id"] for r in self.rows("proposed")}

    def events(self, kind):
        return [json.loads(r[0]) for r in self.con.execute("SELECT payload FROM events WHERE kind = ? ORDER BY id", (kind,))]

    def jira(self, *ks):
        """Odin has synced these issues; returns (source, context)."""
        sid = muninn.ensure_source(self.con, "jira", "jira-dc", "https://jira.example.gov")
        muninn.add_identity(self.con, "jira_user", "bdoe", sid)
        ctx = odin.JiraContext.load(self.con, sid)
        with muninn.Run(self.con, "odin", sid, "issues") as run:
            for n, k in enumerate(ks):
                odin.upsert_issue(run, {"id": str(1000 + n), "key": k, "fields": {
                    "summary": f"Work on {k}", "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
                    "project": {"key": k.split("-")[0]}, "assignee": {"name": "bdoe"},
                    "created": "2026-09-01T08:00:00.000-0400", "updated": "2026-09-30T08:00:00.000-0400"}}, ctx)
        return sid, ctx

    def by_hand(self, sid, ctx, issue_n, minutes, wid="w1"):
        """You logged time in Jira yourself that day."""
        with muninn.Run(self.con, "odin", sid, "worklogs") as run:
            odin.upsert_worklog(run, {"id": wid, "issueId": str(1000 + issue_n), "author": {"name": "bdoe"},
                                      "started": odin.jira_time(muninn.to_ts(t(1, 8))), "timeSpentSeconds": minutes * 60,
                                      "comment": "by hand", "created": "2026-10-01T18:00:00.000-0400"}, ctx)

    def post_all(self):
        """What Odin's posting job does, with Jira saying yes."""
        posted = []
        for n, due in enumerate(odin.posts_due(self.con)):
            post = odin.begin_post(self.con, due["proposal_id"])
            odin.finish_post(self.con, post.worklog_id, f"9{n}{post.worklog_id}")
            posted.append(post)
        return posted


class StoreTests(MuninnCase):
    def test_the_worked_example_is_stored_with_its_basis(self):
        self.worked_example()
        result = self.run_store()
        rows = self.rows()
        self.assertEqual([(r["work_item_key"], r["minutes_raw"], r["minutes_proposed"], r["status"]) for r in rows],
                         [("PROJ-42", 102.5, 90, "proposed"), ("PROJ-51", 35.0, 30, "proposed")])
        self.assertEqual(rows[0]["basis"].splitlines(), [
            "Baldur estimate: 1h30m for PROJ-42 on 2026-10-01",
            "Basis: 6 commits in 2 sessions (09:20-12:25, 14:00-15:05), repo asgard",
            f"Model: baldur-1, policy ambient 0.5, gap 120m, lead-in 30m, rounded down to 15m (run {result.run_id})",
            "Meetings that day: 7h00m"])
        sessions = self.con.execute("SELECT focused_minutes, ambient_minutes, counted_minutes, commit_count, start_basis "
                                    "FROM work_sessions ORDER BY started_at").fetchall()
        self.assertEqual([tuple(r) for r in sessions], [(25, 160, 105, 6, "lead_in"), (0, 65, 32.5, 2, "lead_in")])
        shares = sorted(tuple(r) for r in self.con.execute("SELECT work_item_key, minutes FROM session_allocations"))
        self.assertEqual(shares, [("PROJ-42", 32.5), ("PROJ-42", 70.0), ("PROJ-51", 35.0)])
        self.assertEqual(self.events("estimate_run.created"), [{"date_from": "2026-10-01", "date_to": "2026-10-01",
                                                                "proposals": 2, "withdrawn": 0}])
        params = json.loads(self.con.execute("SELECT params FROM estimate_runs").fetchone()[0])
        self.assertEqual((params["policy"], params["ambient_weight"]), ("ambient", 0.5))

    def test_a_rerun_with_nothing_new_stores_nothing(self):
        self.worked_example()
        self.run_store()
        again = self.run_store()
        self.assertIsNone(again.run_id)
        self.assertEqual({x.action for x in again.changes}, {"unchanged"})
        self.assertEqual((len(self.rows()), self.con.execute("SELECT count(*) FROM estimate_runs").fetchone()[0]), (2, 1))

    def test_new_settings_change_the_hash_and_supersede_open_rows(self):
        self.worked_example()
        self.run_store()
        before = {r["work_item_key"]: r["basis_hash"] for r in self.rows("proposed")}
        self.settings.values["ambient_weight"] = 0.4
        result = self.run_store()
        after = {r["work_item_key"]: (r["basis_hash"], r["minutes_proposed"]) for r in self.rows("proposed")}
        self.assertEqual({k: v[1] for k, v in after.items()}, {"PROJ-42": 75, "PROJ-51": 15})
        self.assertTrue(all(after[k][0] != before[k] for k in before))
        self.assertEqual([x.action for x in result.changes], ["updated", "updated"])
        self.assertEqual(len(self.rows("superseded")), 2)

    def test_evidence_that_keeps_the_number_leaves_an_approval_alone(self):
        self.worked_example()
        self.run_store()
        approvals.approve_day(self.con, DAY.isoformat())
        self.commit(t(1, 14, 45))      # an untracked commit in session 2: PROJ-42 is still 1h30m
        result = self.run_store()
        self.assertEqual({(x.key, x.action) for x in result.changes},
                         {("PROJ-42", "unchanged"), ("PROJ-51", "unchanged"), (None, "new")})
        self.assertEqual([r["work_item_key"] for r in self.rows("approved")], ["PROJ-42", "PROJ-51"])

    def test_a_rejected_number_isnt_proposed_again(self):
        self.worked_example()
        self.run_store()
        approvals.reject(self.con, self.open_ids()["PROJ-51"])
        self.settings.values["lead_in_minutes"] = 25        # new hash, but PROJ-51 is still 30m
        self.run_store()
        self.assertNotIn("PROJ-51", self.open_ids())
        self.settings.values["round_to_minutes"] = 5         # now 35m: new information
        self.settings.values["lead_in_minutes"] = 30
        self.run_store()
        self.assertEqual(self.con.execute("SELECT minutes_proposed FROM day_proposals WHERE id = ?",
                                          (self.open_ids()["PROJ-51"],)).fetchone()[0], 35)

    def test_a_ticket_left_with_no_time_is_withdrawn(self):
        self.worked_example()
        self.run_store()
        for cid, in self.con.execute("SELECT commit_id FROM commit_work_items WHERE work_item_key = 'PROJ-51'").fetchall():
            collect.set_keys(self.con, [cid], ["PROJ-42"])
        result = self.run_store()
        self.assertEqual(sorted((x.key, x.action) for x in result.changes), [("PROJ-42", "updated"), ("PROJ-51", "withdrawn")])
        self.assertEqual(list(self.open_ids()), ["PROJ-42"])
        self.assertEqual(self.events("estimate_run.created")[-1]["withdrawn"], 1)

    def test_untracked_time_is_stored_but_never_approved(self):
        self.commit(t(1, 10))
        self.commit(t(1, 10, 30), "PROJ-1")
        self.run_store()
        untracked = self.con.execute("SELECT id, minutes_raw FROM day_proposals WHERE work_item_key IS NULL").fetchone()
        self.assertEqual(untracked["minutes_raw"], 30)
        self.assertEqual(len(approvals.approve_day(self.con, DAY.isoformat())), 1)
        with self.assertRaisesRegex(muninn.MuninnError, "Untracked"):
            approvals.approve(self.con, untracked["id"])

    def test_a_lead_in_before_midnight_is_stored_on_its_own_day(self):
        self.commit(t(2, 0, 10), "PROJ-1")
        self.run_store(DAY, NEXT)
        # The 20 minutes before midnight make a proposal; the 10 after round to nothing, so only
        # the day with a proposal keeps its sessions, borrowing the next day's commit.
        sessions = self.con.execute("SELECT local_date, commit_count FROM work_sessions ORDER BY started_at").fetchall()
        self.assertEqual([tuple(r) for r in sessions], [("2026-10-01", 1)])
        self.assertEqual([(r["local_date"], r["minutes_proposed"]) for r in self.rows()], [("2026-10-01", 15)])

    def test_a_chain_of_commits_is_read_whole(self):
        for hh, day in ((22, 1), (23, 1), (1, 2), (2, 2)):
            self.commit(t(day, hh, 30), "PROJ-1")
        whole = store.compute(self.con, self.settings, DAY, NEXT, now=NOW)
        for first, last in ((DAY, DAY), (NEXT, NEXT)):
            alone = store.compute(self.con, self.settings, first, last, now=NOW)
            self.assertEqual([(x.start, x.end) for x in alone.parts],
                             [(x.start, x.end) for x in whole.parts if x.local_date == first])
        self.assertEqual([(x.start, x.end) for x in whole.parts], [(t(1, 22, 30), t(2, 0)), (t(2, 0), t(2, 2, 30))])

    def test_switched_off_repositories_are_left_out(self):
        other = self.add_repo("vendor", active=0)
        self.commit(t(1, 10), "PROJ-1", repo=other)
        self.assertEqual(self.run_store().changes, [])

    def test_calendar_coverage_decides_the_policy(self):
        self.commit(t(1, 10), "PROJ-1")
        est = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
        self.assertEqual(est.days[DAY].policy, "independent")
        self.calendar((t(1, 9), t(1, 9, 30)))
        est = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
        self.assertEqual((est.days[DAY].policy, est.days[DAY].meeting_minutes), ("ambient", 30))

    def test_checkouts_in_the_reflog_set_session_starts(self):
        self.commit(t(1, 10), "PROJ-1", branch="feature/PROJ-1-x")
        self.con.execute("INSERT INTO reflog_entries (repo_id, at, action, sha, message) VALUES (?, ?, 'checkout', ?, ?)",
                         (self.repo, muninn.to_ts(t(1, 9)), "e" * 40, "checkout: moving from main to feature/PROJ-1-x"))
        self.run_store()
        row = self.con.execute("SELECT started_at, start_basis FROM work_sessions").fetchone()
        self.assertEqual(tuple(row), (muninn.to_ts(t(1, 9)), "reflog"))
        self.assertIn("(09:00-10:00 from checkout)", self.rows()[0]["basis"])
        self.assertEqual(store.checkout_branch("checkout: moving from feature/x to 1a2b3c4d"), None)


class DeltaBase(MuninnCase):
    """The worked example, with Odin having synced both tickets."""

    def setUp(self):
        super().setUp()
        self.worked_example()
        self.sid, self.ctx = self.jira("PROJ-42", "PROJ-51")


class DeltaTests(DeltaBase):
    """Approved time reaches Jira once, as the difference from what it holds."""

    def test_only_the_difference_is_posted(self):
        self.by_hand(self.sid, self.ctx, 0, 30)               # 30m on PROJ-42, logged by hand
        self.run_store()
        pid = approvals.approve(self.con, self.open_ids()["PROJ-42"], 120)
        self.assertEqual([(d["key"], d["minutes_to_post"]) for d in odin.posts_due(self.con)], [("PROJ-42", 90)])
        post = odin.begin_post(self.con, pid)
        lines = post.comment.splitlines()
        self.assertEqual(lines[0], "Baldur estimate: 1h30m for PROJ-42 on 2026-10-01")
        self.assertIn("Already in Jira: 30m, logged by hand; this worklog adds 1h30m", lines)
        self.assertTrue(lines[-2].startswith("Approved: 2h00m on "))
        self.assertEqual(lines[-1], post.marker)
        odin.finish_post(self.con, post.worklog_id, "88001")
        newer = approvals.change_approval(self.con, pid, 150)
        self.assertEqual([(d["proposal_id"], d["minutes_to_post"]) for d in odin.posts_due(self.con)], [(newer, 30)])
        self.post_all()
        self.run_store()                                       # nothing new: nothing more to post
        self.assertEqual(odin.posts_due(self.con), [])
        self.assertNotIn("PROJ-42", self.open_ids())

    def test_a_lower_estimate_never_retracts(self):
        self.run_store()
        approvals.approve(self.con, self.open_ids()["PROJ-42"])
        self.post_all()
        self.settings.values["lead_in_minutes"] = 0            # PROJ-42 drops to 1h15m
        self.run_store()
        lower = self.open_ids()["PROJ-42"]
        approvals.approve(self.con, lower)
        self.assertEqual(odin.posts_due(self.con), [])
        status = self.con.execute("SELECT approved_minutes, logged_minutes FROM v_day_status "
                                  "WHERE work_item_key = 'PROJ-42'").fetchone()
        self.assertEqual(tuple(status), (75, 90))
        text = report.render_day(self.con, store.compute(self.con, self.settings, DAY, DAY, now=NOW), DAY,
                                 self.settings)
        self.assertIn("Jira holds 15m more, which Baldur leaves alone", text)

    def test_the_day_report(self):
        self.by_hand(self.sid, self.ctx, 0, 30)
        self.run_store()
        est = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
        text = report.render_day(self.con, est, DAY, self.settings,
                                 plan=store.plan(self.con, self.settings, est, DAY, DAY))
        for expected in ("policy: ambient 0.5",
                         "Meetings (calendar)                   7h00m",
                         "Development (estimated from git)      2h00m   PROJ-42 1h30m, PROJ-51 30m",
                         "Already in Jira for these tickets       30m   PROJ-42 30m logged by hand",
                         "Odin will post once approved          1h30m   PROJ-42 1h00m, PROJ-51 30m",
                         "Accounted                             9h00m",
                         "Tour of duty                          8h00m   07:30-16:00, 30m unpaid",
                         "Overlap assumed                       1h00m   coding during calls",
                         "Not visible to Baldur"):
            self.assertIn(expected, text)
        self.assertNotIn("run estimate", text)
        self.commit(t(1, 16), "PROJ-51")                       # collected since the estimate
        est = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
        text = report.render_day(self.con, est, DAY, self.settings,
                                 plan=store.plan(self.con, self.settings, est, DAY, DAY))
        self.assertIn("(run estimate)", text)


# --------------------------------------------------------------------------
# The collector, on real repositories
# --------------------------------------------------------------------------

class Repo:
    def __init__(self, path):
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        for k, v in (("user.email", "brandon@agency.gov"), ("user.name", "Brandon"), ("commit.gpgsign", "false")):
            self.git("config", k, v)
        self.n = 0

    def git(self, *args, at=None, env=None):
        e = dict(os.environ)
        if at:
            e.update(GIT_AUTHOR_DATE=at, GIT_COMMITTER_DATE=at)
        e.update(env or {})
        return subprocess.run([GIT, "-C", str(self.path), *args], env=e, check=True, capture_output=True,
                              text=True).stdout

    def commit(self, at, message, author=None, committer=None):
        self.n += 1
        (self.path / f"f{self.n}.txt").write_text(f"{self.n}\n")
        self.git("add", f"f{self.n}.txt")
        env = {}
        if author:
            env.update(GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1])
        if committer:
            env.update(GIT_COMMITTER_NAME=committer[0], GIT_COMMITTER_EMAIL=committer[1])
        self.git("commit", "-q", "-m", message, at=at, env=env)
        return self.git("rev-parse", "HEAD").strip()


@unittest.skipUnless(GIT, "git isn't installed")
class CollectBase(MuninnCase):
    """A real repository in a temporary folder, with git's own config files out of the way."""

    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
        (self.dir / "gitconfig").write_text("")
        os.environ.update(GIT_CONFIG_GLOBAL=str(self.dir / "gitconfig"), GIT_CONFIG_NOSYSTEM="1")
        muninn.add_identity(self.con, "git_email", "brandon@agency.gov")
        self.git = Repo(self.dir / "src" / "asgard")

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def worked_repo(self):
        g = self.git
        g.commit("2026-09-30T15:00:00", "initial", author=("Sam", "sam@agency.gov"))
        g.git("checkout", "-q", "-b", "feature/PROJ-42-retry", at="2026-09-30T15:30:00")
        g.git("checkout", "-q", "main", at="2026-09-30T15:31:00")
        g.git("checkout", "-q", "-b", "feature/PROJ-51-form", at="2026-09-30T15:32:00")
        g.git("checkout", "-q", "feature/PROJ-42-retry", at="2026-09-30T15:33:00")
        for h, m in WORKED_42[:4]:
            g.commit(f"2026-10-01T{h:02d}:{m:02d}:00", "retry work")
        g.git("checkout", "-q", "feature/PROJ-51-form", at="2026-10-01T12:05:00")
        for h, m in WORKED_51:
            g.commit(f"2026-10-01T{h:02d}:{m:02d}:00", "form work")
        g.git("checkout", "-q", "feature/PROJ-42-retry", at="2026-10-01T12:27:00")
        for h, m in WORKED_42[4:]:
            g.commit(f"2026-10-01T{h:02d}:{m:02d}:00", "retry work")

    def collect(self, **kw):
        result = collect.collect(self.con, self.settings, only=[self.git.path], **kw)
        self.assertEqual(result.failed, [])
        return result.repos[0]


class CollectTests(CollectBase):
    def test_your_commits_with_keys_from_the_reflog(self):
        self.worked_repo()
        g = self.git
        g.commit("2026-10-01T15:30:00", "teammate's work", author=("Sam", "sam@agency.gov"))
        g.commit("2026-10-01T15:35:00", "same name, someone else", author=("Brandon", "brandon.k@contractor.example"))
        g.commit("2026-10-01T15:40:00", "Retry on 503 (#12)", committer=("GitHub", "noreply@github.com"))
        g.commit("2026-10-01T15:50:00", "pairing\n\nCo-authored-by: Brandon <brandon@agency.gov>",
                 author=("Sam", "sam@agency.gov"))
        res = self.collect()
        self.assertEqual((res.commits_new, res.keyed, res.skipped_copies, res.coauthored), (8, 8, 1, 1))
        rows = self.con.execute("SELECT c.subject, c.branch_hint, k.work_item_key, k.method FROM commits c "
                                "JOIN commit_work_items k ON k.commit_id = c.id ORDER BY c.authored_at").fetchall()
        self.assertEqual({tuple(r) for r in rows}, {("retry work", "feature/PROJ-42-retry", "PROJ-42", "reflog"),
                                                    ("form work", "feature/PROJ-51-form", "PROJ-51", "reflog")})
        self.assertEqual(self.con.execute("SELECT subject FROM commits WHERE is_mine = 0").fetchall()[0][0], "pairing")
        est = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
        self.assertEqual({x.key: x.minutes_proposed for x in est.proposals}, {"PROJ-42": 180, "PROJ-51": 60},
                         "no calendar yet, so independent")
        self.assertEqual(self.collect().commits_new, 0, "a second collection adds nothing")

    def test_a_cherry_pick_counts_once(self):
        g = self.git
        g.commit("2026-10-01T08:00:00", "initial", author=("Sam", "sam@agency.gov"))
        g.git("checkout", "-q", "-b", "feature/PROJ-1-x", at="2026-10-01T08:30:00")
        sha = g.commit("2026-10-01T09:00:00", "PROJ-1 fix")
        g.git("checkout", "-q", "main", at="2026-10-01T09:30:00")
        g.git("cherry-pick", sha, at="2026-10-01T09:31:00")
        self.collect()
        patches = [r[0] for r in self.con.execute("SELECT patch_id FROM commits WHERE is_mine = 1")]
        self.assertEqual((len(patches), len(set(patches))), (2, 1))
        est = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
        self.assertEqual(len(est.excluded), 1)

    def test_keys_saved_at_collection_survive_the_branch(self):
        g = self.git
        g.commit("2026-10-01T08:00:00", "initial", author=("Sam", "sam@agency.gov"))
        g.git("checkout", "-q", "-b", "feature/OPS-7-cleanup", at="2026-10-01T08:30:00")
        g.commit("2026-10-01T09:00:00", "cleanup")
        g.git("checkout", "-q", "main", at="2026-10-01T09:30:00")
        g.git("reflog", "expire", "--expire=now", "--all")      # as if the reflog were 90 days old
        self.collect()
        method = "SELECT k.work_item_key, k.method FROM commit_work_items k JOIN commits c ON c.id = k.commit_id"
        self.assertEqual([tuple(r) for r in self.con.execute(method)], [("OPS-7", "branch")])
        g.git("merge", "-q", "--ff-only", "feature/OPS-7-cleanup", at="2026-10-01T10:00:00")
        g.git("branch", "-q", "-d", "feature/OPS-7-cleanup")     # merged, then deleted
        g.git("reflog", "expire", "--expire=now", "--all")
        self.collect(full=True)                                   # sees the commit on main, with no branch evidence
        self.assertEqual([tuple(r) for r in self.con.execute(method)], [("OPS-7", "branch")])

    def test_new_project_keys_look_at_old_commits_again(self):
        self.settings.values["project_keys"] = ["PROJ"]
        g = self.git
        g.commit("2026-10-01T09:00:00", "OPS-5 rotate the logs")
        self.assertEqual(self.collect().keyed, 0)
        self.settings.values["project_keys"] = ["OPS", "PROJ"]
        self.assertEqual(self.collect().keyed, 1)
        self.assertEqual(self.con.execute("SELECT work_item_key, method FROM commit_work_items").fetchone()[:],
                         ("OPS-5", "message"))

    def test_no_identity_or_no_projects_is_refused(self):
        self.con.execute("DELETE FROM identities")
        with self.assertRaisesRegex(collect.CollectError, "which commits are yours"):
            collect.collect(self.con, self.settings, only=[self.git.path])
        muninn.add_identity(self.con, "git_email", "brandon@agency.gov")
        self.settings.values["project_keys"] = []
        with self.assertRaisesRegex(collect.CollectError, "project_keys"):
            collect.collect(self.con, self.settings, only=[self.git.path])

    def test_discovery_stays_under_the_roots(self):
        self.git.commit("2026-10-01T09:00:00", "x")
        (self.dir / "src" / "node_modules" / "pkg" / ".git").mkdir(parents=True)
        deep = Repo(self.dir / "src" / "a" / "b" / "c" / "d" / "deep")
        found = gitread.discover([str(self.dir / "src")], max_depth=3)
        self.assertEqual(found, [self.git.path])
        self.assertTrue(deep.path.exists())


# --------------------------------------------------------------------------
# Regressions found by the independent review
# --------------------------------------------------------------------------

class ReviewEstimatorTests(unittest.TestCase):
    P = E.Params()

    def test_the_same_change_in_two_projects_is_two_pieces_of_work(self):
        a = c(1, t(1, 9), "OPS-5", patch="p", subject="bump requests")
        b = dataclasses.replace(c(2, t(1, 9), "OPS-5", patch="p", subject="bump requests"), origin="svc-b")
        self.assertEqual(len(E.clean([a, b], NOW)[0]), 2)
        self.assertEqual(len(E.clean([a, dataclasses.replace(b, origin="")], NOW)[0]), 1)
        self.assertEqual(E.clean([a], NOW, prior=[("", "p")])[0], [], "an earlier copy outside the range")

    def test_a_clamped_start_isnt_called_a_checkout(self):
        first = c(1, t(1, 10), branch="feature/PROJ-1")
        s = E.build_sessions([first], [E.Checkout(t(1, 9, 58), "asgard", "feature/PROJ-1")], self.P)[0]
        self.assertEqual((s.start, s.start_basis), (t(1, 9, 45), "lead_in"))

    def test_work_after_the_last_calendar_sync_is_flagged(self):
        est = E.estimate([c(1, t(1, 15), "PROJ-1")], [], [], [DAY], self.P, calendar=(DAY, DAY),
                         calendar_synced=t(1, 12), now=NOW)
        self.assertIn("The calendar was last synced at 12:00; meetings after that aren't known yet", est.days[DAY].flags)

    def test_rebases_of_every_kind_keep_their_branch(self):
        entries = [gitread.ReflogRec("t", sha, msg.split(":", 1)[0], msg) for sha, msg in [
            ("a1", "checkout: moving from main to feature/PROJ-1-retry"),
            ("a2", "commit: retry"),
            ("b0", "pull --rebase (start): checkout b0"),
            ("b1", "pull --rebase (pick): retry"),
            ("b1", "pull --rebase (finish): returning to refs/heads/feature/PROJ-1-retry"),
            ("c0", "rebase -i (start): checkout c0"),
            ("c1", "rebase -i (reword): PROJ-1 retry"),
            ("c2", "rebase -i (fixup): PROJ-1 retry"),
            ("c2", "rebase -i (finish): returning to refs/heads/feature/PROJ-1-retry"),
            ("d1", "rebase (pick): abandoned"),
            ("d0", "rebase (abort): returning to refs/heads/feature/PROJ-1-retry"),
            ("e0", "checkout: moving from feature/PROJ-1-retry to HEAD"),
            ("e1", "commit: detached")]]
        made = gitread.reflog_branches(entries)
        self.assertEqual({k: v for k, v in made.items()},
                         {sha: "feature/PROJ-1-retry" for sha in ("a2", "b1", "c1", "c2")})


class ReviewStoreTests(MuninnCase):
    def test_checkouts_belong_to_their_own_repository(self):
        review = self.add_repo("asgard-review")
        self.con.execute("UPDATE repos SET name = 'asgard' WHERE id = ?", (review,))    # a second clone, same name
        self.commit(t(1, 10), "PROJ-1", branch="feature/PROJ-1")
        self.con.execute("INSERT INTO reflog_entries (repo_id, at, action, sha, message) VALUES (?, ?, 'checkout', ?, ?)",
                         (review, muninn.to_ts(t(1, 8, 10)), "e" * 40, "checkout: moving from main to feature/PROJ-1"))
        est = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
        self.assertEqual([(x.start, x.start_basis) for x in est.parts], [(t(1, 9, 30), "lead_in")])

    def test_the_copy_with_the_best_evidence_wins(self):
        clone = self.add_repo("clone")
        for order in ((self.repo, clone), (clone, self.repo)):
            self.con.execute("DELETE FROM commit_work_items")
            self.con.execute("DELETE FROM commits")
            ids = {}
            for repo in order:
                ids[repo] = self.commit(t(1, 10), repo=repo, subject="part one")
                self.con.execute("UPDATE commits SET sha = ? WHERE id = ?", ("a" * 40, ids[repo]))
            self.con.execute("INSERT INTO commit_work_items VALUES (?, 'OPS-9', 'reflog')", (ids[self.repo],))
            for key in ("OPS-9", "OPS-10"):
                self.con.execute("INSERT INTO commit_work_items VALUES (?, ?, 'branch')", (ids[clone], key))
            est = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
            self.assertEqual({x.key: x.minutes_proposed for x in est.proposals}, {"OPS-9": 30})

    def test_an_earlier_copy_outside_the_range_still_counts_as_the_original(self):
        first = self.commit(t(1, 10), "PROJ-1", subject="retry")
        again = self.commit(t(2, 14), "PROJ-1", subject="retry, re-authored")
        self.con.execute("UPDATE commits SET patch_id = 'p1' WHERE id IN (?, ?)", (first, again))
        self.assertEqual(store.compute(self.con, self.settings, NEXT, NEXT, now=NOW).proposals, [])
        self.assertEqual(len(store.compute(self.con, self.settings, DAY, NEXT, now=NOW).proposals), 1)

    def test_a_moved_issue_is_one_ticket(self):
        self.jira("OPS-77")
        self.con.execute("INSERT INTO work_item_aliases (key, work_item_id, status, checked_at) VALUES "
                         "('PROJ-123', (SELECT id FROM work_items WHERE key = 'OPS-77'), 'moved', ?)", (muninn.utcnow(),))
        self.commit(t(1, 9), "PROJ-123")
        self.commit(t(1, 9, 30), "OPS-77")
        self.run_store()
        self.assertEqual([(r["work_item_key"], r["minutes_proposed"]) for r in self.rows()], [("OPS-77", 60)])

    def test_a_lead_in_longer_than_the_idle_gap(self):
        self.settings.values.update(idle_gap_minutes=30, lead_in_minutes=60)
        self.commit(t(1, 23, 30), "PROJ-1")
        self.commit(t(2, 0, 50), "PROJ-1")
        alone = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
        both = store.compute(self.con, self.settings, DAY, NEXT, now=NOW)
        self.assertEqual([(x.start, x.end) for x in alone.parts], [(x.start, x.end) for x in both.parts_on(DAY)])

    def test_settings_that_only_set_flags_keep_open_proposals(self):
        self.worked_example()
        self.run_store()
        self.settings.values["tour_of_duty"] = {"start": "08:00", "end": "16:30", "unpaid_minutes": 30}
        self.assertEqual({x.action for x in self.run_store().changes}, {"unchanged"})

    def test_a_superseded_proposal_points_to_its_replacement(self):
        self.worked_example()
        self.run_store()
        old = self.open_ids()["PROJ-42"]
        self.settings.values["ambient_weight"] = 0.4
        self.run_store()
        with self.assertRaisesRegex(muninn.MuninnError, f"Proposal {self.open_ids()['PROJ-42']} replaced it"):
            approvals.approve(self.con, old)

    def test_estimating_needs_an_email_and_projects(self):
        self.commit(t(1, 10), "PROJ-1")
        self.con.execute("DELETE FROM identities")
        with self.assertRaisesRegex(collect.CollectError, "which commits are yours"):
            self.run_store()
        muninn.add_identity(self.con, "git_email", "brandon@agency.gov")
        self.settings.values["project_keys"] = []
        with self.assertRaisesRegex(collect.CollectError, "project_keys"):
            self.run_store()

    def test_keys_of_projects_you_removed_stop_counting(self):
        self.commit(t(1, 10), "OPS-3")
        self.settings.values["project_keys"] = ["PROJ"]
        est = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
        self.assertEqual([x.key for x in est.proposals], [None])

    def test_removing_an_email_takes_its_commits_back(self):
        muninn.add_identity(self.con, "git_email", "team@agency.gov")
        for hh in (9, 10, 11):
            cid = self.commit(t(1, hh), "PROJ-7")
            self.con.execute("UPDATE commits SET author_email = 'team@agency.gov' WHERE id = ?", (cid,))
        self.commit(t(1, 15), "PROJ-7")
        self.run_store()
        with muninn.transaction(self.con):
            self.con.execute("DELETE FROM identities WHERE value = 'team@agency.gov'")
            self.assertEqual(collect.refresh_ownership(self.con, ["team@agency.gov"]), 3)
        est = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
        self.assertEqual({x.key: x.minutes_proposed for x in est.proposals}, {"PROJ-7": 30})
        kept = self.con.execute("SELECT count(*) FROM commits WHERE is_mine = 0").fetchone()[0]
        self.assertEqual(kept, 3, "the stored run's evidence stays, marked not yours")


class ReviewReportTests(DeltaBase):
    def test_a_worklog_deleted_in_jira_isnt_shown_as_due(self):
        self.run_store()
        approvals.approve(self.con, self.open_ids()["PROJ-42"])
        self.post_all()
        with muninn.Run(self.con, "odin", self.sid, "worklogs") as run:
            for jid, in self.con.execute("SELECT jira_worklog_id FROM worklogs").fetchall():
                odin.mark_worklog_deleted(run, jid)
        text = report.render_day(self.con, store.compute(self.con, self.settings, DAY, DAY, now=NOW), DAY,
                                 self.settings)
        self.assertNotIn("PROJ-42 1h30m", text.split("Odin will post")[1].splitlines()[0])
        self.assertIn("approved 1h30m, deleted in Jira", text)


class ReviewCollectTests(CollectBase):
    def keys_of(self):
        return {r[0]: r[1] for r in self.con.execute(
            "SELECT c.subject, group_concat(k.work_item_key || ':' || k.method) FROM commits c "
            "LEFT JOIN commit_work_items k ON k.commit_id = c.id WHERE c.is_mine = 1 GROUP BY c.id")}

    def test_a_stash_isnt_work(self):
        g = self.git
        g.commit("2026-10-01T08:00:00", "initial", author=("Sam", "sam@agency.gov"))
        g.commit("2026-10-01T09:30:00", "PROJ-1 retry")
        (g.path / "f1.txt").write_text("changed\n")
        g.git("stash", "-q", at="2026-10-01T11:50:00")
        self.collect()
        self.assertEqual(list(self.keys_of()), ["PROJ-1 retry"])

    def test_a_local_squash_of_collected_commits_is_skipped(self):
        g = self.git
        g.commit("2026-10-01T08:00:00", "initial", author=("Sam", "sam@agency.gov"))
        g.git("checkout", "-q", "-b", "feature/PROJ-2-x", at="2026-10-01T08:30:00")
        one = g.commit("2026-10-01T09:00:00", "first")
        two = g.commit("2026-10-01T09:30:00", "second")
        g.git("checkout", "-q", "main", at="2026-10-01T10:00:00")
        g.git("merge", "-q", "--squash", "feature/PROJ-2-x")
        g.git("commit", "-q", "-m", f"Squashed commit of the following:\n\ncommit {two}\n\ncommit {one}\n",
              at="2026-10-01T10:05:00")
        res = self.collect()
        self.assertEqual((res.commits_new, res.skipped_copies), (2, 1))

    def test_stacked_branches_go_to_the_smallest(self):
        g = self.git
        g.commit("2026-10-01T08:00:00", "initial", author=("Sam", "sam@agency.gov"))
        g.git("checkout", "-q", "-b", "feature/OPS-9-part-one", at="2026-10-01T08:30:00")
        g.commit("2026-10-01T09:00:00", "part one")
        g.git("checkout", "-q", "-b", "feature/OPS-10-part-two", at="2026-10-01T09:30:00")
        g.commit("2026-10-01T10:00:00", "part two")
        g.git("checkout", "-q", "main", at="2026-10-01T10:30:00")
        g.git("reflog", "expire", "--expire=now", "--all")
        self.collect()
        self.assertEqual(self.keys_of(), {"part one": "OPS-9:branch", "part two": "OPS-10:branch"})

    def test_a_worktree_is_the_same_repository(self):
        g = self.git
        g.commit("2026-10-01T08:00:00", "initial", author=("Sam", "sam@agency.gov"))
        g.commit("2026-10-01T09:00:00", "PROJ-1 main work")
        g.git("worktree", "add", "-q", "-b", "feature/PROJ-2-wt", str(self.dir / "src" / "asgard-wt"),
              at="2026-10-01T09:30:00")
        wt = self.dir / "src" / "asgard-wt"
        (wt / "w.txt").write_text("w\n")
        g.git("-C", str(wt), "add", "w.txt")
        g.git("-C", str(wt), "commit", "-q", "-m", "worktree work", at="2026-10-01T10:00:00")
        result = collect.collect(self.con, self.settings, only=[wt, g.path])
        self.assertEqual(len(result.repos), 1)
        self.assertEqual([r[0] for r in self.con.execute("SELECT local_path FROM repos WHERE local_path LIKE ?",
                                                          (str(self.dir / "src") + "%",))], [str(g.path.resolve())])
        self.assertEqual(self.con.execute("SELECT count(*) FROM reflog_entries WHERE ref <> 'HEAD'").fetchone()[0] > 0,
                         True, "the worktree's own reflog is kept")
        self.assertEqual(self.keys_of(), {"PROJ-1 main work": "PROJ-1:message", "worktree work": "PROJ-2:reflog"})

    def test_an_old_committer_date_doesnt_hide_newer_work(self):
        g = self.git
        g.commit("2026-10-01T08:00:00", "initial", author=("Sam", "sam@agency.gov"))
        g.commit("2026-10-01T09:00:00", "PROJ-1 recent work")
        (g.path / "z.txt").write_text("z\n")
        g.git("add", "z.txt")
        g.git("commit", "-q", "-m", "from a machine with a bad clock", env={"GIT_COMMITTER_DATE": "2010-01-01T00:00:00",
              "GIT_AUTHOR_DATE": "2010-01-01T00:00:00", "GIT_AUTHOR_EMAIL": "sam@agency.gov"})
        self.collect()
        self.assertIn("PROJ-1 recent work", self.keys_of())

    def test_a_review_only_row_folds_into_the_clone(self):
        g = self.git
        g.commit("2026-10-01T09:00:00", "PROJ-1 work")
        now = muninn.utcnow()
        self.con.execute("UPDATE repos SET local_path = ? WHERE id = ?", (str(g.path.resolve()), self.repo))
        review = self.con.execute("INSERT INTO repos (name, github_repo) VALUES ('asgard', 'team/asgard') "
                                  "RETURNING id").fetchone()[0]
        self.con.execute("INSERT INTO pull_requests (repo_id, number, title, author, head_ref, state, created_at, "
                         "updated_at, url, first_seen_at, last_seen_at) VALUES (?, 7, 't', 'sam', 'feature/x', 'open', "
                         "?, ?, 'u', ?, ?)", (review, now, now, now, now))
        g.git("remote", "add", "origin", "https://github.com/team/asgard.git")
        self.settings.values["github_api"] = config.github_api_url("github.com")
        self.collect()
        rows = [tuple(r) for r in self.con.execute("SELECT id, github_repo FROM repos WHERE github_repo IS NOT NULL")]
        self.assertEqual(rows, [(self.repo, "team/asgard")])
        self.assertEqual(self.con.execute("SELECT repo_id FROM pull_requests").fetchone()[0], self.repo)

    def test_a_malformed_time_zone_doesnt_stop_collection(self):
        g = self.git
        g.commit("2026-10-01T08:00:00", "initial", author=("Sam", "sam@agency.gov"))
        g.commit("2026-10-01T09:00:00", "PROJ-1 work")
        tree, parent = g.git("rev-parse", "HEAD^{tree}").strip(), g.git("rev-parse", "HEAD").strip()
        raw = (f"tree {tree}\nparent {parent}\nauthor Old Import <old@example.com> 1790000000 +5900\n"
               f"committer Old Import <old@example.com> 1790000000 +5900\n\nimported\n")
        # Bytes, not text: on Windows a text pipe turns \n into \r\n, which git can't parse as a commit.
        sha = subprocess.run([GIT, "-C", str(g.path), "hash-object", "-t", "commit", "-w", "--stdin", "--literally"],
                             input=raw.encode(), capture_output=True, check=True).stdout.decode().strip()
        g.git("update-ref", "refs/remotes/origin/legacy", sha)
        self.assertEqual(self.collect().commits_new, 1)

    def test_setup_finds_an_email_kept_in_an_included_config_file(self):
        # git config --global skips [include] files; the email in one was invisible to setup --from-git.
        (self.dir / "work.gitconfig").write_text("[user]\n\temail = brandon@agency.gov\n")
        (self.dir / "gitconfig").write_text(f"[include]\n\tpath = {(self.dir / 'work.gitconfig').as_posix()}\n")
        here = os.getcwd()
        os.chdir(self.dir)              # not inside a repository, so only system and global config apply
        try:
            self.assertEqual(gitread.configured_identity()[0], "brandon@agency.gov")
        finally:
            os.chdir(here)


# --------------------------------------------------------------------------
# GitHub Enterprise Server (Brandon, 2026-10-04)
# --------------------------------------------------------------------------

class EnterpriseServerTests(CollectBase):
    GHES = "https://github.agency.gov/api/v3"

    def test_github_api_takes_the_servers_host_and_stores_its_api_address(self):
        for typed, stored in (("github.agency.gov", self.GHES), (" https://GitHub.Agency.gov/ ", self.GHES),
                              (self.GHES + "/", self.GHES), ("github.agency.gov:8443", "https://github.agency.gov:8443/api/v3"),
                              ("github.com", "https://api.github.com"), ("https://api.github.com", "https://api.github.com"),
                              ("octocorp.ghe.com", "https://api.octocorp.ghe.com"),
                              ("https://api.octocorp.ghe.com", "https://api.octocorp.ghe.com"), ("", ""), ("  ", "")):
            self.assertEqual(config.validate({"github_api": typed})["github_api"], stored, typed)
            self.assertEqual(config.github_api_url(stored), stored, "storing it again changes nothing")
        for host, api in (("github.agency.gov", self.GHES), ("github.com", "https://api.github.com"),
                          ("octocorp.ghe.com", "https://api.octocorp.ghe.com"), (None, "")):
            self.assertEqual(config.github_host(api), host)
        for typed, stored in (("github.agency.gov:443", self.GHES), ("git_hub.agency.gov", "https://git_hub.agency.gov/api/v3")):
            self.assertEqual(config.github_api_url(typed), stored, typed)
        self.assertEqual(config.Settings(dict(config.DEFAULTS, github_api="github.agency.gov")).github_host(),
                         "github.agency.gov", "even before validation")
        for bad in ("http://github.agency.gov", "github.agency.gov/orgs/team", "https://github.agency.gov/api/v4",
                    "https://x:ghp_secret@github.agency.gov", "https://ghp_secret@github.agency.gov\uff0f",
                    "https://[ghp_secret]", "https://[::1", "github.agency.gov?x=1#ghp_secret",
                    "github.agency.gov:port", "github.agency.gov:0", "github.agency.gov:", "github.agency.gov:+443",
                    "github.agency.gov: 443", "github.agency.gov:\u0664\u0664\u0663", "github.com/api/v3",
                    "gist.github.com", "ssh.github.com", "ghe.com", "api.ghe.com", "api.api.ghe.com", 42,
                    "github.agency.gov\n/api/v3"):       # second review: $ matched before the \n
            with self.assertRaises(config.SettingsError, msg=repr(bad)) as caught:
                config.validate({"github_api": bad})
            self.assertNotIn("ghp_secret", str(caught.exception), "a token in the address is never repeated")

    def test_a_bad_github_api_turns_github_off_not_baldur(self):
        path = config.settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"project_keys": ["PROJ"], "github_api": "https://x:ghp_secret@github.agency.gov"}))
        loaded = config.load()
        self.assertFalse(loaded.broken, "github_api never changes a number, so estimates still run")
        self.assertEqual((loaded.github_api, loaded.github_host(), loaded.project_keys), ("", None, ["PROJ"]))
        self.assertTrue(any("Pull request features are off" in w for w in loaded.warnings), loaded.warnings)
        self.assertNotIn("ghp_secret", " ".join(loaded.warnings))

    def test_only_remotes_on_your_github_are_github_repositories(self):
        # The same owner/name on two hosts: github_repo is unique and has no host in it.
        other = Repo(self.dir / "src" / "dotcom")
        for repo, url in ((self.git, "git@github.agency.gov:team/asgard.git"),
                          (other, "https://github.com/team/asgard.git")):
            repo.commit("2026-10-01T09:00:00", "PROJ-1 work")
            repo.git("remote", "add", "origin", url)

        def collect_as(github_api, *order):
            self.settings.values["github_api"] = config.github_api_url(github_api)
            collect.collect(self.con, self.settings, only=list(order))
            return [self.con.execute("SELECT github_repo FROM repos WHERE local_path = ?",
                                     (str(r.path.resolve()),)).fetchone()[0] for r in (self.git, other)]

        self.assertEqual(collect_as("", self.git.path, other.path), [None, None],
                         "with github_api empty, no remote is a GitHub repository")
        self.assertEqual(collect_as("github.com", self.git.path, other.path), [None, "team/asgard"])
        self.assertEqual(collect_as("github.agency.gov", other.path, self.git.path), ["team/asgard", None],
                         "switching to the server moves the name to its clone, whichever folder is read first")
        self.assertEqual(collect_as("github.com", self.git.path, other.path), [None, "team/asgard"])
        self.assertEqual(collect_as("", other.path), [None, None], "turning GitHub off clears every clone")
        self.assertEqual(gitread.parse_remote("ssh://git@github.agency.gov:2222/team/asgard.git"),
                         ("github.agency.gov", "team/asgard"))

    def test_a_squash_merge_on_enterprise_server_is_a_copy(self):
        g = self.git
        g.commit("2026-10-01T08:00:00", "initial", author=("Sam", "sam@agency.gov"))
        g.commit("2026-10-01T09:00:00", "PROJ-1 retry")
        g.commit("2026-10-01T10:00:00", "PROJ-1 retry (#7)", committer=("GitHub Enterprise", "noreply@github.agency.gov"))
        g.commit("2026-10-01T10:30:00", "PROJ-1 form (#8)", committer=("GitHub Enterprise", "no-reply@agency.gov"))
        g.commit("2026-10-01T11:00:00", "PROJ-1 docs (#9)", committer=("GitHub", "web-flow@agency.gov"))
        res = self.collect()
        self.assertEqual((res.commits_new, res.skipped_copies), (1, 3), "whatever the server's no-reply address")
        rows = self.con.execute("SELECT subject, is_merge, (SELECT count(*) FROM commit_work_items k "
                                "WHERE k.commit_id = c.id) FROM commits c WHERE is_mine = 1 ORDER BY authored_at")
        self.assertEqual([tuple(r) for r in rows], [("PROJ-1 retry", 0, 1), ("PROJ-1 retry (#7)", 1, 0),
                                                    ("PROJ-1 form (#8)", 1, 0), ("PROJ-1 docs (#9)", 1, 0)],
                         "copies are kept only as times: is_merge = 1 and no keys")
        est = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
        self.assertEqual([k.subject for x in est.parts for k in x.commits], ["PROJ-1 retry"])

    def test_a_file_edited_on_enterprise_server_still_counts(self):
        g = self.git
        g.commit("2026-10-01T08:00:00", "initial", author=("Sam", "sam@agency.gov"))
        g.commit("2026-10-01T09:00:00", "PROJ-2 Update README.md",
                 committer=("GitHub Enterprise", "noreply@github.agency.gov"))
        g.commit("2026-10-01T09:30:00", "PROJ-2 fix the link (#3)", committer=("Brandon", "brandon@agency.gov"))
        res = self.collect()
        self.assertEqual((res.commits_new, res.skipped_copies), (2, 0),
                         "only a pull request's squash is a copy; a web edit or your own (#3) subject is work")

    def test_a_squash_between_two_stretches_doesnt_raise_the_day(self):
        # Review finding #20, on a real repository: the reviewer's day went from 4h00m to 5h00m.
        g = self.git
        g.commit("2026-10-01T08:00:00", "initial", author=("Sam", "sam@agency.gov"))
        for at in ("09:00", "11:00", "13:30", "15:30"):
            g.commit(f"2026-10-01T{at}:00", f"PROJ-1 work at {at}")
        g.commit("2026-10-01T12:15:00", "PROJ-1 retry (#3)", committer=("GitHub Enterprise", "no-reply@agency.gov"))
        self.assertEqual(self.collect().skipped_copies, 1)
        est = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
        self.assertEqual({x.key: x.minutes_proposed for x in est.proposals}, {"PROJ-1": 240})
        self.assertTrue(any("squash merge links sessions" in f for f in est.days[DAY].flags), est.days[DAY].flags)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["repos"]), 0)
        self.assertIn(" 4 commits", out.getvalue(), "a copy isn't one of your commits in the count")
        labels = [r[0] for r in self.con.execute("SELECT label FROM v_activity WHERE kind = 'commit'")]
        self.assertEqual(len(labels), 4, "v_activity leaves the copy out (schema v2)")
        self.assertNotIn("PROJ-1 retry (#3)", labels)
        self.con.execute("DELETE FROM identities")
        self.assertEqual(collect.refresh_ownership(self.con, ["brandon@agency.gov"]), 4,
                         "the copy isn't counted among the commits that stopped being yours")

    def test_a_clone_still_holding_a_copy_as_work_doesnt_count_it(self):
        # Second review, confirmed defect 3: a clone 0.3.0 collected, never collected again.
        g = self.git
        g.commit("2026-10-01T08:00:00", "initial", author=("Sam", "sam@agency.gov"))
        g.commit("2026-10-01T09:00:00", "PROJ-1 retry")
        sha = g.commit("2026-10-01T10:00:00", "PROJ-1 retry (#7)", committer=("GitHub Enterprise", "no-reply@agency.gov"))
        self.collect()
        stale = self.add_repo("old-clone")
        cid = self.con.execute("INSERT INTO commits (repo_id, sha, author_name, author_email, authored_at, committed_at, "
                               "subject, is_mine, first_seen_at) SELECT ?, sha, author_name, author_email, authored_at, "
                               "committed_at, subject, 1, first_seen_at FROM commits WHERE sha = ? RETURNING id",
                               (stale, sha)).fetchone()[0]
        self.con.execute("INSERT INTO commit_work_items VALUES (?, 'PROJ-1', 'message')", (cid,))
        est = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
        self.assertEqual({x.key: x.minutes_proposed for x in est.proposals}, {"PROJ-1": 30})

    def test_a_local_squash_is_a_copy_whichever_clone_has_its_branch(self):
        # Second review, defect 4 (also in 0.3.0): a single-branch clone can't see the commits a
        # local `git merge --squash` lists, and counted the squash as 2 hours of work.
        g = self.git
        g.commit("2026-10-01T08:00:00", "initial", author=("Sam", "sam@agency.gov"))
        g.git("checkout", "-q", "-b", "feature/PROJ-5-x", at="2026-10-01T08:50:00")
        g.commit("2026-10-01T09:00:00", "PROJ-5 one")
        g.commit("2026-10-01T09:30:00", "PROJ-5 two")
        g.git("checkout", "-q", "main", at="2026-10-01T11:00:00")
        g.git("merge", "-q", "--squash", "feature/PROJ-5-x", at="2026-10-01T11:29:00")
        g.git("commit", "-q", "--no-edit", at="2026-10-01T11:30:00")
        mirror = self.dir / "src" / "mirror"
        subprocess.run([GIT, "clone", "-q", "--single-branch", "--branch", "main", str(g.path), str(mirror)],
                       check=True, capture_output=True)
        answers = []
        for order in ([mirror, g.path], [g.path, mirror]):
            self.con.execute("DELETE FROM commit_work_items")
            self.con.execute("DELETE FROM commits")
            for folder in order + [order[0]]:             # the first one again: it knows the branch's commits now
                result = collect.collect(self.con, self.settings, only=[folder])
                self.assertEqual(result.failed, [])
            est = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
            answers.append({x.key: x.minutes_proposed for x in est.proposals})
            squash = "SELECT is_merge FROM commits WHERE subject LIKE 'Squashed commit%' ORDER BY repo_id"
            self.assertEqual([r[0] for r in self.con.execute(squash)], [1, 1], "a copy in both clones")
        self.assertEqual(answers, [{"PROJ-5": 30}, {"PROJ-5": 30}])

    def test_a_squash_0_3_0_stored_as_work_stops_counting(self):
        g = self.git
        g.commit("2026-10-01T08:00:00", "initial", author=("Sam", "sam@agency.gov"))
        g.commit("2026-10-01T09:00:00", "PROJ-1 retry")
        sha = g.commit("2026-10-01T10:00:00", "PROJ-1 retry (#7)", committer=("GitHub Enterprise", "no-reply@agency.gov"))
        self.collect()
        # As 0.3.0 left it: the squash stored as work, with a key from its message.
        cid = self.con.execute("SELECT id FROM commits WHERE sha = ?", (sha,)).fetchone()[0]
        self.con.execute("UPDATE commits SET is_merge = 0, patch_id = 'p0' WHERE id = ?", (cid,))
        self.con.execute("INSERT INTO commit_work_items VALUES (?, 'PROJ-1', 'message'), (?, 'PROJ-9', 'manual')",
                         (cid, cid))
        self.assertEqual(sum(x.minutes_proposed for x in
                             store.compute(self.con, self.settings, DAY, DAY, now=NOW).proposals), 90)
        self.collect()
        self.assertEqual(self.con.execute("SELECT is_merge, patch_id, (SELECT count(*) FROM commit_work_items "
                                          "WHERE commit_id = ?) FROM commits WHERE id = ?", (cid, cid)).fetchone()[:],
                         (1, None, 0), "a copy keeps no keys, not even hand-set ones (second review, defect 6)")
        self.assertEqual(store.compute(self.con, self.settings, DAY, DAY, now=NOW).proposals[0].minutes_proposed, 30)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertNotEqual(cli.main(["keys", sha[:12], "PROJ-1"]), 0)
        self.assertIn("is a squash copy of commits already counted", err.getvalue())


# --------------------------------------------------------------------------
# Review finding #20: skipping a squash copy must never raise a day
# --------------------------------------------------------------------------

class SquashLinkTests(unittest.TestCase):
    """A squash copy isn't work, but the sessions it links share one length limit."""

    P = E.Params()
    WORK = [(9, 0), (11, 0), (13, 30), (15, 30)]     # the reviewer's day: two stretches 2h30m apart

    def day(self, copies=(), p=None):
        commits = [c(n + 1, t(1, h, m), "PROJ-1") for n, (h, m) in enumerate(self.WORK)]
        return E.estimate(commits, [], [], [DAY], p or self.P, now=NOW, copies=[t(1, *x) for x in copies])

    def test_the_reviewers_day_is_the_single_capped_session(self):
        self.assertEqual(self.day().proposals[0].minutes_proposed, 300, "two sessions of 2h30m without the copy")
        est = self.day(copies=[(12, 15)])
        self.assertEqual(est.proposals[0].minutes_proposed, 240, "the same 4h00m as with the copy counted as work")
        self.assertEqual([(x.start, x.end, x.link_scale) for x in est.parts],
                         [(t(1, 8, 30), t(1, 11), 0.8), (t(1, 13), t(1, 15, 30), 0.8)],
                         "the spans stay as the evidence says; both sessions are scaled alike")
        self.assertIn("A squash merge links sessions on this day, so they share one 4h00m session limit",
                      est.days[DAY].flags)
        chained = self.day(copies=[(11, 40), (12, 50)])     # two copies, each step under the idle gap
        self.assertEqual(chained.proposals[0].minutes_proposed, 240)
        counted = [c(9, t(1, 12, 15))] + [c(n + 1, t(1, h, m), "PROJ-1") for n, (h, m) in enumerate(self.WORK)]
        as_work = E.estimate(counted, [], [], [DAY], self.P, now=NOW)
        self.assertEqual(as_work.days[DAY].counted, est.days[DAY].counted)

    def test_a_copy_never_adds_minutes(self):
        short = [c(1, t(1, 9), "PROJ-1"), c(2, t(1, 12), "PROJ-1")]
        plain = E.estimate(short, [], [], [DAY], self.P, now=NOW)
        linked = E.estimate(short, [], [], [DAY], self.P, now=NOW, copies=[t(1, 10, 30)])
        self.assertEqual((plain.days[DAY].counted, linked.days[DAY].counted), (60, 60),
                         "the gap the copy bridges isn't work")
        self.assertFalse(any("squash" in f for f in linked.days[DAY].flags), linked.days[DAY].flags)
        for copies in ([(11, 10)], [(8, 0)], [(17, 0)], [(11, 5), (11, 20)]):       # 11:20 to 13:30 is over 2h
            self.assertEqual(self.day(copies=copies).proposals[0].minutes_proposed, 300,
                             f"{copies} doesn't link the stretches, so nothing changes")

    def test_copies_never_raise_a_day_over_random_days(self):
        """Linking only lowers, and a linked stretch keeps no more session time than counting the copy would.

        The second check is on session minutes, before meeting weights: counting a copy as work can
        also slide a clamped session's window into a meeting (a copy after your last commit pulls
        it later), and that lower figure comes from time you didn't spend, so it isn't the bar.
        It needs lead_in_minutes <= idle_gap_minutes, as in the defaults: a longer lead-in can reach
        back past a copy that ends the session before it.
        """
        rng = random.Random(2020)
        for _ in range(400):
            commits, meetings, p = DirectionTests().random_day(rng)
            copies = [t(1, 7) + dt.timedelta(minutes=rng.randint(0, 14 * 60)) for _ in range(rng.randint(1, 4))]

            def run(cs, cp=(), q=p, ms=meetings):
                return E.estimate(cs, [], ms, [DAY], q, calendar=(DAY, DAY), now=NOW, copies=cp)

            as_work = commits + [c(900 + i, at) for i, at in enumerate(copies)]
            self.assertLessEqual(run(commits, copies).days[DAY].counted, run(commits).days[DAY].counted + 1e-9,
                                 "linking never raises the day")
            if p.lead_in_minutes > p.idle_gap_minutes:
                continue
            session_time = [sum(x.minutes * x.link_scale for x in run(cs, cp).parts)
                            for cs, cp in ((commits, copies), (as_work, ()))]
            self.assertLessEqual(session_time[0], session_time[1] + 1e-9, "never more session time than as work")
            for policy in ("independent", "overlap"):          # no per-minute meeting weight
                q = dataclasses.replace(p, policy=policy)
                self.assertLessEqual(run(commits, copies, q).days[DAY].counted,
                                     run(as_work, (), q).days[DAY].counted + 1e-9, policy)

    def test_no_ticket_rises_when_copies_link_sessions(self):
        # Second review, confirmed defect 1: cutting a session's start left its minutes to the commits
        # still inside it, so PROJ-2 went from 0m to 15m. Scaling keeps each session's own split.
        work = [c(1, t(1, 8), "PROJ-1")] + [c(2 + n, t(1, 10, 30 + 5 * n), "PROJ-1") for n in range(5)]
        work += [c(10, t(1, 11, 15), "PROJ-2")]
        work += [c(20 + n, t(1, 13, 30) + dt.timedelta(minutes=m), "PROJ-1")
                 for n, m in enumerate((0, 21, 42, 63, 84, 105, 126, 147, 168, 190))]
        copies = [t(1, 9, 30), t(1, 12, 30)]
        skipped = E.estimate(work, [], [], [DAY], self.P, now=NOW)
        linked = E.estimate(work, [], [], [DAY], self.P, now=NOW, copies=copies)
        before = {x.key: x.minutes_raw for x in skipped.proposals}
        for x in linked.proposals:
            self.assertLessEqual(x.minutes_raw, before.get(x.key, 0) + 1e-9, x.key)
        self.assertEqual({x.key: x.minutes_proposed for x in linked.proposals if x.minutes_proposed}, {"PROJ-1": 225})
        self.assertTrue(all(x.end > x.start for x in linked.parts), "no session shrinks to nothing")
        # ... and across midnight (confirmed defect 1b): day 1's part borrowed day 2's commit.
        work = [c(1, t(1, 19), "PROJ-1"), c(2, t(1, 22, 10), "PROJ-1"), c(3, t(2, 0, 5), "PROJ-2")]
        work += [c(10 + n, t(2, 2, 10) + dt.timedelta(minutes=35 * n), "PROJ-3") for n in range(6)]
        days = [DAY, NEXT]
        skipped = E.estimate(work, [], [], days, self.P, now=NOW)
        linked = E.estimate(work, [], [], days, self.P, now=NOW, copies=[t(1, 20, 30), t(2, 1, 30)])
        before = {(x.local_date, x.key): x.minutes_raw for x in skipped.proposals}
        for x in linked.proposals:
            self.assertLessEqual(x.minutes_raw, before.get((x.local_date, x.key), 0) + 1e-9, (x.local_date, x.key))

    def test_no_ticket_rises_over_random_days(self):
        rng = random.Random(77)
        for _ in range(300):
            commits, meetings, p = DirectionTests().random_day(rng)
            p = dataclasses.replace(p, max_daily_dev_minutes=1440)   # the day cap shares freed room by design
            copies = [t(1, 7) + dt.timedelta(minutes=rng.randint(0, 14 * 60)) for _ in range(rng.randint(1, 4))]
            skipped = E.estimate(commits, [], meetings, [DAY], p, calendar=(DAY, DAY), now=NOW)
            linked = E.estimate(commits, [], meetings, [DAY], p, calendar=(DAY, DAY), now=NOW, copies=copies)
            before = {x.key: x.minutes_raw for x in skipped.proposals}
            for x in linked.proposals:
                self.assertLessEqual(x.minutes_raw, before.get(x.key, 0) + 1e-9, x.key)


# --------------------------------------------------------------------------
# The window (spec build step 7): desk.py without a display, window.py with one
# --------------------------------------------------------------------------

class DeskTests(MuninnCase):
    MONDAY = dt.date(2026, 9, 28)

    def test_the_week_and_the_day(self):
        self.worked_example()
        week = desk.load_week(self.con, self.settings, desk.week_of(DAY))
        self.assertEqual([r.day for r in week], [self.MONDAY + dt.timedelta(days=n) for n in range(7)])
        thu = week[3]
        self.assertEqual((thu.day, thu.dev, thu.tickets, thu.review), (DAY, 120, "PROJ-42 1h30m, PROJ-51 30m",
                                                                      "new estimate"))
        self.assertEqual([r.tickets for r in week if r.day != DAY], ["no commits"] * 6)
        view = desk.load_day(self.con, self.settings, DAY)
        self.assertEqual([(t.key, t.estimate, t.open_id, t.approved) for t in view.tickets],
                         [("PROJ-42", 90, None, None), ("PROJ-51", 30, None, None)])
        self.assertIn("Development (estimated from git)", view.report)
        self.assertEqual(self.rows(), [], "looking stores nothing")

    def test_approving_stores_the_estimate_then_approves_your_figures(self):
        self.worked_example()
        ids = desk.approve_day(self.con, self.settings, DAY, {"proj-42": 105})
        self.assertEqual(len(ids), 2)
        self.assertEqual({r["work_item_key"]: r["minutes_final"] for r in self.rows("approved")},
                         {"PROJ-42": 105, "PROJ-51": 30})
        view = desk.load_day(self.con, self.settings, DAY)
        self.assertEqual([(t.key, t.figure, t.open_id is None) for t in view.tickets],
                         [("PROJ-42", 105, True), ("PROJ-51", 30, True)])
        with self.assertRaisesRegex(desk.DeskError, "Nothing to approve"):
            desk.approve_day(self.con, self.settings, DAY)

    def test_a_figure_for_a_ticket_with_nothing_to_approve_is_refused(self):
        self.worked_example()
        with self.assertRaisesRegex(desk.DeskError, "PROJ-99 has nothing to approve"):
            desk.approve_day(self.con, self.settings, DAY, {"PROJ-99": 30})
        self.assertEqual(self.rows("approved"), [], "nothing approved when one figure can't be")
        for bad in (-1, 24 * 60 + 1):
            with self.assertRaises(desk.DeskError):
                desk.approve_day(self.con, self.settings, DAY, {"PROJ-42": bad})

    def test_changing_an_approved_figure_keeps_the_old_row(self):
        self.worked_example()
        desk.approve_day(self.con, self.settings, DAY)
        t = desk.load_day(self.con, self.settings, DAY).tickets[0]
        new_id = desk.change_figure(self.con, t.approved_id, 120)
        self.assertNotEqual(new_id, t.approved_id)
        self.assertEqual(self.con.execute("SELECT status FROM day_proposals WHERE id = ?", (t.approved_id,)).fetchone()[0],
                         "superseded")
        self.assertEqual(desk.load_day(self.con, self.settings, DAY).tickets[0].figure, 120)

    def test_rejecting_a_day(self):
        self.worked_example()
        self.assertEqual(len(desk.reject_day(self.con, self.settings, DAY)), 2)
        self.assertEqual(len(self.rows("rejected")), 2)
        with self.assertRaisesRegex(desk.DeskError, "Nothing to reject"):
            desk.reject_day(self.con, self.settings, DAY)

    def test_copy_for_timesheet(self):
        self.worked_example()
        before = desk.timesheet(desk.load_day(self.con, self.settings, DAY))
        self.assertIn("PROJ-42\t1h30m\t1.50 h\tnot approved yet", before)
        self.assertIn("Total\t2h00m\t2.00 h", before)
        desk.approve_day(self.con, self.settings, DAY, {"PROJ-42": 105})
        after = desk.timesheet(desk.load_day(self.con, self.settings, DAY))
        self.assertEqual(after.splitlines()[1:], ["PROJ-42\t1h45m\t1.75 h", "PROJ-51\t30m\t0.50 h", "Total\t2h15m\t2.25 h"])
        self.assertEqual(after, after.encode("ascii").decode("ascii"), "plain ASCII for any clipboard")
        empty = desk.timesheet(desk.load_day(self.con, self.settings, NEXT))
        self.assertIn("No development time to report.", empty)


class GitHubTests(MuninnCase):
    """Spec build step 4, against tests/fake_github.py: a local fake GitHub Enterprise Server."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(ROOT / "tests"))
        import fake_github
        self.fg = fake_github
        self.server = fake_github.FakeGitHub()
        self.addCleanup(self.server.close)
        self.client = github.Client(self.server.api, fake_github.TOKEN)
        self.con.execute("UPDATE repos SET github_repo = 'csb/asgard' WHERE id = ?", (self.repo,))
        f = fake_github
        self.server.pulls["csb/asgard"] = [
            f.pr(7, "Retry on 503", "bdoe", "feature/PROJ-42-retry", merged_at="2026-10-02T11:00:00Z",
                 updated="2026-10-02T11:00:00Z"),
            f.pr(8, "Session timeout", "sam", "feature/PROJ-51-timeout", requested=("bdoe",)),
            f.pr(9, "Spike", "sam", "spike/idea", draft=True, requested=("bdoe",))]
        self.server.pulls["csb/portal"] = [f.pr(31, "Portal: PIV", "lee", "feature/POR-1", repo="csb/portal",
                                                requested=("bdoe",))]
        self.server.reviews[("csb/asgard", 7)] = [f.review(1, "lead", "APPROVED"), f.review(2, "bdoe", "COMMENTED"),
                                                   f.review(3, "lee", "PENDING")]
        self.server.requested = [("csb/asgard", 8), ("csb/asgard", 9), ("csb/portal", 31)]

    def sync(self):
        return github.sync(self.con, self.settings, self.client)

    def prs(self):
        return {(r["repo"], r["number"]): (r["state"], r["review_requested"], r["is_mine"], r["work_item_key"])
                for r in self.con.execute("SELECT coalesce(r.github_repo, r.name) AS repo, p.* FROM pull_requests p "
                                          "JOIN repos r ON r.id = p.repo_id")}

    def test_your_pull_requests_reviews_and_review_requests(self):
        res = self.sync()
        self.assertEqual((res.login, res.repos, res.requested, res.problems), ("bdoe", 1, 3, []))
        self.assertEqual(self.prs(), {
            ("csb/asgard", 7): ("merged", 0, 1, "PROJ-42"), ("csb/asgard", 8): ("open", 1, 0, "PROJ-51"),
            ("csb/asgard", 9): ("open", 1, 0, None), ("csb/portal", 31): ("open", 1, 0, None)})
        reviews = [tuple(r) for r in self.con.execute("SELECT github_id, reviewer, is_mine, state FROM pr_reviews "
                                                      "ORDER BY github_id")]
        self.assertEqual(reviews, [("1", "lead", 0, "approved"), ("2", "bdoe", 1, "commented")],
                         "a pending review isn't submitted yet")
        portal = self.con.execute("SELECT name, local_path FROM repos WHERE github_repo = 'csb/portal'").fetchone()
        self.assertEqual(tuple(portal), ("portal", None), "a review-only row: alerted on, never estimated")
        self.assertEqual([b.text for b in muninn.tile_badges(self.dir / "muninn.db")["baldur"]][:1],
                         ["3 reviews requested"])
        items = desk.review_items(self.con)
        self.assertEqual([(i.kind, i.label) for i in items][:3],
                         [("requested", "csb/asgard#8"), ("requested", "csb/asgard#9"), ("requested", "csb/portal#31")])
        self.assertEqual(self.con.execute("SELECT value FROM identities WHERE kind = 'github_login'").fetchone()[0],
                         "bdoe")

    def test_unchanged_answers_cost_nothing(self):
        self.sync()
        before = len(self.server.requests)
        changes = self.con.total_changes
        res = self.sync()
        asked = self.server.requests[before:]
        self.assertEqual([s for _, _, s in asked], [304, 304, 304], "user, pulls and search, each conditional")
        self.assertTrue(all(etag for _, etag, _ in asked))
        self.assertEqual((res.pulls_changed, res.reviews_new, res.requested), (0, 0, 3))
        self.assertEqual(self.prs()[("csb/asgard", 8)][1], 1)
        del changes

    def test_answered_requests_stop_counting_and_a_re_request_alerts_again(self):
        self.sync()
        self.con.execute("UPDATE pull_requests SET notified_at = ? WHERE number = 8", (muninn.utcnow(),))
        self.server.requested = [("csb/portal", 31)]
        self.sync()
        self.assertEqual({k: v[1] for k, v in self.prs().items() if v[0] == "open"},
                         {("csb/asgard", 8): 0, ("csb/asgard", 9): 0, ("csb/portal", 31): 1})
        self.server.requested = [("csb/asgard", 8), ("csb/portal", 31)]
        self.sync()
        row = self.con.execute("SELECT review_requested, notified_at FROM pull_requests WHERE number = 8").fetchone()
        self.assertEqual(tuple(row), (1, None), "asked again: it alerts again")

    def test_a_pull_requests_head_branch_keys_its_commits(self):
        untracked = self.commit(t(1, 9), subject="tidy")           # no key anywhere
        keyed = self.commit(t(1, 9, 30), "PROJ-1", subject="PROJ-1 fix")
        self.con.execute("UPDATE commit_work_items SET method = 'reflog' WHERE commit_id = ?", (keyed,))
        shas = [r[0] for r in self.con.execute("SELECT sha FROM commits WHERE id IN (?, ?) ORDER BY id",
                                               (untracked, keyed))]
        self.server.commits[("csb/asgard", 7)] = shas
        self.assertEqual(self.sync().keyed_commits, 1)
        found = {r[0]: (r[1], r[2]) for r in self.con.execute(
            "SELECT commit_id, work_item_key, method FROM commit_work_items")}
        self.assertEqual(found, {untracked: ("PROJ-42", "pr"), keyed: ("PROJ-1", "reflog")},
                         "the pull request's branch names the key; stronger evidence keeps its own")
        est = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
        self.assertEqual({x.key for x in est.proposals}, {"PROJ-42", "PROJ-1"})
        collect._store_keys(self.con, untracked, ["PROJ-2"], "message", ["PROJ"])
        self.assertEqual(self.con.execute("SELECT work_item_key, method FROM commit_work_items WHERE commit_id = ?",
                                          (untracked,)).fetchone()[:], ("PROJ-42", "pr"),
                         "a message key, weaker, doesn't replace it")

    def test_problems_say_what_to_do_and_never_show_the_token(self):
        bad = github.Client(self.server.api, "ghp_wrong_secret")
        with self.assertRaises(github.GitHubError) as caught:
            github.sync(self.con, self.settings, bad)
        self.assertIn("refused the token (401)", str(caught.exception))
        self.assertNotIn("ghp_", str(caught.exception))
        self.server.status["/repos/csb/asgard/pulls"] = 404
        res = self.sync()
        self.assertEqual(len(res.problems), 1)
        self.assertIn("fine-grained token reaches one organization", res.problems[0])
        self.assertEqual(res.requested, 3, "one unreadable repository doesn't stop the rest")
        nowhere = github.Client("http://127.0.0.1:9/api/v3", self.fg.TOKEN, timeout=2)
        with self.assertRaisesRegex(github.GitHubError, "Couldn't reach 127.0.0.1"):
            nowhere.get("/user")
        with self.assertRaisesRegex(github.GitHubError, "https"):
            github.Client("http://github.agency.gov/api/v3", "t")
        with self.assertRaisesRegex(github.GitHubError, "another server"):
            self.client.get("https://evil.example/api/v3/user")

    def test_long_lists_are_paged(self):
        self.server.page_size = 2
        self.server.reviews[("csb/asgard", 7)] = [self.fg.review(n, f"r{n}", "APPROVED") for n in range(1, 6)]
        self.sync()
        self.assertEqual(self.con.execute("SELECT count(*) FROM pr_reviews").fetchone()[0], 5)

    def test_no_token_means_github_is_off(self):
        self.settings.values["github_api"] = config.github_api_url("github.agency.gov")
        with mock.patch.dict(os.environ, {github.TOKEN_ENV: ""}), \
                mock.patch.object(github, "load_token", return_value=None):
            self.assertIsNone(desk.sync_github(self.con, self.settings))
            with self.assertRaisesRegex(cli.CliError, "No GitHub token for github.agency.gov"):
                cli.github_client(self.settings)
        self.settings.values["github_api"] = ""
        with self.assertRaisesRegex(cli.CliError, "GitHub is off"):
            cli.github_client(self.settings)

    def test_the_token_comes_from_the_environment_first(self):
        with mock.patch.dict(os.environ, {github.TOKEN_ENV: " ghp_from_env "}):
            self.assertEqual(github.load_token("github.agency.gov"), "ghp_from_env")
        with self.assertRaises(github.GitHubError):
            github.save_token("github.agency.gov", "two words")


def _tk_root():
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as exc:              # noqa: BLE001 - no display or no Tcl/Tk: skip
        raise unittest.SkipTest(f"no Tk display here ({exc})") from None
    root.withdraw()
    return root


class WindowTests(MuninnCase):
    def setUp(self):
        super().setUp()
        from baldur import window
        self.window_module = window
        self.root = _tk_root()
        self.addCleanup(self.root.destroy)
        # A message box would wait for a click forever; record them instead.
        self.boxes = []
        for name in ("showwarning", "showerror", "showinfo"):
            patcher = mock.patch.object(window.messagebox, name,
                                        side_effect=lambda *a, _n=name, **k: self.boxes.append((_n, a)))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(window.messagebox, "askyesno", return_value=True)
        self.askyesno = patcher.start()
        self.addCleanup(patcher.stop)

    def open(self, **kw):
        kw.setdefault("load_settings", lambda: self.settings)
        return self.window_module.BaldurWindow(self.root, synchronous=True, today=DAY + dt.timedelta(days=2),
                                               dark=False, **kw)

    def test_review_a_day_edit_a_figure_approve_and_copy(self):
        self.worked_example()
        w = self.open()
        self.assertEqual(w.days.item(DAY.isoformat())["values"][:2], ["2h00m", "PROJ-42 1h30m, PROJ-51 30m"])
        self.assertEqual(w.days.selection(), (DAY.isoformat(),), "opens on the latest day with work")
        self.assertEqual(list(w.tickets.get_children()), ["PROJ-42", "PROJ-51"])
        w.tickets.selection_set("PROJ-42")
        w.edit_figure("1h45m")
        self.assertEqual(w.tickets.item("PROJ-42")["values"][1], "1h45m (yours)")
        w.edit_figure("not a time")                              # a warning; nothing changes
        self.assertEqual(w.figures, {(DAY, "PROJ-42"): 105})
        self.assertEqual([b[0] for b in self.boxes], ["showwarning"])
        w.approve(confirm=False)
        self.assertEqual({r["work_item_key"]: r["minutes_final"] for r in self.rows("approved")},
                         {"PROJ-42": 105, "PROJ-51": 30})
        self.assertEqual(w.figures, {})
        self.assertIn("approved 1h45m", w.tickets.item("PROJ-42")["values"][3])
        text = w.copy_timesheet()
        self.assertIn("PROJ-42\t1h45m\t1.75 h", text)
        self.assertEqual(self.root.clipboard_get(), text)

    def test_changing_an_approved_figure_asks_first(self):
        self.worked_example()
        desk.approve_day(self.con, self.settings, DAY)
        w = self.open()
        w.tickets.selection_set("PROJ-51")
        self.askyesno.return_value = False
        w.edit_figure("45m")
        self.assertEqual(len(self.rows("approved")), 2, "said no: nothing changed")
        self.askyesno.return_value = True
        w.edit_figure("45m")
        self.assertIn("Odin posts the difference", self.askyesno.call_args[0][1])
        self.assertEqual({r["work_item_key"]: r["minutes_final"] for r in self.rows("approved")},
                         {"PROJ-42": 90, "PROJ-51": 45})

    def test_weeks_and_a_broken_settings_file(self):
        self.worked_example()
        w = self.open()
        w.move_week(-1)
        self.assertEqual(w.monday, dt.date(2026, 9, 21))
        self.assertEqual(list(w.tickets.get_children()), [])
        w.this_week()
        self.assertEqual(w.monday, dt.date(2026, 9, 28))
        broken = config.Settings(warnings=["couldn't use baldur.json: Expecting value"], broken=True)
        w2 = self.open(load_settings=lambda: broken)
        self.assertIn("has a mistake in it", w2.status.cget("text"))
        self.assertTrue(all(b.instate(["disabled"]) for b in w2.buttons))

    def test_pull_requests_waiting_on_you(self):
        review_only = self.con.execute("INSERT INTO repos (name, github_repo) VALUES ('portal', 'csb/portal') "
                                       "RETURNING id").fetchone()[0]
        now = muninn.utcnow()
        self.con.execute("INSERT INTO pull_requests (repo_id, number, title, author, head_ref, state, review_requested, "
                         "created_at, updated_at, url, first_seen_at, last_seen_at) VALUES (?, 31, 'Portal: PIV', 'lee', "
                         "'feature/POR-1', 'open', 1, ?, ?, 'https://github.agency.gov/csb/portal/pull/31', ?, ?)",
                         (review_only, now, now, now, now))
        self.settings.values["github_api"] = config.github_api_url("github.agency.gov")
        w = self.open()
        with mock.patch.object(github, "load_token", return_value=None):
            w.github_tick()
        self.assertEqual([w.reviews.item(i)["text"] for i in w.reviews.get_children()], ["csb/portal#31"])
        self.assertIn("1 review requested of you", w.reviews_label.cget("text"))
        w.reviews.selection_set(w.reviews.get_children()[0])
        with mock.patch("webbrowser.open") as opened:
            self.assertEqual(w.open_review(), "https://github.agency.gov/csb/portal/pull/31")
        opened.assert_called_once()
        w.close = lambda: None              # the cleanup destroys the root

    def test_problems_are_shown_not_raised(self):
        w = self.open()
        w.approve(confirm=False)                   # no day picked: nothing happens
        self.assertEqual(self.boxes, [])
        w.view = desk.DayView(DAY, "")
        w.approve(confirm=False)                   # no commits, so nothing to approve
        self.assertEqual([b[0] for b in self.boxes], ["showwarning"])
        self.assertIn("Nothing to approve", w.status.cget("text"))


# --------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------

class ParsingTests(unittest.TestCase):
    def test_minutes(self):
        for text, minutes in (("90", 90), ("1h30m", 90), ("1h", 60), ("45m", 45), ("1:30", 90), ("1.5h", 90),
                              ("2h 15m", 135)):
            self.assertEqual(cli.parse_minutes(text), minutes, text)
        for bad in ("", "h", "1:75", "an hour"):
            with self.assertRaises(cli.CliError):
                cli.parse_minutes(bad)

    def test_days(self):
        today = dt.date(2026, 10, 3)
        self.assertEqual([cli.parse_day(x, today) for x in ("today", "yesterday", "-3", "2026-09-01")],
                         [today, dt.date(2026, 10, 2), DAY - dt.timedelta(days=1), dt.date(2026, 9, 1)])
        with self.assertRaises(cli.CliError):
            cli.parse_day("someday", today)

    def test_the_weekly_task(self):
        cmd = cli.schedule_command("MON", "09:00", python=sys.executable)
        self.assertEqual(cmd[:12], ["schtasks", "/Create", "/F", "/SC", "WEEKLY", "/D", "MON", "/ST", "09:00", "/IT",
                                    "/TN", cli.TASK_NAME])
        self.assertTrue(cmd[-1].endswith('cli.py" collect --quiet'))
        self.assertEqual(cli.schedule_command("MON", "09:00", remove=True)[:2], ["schtasks", "/Delete"])


class CliTests(MuninnCase):
    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_before_asgard_has_run(self):
        self.con.close()
        for name in os.listdir(self.dir):
            if name.startswith("muninn.db"):
                os.remove(self.dir / name)
        code, _, err = self.cli("days")
        self.assertEqual(code, 1)
        self.assertIn("Open Asgard once", err)
        self.con = muninn.connect(self.dir / "muninn.db")

    def test_collect_needs_setup_first(self):
        code, _, err = self.cli("collect")
        self.assertEqual(code, 1)
        self.assertIn("cli.py setup --root", err)
        code, out, _ = self.cli("setup")
        self.assertIn("Still to do:", out)

    def test_review_flow(self):
        self.worked_example()
        code, out, _ = self.cli("setup", "--email", "brandon@agency.gov", "--project", "proj", "--set", "history_days=400")
        self.assertEqual(code, 0, out)
        self.assertIn("Jira projects        PROJ", out)
        self.assertEqual(config.load().values["history_days"], 400)
        code, out, _ = self.cli("estimate", "--from", "2026-10-01", "--to", "2026-10-01")
        self.assertEqual(code, 0)
        self.assertIn("PROJ-42 1h30m new, PROJ-51 30m new", out)
        code, out, _ = self.cli("days", "--from", "2026-09-30", "--to", "2026-10-01")
        self.assertIn("Thu 2026-10-01    2h00m   PROJ-42 1h30m, PROJ-51 30m", out)
        self.assertIn("2 to review", out)
        code, out, err = self.cli("approve", "--date", "2026-10-01", "--set", "PROJ-42=1h45m")
        self.assertEqual(code, 0, err)
        self.assertIn("Approved PROJ-42 1h45m on Thu 2026-10-01", out)
        code, out, _ = self.cli("report", "2026-10-01")
        self.assertIn("approved 1h45m", out)
        code, _, err = self.cli("approve", "--date", "2026-10-01")
        self.assertEqual(code, 1)
        self.assertIn("Nothing to approve", err)
        pid = self.rows("approved")[0]["id"]
        code, out, _ = self.cli("change", str(pid), "2h")
        self.assertIn("Approved PROJ-42 2h00m", out)

    def test_broken_settings_stop_everything(self):
        config.settings_path().parent.mkdir(parents=True, exist_ok=True)
        config.settings_path().write_text('{"project_keys": ["PROJ"],}')
        code, _, err = self.cli("days")
        self.assertEqual(code, 1)
        self.assertIn("has a mistake in it", err)

    def test_input_out_of_range_is_a_message(self):
        self.settings = config.update({"project_keys": ["PROJ"]})
        for args, expected in ((("report", "-1000000000"), "isn't a date"), (("days", "--days", "999999999"), "1 to 366"),
                               (("approve", "1", "--minutes", "99999999999999999999h"), "more than a day")):
            code, _, err = self.cli(*args)
            self.assertEqual(code, 1, args)
            self.assertIn(expected, err)
        cid = self.commit(t(1, 10))
        sha = self.con.execute("SELECT sha FROM commits WHERE id = ?", (cid,)).fetchone()[0]
        self.assertEqual(self.cli("keys", sha, "PROJ-9", "proj-9")[0], 0)

    def test_output_survives_a_strict_code_page(self):
        self.settings = config.update({"project_keys": ["PROJ"]})
        cid = self.commit(t(1, 10), "PROJ-1", subject="retry \u2192 backoff \u2713")
        sha = self.con.execute("SELECT sha FROM commits WHERE id = ?", (cid,)).fetchone()[0]
        env = dict(os.environ, PYTHONIOENCODING="cp1252", ASGARD_HOME=str(self.dir))
        done = subprocess.run([sys.executable, str(ROOT / "apps" / "baldur" / "cli.py"), "keys", sha[:12]],
                              capture_output=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr.decode("cp1252", "replace"))
        self.assertIn(b"retry ? backoff ?", done.stdout)

    def test_keys_by_hand(self):
        self.settings = config.update({"project_keys": ["PROJ"]})
        cid = self.commit(t(1, 10))
        sha = self.con.execute("SELECT sha FROM commits WHERE id = ?", (cid,)).fetchone()[0]
        code, out, _ = self.cli("keys", sha[:12], "proj-9")
        self.assertEqual(code, 0)
        self.assertIn("now counts toward PROJ-9", out)
        code, out, _ = self.cli("keys", sha[:12])
        self.assertIn("PROJ-9 (manual)", out)
        code, _, err = self.cli("keys", sha[:12], "not-a-key")
        self.assertEqual(code, 1)

    def test_keys_reach_every_clone_of_a_commit(self):
        self.settings = config.update({"project_keys": ["PROJ"]})
        cid = self.commit(t(1, 10))
        sha = self.con.execute("SELECT sha FROM commits WHERE id = ?", (cid,)).fetchone()[0]
        ts = muninn.to_ts(t(1, 10))
        copy = int(self.con.execute(
            "INSERT INTO commits (repo_id, sha, author_name, author_email, authored_at, committed_at, subject, "
            "is_mine, first_seen_at) VALUES (?, ?, 'Brandon', 'brandon@agency.gov', ?, ?, 'change 1', 1, ?) "
            "RETURNING id", (self.add_repo("asgard-second-clone"), sha, ts, ts, ts)).fetchone()[0])
        code, out, err = self.cli("keys", sha, "PROJ-9")
        self.assertEqual(code, 0, err)
        held = sorted(tuple(r) for r in self.con.execute(
            "SELECT commit_id, work_item_key, method FROM commit_work_items"))
        self.assertEqual(held, [(cid, "PROJ-9", "manual"), (copy, "PROJ-9", "manual")])

    def test_a_database_error_is_a_message(self):
        self.settings = config.update({"project_keys": ["PROJ"]})
        with mock.patch.object(store, "run", side_effect=sqlite3.OperationalError("database is locked")):
            code, _, err = self.cli("estimate")
        self.assertEqual(code, 1)
        self.assertIn("Muninn couldn't do that (database is locked)", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
