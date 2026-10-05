"""Tests for the git-work estimator.

Two halves, deliberately separated:

  * The estimation maths (sessions, attribution, caps) is pure and tested directly. It is where the
    timesheet numbers come from, so it is tested hardest.
  * The git scanning half is tested against REAL temporary repositories created by `git init`, not
    against a mocked subprocess. A mock would only prove that our parser accepts whatever we decided
    git emits, which is the assumption most likely to be wrong. Skipped when git is unavailable.
"""
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from meeting2jira import gitwork

UTC = timezone.utc
HAVE_GIT = shutil.which("git") is not None


def at(hour, minute=0, day=22):
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def commit(hour, minute=0, issue=None, subject="work", day=22, branches=None):
    """A Commit with the issue key supplied via a branch name, as the real thing does."""
    if branches is None:
        branches = ["feature/{}-thing".format(issue)] if issue else []
    return gitwork.Commit(repo="r", sha="{:02d}{:02d}{:02d}".format(day, hour, minute),
                          when=at(hour, minute, day), subject=subject, branches=branches)


class IssueKeyTests(unittest.TestCase):

    def test_finds_keys_in_the_shapes_branches_actually_take(self):
        for text, expected in [
            ("feature/PROJ-123-add-filter", "PROJ-123"),
            ("PROJ-7", "PROJ-7"),
            ("bugfix/ABC1-45_hotfix", "ABC1-45"),
            ("PROJ-123: fix the thing", "PROJ-123"),
            ("feature/PROJ-12-and-PROJ-34", "PROJ-12"),   # first wins
        ]:
            with self.subTest(text=text):
                self.assertEqual(gitwork.first_issue_key(text), expected)

    def test_rejects_things_that_merely_look_like_keys(self):
        """A false positive logs hours against a real issue, so this is the dangerous direction."""
        for text in [
            "release-2024-12",        # date, not a key
            "feature/add-thing",      # no key
            "python-3",               # language-version
            "proj-123",               # lowercase: see first_issue_key's docstring
            "XPROJ-1X",               # trailing character
            "",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(gitwork.first_issue_key(text))

    def test_a_key_embedded_in_a_longer_token_is_not_a_key(self):
        self.assertIsNone(gitwork.first_issue_key("vABC-1"))
        self.assertEqual(gitwork.first_issue_key("v/ABC-1"), "ABC-1")

    def test_branch_name_beats_commit_subject(self):
        """The branch is the convention; the subject is only a fallback."""
        c = gitwork.Commit(repo="r", sha="a", when=at(9), subject="PROJ-999 oops",
                           branches=["feature/PROJ-1-real"])
        self.assertEqual(c.issue, "PROJ-1")

    def test_subject_is_used_when_no_branch_carries_a_key(self):
        c = gitwork.Commit(repo="r", sha="a", when=at(9), subject="PROJ-5 fix", branches=["main"])
        self.assertEqual(c.issue, "PROJ-5")


class SessionTests(unittest.TestCase):

    def test_a_long_gap_splits_the_session(self):
        commits = [commit(9, 0, "PROJ-1"), commit(9, 40, "PROJ-1"),
                   commit(14, 0, "PROJ-1"), commit(14, 10, "PROJ-1")]
        sessions = gitwork.build_sessions(commits, idle_gap_minutes=120)
        self.assertEqual(len(sessions), 2)
        self.assertEqual([s.span_minutes for s in sessions], [40, 10])

    def test_commits_inside_the_gap_stay_one_session(self):
        commits = [commit(9, 0, "PROJ-1"), commit(10, 30, "PROJ-1")]
        self.assertEqual(len(gitwork.build_sessions(commits, idle_gap_minutes=120)), 1)

    def test_out_of_order_input_is_handled(self):
        """git log returns newest first, so unsorted input is the normal case, not an edge case."""
        commits = [commit(14, 10, "PROJ-1"), commit(9, 0, "PROJ-1"), commit(9, 40, "PROJ-1")]
        sessions = gitwork.build_sessions(commits, idle_gap_minutes=120)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0].start, at(9, 0))

    def test_a_session_crossing_midnight_counts_to_the_day_it_started(self):
        late = gitwork.Commit(repo="r", sha="a", when=at(23, 30, day=22), subject="s",
                              branches=["feature/PROJ-1-x"])
        later = gitwork.Commit(repo="r", sha="b", when=at(0, 20, day=23), subject="s",
                               branches=["feature/PROJ-1-x"])
        session = gitwork.build_sessions([late, later], idle_gap_minutes=120)[0]
        self.assertEqual(session.day, late.when.astimezone().date())


class SessionMinutesTests(unittest.TestCase):

    def test_lead_in_is_added_because_the_first_commit_ends_the_first_piece_of_work(self):
        session = gitwork.Session([commit(9, 0, "PROJ-1"), commit(9, 20, "PROJ-1")])
        self.assertEqual(gitwork.session_minutes(session, lead_in_minutes=30), 50)

    def test_a_lone_commit_gets_the_floor_not_zero(self):
        session = gitwork.Session([commit(9, 0, "PROJ-1")])
        self.assertEqual(
            gitwork.session_minutes(session, lead_in_minutes=0, min_minutes=15), 15)

    def test_an_all_day_span_is_capped(self):
        """One commit at 09:00 and one at 17:00 is not an eight-hour session."""
        session = gitwork.Session([commit(9, 0, "PROJ-1"), commit(17, 0, "PROJ-1")])
        self.assertEqual(gitwork.session_minutes(session, max_minutes=240), 240)

    def test_contradictory_bounds_are_rejected(self):
        session = gitwork.Session([commit(9, 0, "PROJ-1")])
        with self.assertRaises(gitwork.GitWorkError):
            gitwork.session_minutes(session, min_minutes=100, max_minutes=10)


class AttributionTests(unittest.TestCase):

    def test_minutes_split_by_commit_count(self):
        session = gitwork.Session([commit(9, 0, "PROJ-1"), commit(9, 10, "PROJ-1"),
                                   commit(9, 20, "PROJ-2")])
        shares = gitwork.attribute(session, 90)
        self.assertEqual(shares["PROJ-1"], 60)
        self.assertEqual(shares["PROJ-2"], 30)

    def test_minutes_are_never_lost_to_integer_division(self):
        session = gitwork.Session([commit(9, 0, "PROJ-1"), commit(9, 5, "PROJ-2"),
                                   commit(9, 10, "PROJ-3")])
        shares = gitwork.attribute(session, 100)
        self.assertEqual(sum(shares.values()), 100)

    def test_untracked_commits_hold_their_share_under_the_empty_key(self):
        session = gitwork.Session([commit(9, 0, "PROJ-1"),
                                   commit(9, 10, None, branches=["feature/no-key"])])
        shares = gitwork.attribute(session, 60)
        self.assertEqual(shares["PROJ-1"], 30)
        self.assertEqual(shares[""], 30)


class DailyCapTests(unittest.TestCase):
    """The cap is what keeps a timesheet honest, so it gets the most attention."""

    def test_tracked_time_is_scaled_to_fit_the_daily_limit(self):
        commits = [commit(h, 0, "PROJ-1") for h in (8, 11, 14, 17)] + \
                  [commit(h, 30, "PROJ-2") for h in (8, 11, 14, 17)]
        proposals = gitwork.propose(commits, max_daily_minutes=480, max_session_minutes=240)
        self.assertEqual(len(proposals), 1)
        self.assertLessEqual(proposals[0].tracked_minutes, 480)

    def test_meetings_already_logged_reduce_the_git_allowance(self):
        """Odin logs meeting time from the calendar; git estimates must not stack on top of it."""
        commits = [commit(9, 0, "PROJ-1"), commit(12, 0, "PROJ-1"), commit(16, 0, "PROJ-1")]
        day = at(9).astimezone().date()
        proposals = gitwork.propose(commits, max_daily_minutes=480,
                                    meeting_minutes_by_day={day: 420})
        self.assertLessEqual(proposals[0].tracked_minutes, 60)
        self.assertIsNotNone(proposals[0].capped_from)

    def test_a_fully_booked_day_yields_no_git_time(self):
        commits = [commit(9, 0, "PROJ-1"), commit(12, 0, "PROJ-1")]
        day = at(9).astimezone().date()
        proposals = gitwork.propose(commits, max_daily_minutes=480,
                                    meeting_minutes_by_day={day: 600})
        self.assertEqual(proposals[0].tracked_minutes, 0)

    def test_scaling_preserves_the_total_exactly(self):
        scaled = gitwork._scale_to({"A": 100, "B": 50, "C": 33}, 90)
        self.assertEqual(sum(scaled.values()), 90)

    def test_the_cap_is_recorded_so_the_user_can_see_it_bit(self):
        commits = [commit(h, 0, "PROJ-1") for h in (8, 11, 14, 17, 20)]
        proposals = gitwork.propose(commits, max_daily_minutes=120)
        self.assertIsNotNone(proposals[0].capped_from)
        self.assertGreater(proposals[0].capped_from, proposals[0].tracked_minutes)

    def test_untracked_time_is_reported_but_never_counted_as_tracked(self):
        commits = [commit(9, 0, None, branches=["feature/no-key"]),
                   commit(9, 20, None, branches=["feature/no-key"])]
        proposals = gitwork.propose(commits)
        self.assertEqual(proposals[0].tracked_minutes, 0)
        self.assertGreater(proposals[0].untracked_minutes, 0)
        self.assertIn("NOT TRACKED", "\n".join(gitwork.render(proposals)))


class RenderTests(unittest.TestCase):

    def test_the_report_states_that_the_numbers_are_estimates(self):
        """The one line that must never be dropped: these are inferences, not measurements."""
        commits = [commit(9, 0, "PROJ-1"), commit(9, 30, "PROJ-1")]
        text = "\n".join(gitwork.render(gitwork.propose(commits)))
        self.assertIn("ESTIMATE", text.upper())

    def test_evidence_is_shown_so_an_estimate_can_be_corrected(self):
        commits = [commit(9, 0, "PROJ-1"), commit(9, 30, "PROJ-1")]
        text = "\n".join(gitwork.render(gitwork.propose(commits), show_evidence=True))
        self.assertIn("commit(s)", text)
        self.assertIn("PROJ-1", text)


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class RealRepositoryTests(unittest.TestCase):
    """Against actual `git init` repositories: proves the parsing, not our idea of git's output."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="odin-gitwork-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def _repo(self, name):
        repo = self.root / name
        repo.mkdir(parents=True)
        self._run(repo, "init", "-q")
        # Local identity only: never touch the user's global git config.
        self._run(repo, "config", "user.name", "Test Author")
        self._run(repo, "config", "user.email", "author@example.gov")
        return repo

    def _run(self, repo, *args):
        completed = subprocess.run(["git", "-C", str(repo)] + list(args),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if completed.returncode != 0:
            self.fail("git {} failed: {}".format(args[0], completed.stderr.decode()))
        return completed.stdout.decode()

    def _commit(self, repo, message, when, filename="f.txt", body=None):
        (repo / filename).write_text(body or message, encoding="utf-8")
        self._run(repo, "add", filename)
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message,
                        "--date={}".format(stamp)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       env=self._env(stamp), check=True)

    def _env(self, stamp):
        import os
        env = dict(os.environ)
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
        return env

    def test_discovers_repositories_without_walking_into_them(self):
        self._repo("alpha")
        self._repo("nested/beta")
        (self.root / "notarepo").mkdir()
        found = gitwork.discover_repos([str(self.root)], max_depth=3)
        names = sorted(p.name for p in found)
        self.assertEqual(names, ["alpha", "beta"])

    def test_collects_own_commits_and_reads_the_issue_key_from_the_branch(self):
        repo = self._repo("alpha")
        self._commit(repo, "initial", at(8))
        self._run(repo, "checkout", "-q", "-b", "feature/PROJ-42-add-thing")
        self._commit(repo, "no key in this subject", at(9), filename="a.txt")
        self._commit(repo, "still no key", at(9, 30), filename="b.txt")

        commits = gitwork.collect_commits(repo, at(7), at(23), authors=["author@example.gov"])
        keyed = [c for c in commits if c.issue == "PROJ-42"]
        self.assertEqual(len(keyed), 2, "branch name should supply the key")

    def test_other_authors_are_excluded(self):
        """Logging the team's commits to one timesheet is the failure this prevents."""
        repo = self._repo("alpha")
        self._commit(repo, "initial", at(8))
        self._run(repo, "config", "user.email", "someone.else@example.gov")
        self._run(repo, "config", "user.name", "Someone Else")
        self._commit(repo, "PROJ-9 their work", at(10), filename="c.txt")

        commits = gitwork.collect_commits(repo, at(7), at(23), authors=["author@example.gov"])
        self.assertTrue(all("their work" not in c.subject for c in commits))

    def test_an_empty_author_list_is_refused(self):
        repo = self._repo("alpha")
        self._commit(repo, "initial", at(8))
        with self.assertRaises(gitwork.GitWorkError):
            gitwork.collect_commits(repo, at(7), at(23), authors=[])

    def test_end_to_end_on_a_real_repository(self):
        repo = self._repo("alpha")
        self._commit(repo, "initial", at(8))
        self._run(repo, "checkout", "-q", "-b", "feature/PROJ-42-add-thing")
        self._commit(repo, "one", at(9), filename="a.txt")
        self._commit(repo, "two", at(9, 45), filename="b.txt")

        commits = gitwork.collect_commits(repo, at(7), at(23), authors=["author@example.gov"])
        proposals = gitwork.propose(commits)
        self.assertEqual(len(proposals), 1)
        self.assertIn("PROJ-42", proposals[0].per_issue)
        self.assertGreater(proposals[0].per_issue["PROJ-42"], 0)

    def test_a_missing_root_is_a_warning_not_a_crash(self):
        self.assertEqual(gitwork.discover_repos([str(self.root / "nope")]), [])


class WindowTests(unittest.TestCase):

    def test_window_starts_at_local_midnight(self):
        now = datetime(2026, 9, 22, 15, 30, tzinfo=UTC)
        start, end = gitwork.window(1, now=now)
        self.assertEqual(end, now)
        local_start = start.astimezone()
        self.assertEqual((local_start.hour, local_start.minute), (0, 0))
        self.assertEqual((now.astimezone().date() - local_start.date()), timedelta(days=1))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class BranchAttributionTests(unittest.TestCase):
    """The bug this class exists for: reachable-from is not the same as added-by.

    `git rev-list <feature-branch>` lists every ancestor, so every commit on main since the start of
    the repository is reachable from every branch cut off it. Attributing on reachability credits
    the whole project history to whichever feature branch was checked out.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="odin-branchattr-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / "repo"
        self.repo.mkdir(parents=True)
        self._run("init", "-q")
        self._run("config", "user.name", "Test Author")
        self._run("config", "user.email", "author@example.gov")

    def _run(self, *args):
        completed = subprocess.run(["git", "-C", str(self.repo)] + list(args),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if completed.returncode != 0:
            self.fail("git {} failed: {}".format(args[0], completed.stderr.decode()))
        return completed.stdout.decode()

    def _commit(self, message, when, filename):
        import os
        (self.repo / filename).write_text(message, encoding="utf-8")
        self._run("add", filename)
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env = dict(os.environ)
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", message,
                        "--date={}".format(stamp)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=True)

    def _collect(self):
        return gitwork.collect_commits(self.repo, at(7), at(23, 59),
                                       authors=["author@example.gov"])

    def test_trunk_history_is_not_credited_to_a_feature_branch(self):
        for i, hour in enumerate((8, 8, 8)):
            self._commit("trunk work {}".format(i), at(hour, i * 5), "t{}.txt".format(i))
        self._run("checkout", "-q", "-b", "feature/PROJ-42-add-thing")
        self._commit("branch work", at(9), "b.txt")

        keyed = [c for c in self._collect() if c.issue == "PROJ-42"]
        self.assertEqual(len(keyed), 1,
                         "only the commit the branch introduced should carry its issue key")

    def test_two_feature_branches_do_not_cross_contaminate(self):
        self._commit("trunk", at(8), "t.txt")
        self._run("checkout", "-q", "-b", "feature/PROJ-1-one")
        self._commit("one", at(9), "a.txt")
        self._run("checkout", "-q", "master" if self._has("master") else "main")
        self._run("checkout", "-q", "-b", "feature/PROJ-2-two")
        self._commit("two", at(10), "c.txt")

        issues = {}
        for commit in self._collect():
            issues.setdefault(commit.issue, 0)
            issues[commit.issue] += 1
        self.assertEqual(issues.get("PROJ-1"), 1)
        self.assertEqual(issues.get("PROJ-2"), 1)

    def _has(self, branch):
        return branch in self._run("for-each-ref", "--format=%(refname:short)", "refs/heads")

    def test_a_commit_with_no_key_anywhere_stays_untracked(self):
        self._commit("trunk", at(8), "t.txt")
        self._run("checkout", "-q", "-b", "chore/tidy-up")
        self._commit("no key here", at(9), "a.txt")
        self.assertTrue(all(c.issue is None for c in self._collect()))
