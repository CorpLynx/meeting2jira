"""asgard.muninn: opening, migrating, syncing, Odin's flows, Baldur's approvals, badges."""
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from asgard import muninn  # noqa: E402
from asgard.muninn import baldur, db, odin  # noqa: E402

BASE = "https://jira.example.gov"


def local_date(ts: str) -> str:
    """The local day SQLite's 'localtime' gives for a UTC timestamp."""
    return muninn.from_ts(ts).astimezone().date().isoformat()


def jira_issue(jira_id="10234", key="ABC-123", status="In Progress", category="indeterminate",
               assignee="jdoe", updated="2026-09-30T09:00:00.000-0400", resolution=None,
               resolved=None, histories=(), **fields):
    f = {
        "summary": fields.pop("summary", "Add PIV fallback to Odin"),
        "status": {"name": status, "statusCategory": {"key": category}},
        "issuetype": {"name": "Story"}, "project": {"key": key.split("-")[0]},
        "priority": {"name": "Major"},
        "assignee": {"name": assignee, "key": assignee, "displayName": assignee.title()} if assignee else None,
        "reporter": {"name": "lead"},
        "labels": ["odin"], "components": [{"name": "auth"}],
        "created": "2026-09-01T08:00:00.000-0400", "updated": updated,
        "resolution": {"name": resolution} if resolution else None, "resolutiondate": resolved,
        "duedate": None,
    }
    f.update(fields)
    return {"id": jira_id, "key": key, "fields": f, "changelog": {"histories": list(histories)}}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        os.environ["ASGARD_HOME"] = str(self.dir)
        self.path = self.dir / "muninn.db"
        muninn.prepare(self.path, backups=self.dir / "backups")
        self.con = muninn.connect(self.path)

    def tearDown(self) -> None:
        self.con.close()
        os.environ.pop("ASGARD_HOME", None)
        self.tmp.cleanup()

    def jira(self):
        sid = muninn.ensure_source(self.con, "jira", "jira-dc", BASE)
        muninn.add_identity(self.con, "jira_user", "JDoe", sid)
        return sid, odin.JiraContext.load(self.con, sid, epic_field="customfield_10008",
                                          story_points_field="customfield_10002", sprint_field="customfield_10005")

    def events(self, kind=None):
        sql = "SELECT kind, ref, payload FROM events" + (" WHERE kind = ?" if kind else "") + " ORDER BY id"
        return [(r[0], r[1], json.loads(r[2])) for r in self.con.execute(sql, (kind,) if kind else ())]


# --------------------------------------------------------------------------
# Opening and migrating
# --------------------------------------------------------------------------

class OpeningTests(Base):
    def test_prepare_creates_once_then_backs_up_daily(self):
        self.assertEqual(muninn.user_version(self.con), muninn.SCHEMA_VERSION)
        self.assertEqual(self.con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        again = muninn.prepare(self.path, backups=self.dir / "backups")
        self.assertFalse(again.created)
        self.assertEqual(again.migrated, [])
        self.assertTrue(again.backup and again.backup.exists())
        self.assertIsNone(muninn.prepare(self.path, backups=self.dir / "backups").backup, "one backup a day")

    def test_daily_backups_keep_seven(self):
        folder = self.dir / "b"
        for day in range(10):
            muninn.daily_backup(self.con, folder, today=dt.date(2026, 9, 1) + dt.timedelta(days=day))
        self.assertEqual(len(list(folder.glob("muninn-*.db"))), 7)
        copy = sqlite3.connect(str(sorted(folder.glob("*.db"))[-1]))
        self.assertEqual(copy.execute("PRAGMA user_version").fetchone()[0], muninn.SCHEMA_VERSION)
        copy.close()

    def test_open_app_checks_versions(self):
        con = muninn.open_app("odin", supported=(1, muninn.SCHEMA_VERSION), path=self.path)
        con.close()
        with self.assertRaises(muninn.NotReady):
            muninn.open_app("odin", supported=(1, muninn.SCHEMA_VERSION), path=self.dir / "nothing.db")
        with self.assertRaises(TypeError, msg="supported has no default: a default would go stale"):
            muninn.open_app("odin", path=self.path)
        self.con.execute("PRAGMA user_version = 7")
        with self.assertRaisesRegex(muninn.VersionError, "Update odin"):
            muninn.open_app("odin", supported=(1, muninn.SCHEMA_VERSION), path=self.path)
        with self.assertRaisesRegex(muninn.VersionError, "Update Asgard"):
            muninn.migrate(self.con)

    def test_an_app_on_old_sqlite_gets_a_clear_message(self):
        real = db.sqlite_problems
        db.sqlite_problems = lambda: ["SQLite 3.31.1 is older than 3.37, which STRICT tables need"]
        try:
            with self.assertRaisesRegex(muninn.MuninnError, "same Python as Asgard"):
                muninn.open_app("odin", supported=(1, muninn.SCHEMA_VERSION), path=self.path)
        finally:
            db.sqlite_problems = real

    def test_readonly_connection_cannot_write(self):
        ro = muninn.connect(self.path, readonly=True)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("INSERT INTO meta (key, value) VALUES ('a', 'b')")
        ro.close()

    def test_split_statements_keeps_triggers_and_comments(self):
        sql = ("-- a comment; with a semicolon\nCREATE TABLE t (x TEXT); -- trailing; note\n"
               "CREATE TRIGGER g AFTER INSERT ON t BEGIN SELECT ';'; SELECT 2; END;\n/* block; */ SELECT 1;")
        parts = db.split_statements(sql)
        self.assertEqual(len(parts), 3)
        self.assertIn("END;", parts[1])
        with self.assertRaises(muninn.MuninnError):
            db.split_statements("SELECT 1; CREATE TABLE broken (")


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.folder = self.dir / "migrations"
        self.folder.mkdir()
        shutil.copy(db.MIGRATIONS_DIR / "0001_initial.sql", self.folder / "0001_initial.sql")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add(self, name, sql):
        (self.folder / name).write_text(sql, encoding="utf-8")

    def test_upgrade_backs_up_first(self):
        path = self.dir / "m.db"
        muninn.prepare(path, folder=self.folder, backups=self.dir / "bk")
        self.add("0002_note.sql", "BEGIN;\nALTER TABLE meta ADD COLUMN note TEXT;\nPRAGMA user_version = 2;\nCOMMIT;\n")
        st = muninn.prepare(path, folder=self.folder, backups=self.dir / "bk")
        self.assertEqual((st.version, st.migrated), (2, ["0002_note.sql"]))
        self.assertTrue(list((self.dir / "bk").glob("*before-v2.db")))

    def test_failed_migration_is_undone(self):
        path = self.dir / "m.db"
        muninn.prepare(path, folder=self.folder, backups=self.dir / "bk")
        self.add("0002_bad.sql", "CREATE TABLE half_done (x TEXT) STRICT;\nINSERT INTO no_such_table VALUES (1);\n")
        with self.assertRaisesRegex(muninn.MuninnError, "failed and was undone"):
            muninn.prepare(path, folder=self.folder, backups=self.dir / "bk")
        con = muninn.connect(path)
        self.assertEqual(muninn.user_version(con), 1)
        self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name = 'half_done'").fetchone())
        con.close()

    def test_table_rebuild_with_foreign_keys_off(self):
        path = self.dir / "m.db"
        muninn.prepare(path, folder=self.folder, backups=self.dir / "bk")
        con = muninn.connect(path)
        con.execute("INSERT INTO sources (kind, name) VALUES ('jira', 'jira-dc')")
        con.execute("INSERT INTO identities (kind, value, source_id) VALUES ('jira_user', 'jdoe', 1)")
        con.close()
        self.add("0002_rebuild.sql", "-- muninn: foreign_keys=off\n"
                 "CREATE TABLE sources_new (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL UNIQUE, "
                 "base_url TEXT, created_at TEXT NOT NULL) STRICT;\n"
                 "INSERT INTO sources_new SELECT id, kind, name, base_url, created_at FROM sources;\n"
                 "DROP TABLE sources;\nALTER TABLE sources_new RENAME TO sources;\n")
        st = muninn.prepare(path, folder=self.folder, backups=self.dir / "bk")
        self.assertEqual(st.version, 2)
        con = muninn.connect(path)
        self.assertEqual(con.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        con.close()

    def test_two_processes_racing_to_create(self):
        path = self.dir / "race.db"
        errors = []

        def go():
            try:
                con = muninn.connect(path)
                muninn.migrate(con, folder=self.folder, backups=self.dir / "bk")
                con.close()
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=go) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        con = muninn.connect(path)
        self.assertEqual(muninn.user_version(con), 1)
        con.close()

    def test_bom_and_a_final_comment_are_fine(self):
        path = self.dir / "m.db"
        muninn.prepare(path, folder=self.folder, backups=self.dir / "bk")
        (self.folder / "0002_note.sql").write_bytes("\ufeffALTER TABLE meta ADD COLUMN note TEXT;\n-- done".encode("utf-8"))
        self.assertEqual(muninn.prepare(path, folder=self.folder, backups=self.dir / "bk").version, 2)

    def test_backups_taken_at_once_dont_collide(self):
        path = self.dir / "m.db"
        muninn.prepare(path, folder=self.folder, backups=self.dir / "bk")
        errors, made = [], []

        def go():
            con = muninn.connect(path)
            try:
                made.append(muninn.backup(con, self.dir / "bk", label="before-v2", keep=3))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)
            finally:
                con.close()          # an open file would also stop Windows deleting the folder

        # Repeated: on Windows the race (and its PermissionError) showed up about one run in five.
        for _ in range(4):
            self._race(go, errors, made)

    def _race(self, go, errors, made):
        del errors[:], made[:]
        for old in (self.dir / "bk").glob("muninn-*-before-v2.db"):
            old.unlink()

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(made)), 1)
        check = sqlite3.connect(str(made[0]))
        self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        check.close()
        self.assertEqual(list((self.dir / "bk").glob(".*.tmp")), [])

    def test_gap_in_migration_numbers_is_refused(self):
        self.add("0003_skip.sql", "SELECT 1;")
        with self.assertRaisesRegex(muninn.MuninnError, "out of sequence"):
            muninn.available_migrations(self.folder)


# --------------------------------------------------------------------------
# Runs and events
# --------------------------------------------------------------------------

class RunTests(Base):
    def test_run_commits_data_events_and_cursor(self):
        sid, _ = self.jira()
        with muninn.Run(self.con, "odin", sid, "issues") as run:
            run.emit("work_item.updated", "work_items", None, "ABC-1")
            run.advance_cursor("2026-10-01T00:00:00Z")
            run.advance_cursor("2026-09-01T00:00:00Z")  # never moves back
        row = self.con.execute("SELECT status, cursor_after FROM sync_runs WHERE id = ?", (run.id,)).fetchone()
        self.assertEqual(tuple(row), ("ok", "2026-10-01T00:00:00Z"))
        self.assertEqual(self.con.execute("SELECT cursor FROM sync_cursors").fetchone()[0], "2026-10-01T00:00:00Z")
        with muninn.Run(self.con, "odin", sid, "issues") as again:
            self.assertEqual(again.cursor, "2026-10-01T00:00:00Z")

    def test_failed_run_keeps_whole_items_but_not_the_cursor(self):
        sid, ctx = self.jira()
        with self.assertRaises(RuntimeError):
            with muninn.Run(self.con, "odin", sid, "issues") as run:
                odin.upsert_issue(run, jira_issue(), ctx)          # committed: re-reading it later writes nothing
                with run.batch():
                    odin.upsert_issue(run, jira_issue(jira_id="2", key="ABC-2"), ctx)
                    raise RuntimeError("Jira went away")           # this batch is undone
        self.assertEqual([r[0] for r in self.con.execute("SELECT key FROM work_items")], ["ABC-123"])
        self.assertEqual([e[0] for e in self.events()], ["work_item.created"])
        self.assertIsNone(self.con.execute("SELECT cursor FROM sync_cursors").fetchone())
        status, error = self.con.execute("SELECT status, error FROM sync_runs WHERE id = ?", (run.id,)).fetchone()
        self.assertEqual(status, "failed")
        self.assertIn("Jira went away", error)

    def test_a_run_holds_no_lock_between_writes(self):
        sid, ctx = self.jira()
        with muninn.Run(self.con, "odin", sid, "issues") as run:
            other = muninn.connect(self.path, timeout=0.2)     # Baldur approving while Odin fetches
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            other.close()
            odin.upsert_issue(run, jira_issue(), ctx)
            self.assertFalse(self.con.in_transaction)

    def test_a_rollback_by_sqlite_stops_the_run(self):
        sid, ctx = self.jira()
        self.con.execute("CREATE TEMP TRIGGER boom BEFORE INSERT ON main.work_items WHEN new.key = 'BOOM-1' "
                         "BEGIN SELECT RAISE(ROLLBACK, 'disk full (simulated)'); END")
        with self.assertRaises(muninn.MuninnError):
            with muninn.Run(self.con, "odin", sid, "issues") as run:
                with run.batch():
                    odin.upsert_issue(run, jira_issue(jira_id="1", key="ABC-1"), ctx)
                    try:
                        odin.upsert_issue(run, jira_issue(jira_id="2", key="BOOM-1"), ctx)
                    except sqlite3.DatabaseError as exc:
                        run.problem(str(exc))                  # the usual per-item handling
                    odin.upsert_issue(run, jira_issue(jira_id="3", key="ABC-3"), ctx)
                run.advance_cursor("2026-10-01T00:00:00Z")
        self.assertEqual(self.con.execute("SELECT count(*) FROM work_items").fetchone()[0], 0,
                         "nothing after the rollback may slip in unprotected")
        self.assertIsNone(self.con.execute("SELECT cursor FROM sync_cursors").fetchone())
        self.assertEqual(self.con.execute("SELECT status FROM sync_runs WHERE id = ?", (run.id,)).fetchone()[0],
                         "failed")

    def test_abandoned_runs_are_closed_by_asgard(self):
        self.con.execute("INSERT INTO sync_runs (app, stream, started_at) VALUES ('odin', 'issues', ?)",
                         (muninn.ago(7 * 3600),))
        self.con.execute("INSERT INTO sync_runs (app, stream, started_at) VALUES ('odin', 'issues', ?)",
                         (muninn.ago(60),))
        muninn.prepare(self.path, backups=self.dir / "backups")
        self.assertEqual([r[0] for r in self.con.execute("SELECT status FROM sync_runs ORDER BY id")],
                         ["failed", "running"])

    def test_partial_run_keeps_its_cursor_so_failures_are_retried(self):
        sid, ctx = self.jira()
        with muninn.Run(self.con, "odin", sid, "issues") as run:
            run.advance_cursor("2026-10-01T00:00:00Z")
        self.con.execute("CREATE TEMP TRIGGER full BEFORE INSERT ON main.work_items WHEN new.jira_id = '2' "
                         "BEGIN SELECT RAISE(ROLLBACK, 'database or disk is full'); END")
        with muninn.Run(self.con, "odin", sid, "issues") as run:
            for i in (1, 2, 3):
                try:
                    odin.upsert_issue(run, jira_issue(jira_id=str(i), key=f"NEW-{i}"), ctx)
                except sqlite3.DatabaseError as exc:
                    run.problem(f"NEW-{i}: {exc}")
                run.advance_cursor(f"2026-10-02T0{i}:00:00Z")
        status, after = self.con.execute("SELECT status, cursor_after FROM sync_runs WHERE id = ?", (run.id,)).fetchone()
        self.assertEqual((status, after), ("partial", "2026-10-01T00:00:00Z"))
        self.assertEqual(self.con.execute("SELECT cursor FROM sync_cursors").fetchone()[0], "2026-10-01T00:00:00Z")
        self.assertEqual([r[0] for r in self.con.execute("SELECT key FROM work_items ORDER BY key")], ["NEW-1", "NEW-3"])

    def test_run_refuses_to_nest(self):
        sid, _ = self.jira()
        self.con.execute("BEGIN")
        with self.assertRaises(muninn.MuninnError):
            muninn.Run(self.con, "odin", sid, "issues").__enter__()
        self.con.execute("ROLLBACK")

    def test_consume_advances_with_the_writes(self):
        for i in range(5):
            muninn.emit(self.con, "odin", "work_item.done" if i % 2 == 0 else "work_item.updated", "work_items", i)
        with self.assertRaises(ValueError):
            with muninn.consume(self.con, "freya", ["work_item.done"]) as batch:
                self.assertEqual([e.entity_id for e in batch], [0, 2, 4])
                raise ValueError("crash while handling")
        with muninn.consume(self.con, "freya", ["work_item.done"], limit=2) as batch:
            self.assertEqual(([e.entity_id for e in batch], batch.truncated), ([0, 2], True))
        with muninn.consume(self.con, "freya", ["work_item.done"]) as batch:
            self.assertEqual([e.entity_id for e in batch], [4])
        with muninn.consume(self.con, "freya", ["work_item.done"]) as batch:
            self.assertEqual(len(batch), 0)
        muninn.emit(self.con, "odin", "work_item.done", "work_items", 9)
        with muninn.consume(self.con, "freya", ["work_item.done"]) as batch:
            self.assertEqual([e.entity_id for e in batch], [9])
            muninn.emit(self.con, "odin", "work_item.done", "work_items", 10)   # arrives while handling
        with muninn.consume(self.con, "freya", ["work_item.done"]) as batch:
            self.assertEqual([e.entity_id for e in batch], [10], "an event written mid-batch isn't skipped")


# --------------------------------------------------------------------------
# Odin: issues and keys
# --------------------------------------------------------------------------

class IssueTests(Base):
    def test_parse_time_formats(self):
        self.assertEqual(odin.parse_time("2026-09-30T09:00:00.000-0400"), "2026-09-30T13:00:00Z")
        self.assertEqual(odin.parse_time("2026-09-30T09:00:00+05:30"), "2026-09-30T03:30:00Z")
        self.assertEqual(odin.parse_time("2026-10-01T13:00:00.0000000"), "2026-10-01T13:00:00Z")
        self.assertEqual(odin.parse_time("2026-10-01T13:00:00Z"), "2026-10-01T13:00:00Z")
        self.assertIsNone(odin.parse_time(None))
        with self.assertRaises(ValueError):
            odin.parse_time("yesterday")
        started = odin.jira_time("2026-10-01T13:05:00Z")
        self.assertRegex(started, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.000[+-]\d{4}$")
        self.assertEqual(odin.parse_time(started), "2026-10-01T13:05:00Z")

    def test_issue_lifecycle_events_and_aliases(self):
        sid, ctx = self.jira()
        raw = jira_issue(customfield_10008="ABC-50", customfield_10002=3,
                         customfield_10005=["com.atlassian.greenhopper.service.sprint.Sprint@1[id=7,name=Sprint 12,state=ACTIVE]"],
                         histories=[{"id": "55001", "created": "2026-09-30T08:00:00.000-0400", "author": {"name": "jdoe"},
                                     "items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"}]}])
        with muninn.Run(self.con, "odin", sid, "issues") as run:
            first = odin.upsert_issue(run, raw, ctx)
        self.assertTrue(first.inserted)
        row = self.con.execute("SELECT * FROM work_items").fetchone()
        self.assertEqual((row["is_mine"], row["epic_key"], row["story_points"], row["sprint"], row["url"]),
                         (1, "ABC-50", 3.0, "Sprint 12", f"{BASE}/browse/ABC-123"))
        self.assertEqual(self.con.execute("SELECT by_me, to_category FROM work_item_transitions").fetchone()[:],
                         (1, "in_progress"))

        with muninn.Run(self.con, "odin", sid, "issues") as run:
            same = odin.upsert_issue(run, raw, ctx)
        self.assertFalse(same.changed)
        self.assertEqual(len(self.events()), 1)

        moved = jira_issue(key="XYZ-45", updated="2026-10-01T10:00:00.000-0400", status="Done", category="done",
                           resolution="Done", resolved="2026-10-01T10:00:00.000-0400")
        with muninn.Run(self.con, "odin", sid, "issues") as run:
            res = odin.upsert_issue(run, moved, ctx)
        self.assertEqual(res.events, ["work_item.updated", "work_item.moved", "work_item.done"])
        self.assertEqual(dict(self.con.execute("SELECT key, status FROM work_item_aliases")),
                         {"ABC-123": "moved", "XYZ-45": "current"})
        self.assertEqual(odin.issue_by_key(self.con, "ABC-123")["key"], "XYZ-45")
        done = self.events("work_item.done")[0]
        self.assertEqual((done[1], done[2]["resolution"]), ("XYZ-45", "Done"))

        reopened = jira_issue(key="XYZ-45", updated="2026-10-02T10:00:00.000-0400", status="In Progress")
        with muninn.Run(self.con, "odin", sid, "issues") as run:
            self.assertIn("work_item.reopened", odin.upsert_issue(run, reopened, ctx).events)

    def test_a_second_change_in_the_same_second_is_kept(self):
        sid, ctx = self.jira()
        with muninn.Run(self.con, "odin", sid, "issues") as run:
            odin.upsert_issue(run, jira_issue(status="In Review", updated="2026-10-01T10:00:00.120-0400"), ctx)
            res = odin.upsert_issue(run, jira_issue(status="Done", category="done", resolution="Done",
                                                    updated="2026-10-01T10:00:00.900-0400"), ctx)
        self.assertTrue(res.changed)
        self.assertIn("work_item.done", res.events)

    def test_jql_time_overlaps_the_cursor(self):
        jql = odin.jql_time("2026-10-01T13:05:30Z")
        self.assertRegex(jql, r"^\d{4}/\d\d/\d\d \d\d:\d\d$")
        local = muninn.from_ts("2026-10-01T13:03:30Z").astimezone().strftime("%Y/%m/%d %H:%M")
        self.assertEqual(jql, local)

    def test_mine_sticks_after_reassignment(self):
        sid, ctx = self.jira()
        with muninn.Run(self.con, "odin", sid, "issues") as run:
            odin.upsert_issue(run, jira_issue(assignee="someone"), ctx, mine=True)
            odin.upsert_issue(run, jira_issue(jira_id="2", key="ABC-2", assignee="someone"), ctx)
        self.assertEqual(dict(self.con.execute("SELECT key, is_mine FROM work_items")), {"ABC-123": 1, "ABC-2": 0})
        self.assertEqual(odin.assigned_to_me(self.con), [])
        with muninn.Run(self.con, "odin", sid, "issues") as run:
            odin.upsert_issue(run, jira_issue(assignee="JDOE", updated="2026-10-01T00:00:00.000-0400"), ctx)
        self.assertEqual([r["key"] for r in odin.assigned_to_me(self.con)], ["ABC-123"])

    def test_children_of_a_tracked_parent(self):
        sid, ctx = self.jira()
        with muninn.Run(self.con, "odin", sid, "issues") as run:
            odin.upsert_issue(run, jira_issue(jira_id="1", key="ABC-1", parent={"key": "ABC-100"}), ctx)
            odin.upsert_issue(run, jira_issue(jira_id="2", key="ABC-2", customfield_10008="ABC-100"), ctx)
            odin.upsert_issue(run, jira_issue(jira_id="3", key="ABC-3"), ctx)
        self.assertEqual([r["key"] for r in odin.children_of(self.con, "ABC-100")], ["ABC-1", "ABC-2"])

    def test_unknown_keys_are_looked_up(self):
        sid, ctx = self.jira()
        self.con.execute("INSERT INTO repos (name, local_path) VALUES ('asgard', 'C:/src/asgard')")
        self.con.execute("INSERT INTO commits (repo_id, sha, author_name, author_email, authored_at, committed_at, "
                         "subject, first_seen_at) VALUES (1, ?, 'Me', 'me@x', ?, ?, 's', ?)",
                         ("a" * 40, muninn.utcnow(), muninn.utcnow(), muninn.utcnow()))
        for key in ("OLD-1", "GONE-9", "ABC-7"):
            self.con.execute("INSERT INTO commit_work_items VALUES (1, ?, 'branch')", (key,))
        self.assertEqual(odin.unknown_keys(self.con), ["ABC-7", "GONE-9", "OLD-1"])
        with muninn.Run(self.con, "odin", sid, "lookups") as run:
            odin.record_lookup(run, "OLD-1", jira_issue(jira_id="77", key="NEW-1"), ctx)   # Jira answers with the moved issue
            odin.record_lookup(run, "GONE-9", None, ctx)
        self.assertEqual(odin.unknown_keys(self.con), ["ABC-7"])
        self.assertEqual(odin.issue_by_key(self.con, "OLD-1")["key"], "NEW-1")
        self.assertIsNone(odin.issue_by_key(self.con, "GONE-9"))

    def test_refresh_batches_and_deletion(self):
        sid, ctx = self.jira()
        with muninn.Run(self.con, "odin", sid, "issues") as run:
            for i in range(1, 6):
                odin.upsert_issue(run, jira_issue(jira_id=str(i), key=f"ABC-{i}", assignee="other"), ctx)
            odin.upsert_issue(run, jira_issue(jira_id="9", key="ABC-9"), ctx)   # yours: refreshed by your own query
        self.assertEqual(odin.refresh_batches(self.con, sid, size=2), [["ABC-1", "ABC-2"], ["ABC-3", "ABC-4"], ["ABC-5"]])
        stale = odin.not_seen_since(self.con, sid, "2999-01-01T00:00:00Z")
        self.assertEqual(len(stale), 6)
        with muninn.Run(self.con, "odin", sid, "issues", mode="full") as run:
            self.assertTrue(odin.mark_issue_deleted(run, "3"))
        self.assertEqual(odin.refresh_batches(self.con, sid, size=10), [["ABC-1", "ABC-2", "ABC-4", "ABC-5"]])


# --------------------------------------------------------------------------
# Odin: calendar and worklogs
# --------------------------------------------------------------------------

class FakeAppointment:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class CalendarTests(Base):
    def test_graph_events_sync_and_sweep(self):
        cal = muninn.ensure_source(self.con, "calendar", "outlook")
        graph = {"id": "AAMk1", "subject": "Design review", "start": {"dateTime": "2026-10-01T15:00:00.0000000", "timeZone": "UTC"},
                 "end": {"dateTime": "2026-10-01T16:00:00.0000000", "timeZone": "UTC"}, "isAllDay": False,
                 "showAs": "tentative", "responseStatus": {"response": "tentativelyAccepted"}, "isCancelled": False}
        ev = odin.event_from_graph(graph)
        self.assertEqual((ev["show_as"], ev["response"], ev["starts_at"]), ("tentative", "tentative", "2026-10-01T15:00:00Z"))
        with self.assertRaises(ValueError):
            odin.event_from_graph(dict(graph, start={"dateTime": "x", "timeZone": "Eastern Standard Time"}))
        other = dict(odin.event_from_graph(graph), external_id="AAMk2", title="Old sync")
        with muninn.Run(self.con, "odin", cal, "calendar") as run:
            eid, changed = odin.upsert_calendar_event(run, ev)
            odin.upsert_calendar_event(run, other)
        self.assertTrue(changed)
        odin.set_meeting_key(self.con, eid, "ABC-123")
        with muninn.Run(self.con, "odin", cal, "calendar", mode="full") as run:
            same_id, changed = odin.upsert_calendar_event(run, ev)
            swept = odin.sweep_calendar(run, "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")
        self.assertEqual((same_id, changed, swept), (eid, False, 1))
        self.assertEqual(self.con.execute("SELECT logged_as_key FROM calendar_events WHERE id = ?", (eid,)).fetchone()[0],
                         "ABC-123")
        self.assertEqual([r[0] for r in self.con.execute("SELECT external_id FROM v_busy_meetings")], ["AAMk1"])

    def test_outlook_appointments(self):
        appt = FakeAppointment(GlobalAppointmentID="040000008200E0", Subject="Standup",
                               StartUTC=dt.datetime(2026, 10, 1, 13, 0, tzinfo=dt.timezone.utc),
                               EndUTC=dt.datetime(2026, 10, 1, 13, 15, tzinfo=dt.timezone.utc),
                               AllDayEvent=False, BusyStatus=2, ResponseStatus=3, MeetingStatus=3, IsRecurring=True)
        ev = odin.event_from_outlook(appt)
        self.assertEqual(ev["external_id"], "040000008200E0:2026-10-01T13:00:00Z")
        self.assertEqual((ev["show_as"], ev["response"], ev["is_cancelled"]), ("busy", "accepted", 0))
        appt.MeetingStatus, appt.IsRecurring = 5, False
        ev = odin.event_from_outlook(appt)
        self.assertEqual((ev["external_id"], ev["is_cancelled"]), ("040000008200E0", 1))


class WorklogBase(Base):
    def setUp(self) -> None:
        super().setUp()
        self.sid, self.ctx = self.jira()
        with muninn.Run(self.con, "odin", self.sid, "issues") as run:
            self.item = odin.upsert_issue(run, jira_issue(), self.ctx).id
        self.started = "2026-10-01T13:05:00Z"
        self.day = local_date(self.started)

    def approved_day(self, minutes=120, key="ABC-123"):
        run_id = self.con.execute("INSERT INTO estimate_runs (date_from, date_to, model_version, params, params_hash) "
                                  "VALUES (?, ?, 'baldur-1', '{}', 'h') RETURNING id", (self.day, self.day)).fetchone()[0]
        pid = self.con.execute("INSERT INTO day_proposals (estimate_run_id, local_date, work_item_key, minutes_raw, "
                               "minutes_proposed, first_started_at, basis, basis_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                               "RETURNING id", (run_id, self.day, key, minutes + 5.5, minutes, self.started,
                                                "Baldur estimate: 2 commits", "bh1")).fetchone()[0]
        baldur.approve(self.con, pid)
        return pid

    def jira_worklog(self, wid, seconds, comment="by hand", author="jdoe", started="2026-10-01T10:00:00.000-0400"):
        return {"id": wid, "issueId": "10234", "author": {"name": author}, "started": started,
                "timeSpentSeconds": seconds, "comment": comment, "created": "2026-10-01T18:00:00.000-0400"}

    def sync(self, *worklogs):
        with muninn.Run(self.con, "odin", self.sid, "worklogs") as run:
            return [odin.upsert_worklog(run, w, self.ctx) for w in worklogs]

    def state(self, wid):
        return tuple(self.con.execute("SELECT state, jira_worklog_id FROM worklogs WHERE id = ?", (wid,)).fetchone())

    def meeting(self, external_id="m1", title="Design review"):
        cal = muninn.ensure_source(self.con, "calendar", "outlook")
        event = {"external_id": external_id, "title": title, "starts_at": "2026-10-01T15:00:00Z",
                 "ends_at": "2026-10-01T16:00:00Z", "is_all_day": 0, "show_as": "busy", "response": "accepted",
                 "is_cancelled": 0}
        with muninn.Run(self.con, "odin", cal, "calendar") as run:
            return odin.upsert_calendar_event(run, event)[0]


class WorklogTests(WorklogBase):

    def test_worklog_sync_keeps_only_yours(self):
        res = self.sync(self.jira_worklog("1", 1800), self.jira_worklog("2", 600, author="other"),
                        dict(self.jira_worklog("3", 600), issueId="99999"))
        self.assertEqual([r.status for r in res], ["inserted", "not_mine", "missing_issue"])
        self.assertEqual(res[2].issue_id, "99999")
        self.assertEqual([r.status for r in self.sync(self.jira_worklog("1", 1800))], ["unchanged"])
        self.assertEqual([r.status for r in self.sync(self.jira_worklog("1", 2400))], ["updated"])
        with muninn.Run(self.con, "odin", self.sid, "worklogs") as run:
            self.assertTrue(odin.mark_worklog_deleted(run, "1"))
        self.assertEqual(self.state(res[0].worklog_id), ("deleted", "1"))

    def test_posting_an_approved_day(self):
        self.sync(self.jira_worklog("1", 1800, started=odin.jira_time(self.started)))   # 30m you logged by hand
        pid = self.approved_day(120)
        due = odin.posts_due(self.con)
        self.assertEqual([(d["key"], d["minutes_to_post"]) for d in due], [("ABC-123", 90)])
        post = odin.begin_post(self.con, pid)
        self.assertEqual((post.key, post.seconds, post.origin), ("ABC-123", 5400, "baldur"))
        self.assertTrue(post.comment.startswith("Baldur estimate") and post.comment.endswith(post.marker))
        self.assertRegex(post.marker, r"^\[asgard:b-[0-9a-f]{8}\]$")
        self.assertEqual(odin.posts_due(self.con), [])
        self.assertIsNone(odin.begin_post(self.con, pid), "a second Odin can't post the same day")
        odin.finish_post(self.con, post.worklog_id, "88001")
        odin.finish_post(self.con, post.worklog_id, "88001")   # repeating is harmless
        self.assertEqual(self.state(post.worklog_id), ("posted", "88001"))
        self.assertEqual(self.events("worklog.posted")[0][2]["proposal_id"], pid)
        status = self.con.execute("SELECT approved_minutes, logged_minutes FROM v_day_status").fetchone()
        self.assertEqual(tuple(status), (120, 120))

    def test_rejected_post_is_offered_again(self):
        pid = self.approved_day(60)
        post = odin.begin_post(self.con, pid)
        odin.fail_post(self.con, post.worklog_id, "400: You do not have permission to log work")
        self.assertEqual(self.state(post.worklog_id), ("failed", None))
        self.assertEqual([d["proposal_id"] for d in odin.posts_due(self.con)], [pid])
        self.assertEqual(self.events("worklog.failed")[0][2]["proposal_id"], pid)

    def test_interrupted_post_is_settled_by_its_marker(self):
        pid = self.approved_day(60)
        post = odin.begin_post(self.con, pid)
        # Odin crashed after Jira saved the worklog. The next worklog sync brings it back with the marker.
        res = self.sync(self.jira_worklog("88002", 3600, comment=post.comment, started=post.started))
        self.assertEqual((res[0].status, res[0].worklog_id), ("reconciled", post.worklog_id))
        self.assertEqual(self.state(post.worklog_id), ("posted", "88002"))
        self.assertEqual(self.con.execute("SELECT count(*) FROM worklogs").fetchone()[0], 1)

    def test_stuck_posts_are_checked_after_two_minutes(self):
        pid = self.approved_day(60)
        post = odin.begin_post(self.con, pid)
        self.assertEqual(odin.stuck_posts(self.con), [])
        self.con.execute("UPDATE worklogs SET created_at = ? WHERE id = ?", (muninn.ago(600), post.worklog_id))
        stuck = odin.stuck_posts(self.con)
        self.assertEqual((stuck[0]["key"], stuck[0]["marker"]), ("ABC-123", post.marker))
        with self.assertRaisesRegex(muninn.MuninnError, "Search the issue's worklogs"):
            odin.resolve_stuck(self.con, post.worklog_id)       # no id and no search: could post twice
        self.assertEqual(self.state(post.worklog_id), ("sending", None))
        odin.resolve_stuck(self.con, post.worklog_id, None, searched=True)   # marker not found in Jira
        self.assertEqual(self.state(post.worklog_id), ("failed", None))
        again = odin.begin_post(self.con, pid)
        odin.resolve_stuck(self.con, again.worklog_id, "88003")  # found it
        self.assertEqual(self.state(again.worklog_id), ("posted", "88003"))

    def test_a_copy_stored_before_the_marker_is_dropped(self):
        pid = self.approved_day(60)
        post = odin.begin_post(self.con, pid)
        self.sync(self.jira_worklog("88004", 3600, comment="marker stripped by an admin"))
        odin.finish_post(self.con, post.worklog_id, "88004")
        rows = self.con.execute("SELECT origin, state FROM worklogs").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("baldur", "posted")])

    def test_deleted_post_is_not_reposted_until_reapproved(self):
        pid = self.approved_day(60)
        post = odin.begin_post(self.con, pid)
        odin.finish_post(self.con, post.worklog_id, "88005")
        with muninn.Run(self.con, "odin", self.sid, "worklogs") as run:
            odin.mark_worklog_deleted(run, "88005")
        self.assertEqual(odin.posts_due(self.con), [])
        new_pid = baldur.change_approval(self.con, pid, 45)
        self.assertEqual([(d["proposal_id"], d["minutes_to_post"]) for d in odin.posts_due(self.con)], [(new_pid, 45)])

    def test_meeting_and_manual_posts(self):
        cal = muninn.ensure_source(self.con, "calendar", "outlook")
        meeting = {"external_id": "m1", "title": "Design review", "starts_at": "2026-10-01T15:00:00Z",
                   "ends_at": "2026-10-01T16:00:00Z", "is_all_day": 0, "show_as": "busy", "response": "accepted",
                   "is_cancelled": 0}
        with muninn.Run(self.con, "odin", cal, "calendar") as run:
            eid, _ = odin.upsert_calendar_event(run, meeting)
        with self.assertRaisesRegex(muninn.MuninnError, "Choose a Jira issue"):
            odin.begin_meeting_post(self.con, eid)
        post = odin.begin_meeting_post(self.con, eid, "ABC-123")
        self.assertEqual((post.seconds, post.origin), (3600, "meeting"))
        self.assertIsNone(odin.begin_meeting_post(self.con, eid), "a meeting is logged once")
        odin.finish_post(self.con, post.worklog_id, "88006")
        pid = self.approved_day(60)
        self.assertEqual([d["minutes_to_post"] for d in odin.posts_due(self.con)], [60], "meeting time isn't dev time")
        manual = odin.begin_manual_post(self.con, "ABC-123", self.started, 900, "Pairing with Sam")
        self.assertRegex(manual.comment, r"^Pairing with Sam\n\[asgard:o-[0-9a-f]{8}\]$")
        with self.assertRaisesRegex(muninn.MuninnError, "isn't in Muninn yet"):
            odin.begin_manual_post(self.con, "NOPE-1", self.started, 900)
        self.assertEqual(odin.posts_due(self.con), [], "nothing more is offered while a post for the day is in doubt")
        odin.finish_post(self.con, manual.worklog_id, "88007")
        self.assertEqual([(d["proposal_id"], d["minutes_to_post"]) for d in odin.posts_due(self.con)], [(pid, 45)])

    def test_earlier_meeting_worklogs_are_recognised(self):
        cal = muninn.ensure_source(self.con, "calendar", "outlook")
        meeting = {"external_id": "m2", "title": "Sprint review", "starts_at": "2026-10-01T15:00:00Z",
                   "ends_at": "2026-10-01T16:00:00Z", "is_all_day": 0, "show_as": "busy", "response": "accepted",
                   "is_cancelled": 0}
        with muninn.Run(self.con, "odin", cal, "calendar") as run:
            eid, _ = odin.upsert_calendar_event(run, meeting)
        self.sync(self.jira_worklog("70001", 3600, comment="Meeting: Sprint review", started="2026-10-01T11:00:00.000-0400"),
                  self.jira_worklog("70002", 3600, comment="Debugging", started="2026-10-01T11:00:00.000-0400"),
                  self.jira_worklog("70003", 1800, comment="Meeting: Sprint review", started="2026-10-01T11:00:00.000-0400"))
        self.assertEqual(odin.classify_meeting_worklogs(self.con, comment_like="Meeting:%"), 1)
        rows = dict(self.con.execute("SELECT jira_worklog_id, origin FROM worklogs"))
        self.assertEqual(rows, {"70001": "meeting", "70002": "jira", "70003": "jira"})
        self.assertEqual(self.con.execute("SELECT logged_as_key FROM calendar_events WHERE id = ?", (eid,)).fetchone()[0],
                         "ABC-123")


# --------------------------------------------------------------------------
# Baldur's approvals and the launcher's badges
# --------------------------------------------------------------------------

    def test_a_post_in_doubt_holds_back_a_newer_approval(self):
        pid = self.approved_day(120)
        post = odin.begin_post(self.con, pid)            # the Jira call times out: the row stays 'sending'
        newer = baldur.change_approval(self.con, pid, 180)
        self.assertEqual(odin.posts_due(self.con), [], "nothing is posted on top of a post in doubt")
        odin.resolve_stuck(self.con, post.worklog_id, searched=True)    # the marker isn't in Jira: it failed
        self.assertEqual([(d["proposal_id"], d["minutes_to_post"]) for d in odin.posts_due(self.con)], [(newer, 180)])

    def test_a_post_in_doubt_that_landed_counts(self):
        pid = self.approved_day(120)
        post = odin.begin_post(self.con, pid)
        newer = baldur.change_approval(self.con, pid, 180)
        odin.resolve_stuck(self.con, post.worklog_id, "88010")   # it did reach Jira
        self.assertEqual([(d["proposal_id"], d["minutes_to_post"]) for d in odin.posts_due(self.con)], [(newer, 60)])

    def test_post_start_stays_inside_the_approved_day(self):
        day_start = self.con.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', ?, 'utc')", (self.day,)).fetchone()[0]
        early = muninn.to_ts(muninn.from_ts(day_start) - dt.timedelta(minutes=20))   # lead-in before midnight
        run_id = self.con.execute("INSERT INTO estimate_runs (date_from, date_to, model_version, params, params_hash) "
                                  "VALUES (?, ?, 'baldur-1', '{}', 'h') RETURNING id", (self.day, self.day)).fetchone()[0]
        pid = self.con.execute("INSERT INTO day_proposals (estimate_run_id, local_date, work_item_key, minutes_raw, "
                               "minutes_proposed, first_started_at, basis, basis_hash) VALUES (?, ?, 'ABC-123', 61, 60, "
                               "?, 'b', 'h') RETURNING id", (run_id, self.day, early)).fetchone()[0]
        baldur.approve(self.con, pid)
        post = odin.begin_post(self.con, pid)
        self.assertEqual(post.started_at, day_start)
        odin.finish_post(self.con, post.worklog_id, "88011")
        self.assertEqual(tuple(self.con.execute("SELECT approved_minutes, logged_minutes FROM v_day_status").fetchone()),
                         (60, 60))

    def test_a_deleted_meeting_worklog_is_not_logged_again_by_itself(self):
        eid = self.meeting()
        post = odin.begin_meeting_post(self.con, eid, "ABC-123")
        odin.finish_post(self.con, post.worklog_id, "88012")
        with muninn.Run(self.con, "odin", self.sid, "worklogs") as run:
            odin.mark_worklog_deleted(run, "88012")
        self.assertIsNone(odin.begin_meeting_post(self.con, eid))
        self.assertIsNotNone(odin.begin_meeting_post(self.con, eid, again=True))

    def test_meeting_classification_is_strict(self):
        with self.assertRaises(ValueError):
            odin.classify_meeting_worklogs(self.con, "%")
        eid = self.meeting("m3", "Planning")
        same = dict(started="2026-10-01T11:00:00.000-0400")
        self.sync(self.jira_worklog("71001", 3600, comment="Meeting: Planning", **same),
                  self.jira_worklog("71002", 3600, comment="Meeting: Planning (dup)", **same))
        self.assertEqual(odin.classify_meeting_worklogs(self.con, "Meeting:%"), 0, "two candidates: too unsure")
        self.con.execute("DELETE FROM worklogs WHERE jira_worklog_id = '71002'")
        post = odin.begin_meeting_post(self.con, eid, "ABC-123")
        odin.finish_post(self.con, post.worklog_id, "71003")
        self.assertEqual(odin.classify_meeting_worklogs(self.con, "Meeting:%"), 0, "the meeting is already logged")


class ApprovalTests(WorklogBase):
    def test_approval_rules(self):
        pid = self.approved_day(60)
        self.assertEqual(self.events("day_proposal.approved")[0][2], {"local_date": self.day, "minutes": 60})
        with self.assertRaises(muninn.MuninnError):
            baldur.approve(self.con, pid)
        newer = self.approved_day(75)   # a later run's proposal for the same day and ticket
        statuses = dict(self.con.execute("SELECT id, status FROM day_proposals"))
        self.assertEqual((statuses[pid], statuses[newer]), ("superseded", "approved"))
        changed = baldur.change_approval(self.con, newer, 30)
        self.assertEqual(self.con.execute("SELECT minutes_final FROM day_proposals WHERE id = ?", (changed,)).fetchone()[0], 30)
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("UPDATE day_proposals SET minutes_final = 10 WHERE id = ?", (changed,))
        run_id = self.con.execute("SELECT estimate_run_id FROM day_proposals WHERE id = ?", (changed,)).fetchone()[0]
        untracked = self.con.execute("INSERT INTO day_proposals (estimate_run_id, local_date, work_item_key, minutes_raw, "
                                     "minutes_proposed, first_started_at, basis, basis_hash) VALUES (?, ?, NULL, 20, 15, ?, "
                                     "'b', 'h') RETURNING id", (run_id, self.day, self.started)).fetchone()[0]
        with self.assertRaisesRegex(muninn.MuninnError, "Untracked"):
            baldur.approve(self.con, untracked)
        with self.assertRaisesRegex(muninn.MuninnError, "Untracked"):
            baldur.reject(self.con, untracked)

    def test_decided_proposals_survive_pruning(self):
        pid = self.approved_day(60)
        run_id = self.con.execute("SELECT estimate_run_id FROM day_proposals WHERE id = ?", (pid,)).fetchone()[0]
        with self.assertRaisesRegex(sqlite3.IntegrityError, "kept for audit"):
            self.con.execute("DELETE FROM estimate_runs WHERE id = ?", (run_id,))
        self.assertEqual([d["proposal_id"] for d in odin.posts_due(self.con)], [pid])
        spare = self.con.execute("INSERT INTO estimate_runs (date_from, date_to, model_version, params, params_hash) "
                                 "VALUES (?, ?, 'baldur-1', '{}', 'h') RETURNING id", (self.day, self.day)).fetchone()[0]
        self.con.execute("INSERT INTO day_proposals (estimate_run_id, local_date, work_item_key, minutes_raw, "
                         "minutes_proposed, first_started_at, basis, basis_hash) VALUES (?, ?, 'ABC-9', 10, 0, ?, 'b', "
                         "'h')", (spare, "2026-09-01", self.started))
        self.con.execute("DELETE FROM estimate_runs WHERE id = ?", (spare,))   # undecided runs prune freely


class BadgeTests(WorklogBase):
    def test_badges(self):
        self.assertEqual(muninn.tile_badges(self.dir / "missing.db"), {})
        self.assertEqual(muninn.tile_badges(self.path), {})
        self.approved_day(60)
        self.con.execute("INSERT INTO repos (name, github_repo) VALUES ('portal', 'team/portal')")
        now = muninn.utcnow()
        self.con.execute("INSERT INTO pull_requests (repo_id, number, title, author, head_ref, work_item_key, state, "
                         "review_requested, created_at, updated_at, url, first_seen_at, last_seen_at) VALUES (1, 31, 't', "
                         "'sam', 'feature/POR-88-x', 'POR-88', 'open', 1, ?, ?, 'u', ?, ?)", (now, now, now, now))
        badges = muninn.tile_badges(self.path)
        self.assertEqual([b.text for b in badges["baldur"]], ["1 review requested"])
        self.assertEqual([b.text for b in badges["odin"]], ["1 worklog to post", "1 key to look up"])
        self.assertEqual(muninn.Badge(1, 3, "days to review").text, "3 days to review")
        gone = self.approved_day(30, key="GONE-1")
        self.con.execute("INSERT INTO work_item_aliases (key, work_item_id, status, checked_at) "
                         "VALUES ('GONE-1', NULL, 'not_found', ?)", (muninn.utcnow(),))
        rows = [tuple(r) for r in self.con.execute("SELECT proposal_id, reason FROM v_unpostable_days")]
        self.assertEqual(rows, [(gone, "not found in Jira")])
        post = odin.begin_post(self.con, odin.posts_due(self.con)[0]["proposal_id"])
        self.con.execute("UPDATE worklogs SET created_at = ? WHERE id = ?", (muninn.ago(3600), post.worklog_id))
        badges = muninn.tile_badges(self.path)
        self.assertEqual([b.text for b in badges["baldur"]], ["1 review requested", "1 day that can't be posted"])
        self.assertEqual([b.text for b in badges["odin"]], ["1 key to look up", "1 post to check"])
        self.con.execute("PRAGMA user_version = 99")
        self.assertEqual(muninn.tile_badges(self.path), {}, "a newer database shows no badges rather than wrong ones")


if __name__ == "__main__":
    unittest.main()
