"""Baldur's command line and GitHub sync, through main() as baldur.cmd runs them (refinements spec, 3).

Coverage on main showed collect, github, reject, schedule and most of setup, estimate and repos never
ran in a test, nor the GitHub token's save, load and delete. These run them on real temporary git
repositories and the fake GitHub server; Windows Credential Manager and Task Scheduler are stood in
for off Windows. Each test checks what a person sees or what lands in Muninn, not lines.
"""
import contextlib
import ctypes
import datetime as dt
import importlib.util
import io
import json
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT, ROOT / "apps" / "baldur", ROOT / "tests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import fake_github as f  # noqa: E402
from asgard import muninn, paths  # noqa: E402
from baldur import cli, github  # noqa: E402
from baldur import settings as config  # noqa: E402
from test_baldur import CollectBase, MuninnCase, t  # noqa: E402


class CliCase(MuninnCase):
    def cli(self, *args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            if stdin is not None:
                stack.enter_context(mock.patch.object(sys, "stdin", stdin))
            code = cli.main(list(args))
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def setup_baldur(self, **values):
        config.update(dict({"project_keys": ["PROJ", "OPS"], "history_days": 3650}, **values))


class SetupTests(CollectBase, CliCase):
    def test_everything_set_says_what_comes_next(self):
        root = self.dir / "src"
        self.assertEqual(self.cli("setup", "--project", "proj", "--root", str(root), "--policy", "overlap"), 0,
                         self.err)
        self.assertIn("Jira projects        PROJ", self.out)
        self.assertIn("(1 repository)", self.out)
        self.assertIn("Policy               overlap (fraction", self.out)
        self.assertIn("Next: cli.py collect", self.out)
        self.assertEqual(config.load().repo_roots, [str(root.resolve())])

    def test_projects_and_folders_come_off_again(self):
        root = self.dir / "src"
        self.cli("setup", "--project", "PROJ", "--project", "OPS", "--root", str(root))
        self.assertEqual(self.cli("setup", "--remove-project", "ops", "--remove-root", str(root)), 0, self.err)
        s = config.load()
        self.assertEqual((s.project_keys, s.repo_roots), (["PROJ"], []))
        self.assertIn("cli.py setup --root", self.out)

    def test_a_folder_that_isnt_there_is_refused(self):
        self.assertEqual(self.cli("setup", "--root", str(self.dir / "nowhere")), 1)
        self.assertIn("isn't a folder", self.err)

    def test_the_email_comes_from_git_config(self):
        (self.dir / "gitconfig").write_text("[user]\n\temail = Brandon.Doe@agency.gov\n")
        self.con.execute("DELETE FROM identities WHERE kind = 'git_email'")
        self.assertEqual(self.cli("setup"), 0)
        self.assertIn("cli.py setup --from-git   (git config says Brandon.Doe@agency.gov)", self.out)
        self.assertEqual(self.cli("setup", "--from-git"), 0, self.err)
        self.assertEqual(muninn.identities(self.con, "git_email"), {"brandon.doe@agency.gov"})

    def test_without_an_email_in_git_config_it_says_how_to_give_one(self):
        self.con.execute("DELETE FROM identities WHERE kind = 'git_email'")
        self.assertEqual(self.cli("setup", "--from-git"), 1)
        self.assertIn("Use --email", self.err)
        self.assertEqual(self.cli("setup"), 0)
        self.assertIn("cli.py setup --email you@agency.gov", self.out)
        self.assertEqual(self.cli("setup", "--email", "not-an-email"), 1)
        self.assertIn("isn't an email address", self.err)
        self.assertEqual(muninn.identities(self.con, "git_email"), set())

    def test_removing_an_email_takes_its_commits_away(self):
        self.setup_baldur()
        self.commit(t(1, 10), "PROJ-1")
        self.assertEqual(self.cli("setup", "--remove-email", "brandon@agency.gov"), 0, self.err)
        self.assertIn("1 commit no longer counts as yours", self.out)
        self.assertEqual(self.con.execute("SELECT count(*) FROM commits WHERE is_mine = 1").fetchone()[0], 0)

    def test_any_setting_by_name_and_a_wrong_name_is_refused(self):
        self.assertEqual(self.cli("setup", "--set", "github_api=github.agency.gov", "--set", "history_days=90"), 0,
                         self.err)
        s = config.load()
        self.assertEqual((s.values["history_days"], s.github_host()), (90, "github.agency.gov"))
        self.assertEqual(self.cli("setup", "--set", "colour=blue"), 1)
        self.assertIn("--set takes NAME=VALUE", self.err)


class CollectCommandTests(CollectBase, CliCase):
    def test_collect_reads_the_repositories_and_says_what_it_found(self):
        self.worked_repo()
        self.setup_baldur(repo_roots=[str(self.dir / "src")])
        self.assertEqual(self.cli("collect"), 0, self.err)
        self.assertRegex(self.out, r"asgard\s+8 commits \(8 new\), 8 with Jira keys, \d+ new reflog entries")
        self.assertIn("Next: cli.py estimate", self.out)
        self.assertEqual(self.con.execute("SELECT count(*) FROM commits WHERE is_mine = 1").fetchone()[0], 8)
        self.assertIn("collect: 1 repository, 8 new commits", (paths.log_dir() / "baldur.log").read_text())
        self.assertEqual(self.cli("collect", "--quiet"), 0)        # a scheduled run prints nothing
        self.assertEqual(self.out, "")

    def test_one_repository_by_its_folder(self):
        self.worked_repo()
        self.setup_baldur()
        self.assertEqual(self.cli("collect", str(self.git.path), "--full"), 0, self.err)
        self.assertIn("8 commits (8 new)", self.out)
        self.assertEqual(self.cli("collect", str(self.dir)), 1)
        self.assertIn("isn't a git repository", self.err)

    def test_a_repository_that_fails_is_named_and_the_others_still_collected(self):
        self.worked_repo()
        broken = self.dir / "src" / "broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: " + str(self.dir / "missing") + "\n")
        self.setup_baldur(repo_roots=[str(self.dir / "src")])
        self.assertEqual(self.cli("collect", str(self.git.path), str(broken)), 1)
        self.assertIn("broken                   failed:", self.out)
        self.assertIn("Baldur: broken wasn't collected:", self.err)
        self.assertNotIn("Next: cli.py estimate", self.out)
        self.assertEqual(self.con.execute("SELECT count(*) FROM commits WHERE is_mine = 1").fetchone()[0], 8)

    def test_no_repositories_under_the_folder(self):
        empty = self.dir / "empty"
        empty.mkdir()
        self.setup_baldur(repo_roots=[str(empty)])
        self.assertEqual(self.cli("collect"), 0, self.err)
        self.assertIn("No repositories found under " + str(empty.resolve()), self.out)


class ReposTests(CliCase):
    def test_list_and_switch_off_and_on(self):
        self.setup_baldur()
        self.commit(t(1, 10), "PROJ-1")
        self.con.execute("UPDATE repos SET last_scanned_at = '2026-10-01T12:00:00Z'")
        self.assertEqual(self.cli("repos"), 0)
        self.assertRegex(self.out, r"on \s+asgard\s+1 commits\s+scanned 2026-10-01T12:00:00Z")
        self.assertEqual(self.cli("repos", "--off", "asgard"), 0)
        self.assertIn("off  asgard", self.out)
        self.assertEqual(self.con.execute("SELECT active FROM repos").fetchone()[0], 0)
        (self.dir / "asgard").mkdir()
        self.assertEqual(self.cli("repos", "--on", str(self.dir / "asgard")), 0, self.err)   # by its folder
        self.assertEqual(self.con.execute("SELECT active FROM repos").fetchone()[0], 1)

    def test_a_name_that_matches_none_or_two_is_refused(self):
        self.con.execute("INSERT INTO repos (source_id, name, local_path) VALUES (?, 'asgard', ?)",
                         (self.source, str(self.dir / "second" / "asgard")))      # a second clone, same name
        self.assertEqual(self.cli("repos", "--off", "asgard"), 1)
        self.assertIn("matches 2 repositories; use its folder instead", self.err)
        self.assertEqual(self.cli("repos", "--off", "nothing"), 1)
        self.assertIn("matches 0 repositories", self.err)

    def test_none_yet(self):
        self.con.execute("DELETE FROM repos")
        self.assertEqual(self.cli("repos"), 0)
        self.assertIn("No repositories yet: cli.py collect", self.out)

    def test_a_review_only_repository_says_so(self):
        self.con.execute("UPDATE repos SET local_path = NULL, github_repo = 'csb/asgard'")
        self.assertEqual(self.cli("repos"), 0)
        self.assertIn("GitHub csb/asgard (review only)", self.out)


class EstimateAndReportTests(CliCase):
    def setUp(self):
        super().setUp()
        self.setup_baldur()

    def test_a_dry_run_stores_nothing_and_report_prints_each_day(self):
        self.worked_example()
        self.assertEqual(self.cli("estimate", "--from", "2026-10-01", "--to", "2026-10-01", "--dry-run", "--report"),
                         0, self.err)
        self.assertIn("(dry run, nothing stored)", self.out)
        self.assertIn("PROJ-42 1h30m", self.out)
        self.assertIn("Thu 2026-10-01", self.out)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.cli("estimate", "--from", "2026-10-01", "--to", "2026-10-01", "--report"), 0)
        self.assertIn("1 day to review: cli.py days, then cli.py report DATE", self.out)
        self.assertEqual(len(self.rows("proposed")), 2)

    def test_a_range_with_no_commits_and_a_commit_dated_in_the_future(self):
        self.assertEqual(self.cli("estimate", "--from", "2026-09-01", "--to", "2026-09-03"), 0, self.err)
        self.assertIn("No commits of yours in this range.", self.out)
        later = dt.datetime.now().astimezone() + dt.timedelta(days=2)
        cid = self.commit(later, "PROJ-1")
        sha = self.con.execute("SELECT sha FROM commits WHERE id = ?", (cid,)).fetchone()[0]
        day = later.date()
        self.assertEqual(self.cli("estimate", "--from", day.isoformat(), "--to", day.isoformat()), 0, self.err)
        self.assertIn(f"Left out {sha[:10]} (", self.out)
        self.assertIn("dated in the future", self.out)

    def test_ranges_that_make_no_sense_are_refused(self):
        for args, why in ((("--from", "2026-10-02", "--to", "2026-10-01"), "--from is after --to"),
                          (("--from", "2025-01-01", "--to", "2026-10-01"), "at most a year"),
                          (("--days", "0"), "1 to 366")):
            self.assertEqual(self.cli("estimate", *args), 1, args)
            self.assertIn(why, self.err)

    def test_report_without_a_date_shows_your_latest_day_and_several_dates_each(self):
        self.worked_example()
        self.assertEqual(self.cli("report"), 0, self.err)
        self.assertIn("Thu 2026-10-01", self.out)
        self.assertEqual(self.cli("report", "2026-10-01", "2026-09-30"), 0, self.err)
        self.assertIn("Wed 2026-09-30", self.out)
        self.assertLess(self.out.index("Wed 2026-09-30"), self.out.index("Thu 2026-10-01"))

    def test_days_says_when_a_stored_day_has_changed(self):
        self.worked_example()
        self.cli("estimate", "--from", "2026-10-01", "--to", "2026-10-01")
        self.commit(t(1, 16, 30), "PROJ-51")
        self.assertEqual(self.cli("days", "--from", "2026-10-01", "--to", "2026-10-01"), 0)
        self.assertIn("Some days changed since they were estimated: cli.py estimate", self.out)


class ApproveRejectTests(CliCase):
    def setUp(self):
        super().setUp()
        self.setup_baldur()
        self.worked_example()
        self.assertEqual(self.cli("estimate", "--from", "2026-10-01", "--to", "2026-10-01"), 0, self.err)
        self.ids = self.open_ids()

    def test_reject_by_id_and_by_day(self):
        self.assertEqual(self.cli("reject", str(self.ids["PROJ-51"])), 0, self.err)
        self.assertIn("Rejected PROJ-51 30m on Thu 2026-10-01", self.out)
        self.assertEqual(self.cli("reject", "--date", "2026-10-01"), 0, self.err)
        self.assertIn("Rejected PROJ-42 1h30m", self.out)
        self.assertEqual(self.cli("reject", "--date", "2026-10-01"), 0)
        self.assertIn("Nothing to reject.", self.out)
        self.assertEqual({r["status"] for r in self.rows()}, {"rejected"})

    def test_ids_or_a_day_not_both_or_neither(self):
        for command in ("approve", "reject"):
            for args in ((), (str(self.ids["PROJ-42"]), "--date", "2026-10-01")):
                self.assertEqual(self.cli(command, *args), 1, (command, args))
                self.assertIn("not both", self.err)
        self.assertEqual({r["status"] for r in self.rows()}, {"proposed"})

    def test_options_that_go_with_a_day_or_one_proposal(self):
        one, two = str(self.ids["PROJ-42"]), str(self.ids["PROJ-51"])
        for args, why in (((one, "--ai"), "--ai goes with --date"),
                          ((one, "--set", "PROJ-42=1h"), "--set goes with --date"),
                          ((one, two, "--minutes", "1h"), "one proposal at a time"),
                          (("--date", "2026-10-01", "--set", "PROJ-42"), "--set takes KEY=MINUTES")):
            self.assertEqual(self.cli("approve", *args), 1, args)
            self.assertIn(why, self.err)
        self.assertEqual({r["status"] for r in self.rows()}, {"proposed"})
        self.assertEqual(self.cli("approve", one, "--minutes", "1h15m"), 0, self.err)
        self.assertIn("Approved PROJ-42 1h15m on Thu 2026-10-01", self.out)
        self.assertIn("Odin posts approved time on its next run.", self.out)


class KeysTests(CliCase):
    def test_a_sha_that_isnt_one_or_matches_several(self):
        self.setup_baldur()
        self.commit(t(1, 10))
        self.commit(t(1, 11))                 # shas 000...01 and 000...02 share their first 39 characters
        self.assertEqual(self.cli("keys", "xyz"), 1)
        self.assertIn("isn't a commit SHA", self.err)
        self.assertEqual(self.cli("keys", "0000"), 1)
        self.assertIn("matches 2 of your commits; give more of the SHA.", self.err)
        self.assertEqual(self.cli("keys", "ffff"), 1)
        self.assertIn("matches 0 of your commits.", self.err)

    def test_a_key_outside_your_projects_is_noted(self):
        self.setup_baldur()
        self.commit(t(1, 10))
        self.assertEqual(self.cli("keys", f"{1:040x}", "OTHER-5"), 0, self.err)
        self.assertIn("Note: OTHER isn't in your project_keys.", self.err)
        self.assertIn("now counts toward OTHER-5", self.out)


class ActualsTests(CliCase):
    def setUp(self):
        super().setUp()
        self.setup_baldur()

    def test_noting_removing_and_listing_real_hours(self):
        self.worked_example()
        self.assertEqual(self.cli("actuals", "--from", "2026-10-01", "--to", "2026-10-01"), 0)
        self.assertIn("No real hours noted between 2026-10-01 and 2026-10-01", self.out)
        self.assertEqual(self.cli("actual", "2026-10-01", "2h30m"), 0, self.err)
        self.assertEqual(self.cli("actual", "2026-10-01", "1h45m", "--key", "proj-42"), 0, self.err)
        self.assertEqual(self.cli("actuals", "--from", "2026-10-01", "--to", "2026-10-01"), 0, self.err)
        self.assertRegex(self.out, r"Thu 2026-10-01\s+2h30m\s+2h00m\s+-30m")
        self.assertRegex(self.out, r"PROJ-42\s+1h45m\s+1h30m\s+-15m")
        self.assertEqual(self.cli("actual", "2026-10-01", "--remove", "--key", "PROJ-42"), 0, self.err)
        self.assertIn("Removed the real PROJ-42 time for Thu 2026-10-01.", self.out)
        self.assertEqual(self.cli("actual", "2026-10-01", "--remove", "--key", "PROJ-42"), 0)
        self.assertIn("Nothing was noted for PROJ-42", self.out)
        self.assertEqual(self.cli("actual", "2026-10-01", "1h", "--remove"), 1)
        self.assertIn("--remove takes no time.", self.err)


class AiCommandTests(CliCase):
    def setUp(self):
        super().setUp()
        self.setup_baldur()
        self.worked_example()
        self.sha = self.con.execute("SELECT sha FROM commits ORDER BY id LIMIT 1").fetchone()[0]

    def test_a_report_from_a_file_and_the_same_one_again(self):
        report = self.dir / "report.json"
        report.write_text(json.dumps({"agent": "kiro", "minutes": 75, "commits": [self.sha], "key": "PROJ-42",
                                      "date": "2026-10-01", "confidence": "medium", "summary": "Retry on 503."}))
        self.assertEqual(self.cli("ai", "record", str(report)), 0, self.err)
        self.assertIn("Recorded r1: kiro, 1h15m on PROJ-42 (2026-10-01)", self.out)
        self.assertEqual(self.cli("ai", "record", str(report)), 0, self.err)
        self.assertIn("Already recorded as r1", self.out)
        self.assertEqual(self.cli("ai", "record", str(report), "--minutes", "1h"), 1)
        self.assertIn("not both", self.err)
        self.assertEqual(self.cli("ai", "record", str(self.dir / "missing.json")), 1)
        self.assertIn("isn't a file", self.err)

    def test_a_report_piped_in_and_too_many_at_once(self):
        piped = io.StringIO(json.dumps([{"agent": "kiro", "minutes": 30, "commits": [self.sha]}] * 101))
        self.assertEqual(self.cli("ai", "record", "-", stdin=piped), 1)
        self.assertIn("from 1 to 100 reports", self.err)

    def test_nothing_piped_from_a_terminal_says_how(self):
        tty = io.StringIO("")
        tty.isatty = lambda: True
        self.assertEqual(self.cli("ai", "record", stdin=tty), 1)
        self.assertIn("pipe it in", self.err)

    def test_an_empty_list_a_day_without_reports_and_the_pack_on_screen(self):
        self.assertEqual(self.cli("ai", "list", "--from", "2026-10-01", "--to", "2026-10-01"), 0)
        self.assertIn("No agent estimates between 2026-10-01 and 2026-10-01.", self.out)
        self.assertEqual(self.cli("ai", "show", "2026-09-15", "--json"), 0)
        config.update({"review_mode": "metadata"})
        self.assertEqual(json.loads(self.out), {"day": "2026-09-15", "method": None, "tickets": [], "flags": []})
        self.assertEqual(self.cli("ai", "pack", "2026-10-01"), 0, self.err)
        self.assertIn("PROJ-42", self.out)

    def test_kiro_files_already_there_are_kept(self):
        workspace = self.dir / "ws"
        workspace.mkdir()
        self.assertEqual(self.cli("ai", "kiro", "--into", str(workspace)), 0, self.err)
        self.assertEqual(self.cli("ai", "kiro", "--into", str(workspace)), 0, self.err)
        self.assertIn("add --force to replace them", self.out)
        self.assertEqual(self.cli("ai", "kiro", "--into", str(self.dir / "nowhere")), 1)
        self.assertIn("isn't a folder", self.err)


class CredentialManager:
    """advapi32's CredReadW, CredWriteW, CredDeleteW and CredFree, holding credentials in a dict, so the
    token code that only runs on Windows runs here. Each function accepts argtypes and restype."""

    class _Fn:
        def __init__(self, fn):
            self.fn = fn

        def __call__(self, *args):
            return self.fn(*args)

    def __init__(self):
        self.creds = {}
        self.last_error = 0
        self.fail = None              # an error code every call fails with
        self.freed = 0
        self.module = None
        self._alive = []
        for name in ("CredReadW", "CredWriteW", "CredDeleteW", "CredFree"):
            setattr(self, name, self._Fn(getattr(self, "_" + name.lower())))

    def _err(self, code):
        self.last_error = code
        return 0

    def _credreadw(self, target, kind, flags, ref):
        if self.fail:
            return self._err(self.fail)
        if (target, kind) not in self.creds:
            return self._err(1168)
        data = self.creds[(target, kind)]["blob"]
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        cred = self.module._CREDENTIAL()
        cred.CredentialBlobSize = len(data)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        self._alive += [buf, cred]
        ref._obj.contents = cred
        return 1

    def _credwritew(self, ref, flags):
        if self.fail:
            return self._err(self.fail)
        cred = ref._obj
        self.creds[(cred.TargetName, cred.Type)] = {
            "blob": ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize), "persist": cred.Persist,
            "user": cred.UserName, "comment": cred.Comment}
        return 1

    def _creddeletew(self, target, kind, flags):
        if self.fail:
            return self._err(self.fail)
        if self.creds.pop((target, kind), None) is None:
            return self._err(1168)
        return 1

    def _credfree(self, pointer):
        self.freed += 1


def github_on_windows(manager):
    """A copy of baldur/github.py loaded as Windows would load it, against the stand-in."""
    spec = importlib.util.spec_from_file_location("baldur._github_on_windows", github.__file__)
    module = importlib.util.module_from_spec(spec)
    manager.module = module
    with mock.patch.object(os, "name", "nt"), \
            mock.patch.object(ctypes, "WinDLL", create=True, new=lambda name, use_last_error=False: manager), \
            mock.patch.dict(sys.modules, {spec.name: module}):      # dataclasses look their module up there
        spec.loader.exec_module(module)
    module.os = types.SimpleNamespace(name="nt", environ=os.environ)
    return module


class TokenTests(CliCase):
    def setUp(self):
        super().setUp()
        self.manager = CredentialManager()
        self.nt = github_on_windows(self.manager)
        for patch in (mock.patch.object(ctypes, "get_last_error", create=True, new=lambda: self.manager.last_error),
                      mock.patch.dict(os.environ), mock.patch.object(cli, "github", self.nt)):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop(github.TOKEN_ENV, None)
        self.setup_baldur(github_api="github.agency.gov")

    def test_save_load_and_delete_through_credential_manager(self):
        self.assertIsNone(self.nt.load_token("github.agency.gov"))
        with mock.patch("getpass.getpass", return_value="  ghp_secret  "):
            self.assertEqual(self.cli("github", "token"), 0, self.err)
        self.assertIn('Saved in Windows Credential Manager as "Asgard Baldur GitHub github.agency.gov"', self.out)
        self.assertNotIn("ghp_secret", self.out)
        saved = self.manager.creds[("Asgard Baldur GitHub github.agency.gov", 1)]
        self.assertEqual((saved["blob"], saved["persist"], saved["user"]), (b"ghp_secret", 2, "baldur"))
        self.assertEqual(self.nt.load_token("github.agency.gov"), "ghp_secret")
        self.assertEqual(self.manager.freed, 1)                      # what CredReadW allocated is freed
        self.assertEqual(self.cli("github"), 0)
        self.assertIn("Token           saved", self.out)
        self.assertEqual(self.cli("github", "token", "--remove"), 0)
        self.assertIn("Removed the token for github.agency.gov.", self.out)
        self.assertEqual(self.cli("github", "token", "--remove"), 0)
        self.assertIn("There was no token for github.agency.gov.", self.out)
        self.assertIsNone(self.nt.load_token("github.agency.gov"))

    def test_the_environment_variable_wins(self):
        self.manager.creds[("Asgard Baldur GitHub github.agency.gov", 1)] = {"blob": b"from-the-manager"}
        os.environ[github.TOKEN_ENV] = " from-the-environment "
        self.assertEqual(self.nt.load_token("github.agency.gov"), "from-the-environment")

    def test_something_that_isnt_a_token_is_refused(self):
        with mock.patch("getpass.getpass", return_value="two words"):
            self.assertEqual(self.cli("github", "token"), 1)
        self.assertIn("doesn't look like a GitHub token", self.err)
        self.assertEqual(self.manager.creds, {})

    def test_credential_manager_errors_are_messages(self):
        self.manager.fail = 5                                        # access denied
        with self.assertRaisesRegex(self.nt.GitHubError, r"read the GitHub token .*error 5"):
            self.nt.load_token("github.agency.gov")
        with self.assertRaisesRegex(self.nt.GitHubError, r"save the token .*error 5"):
            self.nt.save_token("github.agency.gov", "ghp_x")
        with self.assertRaisesRegex(self.nt.GitHubError, r"remove the token .*error 5"):
            self.nt.delete_token("github.agency.gov")
        self.assertEqual(self.cli("github", "sync"), 1)
        self.assertIn("Baldur: Couldn't read the GitHub token", self.err)


class OffWindowsTokenTests(CliCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop(github.TOKEN_ENV, None)

    @unittest.skipIf(os.name == "nt", "Windows has Credential Manager")
    def test_off_windows_it_says_to_use_the_environment_variable(self):
        self.setup_baldur(github_api="github.agency.gov")
        self.assertIsNone(github.load_token("github.agency.gov"))
        self.assertFalse(github.delete_token("github.agency.gov"))
        with mock.patch("getpass.getpass", return_value="ghp_x"):
            self.assertEqual(self.cli("github", "token"), 1)
        self.assertIn("Elsewhere, set BALDUR_GITHUB_TOKEN", self.err)

    def test_github_off_says_how_to_turn_it_on(self):
        self.setup_baldur()
        self.assertEqual(self.cli("github"), 0)
        self.assertIn("GitHub is off (github_api is empty)", self.out)
        for args in (("github", "token"), ("github", "sync")):
            self.assertEqual(self.cli(*args), 1, args)
            self.assertIn("cli.py setup --set github_api=", self.err)

    def test_no_token_yet_says_how_to_add_one(self):
        # A server nobody has a token for, so a real Credential Manager on Windows has none either.
        self.setup_baldur(github_api="github.baldur-tests.example")
        self.assertEqual(self.cli("github", "sync"), 1)
        self.assertIn("No GitHub token for github.baldur-tests.example yet", self.err)
        self.assertEqual(self.cli("github"), 0)
        self.assertIn("Token           none: cli.py github token", self.out)
        self.assertIn("Last sync       never", self.out)


class GitHubCommandTests(CliCase):
    def setUp(self):
        super().setUp()
        self.server = f.FakeGitHub()
        self.addCleanup(self.server.close)
        self.con.execute("UPDATE repos SET github_repo = 'csb/asgard' WHERE id = ?", (self.repo,))
        self.server.pulls["csb/asgard"] = [
            f.pr(7, "Retry on 503", "bdoe", "feature/PROJ-42-retry", merged_at="2026-10-02T11:00:00Z",
                 updated="2026-10-02T11:00:00Z"),
            f.pr(8, "Session timeout", "sam", "feature/PROJ-51-timeout", requested=("bdoe",))]
        self.server.requested = [("csb/asgard", 8)]
        real = github.Client
        for patch in (mock.patch.dict(os.environ, {github.TOKEN_ENV: f.TOKEN}),
                      mock.patch.object(github, "Client", lambda api, token: real(self.server.api, token))):
            patch.start()
            self.addCleanup(patch.stop)
        self.setup_baldur(github_api="github.agency.gov")

    def test_sync_then_status(self):
        self.assertEqual(self.cli("github", "sync"), 0, self.err)
        self.assertRegex(self.out, r"GitHub github\.agency\.gov as bdoe: 1 repository, 2 pull requests changed, "
                                   r"0 new reviews, 1 review requested of you")
        self.assertEqual(self.cli("github"), 0)
        self.assertIn("Token           saved", self.out)
        self.assertNotIn("Last sync       never", self.out)

    def test_a_repository_github_hides_is_a_problem_and_the_exit_code_says_so(self):
        self.server.status["/repos/csb/asgard/pulls"] = 404
        self.assertEqual(self.cli("github", "sync"), 1)
        self.assertIn("csb/asgard:", self.err)
        self.assertIn("won't show", self.err)

    def test_a_refused_token_stops_the_whole_sync(self):
        os.environ[github.TOKEN_ENV] = "ghp_wrong"
        self.assertEqual(self.cli("github", "sync"), 1)
        self.assertIn("refused the token (401)", self.err)
        self.assertNotIn("ghp_wrong", self.err)
        self.assertEqual(self.con.execute("SELECT count(*) FROM pull_requests").fetchone()[0], 0)


class GitHubSyncEdgeTests(MuninnCase):
    """github.sync's branches that coverage showed never ran."""

    def setUp(self):
        super().setUp()
        self.server = f.FakeGitHub()
        self.addCleanup(self.server.close)
        self.client = github.Client(self.server.api, f.TOKEN)
        self.con.execute("UPDATE repos SET github_repo = 'csb/asgard' WHERE id = ?", (self.repo,))
        self.server.pulls["csb/asgard"] = [f.pr(7, "Retry on 503", "bdoe", "feature/PROJ-42-retry")]

    def sync(self):
        return github.sync(self.con, self.settings, self.client)

    def test_an_unchanged_user_comes_from_muninn(self):
        self.sync()
        self.client.requests = 0
        self.server.requests.clear()
        self.assertEqual(self.sync().login, "bdoe")
        self.assertIn(("/user", 304), [(p, s) for p, _, s in self.server.requests])

    def test_a_token_github_cant_name_is_refused(self):
        self.server.login = ""
        with self.assertRaisesRegex(github.GitHubError, "didn't say who the token belongs to"):
            self.sync()

    def test_review_requests_that_fail_are_a_problem_not_the_end(self):
        self.server.status["/search/issues"] = 500
        res = self.sync()
        self.assertEqual(res.repos, 1)
        self.assertTrue(any(p.startswith("review requests:") for p in res.problems), res.problems)
        self.assertEqual(self.con.execute("SELECT count(*) FROM pull_requests").fetchone()[0], 1)

    def test_a_401_on_one_repository_ends_the_sync(self):
        self.server.status["/repos/csb/asgard/pulls"] = 401
        with self.assertRaises(github.GitHubError) as ctx:
            self.sync()
        self.assertEqual(ctx.exception.status, 401)

    def test_a_pull_request_whose_reviews_fail_is_read_again_next_time(self):
        self.server.status["/repos/csb/asgard/pulls/7/reviews"] = 502
        res = self.sync()
        self.assertTrue(any("csb/asgard#7" in p for p in res.problems), res.problems)
        del self.server.status["/repos/csb/asgard/pulls/7/reviews"]
        again = self.sync()
        self.assertEqual(again.problems, [])
        self.assertIn("/repos/csb/asgard/pulls/7/reviews", [p for p, _, s in self.server.requests if s == 200])

    def test_a_backfilled_pull_request_that_fails_is_a_problem(self):
        self.sync()
        self.con.execute("UPDATE pull_requests SET commits_listed = 0")
        self.server.pulls["csb/asgard"][0]["updated_at"] = "2026-10-01T15:00:00Z"     # the list is unchanged
        self.server.status["/repos/csb/asgard/pulls/7"] = 500
        res = self.sync()
        self.assertTrue(any(p.startswith("csb/asgard#7:") for p in res.problems), res.problems)

    def test_rate_limit_and_errors_say_what_to_do(self):
        import urllib.error
        from email.message import Message
        client = github.Client(self.server.api, f.TOKEN)

        def explain(code, **headers):
            h = Message()
            for k, v in headers.items():
                h[k] = v
            return str(client._explain(urllib.error.HTTPError("u", code, "x", h, None), "/x"))

        self.assertIn("rate limit is used up; Baldur tries again after",
                      explain(403, **{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1790000000"}))
        self.assertIn("tries again after later", explain(403, **{"X-RateLimit-Remaining": "0"}))
        self.assertIn("won't show /x to this token (404)", explain(404))
        self.assertIn("answered /x with HTTP 500.", explain(500))


class ScheduleTests(CliCase):
    def test_the_command_it_would_run(self):
        python = self.dir / "python.exe"
        (self.dir / "pythonw.exe").write_text("")
        cmd = cli.schedule_command("TUE", "08:30", python=str(python))
        self.assertEqual(cmd[:11], ["schtasks", "/Create", "/F", "/SC", "WEEKLY", "/D", "TUE", "/ST", "08:30", "/IT",
                                    "/TN"])
        self.assertEqual(cmd[11], "Asgard Baldur collect")
        self.assertEqual(cmd[-1], f'"{self.dir / "pythonw.exe"}" "{cli.ENTRY}" collect --quiet')
        self.assertEqual(cli.schedule_command("MON", "09:00", remove=True),
                         ["schtasks", "/Delete", "/F", "/TN", "Asgard Baldur collect"])
        with self.assertRaisesRegex(cli.CliError, "261 characters"):
            cli.schedule_command("MON", "09:00", python="C:\\" + "x" * 300 + "\\python.exe")

    def schedule(self, *args, returncode=0, stderr=""):
        done = subprocess.CompletedProcess([], returncode, "", stderr)
        fake_os = types.SimpleNamespace(name="nt", path=os.path, environ=os.environ)
        with mock.patch.object(cli, "os", fake_os), mock.patch.object(cli.subprocess, "run", return_value=done) as run:
            code = self.cli("schedule", *args)
        return code, run

    def test_it_asks_task_scheduler_and_says_when(self):
        code, run = self.schedule("--day", "wednesday", "--time", "07:45")
        self.assertEqual(code, 0, self.err)
        self.assertIn("Baldur will collect every WED at 07:45", self.out)
        self.assertEqual(run.call_args[0][0][6:9], ["WED", "/ST", "07:45"])
        code, run = self.schedule("--remove")
        self.assertIn("Removed the weekly collection.", self.out)
        self.assertEqual(run.call_args[0][0][:2], ["schtasks", "/Delete"])

    def test_a_refusal_and_bad_input_are_messages(self):
        code, _ = self.schedule(returncode=1, stderr="ERROR: Access is denied.")
        self.assertEqual(code, 1)
        self.assertIn("Task Scheduler refused: ERROR: Access is denied.", self.err)
        self.assertIn("run cli.py collect at least weekly yourself", self.err)
        for args, why in ((("--day", "FUNDAY"), "--day is one of"), (("--time", "25:00"), "--time is like 09:00")):
            code, run = self.schedule(*args)
            self.assertEqual(code, 1, args)
            self.assertIn(why, self.err)
            run.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Task Scheduler is there")
    def test_off_windows_it_says_to_collect_by_hand(self):
        self.assertEqual(self.cli("schedule"), 1)
        self.assertIn("uses Windows Task Scheduler", self.err)


class MainTests(CliCase):
    def test_no_command_prints_the_help(self):
        self.assertEqual(self.cli(), 2)
        self.assertIn("Baldur: work estimates from git", self.out)

    def test_a_settings_warning_is_shown_and_the_command_still_runs(self):
        config.settings_path().parent.mkdir(parents=True, exist_ok=True)
        config.settings_path().write_text(json.dumps({"project_keys": ["PROJ"], "colour": "blue"}))
        self.assertEqual(self.cli("repos"), 0)
        self.assertIn("Baldur: baldur.json: ignored unknown settings colour", self.err)

    def test_ctrl_c_says_nothing_half_done_was_saved(self):
        with mock.patch.object(cli, "cmd_repos", side_effect=KeyboardInterrupt):
            self.assertEqual(self.cli("repos"), 130)
        self.assertIn("stopped; nothing half-done was saved", self.err)

    def test_a_log_folder_it_cant_write_doesnt_stop_collect(self):
        with mock.patch.object(paths, "log_dir", return_value=self.dir / "file-not-folder"):
            (self.dir / "file-not-folder").write_text("")
            cli._log("collect: 0 repositories")          # no exception


if __name__ == "__main__":
    unittest.main()
