"""Muninn hardening: the ownership guard, secret scrubbing, keys, damage and restore, busy handling,
housekeeping, the full check and repairs, schema v3, and the console commands."""
import contextlib
import datetime as dt
import io
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT, ROOT / "tests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from asgard import muninn  # noqa: E402
from asgard.muninn import baldur, cli, db, guard, integrity, keys, odin, redact  # noqa: E402
from test_muninn import Base, WorklogBase  # noqa: E402

NOW = "2026-10-02T14:05:00Z"


def schema_tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_schema WHERE type = 'table' "
                                      "AND name NOT LIKE 'sqlite%' AND name NOT LIKE 'search%'")}


# --------------------------------------------------------------------------
# The ownership guard (rule 5)
# --------------------------------------------------------------------------

class GuardTests(WorklogBase):
    def app(self, name):
        con = muninn.open_app(name, supported=(1, muninn.SCHEMA_VERSION), path=self.path)
        self.addCleanup(con.close)
        return con

    def test_every_table_has_an_owner(self):
        known = set(guard.OWNERS) | set(guard.SHARED)
        self.assertEqual(sorted(schema_tables(self.con) - known), [], "add new tables to guard.OWNERS or SHARED")
        self.assertEqual(sorted(known - schema_tables(self.con)), [], "guard names a table the schema doesn't have")

    def test_an_app_writes_its_own_tables_and_the_search_index_follows(self):
        con = self.app("baldur")
        repo = con.execute("INSERT INTO repos (name, local_path) VALUES ('r', '/r') RETURNING id").fetchone()[0]
        con.execute("INSERT INTO commits (repo_id, sha, author_name, author_email, authored_at, committed_at, subject, "
                    "first_seen_at) VALUES (?, ?, 'Me', 'me@x', ?, ?, 'Retry the poll', ?)",
                    (repo, "a" * 40, NOW, NOW, NOW))
        self.assertEqual(con.execute("SELECT count(*) FROM search WHERE search MATCH 'retry'").fetchone()[0], 1)

    def test_writes_to_other_apps_tables_are_refused_with_a_reason(self):
        con = self.app("baldur")
        with self.assertRaisesRegex(sqlite3.DatabaseError, "not authorized"):
            con.execute("UPDATE worklogs SET seconds = 1")
        self.assertEqual(guard.last_denial("baldur"), "baldur may not update worklogs, which odin owns")
        try:
            con.execute("DELETE FROM work_items")
        except sqlite3.DatabaseError as exc:
            self.assertIn("baldur may not delete from work_items", guard.describe(exc))
        odin_con = self.app("odin")
        with self.assertRaisesRegex(sqlite3.DatabaseError, "not authorized"):
            odin_con.execute("UPDATE day_proposals SET status = 'approved'")

    def test_shared_tables_take_only_what_every_app_needs(self):
        con = self.app("loki")
        muninn.emit(con, "loki", "meeting.recapped", "meetings", 1)
        for sql in ("UPDATE events SET kind = 'x.y'", "DELETE FROM events", "DELETE FROM sync_runs",
                    "DELETE FROM sources"):
            with self.subTest(sql=sql), self.assertRaisesRegex(sqlite3.DatabaseError, "not authorized"):
                con.execute(sql)

    def test_schema_changes_attach_and_dangerous_pragmas_are_refused(self):
        con = self.app("freya")
        for sql in ("CREATE TABLE mine (x)", "DROP VIEW v_activity", "CREATE INDEX ix_x ON citations (owner_id)",
                    f"ATTACH DATABASE '{self.dir / 'other.db'}' AS other", "PRAGMA foreign_keys = OFF",
                    "PRAGMA user_version = 99", "PRAGMA query_only = 0", "PRAGMA writable_schema = ON",
                    "PRAGMA recursive_triggers = OFF"):
            with self.subTest(sql=sql), self.assertRaisesRegex(sqlite3.DatabaseError, "not authorized"):
                con.execute(sql)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], muninn.SCHEMA_VERSION, "reading is fine")
        self.assertEqual(con.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    @unittest.skipUnless(hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"), "defensive mode needs Python 3.12+")
    def test_the_search_index_tables_are_read_only_to_apps(self):
        con = self.app("baldur")
        with self.assertRaises(sqlite3.DatabaseError):
            con.execute("DELETE FROM search_content")

    def test_a_refused_write_inside_a_run_is_recorded_with_its_reason(self):
        con = self.app("baldur")
        with self.assertRaises(sqlite3.DatabaseError):
            with muninn.Run(con, "baldur", None, "commits:x") as run:
                with run.batch():
                    con.execute("UPDATE work_items SET summary = 'x'")
        error = self.con.execute("SELECT error FROM sync_runs WHERE app = 'baldur' ORDER BY id DESC").fetchone()[0]
        self.assertIn("baldur may not update work_items, which odin owns", error)

    def test_unknown_apps_are_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown app"):
            muninn.open_app("mallory", supported=(1, muninn.SCHEMA_VERSION), path=self.path)


# --------------------------------------------------------------------------
# Secrets never reach Muninn (rule 9)
# --------------------------------------------------------------------------

class RedactTests(WorklogBase):
    def test_scrub_masks_credentials_by_shape(self):
        cases = {
            "GET https://bob:hunter2@jira.example.gov/rest": "GET https://bob:***@jira.example.gov/rest",
            "Authorization: Bearer abcdefgh12345678": "Authorization: Bearer ***",
            "GITHUB_TOKEN=s3cr3tVALUE9 JIRA_PAT=abc123def456": "GITHUB_TOKEN=*** JIRA_PAT=***",
            "Cookie: JSESSIONID=abc123; other=1": "Cookie: ***",
            "https://bob:p@ss@jira.example.gov/x": "https://bob:***@jira.example.gov/x",
            "GET /rest?access_token=abc123def": "GET /rest?access_token=***",
            "sent bearer abcdefgh12345678 to it": "sent bearer *** to it",
            "token=s3cr3t&x=1": "token=***&x=1",
            '{"password": "pw", "user": "u"}': '{"password": "***", "user": "u"}',
            "ghp_" + "a" * 36 + " leaked": "*** leaked",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----": "***",
            "AKIAABCDEFGHIJKLMNOP": "***",
        }
        for text, want in cases.items():
            with self.subTest(text=text[:30]):
                self.assertEqual(redact.scrub(text), want)
        for plain in ("PROJ-123 took 2h", "Add Basic authentication fallback", "Authorization: approved by lead",
                      "token: refresh race", "Pat: 1:1 notes", "ssh://git@github.com/o/r.git"):
            self.assertEqual(redact.scrub(plain), plain)
        self.assertIsNone(redact.scrub(None))
        self.assertEqual(redact.scrub_value({"a": ["token=s3cr3t1", 3], "b": ("Bearer abcdefgh12345678",),
                                             "password=hunter2": {1, 2}, "e": RuntimeError("pwd=x9")}),
                         {"a": ["token=***", 3], "b": ["Bearer ***"], "password=***": [1, 2], "e": "pwd=***"})

    def test_redact_url_drops_passwords_and_tokens_but_keeps_names(self):
        self.assertEqual(redact.redact_url("https://bob:pw@host/x.git"), "https://bob@host/x.git")
        self.assertEqual(redact.redact_url("https://ghp_" + "b" * 36 + "@host/x.git"), "https://host/x.git")
        self.assertEqual(redact.redact_url("git@github.agency.gov:csb/asgard.git"), "git@github.agency.gov:csb/asgard.git")
        self.assertEqual(redact.redact_url("https://jira.example.gov"), "https://jira.example.gov")

    def test_the_writer_boundary_scrubs_everything_it_stores(self):
        sid = muninn.ensure_source(self.con, "git", "local-git", "https://bob:pw@git.example.gov")
        self.assertEqual(self.con.execute("SELECT base_url FROM sources WHERE id = ?", (sid,)).fetchone()[0],
                         "https://bob@git.example.gov")
        muninn.emit(self.con, "odin", "worklog.failed", "worklogs", 1, payload={"error": "Authorization: Bearer abcdefgh12345678"})
        payload = self.con.execute("SELECT payload FROM events ORDER BY id DESC").fetchone()[0]
        self.assertNotIn("abcdefgh12345678", payload)
        with muninn.Run(self.con, "odin", self.sid, "issues") as run:
            run.problem("ABC-9 failed: token=letmein")
        self.assertNotIn("letmein", self.con.execute("SELECT error FROM sync_runs ORDER BY id DESC").fetchone()[0])
        with self.assertRaises(RuntimeError):
            with muninn.Run(self.con, "odin", self.sid, "issues"):
                raise RuntimeError("proxy said: https://u:topsecret@proxy.example.gov")
        self.assertNotIn("topsecret", self.con.execute("SELECT error FROM sync_runs ORDER BY id DESC").fetchone()[0])
        post = odin.begin_post(self.con, self.approved_day(60))
        odin.fail_post(self.con, post.worklog_id, "HTTP 400 echoing Authorization: Bearer abcdefgh12345678")
        stored = self.con.execute("SELECT error FROM worklogs WHERE id = ?", (post.worklog_id,)).fetchone()[0]
        self.assertNotIn("abcdefgh12345678", stored)
        self.assertNotIn("abcdefgh12345678", self.con.execute("SELECT payload FROM events ORDER BY id DESC").fetchone()[0])


# --------------------------------------------------------------------------
# Jira keys
# --------------------------------------------------------------------------

class KeyTests(WorklogBase):
    def test_the_sql_rule_and_the_python_rule_agree(self):
        mem = sqlite3.connect(":memory:")
        expr = keys.SQL_IS_KEY.format(col="?1")
        samples = ["ABC-123", "A-1", "AB_C-10", "A1-2", "abc-123", "ABC-0", "ABC-012", "ABC-12a", "ABC--1",
                   "ABC-1-2", "-1", "ABC", "1AB-2", "AB C-1", "ABC-1 ", "ÄB-1", "AB*-1", "AB?-1", "AB[-1", "", "_A-1"]
        for s in samples:
            with self.subTest(key=s):
                self.assertEqual(bool(mem.execute("SELECT " + expr, (s,)).fetchone()[0]), keys.is_key(s))
        mem.close()

    def test_normalize(self):
        self.assertEqual(keys.normalize_key(" abc-12 "), "ABC-12")
        with self.assertRaisesRegex(ValueError, "isn't a Jira key"):
            keys.normalize_key("ABC 12")

    def test_odin_stores_the_keys_you_type_in_capitals(self):
        eid = self.meeting()
        odin.set_meeting_key(self.con, eid, "abc-123")
        self.assertEqual(self.con.execute("SELECT logged_as_key FROM calendar_events WHERE id = ?", (eid,)).fetchone()[0],
                         "ABC-123")
        with self.assertRaisesRegex(muninn.MuninnError, "isn't a Jira key"):
            odin.set_meeting_key(self.con, eid, "ABC 123")
        post = odin.begin_meeting_post(self.con, eid, "abc-123")
        self.assertEqual(post.key, "ABC-123")

    def test_approve_day_matches_keys_whatever_their_case(self):
        run_id = self.con.execute("INSERT INTO estimate_runs (date_from, date_to, model_version, params, params_hash) "
                                  "VALUES (?, ?, 'baldur-1', '{}', 'h') RETURNING id", (self.day, self.day)).fetchone()[0]
        self.con.execute("DROP TRIGGER day_proposals_key_ins")     # a row written before schema v3
        pid = self.con.execute("INSERT INTO day_proposals (estimate_run_id, local_date, work_item_key, minutes_raw, "
                               "minutes_proposed, first_started_at, basis, basis_hash) VALUES (?, ?, 'Abc-123', 61, 60, "
                               "?, 'b', 'h') RETURNING id", (run_id, self.day, self.started)).fetchone()[0]
        self.assertEqual(baldur.approve_day(self.con, self.day, {"abc-123": 45}), [pid])
        self.assertEqual(self.con.execute("SELECT minutes_final FROM day_proposals WHERE id = ?", (pid,)).fetchone()[0], 45)

    def test_approvals_fit_in_a_day(self):
        pid = self.approved_day(60)
        with self.assertRaisesRegex(muninn.MuninnError, "more than a day"):
            baldur.change_approval(self.con, pid, 1441)
        self.assertEqual(self.con.execute("SELECT minutes_final FROM day_proposals WHERE status = 'approved'")
                         .fetchone()[0], 60)


# --------------------------------------------------------------------------
# Schema v3 through the API
# --------------------------------------------------------------------------

class SchemaV3Tests(WorklogBase):
    def test_a_posted_worklog_never_goes_back_and_is_never_deleted(self):
        post = odin.begin_post(self.con, self.approved_day(60))
        odin.finish_post(self.con, post.worklog_id, "88001")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "can only become deleted"):
            self.con.execute("UPDATE worklogs SET state = 'failed', jira_worklog_id = NULL WHERE id = ?",
                             (post.worklog_id,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "posted twice"):
            self.con.execute("DELETE FROM worklogs WHERE id = ?", (post.worklog_id,))

    def test_baldur_time_needs_an_approval_at_insert(self):
        pid = self.approved_day(60)
        baldur.change_approval(self.con, pid, 30)                 # pid is superseded now
        with self.assertRaisesRegex(sqlite3.IntegrityError, "only for an approved day"):
            self.con.execute("INSERT INTO worklogs (work_item_id, origin, state, started_at, seconds, proposal_id) "
                             "VALUES (?, 'baldur', 'sending', ?, 60, ?)", (self.item, self.started, pid))

    def test_a_sweep_needs_a_full_run_without_problems(self):
        cal = muninn.ensure_source(self.con, "calendar", "outlook")
        self.meeting()
        with self.assertRaisesRegex(muninn.MuninnError, "mode='full'"):
            with muninn.Run(self.con, "odin", cal, "calendar") as run:
                odin.sweep_calendar(run, "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")
        with muninn.Run(self.con, "odin", cal, "calendar", mode="full") as run:
            run.problem("one event failed to store")
            self.assertEqual(odin.sweep_calendar(run, "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"), 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM calendar_events WHERE deleted_at IS NULL").fetchone()[0], 1)

    def test_insert_or_replace_takes_the_search_entry_with_it(self):
        sid = muninn.ensure_source(self.con, "manual", "paste")
        self.con.execute("INSERT INTO meetings (source_id, external_id, title, starts_at, recap_origin, first_seen_at) "
                         "VALUES (?, 'x1', 'Budget review', ?, 'paste', ?)", (sid, NOW, NOW))
        self.con.execute("INSERT OR REPLACE INTO meetings (source_id, external_id, title, starts_at, recap_origin, "
                         "first_seen_at) VALUES (?, 'x1', 'Budget review again', ?, 'paste', ?)", (sid, NOW, NOW))
        self.assertEqual(integrity.search_drift(self.con), {})

    def test_time_posted_twice_is_on_odins_tile(self):
        pid = self.approved_day(60)
        for jid in ("88001", "88002"):            # what a buggy poster, or plain sqlite3, could do
            self.con.execute("INSERT INTO worklogs (work_item_id, jira_worklog_id, origin, state, started_at, seconds, "
                             "proposal_id, posted_at) VALUES (?, ?, 'baldur', 'posted', ?, 3600, ?, ?)",
                             (self.item, jid, self.started, pid, NOW))
        badges = muninn.tile_badges(self.path)
        self.assertEqual(badges["odin"][0].text, "1 worklog posted twice")
        found = integrity.check(self.con).of("worklogs")
        self.assertEqual([f.level for f in found], ["error", "warning"])
        self.assertIn("88001, 88002", found[0].message)
        self.assertIn("Asgard sent 120 min", found[1].message)     # and Jira now holds more than approved

    def test_an_upgrade_from_v2_keeps_data_and_brings_search_up_to_date(self):
        folder = self.dir / "v2"
        folder.mkdir()
        for m in db.available_migrations()[:2]:
            shutil.copy(m.path, folder / m.name)
        path = self.dir / "old.db"
        muninn.prepare(path, folder=folder, backups=self.dir / "bk")
        old = muninn.connect(path)
        old.execute("INSERT INTO repos (name, local_path) VALUES ('r', '/r')")
        old.execute("INSERT INTO pull_requests (repo_id, number, title, author, head_ref, state, created_at, updated_at, "
                    "url, first_seen_at, last_seen_at) VALUES (1, 7, 'Retry', 'olduser', 'feature/X-1', 'open', ?, ?, "
                    "'u', ?, ?)", (NOW, NOW, NOW, NOW))
        old.execute("UPDATE pull_requests SET author = 'newuser'")      # v2's trigger missed this
        self.assertEqual(integrity.search_drift(old), {"pull_requests": (0, 1, 0)})
        old.close()
        st = muninn.prepare(path, backups=self.dir / "bk")
        self.assertEqual(st.version, muninn.SCHEMA_VERSION)
        self.assertEqual(st.migrated, [m.name for m in db.available_migrations()[2:]])
        self.assertEqual(st.migrated[0], "0003_hardening.sql")
        new = muninn.connect(path)
        self.addCleanup(new.close)
        self.assertEqual(integrity.search_drift(new), {})
        self.assertEqual(new.execute("SELECT title FROM pull_requests").fetchone()[0], "Retry")
        self.assertTrue(list((self.dir / "bk").glob("*before-v3.db")))


# --------------------------------------------------------------------------
# A damaged file, and putting a backup back
# --------------------------------------------------------------------------

class DamageAndRestoreTests(Base):
    def damage(self):
        self.con.close()
        for suffix in ("-wal", "-shm"):
            p = self.path.with_name(self.path.name + suffix)
            if p.exists():
                p.unlink()
        self.path.write_bytes(b"this is not a database" * 400)
        self.con = sqlite3.connect(":memory:")       # tearDown closes it

    def test_a_damaged_file_names_the_newest_backup_and_the_command(self):
        con = muninn.connect(self.path)
        muninn.backup(con, self.dir / "backups", label="manual", keep=5)
        con.close()
        self.damage()
        with self.assertRaises(muninn.CorruptError) as caught:
            muninn.prepare(self.path, backups=self.dir / "backups")
        text = str(caught.exception)
        self.assertIn("muninn-", text)
        self.assertIn("--muninn restore", text)
        with self.assertRaises(muninn.CorruptError):
            muninn.open_app("odin", supported=(1, muninn.SCHEMA_VERSION), path=self.path)
        self.assertTrue(self.path.exists(), "nothing is moved until you ask")

    def test_a_damaged_file_with_no_backup_says_so(self):
        self.damage()
        with self.assertRaisesRegex(muninn.CorruptError, "no backup"):
            muninn.prepare(self.path, backups=self.dir / "none")

    def test_restore_puts_the_backup_back_and_keeps_the_old_file(self):
        self.con.execute("INSERT INTO meta (key, value) VALUES ('marker', 'before')")
        backup = muninn.backup(self.con, self.dir / "backups", label="manual", keep=5)
        self.con.execute("UPDATE meta SET value = 'after' WHERE key = 'marker'")
        self.con.close()
        restored = muninn.restore(path=self.path, backups=self.dir / "backups")
        self.con = muninn.connect(restored)
        self.assertEqual(self.con.execute("SELECT value FROM meta WHERE key = 'marker'").fetchone()[0], "before")
        aside = list(self.dir.glob("muninn.before-restore-*.db"))
        self.assertEqual(len(aside), 1)
        kept = sqlite3.connect(str(aside[0]))
        self.assertEqual(kept.execute("SELECT value FROM meta WHERE key = 'marker'").fetchone()[0], "after")
        kept.close()
        self.assertTrue(backup.exists(), "the backup stays")

    def test_restore_refuses_a_bad_backup_and_a_busy_database(self):
        bad = self.dir / "backups" / "muninn-20260101-manual.db"
        bad.parent.mkdir(exist_ok=True)
        bad.write_bytes(b"x" * 4096)
        with self.assertRaisesRegex(muninn.MuninnError, "can't be used as a backup|isn't a usable backup"):
            muninn.restore(bad, path=self.path)
        good = muninn.backup(self.con, self.dir / "backups", label="manual", keep=5)
        self.con.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaisesRegex(muninn.MuninnError, "Close Asgard"):
                muninn.restore(good, path=self.path)
        finally:
            self.con.execute("ROLLBACK")

    def test_an_unwritable_backup_folder_doesnt_stop_startup(self):
        blocker = self.dir / "file-not-folder"
        blocker.write_text("x")
        st = muninn.prepare(self.path, backups=blocker / "backups")    # a folder that can't be made
        self.assertTrue(any("backup wasn't made" in w for w in st.warnings), st.warnings)

    def test_stale_half_written_backups_are_swept(self):
        folder = self.dir / "backups"
        folder.mkdir(exist_ok=True)
        stale = folder / ".muninn-20260101.db.123-abc.tmp"
        stale.write_bytes(b"x")
        old = time.time() - db.STALE_TMP_SECONDS - 60
        os.utime(stale, (old, old))
        fresh = folder / ".muninn-20260102.db.456-def.tmp"
        fresh.write_bytes(b"x")
        muninn.backup(self.con, folder, label="manual", keep=5, replace=True)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists(), "a copy another process is still writing is left alone")

    def test_console_python_prefers_python_over_pythonw(self):
        exe = self.dir / "pythonw.exe"
        exe.write_text("")
        (self.dir / "python.exe").write_text("")
        with mock.patch.object(db.sys, "executable", str(exe)):
            self.assertEqual(Path(db.console_python()).name, "python.exe")


# --------------------------------------------------------------------------
# Waiting for the write lock
# --------------------------------------------------------------------------

class BusyTests(Base):
    def test_retry_busy_retries_then_says_plainly(self):
        calls = []

        def locked():
            calls.append(1)
            raise sqlite3.OperationalError("database is locked")
        with self.assertRaisesRegex(muninn.BusyError, "Nothing was lost"):
            muninn.retry_busy(locked, total=0.2)
        self.assertGreater(len(calls), 1)
        with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
            muninn.retry_busy(lambda: (_ for _ in ()).throw(sqlite3.OperationalError("no such table: x")), total=5)

    def test_a_write_waits_out_another_apps_lock(self):
        holder = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)   # another app
        holder.execute("BEGIN IMMEDIATE")
        threading.Timer(0.6, lambda: holder.execute("COMMIT")).start()
        quick = muninn.connect(self.path, timeout=0.1)          # gives up on each attempt after 0.1 s
        try:
            with muninn.transaction(quick):
                quick.execute("INSERT INTO meta (key, value) VALUES ('waited', 'yes')")
        finally:
            quick.close()
            time.sleep(0.05)
            holder.close()
        self.assertEqual(self.con.execute("SELECT value FROM meta WHERE key = 'waited'").fetchone()[0], "yes")

    def test_times_outside_the_calendar_are_value_errors(self):
        with self.assertRaises(ValueError):
            muninn.to_ts(dt.datetime(9999, 12, 31, 23, 0, tzinfo=dt.timezone(dt.timedelta(hours=-5))))
        with self.assertRaises(ValueError):
            odin.parse_time("9999-12-31T23:59:59.000-0500")


# --------------------------------------------------------------------------
# Housekeeping, the check, repairs
# --------------------------------------------------------------------------

class MaintainTests(WorklogBase):
    def old(self, days):
        return muninn.ago(days * 86400)

    def seed_old_rows(self):
        old_run = self.con.execute("INSERT INTO sync_runs (app, stream, started_at, status) VALUES "
                                   "('odin', 'issues', ?, 'ok') RETURNING id", (self.old(200),)).fetchone()[0]
        self.con.execute("UPDATE work_items SET run_id = ? WHERE id = ?", (old_run, self.item))
        self.con.execute("INSERT INTO sync_runs (app, stream, started_at, status) VALUES ('odin', 'issues', ?, "
                         "'running')", (self.old(200),))
        er = "INSERT INTO estimate_runs (created_at, date_from, date_to, model_version, params, params_hash) " \
             "VALUES (?, '2026-01-01', '2026-01-01', 'baldur-1', '{}', 'h') RETURNING id"
        undecided, decided, waiting = (self.con.execute(er, (self.old(100),)).fetchone()[0] for _ in range(3))
        prop = ("INSERT INTO day_proposals (estimate_run_id, local_date, work_item_key, minutes_raw, minutes_proposed, "
                "first_started_at, basis, basis_hash, status, decided_at) VALUES (?, ?, 'ABC-123', 10, 10, ?, 'b', 'h', "
                "?, ?)")
        self.con.execute(prop, (undecided, "2026-01-01", NOW, "superseded", None))
        self.con.execute(prop, (decided, "2026-01-02", NOW, "rejected", NOW))
        self.con.execute(prop, (waiting, "2026-01-03", NOW, "proposed", None))
        self.con.execute("INSERT INTO repos (name, local_path) VALUES ('r', '/r')")
        for days in (400, 10):
            self.con.execute("INSERT INTO reflog_entries (repo_id, at, action, sha) VALUES (1, ?, 'checkout', ?)",
                             (self.old(days), f"{days:040d}"))
        return undecided, decided, waiting

    def test_housekeeping_runs_once_a_day_and_prunes_nothing_until_retention_is_on(self):
        self.seed_old_rows()
        report = integrity.maintain(self.con, today=dt.date(2030, 1, 1))
        self.assertTrue(report.ran)
        self.assertEqual((report.pruned, report.retention), ({}, False))
        self.assertFalse(integrity.maintain(self.con, today=dt.date(2030, 1, 1)).ran, "once a day")
        self.assertEqual(self.con.execute("SELECT count(*) FROM reflog_entries").fetchone()[0], 2)

    def test_retention_prunes_only_what_nothing_needs(self):
        undecided, decided, waiting = self.seed_old_rows()
        integrity.set_retention(self.con, True)
        report = integrity.maintain(self.con, force=True)
        self.assertEqual(report.pruned, {"sync_runs": 1, "estimate_runs": 1, "reflog_entries": 1})
        self.assertIsNone(self.con.execute("SELECT run_id FROM work_items WHERE id = ?", (self.item,)).fetchone()[0])
        left = {r[0] for r in self.con.execute("SELECT id FROM estimate_runs")}
        self.assertTrue({decided, waiting} <= left and undecided not in left)
        self.assertEqual(self.con.execute("SELECT count(*) FROM sync_runs WHERE status = 'running'").fetchone()[0], 1)

    def test_pruning_stops_at_its_time_budget(self):
        self.seed_old_rows()
        pruned, more = integrity.prune(self.con, budget=0)
        self.assertEqual((pruned, more), ({}, True))

    def test_prepare_does_the_days_housekeeping(self):
        st = muninn.prepare(self.path, backups=self.dir / "backups")
        self.assertIsInstance(st.maintained, integrity.MaintainReport)


class CheckAndRepairTests(WorklogBase):
    def fill_every_search_type(self):
        c = self.con
        sid = muninn.ensure_source(c, "manual", "paste")
        c.execute("INSERT INTO repos (name, local_path) VALUES ('r', '/r')")
        c.execute("INSERT INTO commits (repo_id, sha, author_name, author_email, authored_at, committed_at, subject, "
                  "branch_hint, first_seen_at) VALUES (1, ?, 'Me', 'me@x', ?, ?, 'Retry', 'feature/ABC-1', ?)",
                  ("c" * 40, NOW, NOW, NOW))
        c.execute("INSERT INTO pull_requests (repo_id, number, title, author, head_ref, state, created_at, updated_at, "
                  "url, first_seen_at, last_seen_at) VALUES (1, 7, 'Retry', 'me', 'feature/ABC-1', 'open', ?, ?, 'u', "
                  "?, ?)", (NOW, NOW, NOW, NOW))
        mid = c.execute("INSERT INTO meetings (source_id, external_id, title, starts_at, recap_origin, notes_summary, "
                        "first_seen_at) VALUES (?, 'm', 'Sync', ?, 'paste', 'notes', ?) RETURNING id",
                        (sid, NOW, NOW)).fetchone()[0]
        c.execute("INSERT INTO action_items (meeting_id, text, owner, work_item_key) VALUES (?, 'Do it', 'me', 'ABC-1')",
                  (mid,))
        c.execute("INSERT INTO blufs (subject_type, subject_ref, bottom_line, body_md, tier, prompt_version) "
                  "VALUES ('adhoc', 'x', 'Bottom line', 'Body', 'manual', 'p1')")
        c.execute("INSERT INTO submissions (system, title, external_id, work_item_key) "
                  "VALUES ('secchm', 'Change', 'CHG1', 'ABC-1')")
        c.execute("INSERT INTO accomplishments (jira_id, work_item_key, summary, issue_type, url, state, first_done_at, "
                  "last_done_at, stats_refreshed_at, impact_note) VALUES ('1', 'ABC-1', 'Won', 'Story', 'u', 'done', "
                  "?, ?, ?, 'mattered')", (NOW, NOW, NOW))
        # Every column each entry shows, changed once, so the update triggers are exercised too.
        c.execute("UPDATE pull_requests SET author = 'me2', number = 8, title = 'Retry 2', head_ref = 'feature/ABC-2'")
        c.execute("UPDATE submissions SET system = 'bears', external_id = 'CHG2', work_item_key = 'ABC-2'")
        c.execute("UPDATE commits SET subject = 'Retry again', branch_hint = NULL")
        c.execute("UPDATE meetings SET title = 'Sync 2', notes_summary = NULL")
        c.execute("UPDATE action_items SET owner = NULL")
        c.execute("UPDATE accomplishments SET impact_note = NULL, epic_key = 'ABC-9'")

    def test_the_check_scripts_expressions_match_the_triggers(self):
        self.fill_every_search_type()
        counts = {r[0] for r in self.con.execute("SELECT rowid % 16 FROM search")}
        self.assertEqual(counts, set(integrity.SEARCH_SOURCES), "a row of every indexed type")
        self.assertEqual(integrity.search_drift(self.con), {})

    def test_a_healthy_database_has_nothing_to_report(self):
        self.fill_every_search_type()
        report = integrity.check(self.con)
        self.assertEqual(report.findings, [])
        self.assertTrue(report.ok)

    def test_drift_is_found_and_repaired(self):
        self.fill_every_search_type()
        self.con.execute("DELETE FROM search WHERE rowid % 16 = 2")
        self.con.execute("UPDATE search SET title = 'wrong' WHERE rowid % 16 = 4")
        self.con.execute("INSERT INTO search (rowid, title, body, kind) VALUES (999 * 16 + 6, 'ghost', '', 'blufs')")
        self.assertEqual(integrity.search_drift(self.con),
                         {"commits": (1, 0, 0), "meetings": (0, 1, 0), "blufs": (0, 0, 1)})
        self.assertEqual(len(integrity.check(self.con).of("search")), 3)
        done = integrity.repair(self.con)
        self.assertIn("Rebuilt the search index", done[0])
        self.assertEqual(integrity.search_drift(self.con), {})

    def test_other_findings_say_what_to_do(self):
        self.con.execute("DROP TRIGGER commit_work_items_key_ins")       # as rows written before v3
        self.con.execute("INSERT INTO repos (name, local_path) VALUES ('r', '/r')")
        self.con.execute("INSERT INTO commits (repo_id, sha, author_name, author_email, authored_at, committed_at, "
                         "subject, first_seen_at) VALUES (1, ?, 'Me', 'me@x', ?, ?, 's', ?)", ("d" * 40, NOW, NOW, NOW))
        self.con.execute("INSERT INTO commit_work_items (commit_id, work_item_key, method) VALUES (1, 'abc-1', 'manual')")
        post = odin.begin_post(self.con, self.approved_day(60))
        self.con.execute("UPDATE worklogs SET created_at = ? WHERE id = ?", (muninn.ago(7200), post.worklog_id))
        self.con.execute("DROP TRIGGER event_cursors_within_log_ins")
        self.con.execute("INSERT INTO event_cursors (app, last_event_id, updated_at) VALUES ('freya', 999999, ?)", (NOW,))
        report = integrity.check(self.con)
        by_area = {f.area: f for f in report.findings}
        self.assertIn("'abc-1'", by_area["keys"].message)
        self.assertIn("Run Odin", by_area["worklogs"].fix)
        self.assertEqual(by_area["events"].level, "error")
        self.assertFalse(report.ok)
        self.assertEqual(integrity.repair_cursors(self.con), ["freya"])
        self.assertEqual(integrity.check(self.con).of("events"), [])


# --------------------------------------------------------------------------
# Console commands
# --------------------------------------------------------------------------

class CliTests(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--db", str(self.path), "--backups", str(self.dir / "backups"), *args])
        return code, out.getvalue() + err.getvalue()

    def test_status_check_backup_maintain_retention(self):
        code, text = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn(f"version {muninn.SCHEMA_VERSION}", text)
        self.assertEqual(self.run_cli("check"), (0, "Muninn is healthy: nothing to report.\n"))
        code, text = self.run_cli("backup")
        self.assertEqual(code, 0)
        self.assertTrue(list((self.dir / "backups").glob("muninn-*-manual.db")))
        self.assertEqual(self.run_cli("maintain")[0], 0)
        code, text = self.run_cli("retention", "on")
        self.assertIn("Retention is on", text)
        self.assertTrue(integrity.retention_on(self.con))

    def test_prepare_creates_or_updates_without_a_window(self):
        fresh = self.dir / "fresh" / "muninn.db"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--db", str(fresh), "--backups", str(self.dir / "fresh-backups"), "prepare"])
        self.assertEqual(code, 0)
        self.assertIn(f"schema v{muninn.SCHEMA_VERSION}", out.getvalue())
        self.assertIn("(created)", out.getvalue())
        code, text = self.run_cli("prepare")              # already current: nothing to migrate
        self.assertEqual(code, 0)
        self.assertNotIn("migrated", text)

    def test_check_exits_1_on_an_error(self):
        self.con.execute("DROP TRIGGER event_cursors_within_log_ins")
        self.con.execute("INSERT INTO event_cursors (app, last_event_id, updated_at) VALUES ('freya', 50, ?)", (NOW,))
        code, text = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("[error] events", text)

    def test_restore_asks_unless_told(self):
        self.run_cli("backup")
        with mock.patch.object(cli.sys, "stdin", io.StringIO("")):
            code, text = self.run_cli("restore")
        self.assertEqual(code, 2)
        self.assertIn("--yes", text)
        self.con.close()
        code, text = self.run_cli("restore", "--yes")
        self.con = muninn.connect(self.path)
        self.assertEqual(code, 0, text)
        self.assertIn("Restored", text)

    def test_the_launcher_routes_muninn_commands_without_a_window(self):
        from asgard import launcher
        with mock.patch.object(cli, "main", return_value=0) as fake:
            self.assertEqual(launcher.main(["--muninn", "check"]), 0)
        fake.assert_called_once_with(["check"])


class SourceRules(unittest.TestCase):
    def test_no_insert_or_replace_anywhere(self):
        """REPLACE deletes rows behind the app's back; use INSERT ... ON CONFLICT DO UPDATE."""
        pattern = re.compile(r"\b(INSERT\s+OR\s+REPLACE|REPLACE\s+INTO)\b", re.IGNORECASE)
        hits = [str(p.relative_to(ROOT)) for folder in ("asgard", "apps") for p in (ROOT / folder).rglob("*.py")
                if pattern.search(p.read_text(encoding="utf-8"))]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
