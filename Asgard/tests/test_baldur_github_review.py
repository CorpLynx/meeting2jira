"""Regression tests for the independent review of Baldur's GitHub keys (docs/review-2026-10-06.md).

Each test is one confirmed finding (F1-F8) and fails without its fix. They run against
tests/fake_github.py; F1 and F8 also use a real git repository.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT, ROOT / "apps" / "baldur", ROOT / "tests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import fake_github as f  # noqa: E402
from baldur import github, store  # noqa: E402
from test_baldur import DAY, GIT, NOW, CollectBase, MuninnCase, t  # noqa: E402

NOW_TS = "2026-10-03T18:00:00Z"


class GitHubCase(MuninnCase):
    def setUp(self):
        super().setUp()
        self.server = f.FakeGitHub()
        self.addCleanup(self.server.close)
        self.client = github.Client(self.server.api, f.TOKEN)
        self.con.execute("UPDATE repos SET github_repo = 'csb/asgard' WHERE id = ?", (self.repo,))
        self.server.pulls["csb/asgard"] = []

    def sync(self):
        return github.sync(self.con, self.settings, self.client)

    def sha(self, cid):
        return self.con.execute("SELECT sha FROM commits WHERE id = ?", (cid,)).fetchone()[0]

    def keys(self, cid):
        return sorted(tuple(r) for r in self.con.execute(
            "SELECT work_item_key, method FROM commit_work_items WHERE commit_id = ?", (cid,)))

    def estimate(self):
        est = store.compute(self.con, self.settings, DAY, DAY, now=NOW)
        return {p.key: p.minutes_proposed for p in est.proposals}

    def touch(self, number, updated):
        next(p for p in self.server.pulls["csb/asgard"] if p["number"] == number)["updated_at"] = updated


class OverlappingPullRequests(GitHubCase):
    def test_f2_a_commit_in_two_prs_takes_the_smallest_and_never_flips(self):
        a, b = self.commit(t(1, 9, 50)), self.commit(t(1, 10, 20))
        c = self.commit(t(1, 10, 55))
        self.server.pulls["csb/asgard"] = [
            f.pr(1, "Part one", "bdoe", "feature/PROJ-42-a", merged_at="2026-10-02T10:00:00Z",
                 updated="2026-10-02T10:00:00Z"),
            f.pr(2, "Part two", "bdoe", "feature/PROJ-43-b", merged_at="2026-10-02T11:00:00Z",
                 updated="2026-10-02T11:00:00Z")]
        self.server.commits[("csb/asgard", 1)] = [self.sha(a), self.sha(b)]
        self.server.commits[("csb/asgard", 2)] = [self.sha(a), self.sha(b), self.sha(c)]
        self.sync()
        first = (self.keys(a), self.keys(b), self.keys(c))
        self.assertEqual(first, ([("PROJ-42", "pr")], [("PROJ-42", "pr")], [("PROJ-43", "pr")]))
        before = self.estimate()
        for number, when in ((2, "2026-10-09T09:00:00Z"), (1, "2026-10-10T09:00:00Z")):   # comments only
            self.touch(number, when)
            self.sync()
            self.assertEqual((self.keys(a), self.keys(b), self.keys(c)), first)
            self.assertEqual(self.estimate(), before)

    def test_f2_a_release_pr_doesnt_take_commits_from_their_own_prs(self):
        m1 = self.commit(t(1, 9, 50), "PROJ-10", subject="PROJ-10 fix login")
        m2 = self.commit(t(1, 10, 20), subject="search tweaks")
        self.server.pulls["csb/asgard"] = [
            f.pr(11, "Fix login", "bdoe", "fix-login", merged_at="2026-10-01T15:00:00Z", updated="2026-10-01T15:00:00Z"),
            f.pr(12, "Search", "bdoe", "PROJ-11-search", merged_at="2026-10-01T16:00:00Z",
                 updated="2026-10-01T16:00:00Z")]
        self.server.commits[("csb/asgard", 11)] = [self.sha(m1)]
        self.server.commits[("csb/asgard", 12)] = [self.sha(m2)]
        self.sync()
        before = (self.keys(m1), self.keys(m2), self.estimate())
        self.assertEqual(before[:2], ([("PROJ-10", "message")], [("PROJ-11", "pr")]))
        self.server.pulls["csb/asgard"].append(f.pr(50, "October release", "bdoe", "release/PROJ-900-october",
                                                    updated="2026-10-05T09:00:00Z"))
        self.server.commits[("csb/asgard", 50)] = [self.sha(m1), self.sha(m2)]
        self.sync()
        self.assertEqual((self.keys(m1), self.keys(m2), self.estimate()), before)


class StalePullRequestKeys(GitHubCase):
    def test_f3_a_commit_that_leaves_a_pr_falls_back_to_its_message(self):
        x = self.commit(t(1, 9, 50), subject="OPS-7 tidy the poller")
        self.server.pulls["csb/asgard"] = [f.pr(7, "Retry", "bdoe", "feature/PROJ-42-retry",
                                                updated="2026-10-01T15:00:00Z")]
        self.server.commits[("csb/asgard", 7)] = [self.sha(x)]
        self.sync()
        self.assertEqual(self.keys(x), [("PROJ-42", "pr")])
        self.server.commits[("csb/asgard", 7)] = []          # force-pushed out of the PR
        self.touch(7, "2026-10-02T09:00:00Z")
        self.sync()
        self.assertEqual(self.keys(x), [("OPS-7", "message")])

    def test_f3_a_head_renamed_to_no_key_takes_its_key_away(self):
        x = self.commit(t(1, 9, 50), subject="tidy")
        self.server.pulls["csb/asgard"] = [f.pr(9, "Tidy", "bdoe", "feature/OPS-9-tidy",
                                                updated="2026-10-01T15:00:00Z")]
        self.server.commits[("csb/asgard", 9)] = [self.sha(x)]
        self.sync()
        self.assertEqual(self.keys(x), [("OPS-9", "pr")])
        self.server.pulls["csb/asgard"][0]["head"]["ref"] = "chore/tidy"
        self.touch(9, "2026-10-02T09:00:00Z")
        self.sync()
        self.assertEqual(self.keys(x), [])


class PagingErrorsAndRedirects(GitHubCase):
    def test_f4_every_changed_pr_is_read_past_the_first_page(self):
        self.server.page_size = 50
        mine = self.commit(t(1, 9, 50))
        self.server.pulls["csb/asgard"] = [f.pr(n, f"Bot {n}", "bot", f"bot/{n}", updated=f"2026-10-02T{n % 24:02d}:00:00Z")
                                           for n in range(100, 159)]
        self.server.pulls["csb/asgard"].append(f.pr(7, "Mine", "bdoe", "feature/PROJ-42", updated="2026-10-01T09:00:00Z"))
        self.server.commits[("csb/asgard", 7)] = [self.sha(mine)]
        self.sync()
        self.assertEqual(self.con.execute("SELECT count(*) FROM pull_requests").fetchone()[0], 60)
        self.assertEqual(self.keys(mine), [("PROJ-42", "pr")])

    def test_f5_one_failure_doesnt_end_the_pass_and_is_retried(self):
        a, b = self.commit(t(1, 9, 50)), self.commit(t(1, 10, 20))
        self.server.pulls["csb/asgard"] = [f.pr(7, "Seven", "bdoe", "feature/PROJ-7", updated="2026-10-01T15:00:00Z"),
                                           f.pr(8, "Eight", "bdoe", "feature/PROJ-8", updated="2026-10-01T14:00:00Z")]
        self.server.commits[("csb/asgard", 7)] = [self.sha(a)]
        self.server.commits[("csb/asgard", 8)] = [self.sha(b)]
        self.server.status["/repos/csb/asgard/pulls/8/commits"] = 500
        other = self.add_repo("portal")
        self.con.execute("UPDATE repos SET github_repo = 'csb/portal' WHERE id = ?", (other,))
        self.server.status["/repos/csb/portal/pulls"] = 500
        res = self.sync()
        self.assertEqual(len(res.problems), 2, res.problems)
        self.assertEqual((self.keys(a), self.keys(b)), ([("PROJ-7", "pr")], []))
        del self.server.status["/repos/csb/asgard/pulls/8/commits"]
        res = self.sync()
        self.assertEqual(self.keys(b), [("PROJ-8", "pr")], "the failed PR is read again next time")

    def test_f6_the_token_never_follows_a_redirect_to_another_server(self):
        elsewhere = f.FakeGitHub()
        self.addCleanup(elsewhere.close)
        self.server.redirect["/user"] = elsewhere.api + "/user"
        with self.assertRaisesRegex(github.GitHubError, "redirected to another server"):
            self.client.get("/user")
        self.assertEqual(elsewhere.requests, [])
        port = self.client.api.split(":")[2].split("/")[0]
        lookalike = self.client.api.replace(f":{port}", f":{port}1")
        self.assertFalse(self.client.same_server(lookalike + "/user"))
        self.assertFalse(github.Client("https://api.github.com", "t").same_server("https://api.github.com.evil/x"))
        self.assertTrue(self.client.same_server(self.client.api + "/repos/x/y/pulls?page=2"))

    def test_f7_owner_and_name_in_another_case_are_the_same_repository(self):
        self.con.execute("UPDATE repos SET github_repo = 'CSB/Asgard' WHERE id = ?", (self.repo,))
        self.assertEqual(github._repo_for(self.con, self.source, "csb/asgard"), self.repo)


@unittest.skipUnless(GIT, "git isn't installed")
class WithRealGit(CollectBase):
    def setUp(self):
        super().setUp()
        self.server = f.FakeGitHub()
        self.addCleanup(self.server.close)
        self.client = github.Client(self.server.api, f.TOKEN)
        self.settings.values["github_api"] = "https://github.agency.gov/api/v3"

        self.git.git("remote", "add", "origin", "https://github.agency.gov/csb/asgard.git")

    def keys_of(self, sha):
        return sorted(tuple(r) for r in self.con.execute(
            "SELECT w.work_item_key, w.method FROM commit_work_items w JOIN commits c ON c.id = w.commit_id "
            "WHERE c.sha = ?", (sha,)))

    def test_f1_github_synced_before_the_commits_are_collected_still_keys_them(self):
        g = self.git
        g.commit("2026-09-30T15:00:00", "initial", author=("Sam", "sam@agency.gov"))
        self.collect()
        later = [g.commit(f"2026-10-01T{h:02d}:00:00", "retry work") for h in (9, 10)]
        self.server.pulls["csb/asgard"] = [f.pr(7, "Retry", "bdoe", "feature/PROJ-42-retry",
                                                merged_at="2026-10-02T11:00:00Z", updated="2026-10-02T11:00:00Z")]
        self.server.commits[("csb/asgard", 7)] = later
        github.sync(self.con, self.settings, self.client)     # the window's start-up tick, before Collect
        self.collect()
        self.assertEqual([self.keys_of(s) for s in later], [[("PROJ-42", "pr")]] * 2)

    def test_f8_a_squash_with_an_edited_title_is_still_a_copy(self):
        g = self.git
        g.commit("2026-09-30T15:00:00", "initial", author=("Sam", "sam@agency.gov"))
        g.git("checkout", "-q", "-b", "work")
        work = [g.commit(f"2026-10-01T{h:02d}:00:00", "retry work") for h in (9, 10)]
        g.git("checkout", "-q", "main")
        g.git("merge", "-q", "--squash", "work")
        g.git("commit", "-q", "-m", "Retry on 503\n\n* PROJ-42 retry work",
              env={"GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": "noreply@github.com",
                   "GIT_AUTHOR_DATE": "2026-10-01T15:40:00", "GIT_COMMITTER_DATE": "2026-10-01T15:40:00"})
        squash = g.git("rev-parse", "HEAD").strip()
        self.collect()                                            # the PR's own commits are collected
        g.git("branch", "-q", "-D", "work")
        self.collect()
        self.server.pulls["csb/asgard"] = [f.pr(7, "Retry on 503", "bdoe", "feature/PROJ-42-retry",
                                                merged_at="2026-10-02T11:00:00Z", updated="2026-10-02T11:00:00Z",
                                                merge_commit_sha=squash)]
        self.server.commits[("csb/asgard", 7)] = work
        github.sync(self.con, self.settings, self.client)
        row = self.con.execute("SELECT is_merge FROM commits WHERE sha = ?", (squash,)).fetchone()
        self.assertEqual((row[0], self.keys_of(squash)), (1, []))
        self.collect()                                            # and collection doesn't undo it
        row = self.con.execute("SELECT is_merge FROM commits WHERE sha = ?", (squash,)).fetchone()
        self.assertEqual((row[0], self.keys_of(squash)), (1, []))


class Rereview(GitHubCase):
    """The second, independent review (G1-G8 in docs/review-2026-10-06.md)."""

    def merged(self, number, head, merge_sha=None, updated="2026-10-02T11:00:00Z", **kw):
        return f.pr(number, f"PR {number}", "bdoe", head, merged_at=updated, updated=updated,
                    merge_commit_sha=merge_sha, **kw)

    def patch(self, cid, patch_id):
        self.con.execute("UPDATE commits SET patch_id = ? WHERE id = ?", (patch_id, cid))

    def test_g1_prs_stored_before_v3_are_listed_even_when_the_list_is_unchanged(self):
        x = self.commit(t(1, 9, 50), subject="OPS-7 tidy")
        self.server.pulls["csb/asgard"] = [f.pr(40, "Old", "bdoe", "feature/PROJ-40", updated="2026-09-01T09:00:00Z")]
        self.server.commits[("csb/asgard", 40)] = []
        self.sync()
        # As a v2 database left it: a 'pr' key, no commit list, and a cursor that answers 304.
        self.con.execute("DELETE FROM pull_request_commits")
        self.con.execute("UPDATE pull_requests SET commits_listed = 0")
        self.con.execute("INSERT INTO commit_work_items (commit_id, work_item_key, method) VALUES (?, 'PROJ-40', 'pr')"
                         " ON CONFLICT DO NOTHING", (x,))
        self.con.execute("DELETE FROM commit_work_items WHERE commit_id = ? AND method <> 'pr'", (x,))
        self.sync()
        self.assertEqual(self.keys(x), [("OPS-7", "message")], "PR #40 doesn't hold it, so its message decides")

    def test_g2_a_squash_titled_like_one_of_its_commits_is_still_a_copy(self):
        a, b = self.commit(t(1, 9, 50), subject="retry work"), self.commit(t(1, 10, 20), subject="retry tests")
        s = self.commit(t(1, 15, 40), subject="retry work")          # squash, titled like the first commit
        self.patch(a, "p-a")
        self.patch(b, "p-b")
        self.patch(s, "p-squash")
        self.server.pulls["csb/asgard"] = [self.merged(7, "feature/PROJ-42-retry", self.sha(s))]
        self.server.commits[("csb/asgard", 7)] = [self.sha(a), self.sha(b)]
        self.sync()
        self.assertEqual(self.con.execute("SELECT is_merge FROM commits WHERE id = ?", (s,)).fetchone()[0], 1)

    def test_g6_g7_a_rebase_merge_counts_once_with_the_prs_key(self):
        a, b = self.commit(t(1, 9, 50)), self.commit(t(1, 10, 20))
        a2, b2 = self.commit(t(1, 9, 50, ), subject="rebased a"), self.commit(t(1, 10, 20), subject="rebased b")
        for cid, p in ((a, "p-a"), (b, "p-b"), (a2, "p-a"), (b2, "p-b")):
            self.patch(cid, p)
        self.server.pulls["csb/asgard"] = [self.merged(7, "feature/PROJ-42-retry", self.sha(b2))]
        self.server.commits[("csb/asgard", 7)] = [self.sha(a), self.sha(b)]
        self.sync()
        self.assertEqual(self.con.execute("SELECT is_merge FROM commits WHERE id = ?", (b2,)).fetchone()[0], 0,
                         "a rebase copy isn't a squash")
        self.assertEqual([self.keys(c) for c in (a, b, a2, b2)], [[("PROJ-42", "pr")]] * 4,
                         "the rebased copies carry the PR's key too, so either copy counts the same")

    def test_g7_a_squash_of_work_done_elsewhere_counts(self):
        s = self.commit(t(1, 15, 40), subject="Retry on 503")
        self.patch(s, "p-squash")
        self.server.pulls["csb/asgard"] = [self.merged(7, "feature/PROJ-42-retry", self.sha(s))]
        self.server.commits[("csb/asgard", 7)] = ["a" * 40, "b" * 40]  # never collected here
        self.sync()
        self.assertEqual(self.con.execute("SELECT is_merge FROM commits WHERE id = ?", (s,)).fetchone()[0], 0)

    def test_g3_g4_merged_beats_closed_and_draft_and_numbers_stay_in_their_repo(self):
        a = self.commit(t(1, 9, 50))
        fork = self.add_repo("asgard-fork")
        self.con.execute("UPDATE repos SET github_repo = 'bdoe/asgard' WHERE id = ?", (fork,))
        self.server.pulls["bdoe/asgard"] = [f.pr(2, "wip", "bdoe", "wip", updated="2026-10-01T15:00:00Z",
                                                 repo="bdoe/asgard")]
        self.server.commits[("bdoe/asgard", 2)] = [self.sha(a)]
        self.server.pulls["csb/asgard"] = [
            self.merged(12, "feature/PROJ-42-retry"),
            f.pr(13, "spike", "bdoe", "feature/PROJ-41-spike", state="closed", updated="2026-10-03T09:00:00Z"),
            f.pr(14, "later", "bdoe", "feature/OPS-9-later", draft=True, updated="2026-10-08T09:00:00Z")]
        for n in (12, 13, 14):
            self.server.commits[("csb/asgard", n)] = [self.sha(a)]
        self.server.commits[("csb/asgard", 12)].append("c" * 40)       # the merged PR is the bigger one
        self.sync()
        self.assertEqual(self.keys(a), [("PROJ-42", "pr")])

    def test_g4_two_lists_cut_off_at_250_are_no_evidence(self):
        a = self.commit(t(1, 9, 50), "OPS-3", subject="OPS-3 retry")
        self.server.page_size = 1000
        self.server.pulls["csb/asgard"] = [self.merged(3, "release/PROJ-900"), self.merged(5, "feature/PROJ-42")]
        self.server.commits[("csb/asgard", 3)] = [self.sha(a)] + [f"{n:040x}" for n in range(10**6, 10**6 + 249)]
        self.server.commits[("csb/asgard", 5)] = [self.sha(a)] + [f"{n:040x}" for n in range(2 * 10**6, 2 * 10**6 + 249)]
        self.sync()
        self.assertEqual(self.keys(a), [("OPS-3", "message")], "neither PR's size can be known: the message decides")

    def test_g5_a_stored_newest_pr_doesnt_stop_the_first_read(self):
        # A review-only row folded into this clone holds the newest PR; the rest were never read.
        self.server.page_size = 2
        mine = self.commit(t(1, 9, 50))
        self.server.pulls["csb/asgard"] = [f.pr(n, f"PR {n}", "bdoe" if n == 2 else "sam", f"feature/PROJ-{n}",
                                                updated=f"2026-10-0{n}T09:00:00Z") for n in (1, 2, 3, 4)]
        self.server.commits[("csb/asgard", 2)] = [self.sha(mine)]
        run = self.con.execute("INSERT INTO sync_runs (app, stream) VALUES ('baldur', 'x') RETURNING id").fetchone()[0]
        self.con.execute("INSERT INTO pull_requests (repo_id, number, title, author, head_ref, state, created_at, "
                         "updated_at, url, first_seen_at, last_seen_at, run_id) VALUES (?, 4, 'PR 4', 'sam', "
                         "'feature/PROJ-4', 'open', ?, '2026-10-04T09:00:00Z', 'u', ?, ?, ?)",
                         (self.repo, NOW_TS, NOW_TS, NOW_TS, run))
        self.sync()
        self.assertEqual(self.keys(mine), [("PROJ-2", "pr")])

    def test_g5_a_list_longer_than_the_page_limit_is_read_over_the_next_passes(self):
        self.server.page_size = 5
        mine = self.commit(t(1, 9, 50))
        self.server.pulls["csb/asgard"] = [f.pr(n, f"Bot {n}", "bot", f"bot/{n}",
                                                updated=f"2026-10-02T{n // 60:02d}:{n % 60:02d}:00Z")
                                           for n in range(100, 160)]
        self.server.pulls["csb/asgard"].append(f.pr(1, "Mine", "bdoe", "feature/PROJ-1", updated="2026-10-01T09:00:00Z"))
        self.server.commits[("csb/asgard", 1)] = [self.sha(mine)]
        first = self.sync()
        self.assertTrue(any("next passes" in p for p in first.problems), first.problems)
        for _ in range(3):
            self.sync()
        self.assertEqual(self.con.execute("SELECT count(*) FROM pull_requests").fetchone()[0], 61)
        self.assertEqual(self.keys(mine), [("PROJ-1", "pr")])

    def test_g8_falling_back_uses_the_whole_message(self):
        x = self.commit(t(1, 9, 50), subject="tidy")
        self.server.pulls["csb/asgard"] = [f.pr(7, "Retry", "bdoe", "feature/PROJ-42", updated="2026-10-01T15:00:00Z")]
        self.server.commits[("csb/asgard", 7)] = [self.sha(x)]
        self.sync()
        self.server.commits[("csb/asgard", 7)] = []
        self.touch(7, "2026-10-02T09:00:00Z")
        self.sync()
        from baldur import collect as C
        C.apply_pr_evidence(self.con, ["PROJ"], {x: ["PROJ-77"]})      # what collection found in the body
        self.assertEqual(self.keys(x), [])                              # already fell back to the subject: none
        self.con.execute("INSERT INTO commit_work_items (commit_id, work_item_key, method) VALUES (?, 'PROJ-42', 'pr')",
                         (x,))
        C.apply_pr_evidence(self.con, ["PROJ"], {x: ["PROJ-77"]})
        self.assertEqual(self.keys(x), [("PROJ-77", "message")])


if __name__ == "__main__":
    unittest.main()
