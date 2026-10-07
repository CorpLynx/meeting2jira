"""Regression tests for the independent review of Muninn's hardening (docs/review-2026-10-06.md).

Each test is one confirmed finding, named by its id in the review, and fails without its fix.
"""
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT, ROOT / "tests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from asgard import muninn  # noqa: E402
from asgard.muninn import baldur, db, integrity, keys, odin  # noqa: E402
from test_muninn import Base, WorklogBase  # noqa: E402

NOW = "2026-10-02T14:05:00Z"


class RestoreReview(Base):
    def test_h1_restoring_the_live_file_onto_itself_is_refused_and_nothing_moves(self):
        self.con.execute("INSERT INTO meta (key, value) VALUES ('marker', 'keep me')")
        self.con.close()
        with self.assertRaisesRegex(muninn.MuninnError, "is the database itself"):
            muninn.restore(self.path, path=self.path, backups=self.dir / "backups")
        self.con = muninn.connect(self.path)
        self.assertEqual(self.con.execute("SELECT value FROM meta WHERE key = 'marker'").fetchone()[0], "keep me")
        self.assertEqual(list(self.dir.glob("*.before-restore-*")), [])

    def test_h1_a_failure_after_the_rename_puts_the_original_back(self):
        self.con.execute("INSERT INTO meta (key, value) VALUES ('marker', 'original')")
        backup = muninn.backup(self.con, self.dir / "backups", label="manual", keep=5)
        self.con.close()
        real = os.replace

        def failing(src, dst):
            if str(src).endswith(".tmp"):
                raise PermissionError("in use")
            return real(src, dst)
        with mock.patch.object(db.os, "replace", side_effect=failing):
            with self.assertRaisesRegex(muninn.MuninnError, "Your database is unchanged"):
                muninn.restore(backup, path=self.path)
        self.con = muninn.connect(self.path)
        self.assertEqual(self.con.execute("SELECT value FROM meta WHERE key = 'marker'").fetchone()[0], "original")
        self.assertEqual(list(self.dir.glob("*.before-restore-*")), [])
        self.assertEqual(list(self.dir.glob(".*.tmp")), [])

    def test_h1_a_new_empty_database_beside_earlier_copies_says_so(self):
        muninn.backup(self.con, self.dir / "backups", label="manual", keep=5)
        self.con.close()
        for suffix in ("", "-wal", "-shm"):
            p = self.path.with_name(self.path.name + suffix)
            if p.exists():
                p.unlink()
        st = muninn.prepare(self.path, backups=self.dir / "backups")
        self.con = muninn.connect(self.path)
        self.assertTrue(st.created)
        self.assertTrue(any("new, empty database" in w and "--muninn restore" in w for w in st.warnings), st.warnings)

    def test_l3_an_idle_connection_blocks_the_restore(self):
        backup = muninn.backup(self.con, self.dir / "backups", label="manual", keep=5)
        idle = muninn.open_app("odin", supported=(1, muninn.SCHEMA_VERSION), path=self.path)
        idle.execute("SELECT count(*) FROM work_items").fetchone()
        self.con.close()
        self.con = idle
        with self.assertRaisesRegex(muninn.MuninnError, "Close Asgard"):
            muninn.restore(backup, path=self.path)


class SeededRestoreReview(WorklogBase):
    def test_l2_a_backup_that_passes_quick_check_but_not_integrity_check_is_refused(self):
        bad = muninn.backup(self.con, self.dir / "backups", label="manual", keep=5)
        x = sqlite3.connect(str(bad))
        x.execute("PRAGMA writable_schema = ON")
        x.execute("UPDATE sqlite_schema SET sql = replace(sql, '(source_id, updated_at)', '(source_id, last_seen_at)') "
                  "WHERE name = 'ix_work_items_updated'")
        x.commit()
        x.close()
        with self.assertRaisesRegex(muninn.MuninnError, "usable backup|can't be used"):
            muninn.restore(bad, path=self.dir / "elsewhere.db")


class SearchDamageReview(WorklogBase):
    def damage(self, sql):
        self.con.close()
        raw = sqlite3.connect(str(self.path), isolation_level=None)       # as an app on Python < 3.12 could
        raw.execute(sql)
        raw.close()
        self.con = muninn.connect(self.path)

    def test_m1_a_damaged_search_index_is_rebuilt_at_start_not_called_file_damage(self):
        for label, sql in (("index", "DELETE FROM search_data"),
                           ("format", "UPDATE search_config SET v = 99 WHERE k = 'version'")):
            with self.subTest(label):
                self.damage(sql)
                report = integrity.check(self.con)
                self.assertEqual([f.area for f in report.findings if f.level == "error"], ["search"], report.findings)
                st = muninn.prepare(self.path, backups=self.dir / "backups")
                self.assertTrue(any("search index was damaged" in w for w in st.warnings), st.warnings)
                self.assertEqual(integrity.check(self.con).findings, [])
                self.assertEqual(self.con.execute("SELECT count(*) FROM search WHERE search MATCH 'PIV'")
                                 .fetchone()[0], 1)

    def test_m1_repair_fixes_a_damaged_index_too(self):
        self.damage("DELETE FROM search_content")
        done = integrity.repair(self.con)
        self.assertTrue(any("Rebuilt the search index" in d for d in done), done)
        self.assertEqual(integrity.check(self.con).findings, [])

    def test_l1_check_on_a_read_only_connection_skips_the_search_check(self):
        ro = muninn.connect(self.path, readonly=True)
        try:
            report = integrity.check(ro)
        finally:
            ro.close()
        self.assertTrue(report.ok, report.findings)
        self.assertEqual([f.level for f in report.of("search")], ["info"])

    def test_l1_check_while_another_app_writes_says_try_again(self):
        holder = sqlite3.connect(str(self.path), isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch.object(integrity, "retry_busy", side_effect=muninn.BusyError("busy")):
                report = integrity.check(self.con)
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        self.assertTrue(report.ok)
        self.assertIn("another app was writing", report.of("search")[0].message)


class GuardReview(WorklogBase):
    def app(self, name):
        con = muninn.open_app(name, supported=(1, muninn.SCHEMA_VERSION), path=self.path)
        self.addCleanup(con.close)
        return con

    def test_m2_foreign_keys_can_only_be_turned_on(self):
        con = self.app("loki")
        for value in ("00", "-0", "0x0", "' 0'", "'n'", "banana", "OFF", "false"):
            with self.subTest(value=value), self.assertRaisesRegex(sqlite3.DatabaseError, "not authorized"):
                con.execute(f"PRAGMA foreign_keys = {value}")
        con.execute("PRAGMA foreign_keys = ON")
        self.assertEqual(con.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_m2_l10_pragmas_are_an_allow_list_and_sqlite_tables_are_closed(self):
        con = self.app("loki")
        con.execute("PRAGMA busy_timeout = 2000")
        self.assertTrue(con.execute("PRAGMA table_info(meetings)").fetchall())
        for sql in ("PRAGMA hard_heap_limit = 1", "PRAGMA soft_heap_limit = 1", "PRAGMA legacy_alter_table = 1",
                    "PRAGMA case_sensitive_like = 1"):
            with self.subTest(sql=sql), self.assertRaisesRegex(sqlite3.DatabaseError, "not authorized"):
                con.execute(sql)
        self.con.execute("PRAGMA optimize")                          # Asgard's own connection makes sqlite_stat1
        if self.con.execute("SELECT 1 FROM sqlite_schema WHERE name = 'sqlite_stat1'").fetchone():
            with self.assertRaisesRegex(sqlite3.DatabaseError, "not authorized"):
                con.execute("DELETE FROM sqlite_stat1")

    def test_m3_an_app_keeps_to_its_own_rows_in_shared_tables(self):
        self.con.execute("INSERT INTO events (app, kind, entity_type) VALUES ('odin', 'work_item.done', 'work_items')")
        self.con.execute("INSERT INTO event_cursors (app, last_event_id, updated_at) VALUES ('freya', 0, ?)", (NOW,))
        odin_run = self.con.execute("SELECT id FROM sync_runs WHERE app = 'odin' ORDER BY id DESC").fetchone()[0]
        loki = self.app("loki")
        attempts = {
            "another app's event cursor": "UPDATE event_cursors SET last_event_id = 1 WHERE app = 'freya'",
            "another app's sync run": f"UPDATE sync_runs SET status = 'failed' WHERE id = {odin_run}",
            "an event in another app's name": "INSERT INTO events (app, kind, entity_type) VALUES ('odin', 'x.y', 't')",
            "a run in another app's name": "INSERT INTO sync_runs (app, stream) VALUES ('odin', 'issues')",
            "another app's sync cursor": f"INSERT INTO sync_cursors (source_id, stream, cursor, updated_at, run_id) "
                                         f"VALUES ({self.sid}, 'issues', '9999', '{NOW}', {odin_run}) "
                                         "ON CONFLICT (source_id, stream) DO UPDATE SET cursor = excluded.cursor",
            "renaming a source": f"UPDATE sources SET kind = 'manual' WHERE id = {self.sid}",
            "another app's identities": "DELETE FROM identities WHERE kind = 'jira_user'",
            "an identity that makes commits yours": "INSERT INTO identities (kind, value) VALUES ('git_email', 'x@y')",
        }
        for label, sql in attempts.items():
            with self.subTest(label), self.assertRaises(sqlite3.DatabaseError):
                loki.execute(sql)
        self.assertEqual(self.con.execute("SELECT count(*) FROM identities WHERE kind = 'jira_user'").fetchone()[0], 1)
        # What Loki may do with its own rows still works.
        muninn.emit(loki, "loki", "meeting.recapped", "meetings", 1)
        with muninn.consume(loki, "loki", ["meeting.recapped"]) as batch:
            self.assertEqual(len(batch), 1)
        src = muninn.ensure_source(loki, "manual", "paste")
        with muninn.Run(loki, "loki", src, "recaps") as run:
            run.advance_cursor("2026-10-01T00:00:00Z")
        baldur_con = self.app("baldur")
        muninn.add_identity(baldur_con, "git_email", "me@agency.gov")

    def test_m3_the_guard_survives_an_attempt_to_drop_its_rules(self):
        loki = self.app("loki")
        with self.assertRaisesRegex(sqlite3.DatabaseError, "not authorized"):
            loki.execute("DROP TRIGGER temp.guard_events")


class SchemaDriftReview(WorklogBase):
    def test_m6_a_dropped_protection_is_reported_and_put_back_at_start(self):
        pid = self.approved_day(120)
        post = odin.begin_post(self.con, pid)
        odin.finish_post(self.con, post.worklog_id, "1")
        self.con.close()
        raw = sqlite3.connect(str(self.path))
        for t in ("worklogs_asgard_rows_are_kept", "events_no_delete"):
            raw.execute(f"DROP TRIGGER {t}")
        raw.execute("DROP INDEX ux_day_proposals_approved")
        raw.execute("CREATE TRIGGER sneaky AFTER INSERT ON events BEGIN DELETE FROM meta; END")
        raw.commit()
        raw.close()
        self.con = muninn.connect(self.path)
        found = integrity.check(self.con).of("schema")
        self.assertEqual(len(found), 4, found)
        self.assertTrue(all(f.level == "error" for f in found))
        st = muninn.prepare(self.path, backups=self.dir / "backups")
        self.assertTrue(any("put back 4" in w for w in st.warnings), st.warnings)
        self.assertEqual(integrity.check(self.con).of("schema"), [])
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("DELETE FROM worklogs WHERE id = ?", (post.worklog_id,))

    def test_m6_a_healthy_database_matches_its_migrations(self):
        self.assertFalse(integrity.schema_drift(self.con))


class PostingReview(WorklogBase):
    def test_m7_the_same_meeting_from_two_calendars_is_logged_once(self):
        outlook = self.meeting("AAMk-outlook")
        graph = muninn.ensure_source(self.con, "calendar", "graph")
        with muninn.Run(self.con, "odin", graph, "calendar") as run:
            twin = odin.upsert_calendar_event(run, {
                "external_id": "graph-1", "title": "Design review ", "starts_at": "2026-10-01T15:00:00Z",
                "ends_at": "2026-10-01T16:00:00Z", "is_all_day": 0, "show_as": "busy", "response": "accepted",
                "is_cancelled": 0})[0]
        first = odin.begin_meeting_post(self.con, outlook, "ABC-123")
        odin.finish_post(self.con, first.worklog_id, "920")
        self.assertIsNone(odin.begin_meeting_post(self.con, twin, "ABC-123"))
        # And if something posts it anyway, it shows as posted twice.
        self.con.execute("INSERT INTO worklogs (work_item_id, jira_worklog_id, origin, state, started_at, seconds, "
                         "calendar_event_id, posted_at) VALUES (?, '921', 'meeting', 'posted', ?, 3600, ?, ?)",
                         (self.item, "2026-10-01T15:00:00Z", twin, NOW))
        self.assertEqual([tuple(r)[:3] for r in self.con.execute("SELECT kind, ref_id, n FROM v_double_posts")],
                         [("meeting", outlook, 2)])

    def test_l5_a_days_approvals_fit_in_the_day_together(self):
        run_id = self.con.execute("INSERT INTO estimate_runs (date_from, date_to, model_version, params, params_hash) "
                                  "VALUES (?, ?, 'baldur-1', '{}', 'h') RETURNING id", (self.day, self.day)).fetchone()[0]
        for key in ("ABC-1", "ABC-2"):
            self.con.execute("INSERT INTO day_proposals (estimate_run_id, local_date, work_item_key, minutes_raw, "
                             "minutes_proposed, first_started_at, basis, basis_hash) VALUES (?, ?, ?, 1000, 1000, ?, "
                             "'b', 'h')", (run_id, self.day, key, self.started))
        with self.assertRaisesRegex(muninn.MuninnError, "over 24 hours"):
            baldur.approve_day(self.con, self.day)
        self.assertEqual(self.con.execute("SELECT count(*) FROM day_proposals WHERE status = 'approved'")
                         .fetchone()[0], 0, "all or nothing")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "1440"):
            self.con.execute("UPDATE day_proposals SET status = 'approved', minutes_final = 1000, decided_at = ? "
                             "WHERE local_date = ?", (NOW, self.day))

    def test_l8_one_post_in_jira_twice_is_flagged(self):
        post = odin.begin_post(self.con, self.approved_day(120))
        odin.finish_post(self.con, post.worklog_id, "910")
        self.sync(dict(self.jira_worklog("911", post.seconds, comment=post.comment),
                       started=odin.jira_time(post.started_at)))
        found = [f for f in integrity.check(self.con).of("worklogs") if "One post" in f.message]
        self.assertEqual(len(found), 1, integrity.check(self.con).findings)
        self.assertIn(post.marker, found[0].message)

    def test_l9_a_lower_case_key_approved_before_v3_can_still_be_changed(self):
        run_id = self.con.execute("INSERT INTO estimate_runs (date_from, date_to, model_version, params, params_hash) "
                                  "VALUES (?, ?, 'baldur-1', '{}', 'h') RETURNING id", (self.day, self.day)).fetchone()[0]
        self.con.execute("DROP TRIGGER day_proposals_key_ins")
        pid = self.con.execute("INSERT INTO day_proposals (estimate_run_id, local_date, work_item_key, minutes_raw, "
                               "minutes_proposed, minutes_final, first_started_at, basis, basis_hash, status, "
                               "decided_at) VALUES (?, ?, 'abc-123', 61, 60, 60, ?, 'b', 'h', 'approved', ?) "
                               "RETURNING id", (run_id, self.day, self.started, NOW)).fetchone()[0]
        new = baldur.change_approval(self.con, pid, 45)
        self.assertEqual(self.con.execute("SELECT work_item_key FROM day_proposals WHERE id = ?", (new,)).fetchone()[0],
                         "ABC-123")

    def test_l12_a_key_with_a_trailing_newline_isnt_a_key(self):
        self.assertFalse(keys.is_key("ABC-1\n"))
        with self.assertRaises(ValueError):
            keys.normalize_key("ABC-1\nX")


if __name__ == "__main__":
    unittest.main()
