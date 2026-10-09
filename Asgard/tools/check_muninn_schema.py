"""Exercise Muninn's schema (asgard/muninn/migrations/0001_initial.sql and the migrations after it) directly:
constraints, triggers, upserts, key resolution, worklog posting, Freya's
accomplishments, search, views and index use.

Run:  python tools/check_muninn_schema.py [schema.sql]   (exit code 1 if any check fails)
Local-day checks run in America/New_York so they cross a UTC midnight and a DST change,
so run this on Linux, macOS or WSL; tests/test_muninn_schema.py does that for you.
"""
import glob
import os
import sqlite3
import sys
import tempfile
import time

os.environ["TZ"] = "America/New_York"
if hasattr(time, "tzset"):
    time.tzset()

_HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = (sys.argv[1] if len(sys.argv) > 1
          else os.path.join(_HERE, "muninn_schema.sql") if os.path.exists(os.path.join(_HERE, "muninn_schema.sql"))
          else os.path.join(os.path.dirname(_HERE), "asgard", "muninn", "migrations", "0001_initial.sql"))
db_path = os.path.join(tempfile.mkdtemp(), "muninn.db")


def connect():
    c = sqlite3.connect(db_path, isolation_level=None)  # autocommit; transactions are explicit
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 5000")
    c.execute("PRAGMA synchronous = NORMAL")
    return c


con = connect()
if con.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', '2026-10-01', 'utc')").fetchone()[0] != "2026-10-01T04:00:00Z":
    sys.exit("SQLite isn't using America/New_York for local time (Windows Python can't switch zones); "
             "run this under Linux, macOS or WSL.")
con.execute("PRAGMA journal_mode = WAL")
con.executescript(open(SCHEMA, encoding="utf-8").read())
# With the shipped schema, apply the later migrations too (each carries its own BEGIN/COMMIT),
# so the checks run against the schema apps get.
MIGRATIONS = sorted(glob.glob(os.path.join(os.path.dirname(SCHEMA), "0[0-9][0-9][0-9]_*.sql")))
if os.path.basename(SCHEMA) == "0001_initial.sql":
    for later in MIGRATIONS[1:]:
        con.executescript(open(later, encoding="utf-8").read())
LATEST = len(MIGRATIONS) if os.path.basename(SCHEMA) == "0001_initial.sql" else 1

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (" :: " + str(detail) if detail and not ok else ""))


def rejects(name, sql, params=()):
    try:
        con.execute(sql, params)
        check(name, False, "statement was accepted")
    except sqlite3.DatabaseError as e:
        check(name, True, type(e).__name__ + ": " + str(e)[:70])


def one(sql, params=()):
    row = con.execute(sql, params).fetchone()
    return None if row is None else (row[0] if len(row) == 1 else tuple(row))


def rows(sql, params=()):
    return [tuple(r) for r in con.execute(sql, params).fetchall()]


# =====================================================================
# Shape
# =====================================================================
check("SQLite is 3.37 or newer", sqlite3.sqlite_version_info >= (3, 37), sqlite3.sqlite_version)
check(f"user_version is {LATEST}", one("PRAGMA user_version") == LATEST)
check("journal_mode is WAL", one("PRAGMA journal_mode") == "wal")
check("integrity_check ok", one("PRAGMA integrity_check") == "ok")
tables = [r[0] for r in rows("SELECT name FROM sqlite_schema WHERE type='table' "
                              "AND name NOT LIKE 'search%' AND name NOT LIKE 'sqlite%' ORDER BY name")]
indexes = [r[0] for r in rows("SELECT name FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL")]
views = [r[0] for r in rows("SELECT name FROM sqlite_schema WHERE type='view' ORDER BY name")]
triggers = [r[0] for r in rows("SELECT name FROM sqlite_schema WHERE type='trigger'")]
print(f"  {len(tables)} tables + search, {len(indexes)} indexes, {len(views)} views, {len(triggers)} triggers")
strict = rows("SELECT name FROM pragma_table_list WHERE type='table' AND strict=0 "
              "AND name NOT LIKE 'search%' AND name NOT LIKE 'sqlite%'")
check("every table is STRICT", strict == [], strict)

NOW = "2026-10-02T14:05:00Z"

# =====================================================================
# Provenance conventions
# =====================================================================
con.execute("INSERT INTO sources (kind, name, base_url) VALUES ('jira','jira-dc','https://jira.example.gov')")   # 1
con.execute("INSERT INTO sources (kind, name, base_url) VALUES ('github','github','https://github.example.gov')")  # 2
con.execute("INSERT INTO sources (kind, name) VALUES ('calendar','outlook')")                                      # 3
con.execute("INSERT INTO sources (kind, name) VALUES ('manual','paste')")                                          # 4
con.execute("INSERT INTO identities (kind, value, source_id) VALUES ('git_email','Me@Agency.gov',NULL)")
rejects("identity values are unique ignoring case",
        "INSERT INTO identities (kind, value) VALUES ('git_email','me@agency.gov')")
rejects("ts rejects a space separator", "INSERT INTO sources (kind,name,created_at) VALUES ('jira','x1','2026-10-02 14:05:00')")
rejects("ts rejects month 13", "INSERT INTO sources (kind,name,created_at) VALUES ('jira','x2','2026-13-02T14:05:00Z')")
rejects("ts rejects fractional seconds", "INSERT INTO sources (kind,name,created_at) VALUES ('jira','x3','2026-10-02T14:05:00.123Z')")
rejects("ts rejects a missing Z", "INSERT INTO sources (kind,name,created_at) VALUES ('jira','x4','2026-10-02T14:05:00')")
rejects("STRICT rejects text in an INTEGER column", "INSERT INTO repos (name, local_path, active) VALUES ('x','y','yes')")
check("ts default is canonical", one("SELECT created_at GLOB '????-??-??T??:??:??Z' FROM sources WHERE id=1") == 1)

# =====================================================================
# Odin: work items, aliases, transitions
# =====================================================================
run = one("INSERT INTO sync_runs (app, source_id, stream) VALUES ('odin',1,'issues') RETURNING id")
UPSERT = """
INSERT INTO work_items (source_id, jira_id, key, project_key, issue_type, summary, status, status_category,
                        resolution, is_mine, labels, url, created_at, updated_at, resolved_at,
                        first_seen_at, last_seen_at, run_id)
VALUES (:source_id, :jira_id, :key, :project_key, :issue_type, :summary, :status, :status_category,
        :resolution, :is_mine, :labels, :url, :created_at, :updated_at, :resolved_at, :now, :now, :run_id)
ON CONFLICT (source_id, jira_id) DO UPDATE SET
    key = excluded.key, project_key = excluded.project_key, issue_type = excluded.issue_type,
    summary = excluded.summary, status = excluded.status, status_category = excluded.status_category,
    resolution = excluded.resolution, is_mine = max(work_items.is_mine, excluded.is_mine),
    labels = excluded.labels, url = excluded.url, updated_at = excluded.updated_at,
    resolved_at = excluded.resolved_at, deleted_at = NULL, run_id = excluded.run_id
WHERE excluded.updated_at > work_items.updated_at
RETURNING id, (first_seen_at = :now) AS inserted
"""
TOUCH = "UPDATE work_items SET last_seen_at = :now WHERE source_id = :source_id AND jira_id = :jira_id"
ALIAS = """
INSERT INTO work_item_aliases (key, work_item_id, status, checked_at) VALUES (?, ?, ?, ?)
ON CONFLICT (key) DO UPDATE SET work_item_id = excluded.work_item_id, status = excluded.status,
                                checked_at = excluded.checked_at
"""
item = dict(source_id=1, jira_id="10234", key="ABC-123", project_key="ABC", issue_type="Story",
            summary="Add PIV fallback to Odin", status="In Progress", status_category="in_progress",
            resolution=None, is_mine=1, labels='["odin","auth"]', url="https://jira.example.gov/browse/ABC-123",
            created_at="2026-09-01T12:00:00Z", updated_at="2026-09-30T09:00:00Z", resolved_at=None,
            now=NOW, run_id=run)
con.execute("BEGIN IMMEDIATE")
r1 = con.execute(UPSERT, item).fetchall()
con.execute(TOUCH, item)
con.execute(ALIAS, ("ABC-123", r1[0][0], "current", NOW))
con.execute("COMMIT")
WI = r1[0][0]
check("first upsert inserts", len(r1) == 1 and r1[0][1] == 1, r1)
r2 = con.execute(UPSERT, dict(item, now="2026-10-02T15:00:00Z")).fetchall()
con.execute(TOUCH, dict(item, now="2026-10-02T15:00:00Z"))
check("unchanged upsert returns nothing (no event written)", r2 == [], r2)
check("touch still moves last_seen_at", one("SELECT last_seen_at FROM work_items") == "2026-10-02T15:00:00Z")

# Baldur's branch for this ticket was cut before the move, so Baldur keeps seeing ABC-123.
moved = dict(item, key="XYZ-45", project_key="XYZ", status="In Review", updated_at="2026-10-01T10:00:00Z",
             url="https://jira.example.gov/browse/XYZ-45", now="2026-10-02T16:00:00Z")
con.execute("BEGIN IMMEDIATE")
r3 = con.execute(UPSERT, moved).fetchall()
con.execute(ALIAS, ("ABC-123", WI, "moved", "2026-10-02T16:00:00Z"))
con.execute(ALIAS, ("XYZ-45", WI, "current", "2026-10-02T16:00:00Z"))
con.execute("COMMIT")
check("a moved issue updates the same row", r3 and r3[0][0] == WI and r3[0][1] == 0, r3)
check("both keys resolve to the one issue",
      rows("SELECT key, status FROM work_item_aliases WHERE work_item_id = ? ORDER BY key", (WI,))
      == [("ABC-123", "moved"), ("XYZ-45", "current")])
rejects("an issue has one current key",
        "INSERT INTO work_item_aliases (key, work_item_id, status, checked_at) VALUES ('XYZ-9999', ?, 'current', ?)", (WI, NOW))
rejects("a not_found alias cannot point at an issue",
        "INSERT INTO work_item_aliases (key, work_item_id, status, checked_at) VALUES ('Q-1', 1, 'not_found', ?)", (NOW,))
rejects("a current alias must point at an issue",
        "INSERT INTO work_item_aliases (key, work_item_id, status, checked_at) VALUES ('Q-2', NULL, 'current', ?)", (NOW,))
rejects("labels must be a JSON array", "UPDATE work_items SET labels = '{}' WHERE id = 1")
rejects("status_category is an enum", "UPDATE work_items SET status_category = 'blocked' WHERE id = 1")

hits = rows("SELECT kind, rowid >> 4, title FROM search WHERE search MATCH 'fallback' ORDER BY bm25(search)")
check("search finds the moved issue under its new key", hits and hits[0][0] == "work_items" and "XYZ-45" in hits[0][2], hits)
check("search keeps no stale copy of the old key", one("SELECT count(*) FROM search WHERE search MATCH 'ABC'") == 0)

con.execute("INSERT INTO work_item_transitions (work_item_id, changelog_id, at, from_status, to_status, "
            "to_category, by_me) VALUES (?, '55001', '2026-10-01T10:00:00Z', 'In Progress', 'In Review', "
            "'in_progress', 1)", (WI,))

# A second issue of mine, finished later, and one that someone else owns.
other = dict(item, jira_id="10300", key="XYZ-50", summary="Retry SeCcHm polling", status="Done",
             status_category="done", resolution="Done", url="https://jira.example.gov/browse/XYZ-50",
             updated_at="2026-10-02T12:00:00Z", resolved_at="2026-10-02T12:00:00Z")
WI2 = con.execute(UPSERT, other).fetchone()[0]
con.execute(ALIAS, ("XYZ-50", WI2, "current", NOW))
wont = dict(item, jira_id="10301", key="XYZ-51", summary="Old export idea", status="Closed",
            status_category="done", resolution="Won't Do", url="https://jira.example.gov/browse/XYZ-51",
            updated_at="2026-10-02T12:00:00Z", resolved_at="2026-10-02T12:00:00Z")
WI3 = con.execute(UPSERT, wont).fetchone()[0]
con.execute(ALIAS, ("XYZ-51", WI3, "current", NOW))

# =====================================================================
# Shared: events and consumer cursors
# =====================================================================
con.execute("INSERT INTO events (app, kind, entity_type, entity_id, ref, run_id, payload) "
            "VALUES ('odin','work_item.updated','work_items',?, 'XYZ-45', ?, '{\"to\":\"In Review\"}')", (WI, run))
rejects("events cannot be updated", "UPDATE events SET ref = 'x'")
rejects("events cannot be deleted", "DELETE FROM events")
rejects("event kind must be noun.verb", "INSERT INTO events (app, kind, entity_type) VALUES ('odin','updated','work_items')")
rejects("event payload must be an object", "INSERT INTO events (app, kind, entity_type, payload) VALUES ('odin','a.b','x','[]')")
rejects("event app is an enum", "INSERT INTO events (app, kind, entity_type) VALUES ('thor','a.b','x')")
rejects("an event cursor cannot go negative",
        "INSERT INTO event_cursors (app, last_event_id, updated_at) VALUES ('freya', -1, ?)", (NOW,))

# =====================================================================
# Odin: calendar
# =====================================================================
CAL = ("INSERT INTO calendar_events (source_id, external_id, title, starts_at, ends_at, is_all_day, show_as, "
       "response, is_cancelled, first_seen_at, last_seen_at, deleted_at) VALUES (3,?,?,?,?,?,?,?,?,?,?,?)")
cal_rows = [
    ("e1", "Standup", "2026-10-01T13:00:00Z", "2026-10-01T13:15:00Z", 0, "busy", "accepted", 0, None),
    ("e2", "Design review", "2026-10-01T15:00:00Z", "2026-10-01T16:00:00Z", 0, "tentative", "tentative", 0, None),
    ("e3", "Declined sync", "2026-10-01T17:00:00Z", "2026-10-01T17:30:00Z", 0, "busy", "declined", 0, None),
    ("e4", "Focus block", "2026-10-01T18:00:00Z", "2026-10-01T20:00:00Z", 0, "free", "organizer", 0, None),
    ("e5", "Holiday", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", 1, "oof", "none", 0, None),
    ("e6", "Cancelled", "2026-10-01T19:00:00Z", "2026-10-01T19:30:00Z", 0, "busy", "accepted", 1, None),
    ("e7", "Removed", "2026-10-01T20:00:00Z", "2026-10-01T20:30:00Z", 0, "busy", "accepted", 0, NOW),
]
for r in cal_rows:
    con.execute(CAL, r[:8] + (NOW, NOW, r[8]))
check("v_busy_meetings keeps busy and tentative, drops declined, free, all-day, cancelled, deleted",
      [r[0] for r in rows("SELECT external_id FROM v_busy_meetings ORDER BY starts_at")] == ["e1", "e2"])
rejects("a calendar event cannot end before it starts",
        CAL, ("e8", "Bad", "2026-10-01T10:00:00Z", "2026-10-01T09:00:00Z", 0, "busy", "accepted", 0, NOW, NOW, None))

# =====================================================================
# Baldur: repos, commits, keys, reflog, PRs
# =====================================================================
rejects("a repo needs a GitHub name or a local path", "INSERT INTO repos (name) VALUES ('nowhere')")
con.execute("INSERT INTO repos (source_id, name, github_repo, local_path, default_branch) "
            "VALUES (2, 'asgard', 'team/asgard', 'C:\\src\\asgard', 'main')")             # 1
con.execute("INSERT INTO repos (source_id, name, github_repo) VALUES (2, 'portal', 'team/portal')")   # 2: review-only
COMMIT_SQL = ("INSERT INTO commits (repo_id, sha, patch_id, author_name, author_email, authored_at, committed_at, "
              "subject, additions, deletions, is_mine, branch_hint, first_seen_at) "
              "VALUES (1,?,?,'Me','me@agency.gov',?,?,?,10,2,1,?,?) RETURNING id")
c1 = one(COMMIT_SQL, ("a" * 40, "p1", "2026-10-01T13:40:00Z", "2026-10-01T13:40:00Z", "Add curl PIV path",
                      "feature/ABC-123-piv", NOW))
c2 = one(COMMIT_SQL, ("b" * 40, "p2", "2026-10-01T15:30:00Z", "2026-10-01T15:30:00Z", "Handle expired cert",
                      "feature/ABC-123-piv", NOW))
c3 = one(COMMIT_SQL, ("c" * 40, "p3", "2026-10-02T13:10:00Z", "2026-10-02T13:10:00Z", "XYZ-50 retry poll",
                      "main", NOW))
c4 = one(COMMIT_SQL, ("d" * 40, "p4", "2026-10-02T14:00:00Z", "2026-10-02T14:00:00Z", "NEW-7 spike",
                      "spike", NOW))
rejects("SHA length is checked",
        "INSERT INTO commits (repo_id, sha, author_name, author_email, authored_at, committed_at, subject, "
        "first_seen_at) VALUES (1,'abc','a','b',?,?,'s',?)", (NOW, NOW, NOW))
con.executemany("INSERT INTO commit_work_items (commit_id, work_item_key, method) VALUES (?,?,?)",
                [(c1, "ABC-123", "reflog"), (c2, "ABC-123", "branch"), (c3, "XYZ-50", "message"),
                 (c4, "NEW-7", "message"), (c4, "GONE-9", "message"), (c4, "OLD-2", "message")])
rejects("a commit lists a key once", "INSERT INTO commit_work_items VALUES (?, 'ABC-123', 'pr')", (c1,))
rejects("key method is an enum", "INSERT INTO commit_work_items VALUES (?, 'ABC-9', 'fuzzy')", (c1,))
con.execute("INSERT INTO reflog_entries (repo_id, ref, at, action, sha, message) VALUES "
            "(1, 'HEAD', '2026-10-01T13:05:00Z', 'checkout', ?, 'checkout: moving from main to feature/ABC-123-piv')",
            ("a" * 40,))
con.execute("INSERT INTO reflog_entries (repo_id, ref, at, action, sha, message) VALUES "
            "(1, 'refs/heads/feature/ABC-123-piv', '2026-10-01T13:05:00Z', 'branch', ?, "
            "'branch: Created from main')", ("a" * 40,))
rejects("the same reflog line is stored once",
        "INSERT INTO reflog_entries (repo_id, ref, at, action, sha) VALUES "
        "(1, 'HEAD', '2026-10-01T13:05:00Z', 'checkout', ?)", ("a" * 40,))
PR = ("INSERT INTO pull_requests (repo_id, number, title, author, is_mine, head_ref, work_item_key, state, "
      "is_draft, review_requested, created_at, updated_at, merged_at, url, first_seen_at, last_seen_at) "
      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id")
pr7 = one(PR, (1, 7, "PIV fallback for Odin", "me", 1, "feature/ABC-123-piv", "ABC-123", "merged", 0, 0,
               "2026-10-01T16:00:00Z", "2026-10-02T11:00:00Z", "2026-10-02T11:00:00Z",
               "https://github.example.gov/team/asgard/pull/7", NOW, NOW))
pr31 = one(PR, (2, 31, "Portal: session timeout", "teammate", 0, "feature/POR-88-timeout", "POR-88", "open", 0, 1,
                "2026-10-02T13:00:00Z", "2026-10-02T13:30:00Z", None,
                "https://github.example.gov/team/portal/pull/31", NOW, NOW))
one(PR, (2, 32, "Portal: draft idea", "teammate", 0, "feature/POR-90-idea", "POR-90", "open", 1, 1,
         "2026-10-02T13:00:00Z", "2026-10-02T13:30:00Z", None, "https://github.example.gov/team/portal/pull/32", NOW, NOW))
rejects("a merged PR needs merged_at", PR, (1, 8, "t", "a", 0, "x", None, "merged", 0, 0, NOW, NOW, None, "u", NOW, NOW))
rejects("a PR needs its head branch", PR, (1, 9, "t", "a", 0, None, None, "open", 0, 0, NOW, NOW, None, "u", NOW, NOW))
con.execute("INSERT INTO pr_reviews (pr_id, github_id, reviewer, is_mine, state, submitted_at) "
            "VALUES (?, 'R1', 'lead', 0, 'approved', '2026-10-02T10:30:00Z')", (pr7,))
con.execute("INSERT INTO pr_reviews (pr_id, github_id, reviewer, is_mine, state, submitted_at, notified_at) "
            "VALUES (?, 'R2', 'me', 1, 'commented', '2026-10-02T13:45:00Z', NULL)", (pr31,))
to_notify = rows("SELECT v.github_id FROM pr_reviews v JOIN pull_requests p ON p.id = v.pr_id "
                 "WHERE v.notified_at IS NULL AND v.is_mine = 0 AND p.is_mine = 1")
check("reviews of your PRs wait for a notification", to_notify == [("R1",)], to_notify)
act = rows("SELECT kind FROM v_activity ORDER BY at, kind")
check("v_activity merges commits, reflog, my reviews and my transitions in time order",
      [a[0] for a in act] == ["transition", "reflog", "reflog", "commit", "commit", "commit", "pr_review", "commit"], act)
if one("PRAGMA user_version") >= 2:
    con.execute("INSERT INTO commits (repo_id, sha, author_name, author_email, authored_at, committed_at, subject, "
                "is_merge, is_mine, first_seen_at) VALUES (1, ?, 'Me', 'me@agency.gov', '2026-10-02T12:00:00Z', "
                "'2026-10-02T12:00:00Z', 'Retry poll (#7)', 1, 1, ?)", ("f" * 40, NOW))
    check("v_activity leaves out squash copies (is_merge = 1; schema v2)",
          rows("SELECT kind FROM v_activity ORDER BY at, kind") == act)

# =====================================================================
# Key resolution: v_unknown_keys
# =====================================================================
# v_unknown_keys compares with the real clock ('now', '-7 days'), so these two are relative to it;
# fixed dates here made the checks start failing a week after they were written.
con.execute(ALIAS, ("GONE-9", None, "not_found", one("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 day')")))
con.execute(ALIAS, ("OLD-2", None, "not_found", one("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-20 days')")))
unknown = [r[0] for r in rows("SELECT key FROM v_unknown_keys ORDER BY key")]
check("v_unknown_keys: new keys and stale misses, not moved or recent misses",
      unknown == ["NEW-7", "OLD-2", "POR-88", "POR-90"], unknown)

# =====================================================================
# Baldur: calibration, runs, sessions, proposals
# =====================================================================
CAL_RUN = ("INSERT INTO calibration_runs (gap_minutes, lead_in_minutes, ambient_weight, days_used, "
           "mean_abs_error_min, bias_min, is_active) VALUES (?,?,?,?,?,?,?)")
con.execute(CAL_RUN, (120, 30, 0.5, 15, 42.5, -8.0, 1))
rejects("calibration may only bias low", CAL_RUN, (90, 30, 0.5, 15, 40.0, 2.0, 0))
rejects("only one active calibration", CAL_RUN, (90, 30, 0.5, 15, 40.0, -2.0, 1))
rejects("ambient weight is 0..1", CAL_RUN, (90, 30, 1.5, 15, 40.0, -2.0, 0))
RUN = ("INSERT INTO estimate_runs (date_from, date_to, model_version, params, params_hash, calibration_id) "
       "VALUES (?, ?, 'baldur-1', '{\"gap\":120}', 'h1', 1) RETURNING id")
er1 = one(RUN, ("2026-10-01", "2026-10-02"))
rejects("estimate run dates in order", RUN, ("2026-10-02", "2026-10-01"))
SESSION = ("INSERT INTO work_sessions (estimate_run_id, local_date, started_at, ended_at, start_basis, policy, "
           "focused_minutes, ambient_minutes, counted_minutes, commit_count) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id")
s1 = one(SESSION, (er1, "2026-10-01", "2026-10-01T13:05:00Z", "2026-10-01T15:30:00Z", "reflog", "overlap",
                   130.0, 0.0, 130.0, 2))
rejects("counted minutes never exceed focused + ambient", SESSION,
        (er1, "2026-10-01", "2026-10-01T13:05:00Z", "2026-10-01T15:30:00Z", "reflog", "overlap", 100.0, 10.0, 120.0, 1))
rejects("session policy is an enum", SESSION,
        (er1, "2026-10-01", "2026-10-01T13:05:00Z", "2026-10-01T15:30:00Z", "reflog", "double", 100.0, 0.0, 90.0, 1))
con.executemany("INSERT INTO session_commits VALUES (?, ?)", [(s1, c1), (s1, c2)])
con.execute("INSERT INTO session_allocations (session_id, work_item_key, minutes) VALUES (?, 'ABC-123', 120)", (s1,))
con.execute("INSERT INTO session_allocations (session_id, work_item_key, minutes) VALUES (?, NULL, 10)", (s1,))
rejects("one untracked allocation per session",
        "INSERT INTO session_allocations (session_id, work_item_key, minutes) VALUES (?, NULL, 5)", (s1,))

PROPOSE = ("INSERT INTO day_proposals (estimate_run_id, local_date, work_item_key, minutes_raw, minutes_proposed, "
           "first_started_at, basis, basis_hash) VALUES (?,?,?,?,?,?,?,?) RETURNING id")
p1 = one(PROPOSE, (er1, "2026-10-01", "ABC-123", 128.4, 120, "2026-10-01T13:05:00Z", "2 commits, reflog start", "bh1"))
pu = one(PROPOSE, (er1, "2026-10-01", None, 10.0, 0, "2026-10-01T13:05:00Z", "untracked", "bhu"))
rejects("rounding only goes down", PROPOSE,
        (er1, "2026-10-02", "XYZ-50", 50.0, 60, "2026-10-02T12:00:00Z", "b", "bh"))
rejects("one open proposal per day and ticket", PROPOSE,
        (er1, "2026-10-01", "ABC-123", 60.0, 60, "2026-10-01T13:05:00Z", "b", "bh"))
rejects("untracked time is never approved",
        "UPDATE day_proposals SET status='approved', minutes_final=0, decided_at=? WHERE id=?", (NOW, pu))
rejects("an approval needs its final minutes",
        "UPDATE day_proposals SET status='approved', decided_at=? WHERE id=?", (NOW, p1))
rejects("an approval needs decided_at",
        "UPDATE day_proposals SET status='approved', minutes_final=120 WHERE id=?", (p1,))
con.execute("UPDATE day_proposals SET status='approved', minutes_final=120, decided_at='2026-10-02T09:00:00Z' "
            "WHERE id=?", (p1,))
rejects("an approved proposal is final", "UPDATE day_proposals SET minutes_final=150 WHERE id=?", (p1,))
rejects("an approved proposal cannot go back to proposed", "UPDATE day_proposals SET status='proposed' WHERE id=?", (p1,))
check("time_by_item_month reads approvals", rows("SELECT * FROM v_time_by_item_month") == [("2026-10", "ABC-123", 120)])

# =====================================================================
# v_day_status and v_worklogs_to_post (Odin's posting queue)
# =====================================================================
day = rows("SELECT local_date, work_item_key, work_item_id, approved_minutes, logged_minutes FROM v_day_status")
check("an old key resolves through its alias", day == [("2026-10-01", "ABC-123", WI, 120, 0)], day)

WL = ("INSERT INTO worklogs (work_item_id, jira_worklog_id, origin, state, started_at, seconds, comment, "
      "proposal_id, calendar_event_id, posted_at) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id")
# 2026-10-01 in New York is 04:00Z on Oct 1 to 04:00Z on Oct 2.
one(WL, (WI, "w-early", "jira", "posted", "2026-10-01T03:30:00Z", 1800, "Sep 30 local", None, None, NOW))
one(WL, (WI, "w-manual", "jira", "posted", "2026-10-01T14:00:00Z", 1800, "by hand", None, None, NOW))
one(WL, (WI, "w-late", "jira", "posted", "2026-10-02T03:30:00Z", 899, "11:30 pm local", None, None, NOW))
one(WL, (WI, "w-meet", "meeting", "posted", "2026-10-01T15:00:00Z", 3600, "Design review", None, 2, NOW))
day = rows("SELECT logged_minutes FROM v_day_status")
check("logged = manual + late-evening work; meetings and the prior local day excluded; seconds round up",
      day == [(45,)], day)
post = rows("SELECT work_item_key, proposal_id, minutes_to_post FROM v_worklogs_to_post")
check("Odin posts only the shortfall", post == [("ABC-123", p1, 75)], post)

rejects("a posted worklog needs its Jira id", WL, (WI, None, "manual", "posted", NOW, 60, None, None, None, NOW))
rejects("a sending worklog has no Jira id yet", WL, (WI, "w-x", "baldur", "sending", NOW, 60, None, p1, None, None))
rejects("a Jira-found worklog is never sending", WL, (WI, None, "jira", "sending", NOW, 60, None, None, None, None))
rejects("a Baldur worklog names its approval", WL, (WI, None, "baldur", "sending", NOW, 60, None, None, None, None))
rejects("only Baldur worklogs name an approval", WL, (WI, None, "manual", "sending", NOW, 60, None, p1, None, None))
rejects("a meeting worklog names its calendar event", WL, (WI, None, "meeting", "sending", NOW, 60, None, None, None, None))
rejects("a Jira worklog id is stored once per issue", WL,
        (WI, "w-manual", "jira", "posted", NOW, 60, None, None, None, NOW))

# Two-phase post: the sending row lands before the Jira call.
con.execute("BEGIN IMMEDIATE")
wl = one(WL, (WI, None, "baldur", "sending", "2026-10-01T13:05:00Z", 75 * 60,
              "Baldur estimate [asgard:p%d:bh1]" % p1, p1, None, None))
con.execute("COMMIT")
check("a sending row already counts, so a crash can't double-post", rows("SELECT * FROM v_worklogs_to_post") == [])
check("a stuck sending row is easy to find on restart",
      "ix_worklogs_sending" in " ".join(r[3] for r in con.execute(
          "EXPLAIN QUERY PLAN SELECT id FROM worklogs WHERE state = 'sending' ORDER BY created_at")))
con.execute("UPDATE worklogs SET state='failed', error='HTTP 503' WHERE id=?", (wl,))
check("a failed post is offered again", rows("SELECT minutes_to_post FROM v_worklogs_to_post") == [(75,)])
wl = one(WL, (WI, None, "baldur", "sending", "2026-10-01T13:05:00Z", 75 * 60,
              "Baldur estimate [asgard:p%d:bh1]" % p1, p1, None, None))
con.execute("UPDATE worklogs SET state='posted', jira_worklog_id='88001', posted_at=? WHERE id=?", (NOW, wl))
check("after posting, Jira matches the approval",
      rows("SELECT approved_minutes, logged_minutes FROM v_day_status") == [(120, 120)])

# You delete Odin's worklog in Jira: not re-posted for the same approval.
con.execute("UPDATE worklogs SET state='deleted' WHERE id=?", (wl,))
check("a worklog you deleted in Jira is not re-posted", rows("SELECT * FROM v_worklogs_to_post") == [])

# You re-approve the day at 105 under the new key: supersede + insert in one transaction.
con.execute("BEGIN IMMEDIATE")
con.execute("UPDATE day_proposals SET status='superseded' WHERE id=?", (p1,))
p2 = one(PROPOSE, (er1, "2026-10-01", "XYZ-45", 128.4, 120, "2026-10-01T13:05:00Z", "2 commits, reflog start", "bh1"))
con.execute("UPDATE day_proposals SET status='approved', minutes_final=105, decided_at='2026-10-02T17:00:00Z' "
            "WHERE id=?", (p2,))
con.execute("COMMIT")
post = rows("SELECT work_item_key, proposal_id, minutes_to_post FROM v_worklogs_to_post")
check("a new approval is new consent: post 105 - 45", post == [("XYZ-45", p2, 60)], post)

# An older approval under the old key, decided earlier, loses to the newer one for the same issue.
er0 = one(RUN, ("2026-10-01", "2026-10-01"))
p0 = one(PROPOSE, (er0, "2026-10-01", "ABC-123", 200.0, 195, "2026-10-01T13:00:00Z", "older run", "bh0"))
con.execute("UPDATE day_proposals SET status='approved', minutes_final=195, decided_at='2026-10-02T08:00:00Z' "
            "WHERE id=?", (p0,))
day = rows("SELECT work_item_key, approved_minutes FROM v_day_status")
check("one row per issue per day; the newest approval wins across keys", day == [("XYZ-45", 105)], day)

# A ticket Odin hasn't resolved yet stays out of the posting queue.
p3 = one(PROPOSE, (er1, "2026-10-02", "NEW-7", 40.0, 30, "2026-10-02T13:30:00Z", "1 commit", "bh3"))
con.execute("UPDATE day_proposals SET status='approved', minutes_final=30, decided_at=? WHERE id=?", (NOW, p3))
check("unresolved keys appear in day status without an issue",
      ("NEW-7", None) in rows("SELECT work_item_key, work_item_id FROM v_day_status"))
check("unresolved keys are not posted", [r[0] for r in rows("SELECT work_item_key FROM v_worklogs_to_post")] == ["XYZ-45"])
# A deleted issue is not posted to.
p4 = one(PROPOSE, (er1, "2026-10-02", "XYZ-50", 50.0, 45, "2026-10-02T13:00:00Z", "1 commit", "bh4"))
con.execute("UPDATE day_proposals SET status='approved', minutes_final=45, decided_at=? WHERE id=?", (NOW, p4))
check("XYZ-50 is queued", "XYZ-50" in [r[0] for r in rows("SELECT work_item_key FROM v_worklogs_to_post")])
con.execute("UPDATE work_items SET deleted_at=? WHERE id=?", (NOW, WI2))
check("a deleted issue is not posted to", "XYZ-50" not in [r[0] for r in rows("SELECT work_item_key FROM v_worklogs_to_post")])
con.execute("UPDATE work_items SET deleted_at=NULL WHERE id=?", (WI2,))

# Daylight saving: 2026-11-01 in New York runs 04:00Z to 05:00Z the next day (25 hours).
er2 = one(RUN, ("2026-11-01", "2026-11-01"))
p5 = one(PROPOSE, (er2, "2026-11-01", "XYZ-50", 60.0, 60, "2026-11-01T14:00:00Z", "dst", "bh5"))
con.execute("UPDATE day_proposals SET status='approved', minutes_final=60, decided_at=? WHERE id=?", (NOW, p5))
one(WL, (WI2, "w-dst", "jira", "posted", "2026-11-02T04:30:00Z", 1800, "11:30 pm local", None, None, NOW))
check("local day bounds follow daylight saving",
      one("SELECT logged_minutes FROM v_day_status WHERE local_date='2026-11-01'") == 30)

rejects("an approved proposal with a worklog cannot be deleted",
        "DELETE FROM day_proposals WHERE id=?", (p1,))
rejects("an estimate run with posted approvals cannot be pruned", "DELETE FROM estimate_runs WHERE id=?", (er1,))

# =====================================================================
# Calibration actuals
# =====================================================================
con.execute("INSERT INTO time_actuals (on_date, minutes) VALUES ('2026-10-01', 420)")
rejects("one whole-day actual per date", "INSERT INTO time_actuals (on_date, minutes) VALUES ('2026-10-01', 400)")
rejects("date convention", "INSERT INTO time_actuals (on_date, minutes) VALUES ('2026-10-32', 10)")

# =====================================================================
# Loki
# =====================================================================
con.execute("INSERT INTO meetings (source_id, external_id, calendar_event_id, title, starts_at, ends_at, "
            "recap_origin, notes_summary, first_seen_at) VALUES (4,'paste:9f2c',2,'BEARs intake sync',"
            "'2026-10-01T15:00:00Z','2026-10-01T16:00:00Z','paste',"
            "'Agreed to submit BEARs workbook Friday; Confluence page owner is the PMO.',?)", (NOW,))
con.execute("INSERT INTO action_items (meeting_id, text, owner, is_mine, due_on) "
            "VALUES (1,'Submit BEARs workbook','me',1,'2026-10-09')")
con.execute("INSERT INTO blufs (subject_type, subject_ref, bottom_line, body_md, tier, prompt_version) VALUES "
            "('meeting','paste:9f2c','BEARs workbook goes to the PMO page Friday.','**BLUF:** ...','clipboard','loki-v1')")
rejects("a posted BLUF needs posted_at and sent_via", "UPDATE blufs SET state = 'posted' WHERE id = 1")
con.execute("UPDATE blufs SET state='approved', approved_at=? WHERE id=1", (NOW,))
con.execute("UPDATE blufs SET state='posted', posted_at='2026-10-01T17:00:00Z', sent_via='mailto' WHERE id=1")

# =====================================================================
# Freya: accomplishments from work_item.done events
# =====================================================================
ev_done = one("INSERT INTO events (app, kind, entity_type, entity_id, ref, payload) VALUES "
              "('odin','work_item.done','work_items',?,'XYZ-50','{\"resolution\":\"Done\"}') RETURNING id", (WI2,))
ev_wont = one("INSERT INTO events (app, kind, entity_type, entity_id, ref, payload) VALUES "
              "('odin','work_item.done','work_items',?,'XYZ-51','{\"resolution\":\"Won''t Do\"}') RETURNING id", (WI3,))
con.execute("INSERT INTO event_cursors (app, last_event_id, updated_at) VALUES ('freya', 0, ?)", (NOW,))
PENDING = ("SELECT id, entity_id FROM events WHERE id > (SELECT last_event_id FROM event_cursors WHERE app = 'freya') "
           "AND kind IN ('work_item.done', 'work_item.reopened') ORDER BY id")
ACCOMPLISH = """
INSERT INTO accomplishments (jira_id, work_item_key, summary, issue_type, epic_key, url, state, resolution,
                             first_done_at, last_done_at, story_points, approved_minutes, commit_count,
                             merged_pr_count, first_activity_at, last_activity_at, stats_refreshed_at)
SELECT w.jira_id, w.key, w.summary, w.issue_type, w.epic_key, w.url, 'done', w.resolution,
       w.resolved_at, w.resolved_at, w.story_points,
       coalesce((SELECT sum(d.approved_minutes) FROM v_day_status d WHERE d.work_item_id = w.id), 0),
       (SELECT count(DISTINCT coalesce(c.patch_id, c.sha)) FROM work_item_aliases al
          JOIN commit_work_items ci ON ci.work_item_key = al.key
          JOIN commits c ON c.id = ci.commit_id AND c.is_mine = 1 AND c.is_merge = 0
         WHERE al.work_item_id = w.id),
       (SELECT count(*) FROM work_item_aliases al
          JOIN pull_requests p ON p.work_item_key = al.key AND p.is_mine = 1 AND p.state = 'merged'
         WHERE al.work_item_id = w.id),
       NULL, NULL, :now
  FROM work_items w
 WHERE w.id = :id AND w.resolution NOT IN ('Won''t Do', 'Duplicate', 'Cannot Reproduce')
ON CONFLICT (jira_id) DO UPDATE SET
    state = 'done', last_done_at = excluded.last_done_at, work_item_key = excluded.work_item_key,
    summary = excluded.summary, approved_minutes = excluded.approved_minutes,
    commit_count = excluded.commit_count, merged_pr_count = excluded.merged_pr_count,
    stats_refreshed_at = excluded.stats_refreshed_at
WHERE accomplishments.frozen_at IS NULL
"""
con.execute("BEGIN IMMEDIATE")
pending = rows(PENDING)
for _eid, wid in pending:
    con.execute(ACCOMPLISH, dict(id=wid, now=NOW))
con.execute("UPDATE event_cursors SET last_event_id=?, updated_at=? WHERE app='freya'", (pending[-1][0], NOW))
con.execute("COMMIT")
check("Freya reads both done events", [p[0] for p in pending] == [ev_done, ev_wont], pending)
acc = rows("SELECT work_item_key, approved_minutes, commit_count, merged_pr_count FROM accomplishments")
check("a won't-do resolution is not an accomplishment; stats come from Baldur's tables",
      acc == [("XYZ-50", 105, 1, 0)], acc)
check("the cursor stops Freya handling an event twice", rows(PENDING) == [])
con.execute("UPDATE accomplishments SET impact_note='Cut failed SeCcHm polls from 12 to 0 a week' "
            "WHERE work_item_key='XYZ-50'")
check("an impact note is searchable",
      one("SELECT kind FROM search WHERE search MATCH 'SeCcHm AND polls' AND kind='accomplishments'") == "accomplishments")
con.execute("UPDATE accomplishments SET state='reopened', reopened_count=reopened_count+1 WHERE work_item_key='XYZ-50'")
con.execute("UPDATE accomplishments SET state='done', last_done_at='2026-10-03T12:00:00Z' WHERE work_item_key='XYZ-50'")
rejects("last done cannot precede first done",
        "UPDATE accomplishments SET last_done_at='2026-01-01T00:00:00Z' WHERE work_item_key='XYZ-50'")
con.execute("UPDATE accomplishments SET frozen_at=? WHERE work_item_key='XYZ-50'", (NOW,))
con.execute(ACCOMPLISH, dict(id=WI2, now="2026-10-05T00:00:00Z"))
check("a frozen accomplishment is not refreshed",
      one("SELECT stats_refreshed_at FROM accomplishments WHERE work_item_key='XYZ-50'") == NOW)

con.execute("INSERT INTO review_periods (label, starts_on, ends_on, rubric) VALUES "
            "('FY2026 annual','2025-10-01','2026-09-30','[{\"element\":\"Technical execution\"}]')")
rejects("review period dates in order",
        "INSERT INTO review_periods (label, starts_on, ends_on) VALUES ('bad','2026-10-01','2026-09-30')")
con.execute("INSERT INTO review_periods (label, starts_on, ends_on) VALUES ('FY2027 annual','2026-10-01','2027-09-30')")
ev = rows("SELECT evidence_type, evidence_ref FROM v_review_evidence WHERE period_id = 2 ORDER BY 1")
check("v_review_evidence: accomplishments, merged PRs and posted BLUFs",
      ev == [("accomplishment", "XYZ-50"), ("bluf", "paste:9f2c"), ("pull_request", "asgard#7")], ev)
con.execute("INSERT INTO review_drafts (period_id, element, text, tier, prompt_version) VALUES "
            "(2,'Technical execution','Fixed SeCcHm polling (XYZ-50, asgard#7).','api','freya-v1')")
con.execute("INSERT INTO citations (owner_type, owner_id, evidence_type, evidence_ref) VALUES "
            "('review_draft',1,'accomplishment','XYZ-50')")
con.execute("INSERT INTO citations (owner_type, owner_id, evidence_type, evidence_ref) VALUES "
            "('review_draft',1,'accomplishment','XYZ-999')")
rejects("a verified citation needs evidence_id", "UPDATE citations SET verified = 1 WHERE evidence_ref = 'XYZ-999'")
con.execute("""UPDATE citations SET evidence_id = (SELECT a.id FROM accomplishments a
                                                   JOIN work_item_aliases al ON al.key = citations.evidence_ref
                                                   JOIN work_items w ON w.id = al.work_item_id AND w.jira_id = a.jira_id),
                                    verified = 1
               WHERE evidence_type = 'accomplishment'
                 AND EXISTS (SELECT 1 FROM work_item_aliases al WHERE al.key = citations.evidence_ref
                                AND al.status <> 'not_found')""")
check("the checker leaves an invented ID unverified",
      rows("SELECT evidence_ref FROM v_unverified_citations") == [("XYZ-999",)])

# =====================================================================
# Heimdall and Bifrost
# =====================================================================
rejects("a non-draft submission needs approved_at",
        "INSERT INTO submissions (system, title, state) VALUES ('bears','Q4 BEARs','approved')")
con.execute("INSERT INTO submissions (system, title, work_item_key) VALUES ('bears','Q4 BEARs workbook','XYZ-45')")
rejects("submitted needs submitted_at", "UPDATE submissions SET state='submitted', approved_at=? WHERE id=1", (NOW,))
con.execute("UPDATE submissions SET state='approved', approved_at=? WHERE id=1", (NOW,))
con.execute("UPDATE submissions SET state='submitted', submitted_at=?, external_id='884213', "
            "next_poll_at='2026-10-02T14:35:00Z' WHERE id=1", (NOW,))
con.execute("UPDATE submissions SET state='in_review', last_status_raw='Under PMO review', "
            "last_polled_at='2026-10-02T14:35:00Z' WHERE id=1")
hist = rows("SELECT from_state, to_state, raw_status FROM submission_status_history ORDER BY id")
check("the history trigger logs every state",
      hist == [(None, "draft", None), ("draft", "approved", None), ("approved", "submitted", None),
               ("submitted", "in_review", "Under PMO review")], hist)

hits = {r[0] for r in rows("SELECT kind FROM search WHERE search MATCH 'bears'")}
check("search spans meetings, action items, BLUFs and submissions",
      hits == {"meetings", "action_items", "blufs", "submissions"}, hits)

# =====================================================================
# Launcher badges
# =====================================================================
badges = rows("SELECT app, priority, n, label FROM v_tile_badges ORDER BY app, priority")
check("tile badges count what needs you and skip zeros",
      badges == [("baldur", 1, 2, "reviews requested"), ("freya", 1, 1, "citations to check"),
                 ("odin", 1, 3, "worklogs to post"), ("odin", 2, 4, "keys to look up")], badges)

# =====================================================================
# Safeguards for posting
# =====================================================================
er9 = one(RUN, ("2026-12-01", "2026-12-01"))
p9 = one(PROPOSE, (er9, "2026-12-01", "XYZ-50", 61.0, 60, "2026-12-01T15:00:00Z", "basis", "bh9"))
con.execute("UPDATE day_proposals SET status='approved', minutes_final=60, decided_at=? WHERE id=?", (NOW, p9))
rejects("an unposted approval can't be pruned with its run", "DELETE FROM estimate_runs WHERE id = ?", (er9,))
due = lambda: [r[0] for r in rows("SELECT proposal_id FROM v_worklogs_to_post")]  # noqa: E731
check("the approved day is queued", p9 in due())
w9 = one(WL, (WI2, None, "manual", "sending", "2026-12-01T16:00:00Z", 600, "[asgard:o-0badf00d]", None, None, None))
check("a post in doubt that day holds the approval back", p9 not in due())
con.execute("UPDATE worklogs SET state = 'failed', error = 'HTTP 400' WHERE id = ?", (w9,))
check("once it settles, the day is offered again", p9 in due())
gone = one(PROPOSE, (er9, "2026-12-02", "GONE-9", 31.0, 30, "2026-12-02T15:00:00Z", "basis", "bh10"))
con.execute("UPDATE day_proposals SET status='approved', minutes_final=30, decided_at=? WHERE id=?", (NOW, gone))
check("approved time for a key Jira doesn't have is shown, not lost",
      rows("SELECT proposal_id, reason FROM v_unpostable_days") == [(gone, "not found in Jira")],
      rows("SELECT proposal_id, reason FROM v_unpostable_days"))

# =====================================================================
# Schema v3: rules that used to hold only in Python
# =====================================================================
if one("PRAGMA user_version") >= 3:
    con.execute("SAVEPOINT v3")          # everything here is undone at the end
    commit_id = one("SELECT id FROM commits ORDER BY id LIMIT 1")
    for bad in ("abc-123", "ABC-0", "ABC-012", "ABC 123", "ABC-1-2", "ABC-12a", "-12", "ABC"):
        rejects(f"a cross-app key must look like PROJ-123: {bad!r} (v3)",
                "INSERT INTO commit_work_items (commit_id, work_item_key, method) VALUES (?, ?, 'manual')",
                (commit_id, bad))
    con.execute("INSERT INTO commit_work_items (commit_id, work_item_key, method) VALUES (?, 'A_B2-7', 'manual')",
                (commit_id,))
    check("a key with digits and underscores in its project is taken (v3)",
          one("SELECT count(*) FROM commit_work_items WHERE work_item_key = 'A_B2-7'") == 1)
    rejects("day_proposals refuse a lower-case key (v3)", PROPOSE,
            (er9, "2026-12-03", "xyz-50", 10.0, 10, "2026-12-03T15:00:00Z", "basis", "bh-v3a"))
    ev_id = one("INSERT INTO calendar_events (source_id, external_id, title, starts_at, ends_at, first_seen_at, "
                "last_seen_at) VALUES (1, 'v3-meeting', 'Standup', '2026-12-03T14:00:00Z', '2026-12-03T14:15:00Z', "
                "?, ?) RETURNING id", (NOW, NOW))
    rejects("a meeting's Jira key is checked too (v3)",
            "UPDATE calendar_events SET logged_as_key = 'xyz-50' WHERE id = ?", (ev_id,))

    p_cap = one(PROPOSE, (er9, "2026-12-04", "XYZ-50", 61.0, 60, "2026-12-04T15:00:00Z", "basis", "bh-v3b"))
    rejects("an approval can't be for more than 1440 minutes (v3)",
            "UPDATE day_proposals SET status='approved', minutes_final=1441, decided_at=? WHERE id=?", (NOW, p_cap))
    rejects("Baldur time is posted only for an approved day (v3)", WL,
            (WI2, None, "baldur", "sending", "2026-12-04T15:00:00Z", 600, "[asgard:b-0000beef]", p_cap, None, None))
    rejects("Asgard posts at most 24 hours in one worklog (v3)", WL,
            (WI2, None, "manual", "sending", "2026-12-04T15:00:00Z", 86401, "[asgard:o-0000beef]", None, None, None))
    big = one(WL, (WI2, "99001", "jira", "posted", "2026-12-04T15:00:00Z", 90000, "two days", None, None, NOW))
    check("a worklog read from Jira is stored as Jira has it, even over 24 hours (v3)", big is not None)

    posted = one(WL, (WI2, "99002", "manual", "posted", "2026-12-05T15:00:00Z", 600, "[asgard:o-0000cafe]",
                      None, None, NOW))
    rejects("a posted worklog can't go back to failed (v3)",
            "UPDATE worklogs SET state = 'failed', jira_worklog_id = NULL WHERE id = ?", (posted,))
    rejects("a worklog Asgard sent is never deleted (v3)", "DELETE FROM worklogs WHERE id = ?", (posted,))
    con.execute("UPDATE worklogs SET state = 'deleted' WHERE id = ?", (posted,))
    check("a posted worklog can still become deleted (v3)",
          one("SELECT state FROM worklogs WHERE id = ?", (posted,)) == "deleted")
    failed = one(WL, (WI2, None, "manual", "failed", "2026-12-05T16:00:00Z", 600, "[asgard:o-0000f00d]",
                      None, None, None))
    con.execute("DELETE FROM worklogs WHERE id = ?", (failed,))
    check("a failed worklog, never in Jira, can be deleted (v3)",
          one("SELECT count(*) FROM worklogs WHERE id = ?", (failed,)) == 0)

    newest = one("SELECT max(id) FROM events")
    rejects("an event cursor can't pass the newest event (v3)",
            "INSERT INTO event_cursors (app, last_event_id, updated_at) VALUES ('loki', ?, ?)", (newest + 1, NOW))

    pr_id = one("SELECT id FROM pull_requests ORDER BY id LIMIT 1")
    if pr_id is not None:
        con.execute("UPDATE pull_requests SET author = 'renamed-author' WHERE id = ?", (pr_id,))
        check("a pull request's search entry follows its author (v3)",
              one("SELECT count(*) FROM search WHERE search MATCH 'renamed'") == 1)

    for jid in ("99003", "99004"):
        one(WL, (WI2, jid, "baldur", "posted", "2026-12-01T15:00:00Z", 3600, "[asgard:b-%s0000]" % jid[-4:],
                 p9, None, NOW))
    check("time posted twice for one approval shows in v_double_posts (v3)",
          rows("SELECT kind, ref_id, n FROM v_double_posts") == [("proposal", p9, 2)],
          rows("SELECT * FROM v_double_posts"))
    check("and on Odin's tile, first (v3)",
          rows("SELECT priority, n, label FROM v_tile_badges WHERE app = 'odin' ORDER BY priority")[0]
          == (0, 1, "worklogs posted twice"))
    con.execute("ROLLBACK TO v3")
    con.execute("RELEASE v3")

# =====================================================================
# Schema v4: agent estimates, for Baldur's AI-assisted method
# =====================================================================
if one("PRAGMA user_version") >= 4:
    con.execute("SAVEPOINT v4")          # everything here is undone at the end
    AE = ("INSERT INTO agent_estimates (agent, work_item_key, local_date, minutes, minutes_low, confidence, summary, "
          "report_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id")
    ae = one(AE, ("kiro", "XYZ-45", "2026-10-01", 90, 60, "medium", "Retry with backoff in the poller", "rh1"))
    check("an agent estimate is recorded (v4)", ae is not None)
    for name, params in (("its key is checked", ("kiro", "xyz-45", "2026-10-01", 90, None, "low", "s", "rh2")),
                         ("it is at least a minute", ("kiro", None, "2026-10-01", 0, None, "low", "s", "rh3")),
                         ("it fits in a day", ("kiro", None, "2026-10-01", 1441, None, "low", "s", "rh4")),
                         ("its low end isn't above it", ("kiro", None, "2026-10-01", 30, 45, "low", "s", "rh5")),
                         ("its confidence is high, medium or low", ("kiro", None, "2026-10-01", 30, None, "sure", "s",
                                                                    "rh6")),
                         ("its summary is one short line", ("kiro", None, "2026-10-01", 30, None, "low", "x" * 301,
                                                            "rh7")),
                         ("the same report is stored once", ("kiro", None, "2026-10-01", 30, None, "low", "s", "rh1")),
                         ("it names its agent", ("", None, "2026-10-01", 30, None, "low", "s", "rh9"))):
        rejects(f"an agent estimate: {name} (v4)", AE, params)
    rejects("an agent session can't end before it starts (v4)",
            "INSERT INTO agent_estimates (agent, local_date, started_at, ended_at, minutes, confidence, summary, "
            "report_hash) VALUES ('kiro', '2026-10-01', '2026-10-01T15:00:00Z', '2026-10-01T14:00:00Z', 30, 'low', "
            "'s', 'rh8')")
    con.execute("INSERT INTO agent_estimate_commits (estimate_id, sha) VALUES (?, ?)", (ae, "a" * 40))
    con.execute("INSERT INTO agent_estimate_commits (estimate_id, sha) VALUES (?, ?)", (ae, "b1c2d3e"))
    check("an agent estimate cites full and short SHAs (v4)",
          one("SELECT count(*) FROM agent_estimate_commits WHERE estimate_id = ?", (ae,)) == 2)
    for bad in ("ABCDEF1", "abc12", "g" * 40):
        rejects(f"an agent estimate's commit is a lower-case hex SHA of 7 to 64 characters: {bad!r} (v4)",
                "INSERT INTO agent_estimate_commits (estimate_id, sha) VALUES (?, ?)", (ae, bad))
    rejects("an agent estimate is never edited (v4)", "UPDATE agent_estimates SET minutes = 30 WHERE id = ?", (ae,))
    rejects("an agent estimate is never deleted (v4)", "DELETE FROM agent_estimates WHERE id = ?", (ae,))
    rejects("its commits are never removed (v4)", "DELETE FROM agent_estimate_commits WHERE estimate_id = ?", (ae,))
    rejects("its commits are never changed (v4)",
            "UPDATE agent_estimate_commits SET sha = 'c1c2c3c4' WHERE estimate_id = ?", (ae,))
    rejects("withdrawing records when (v4)", "UPDATE agent_estimates SET status = 'withdrawn' WHERE id = ?", (ae,))
    con.execute("UPDATE agent_estimates SET status = 'withdrawn', withdrawn_at = ? WHERE id = ?", (NOW, ae))
    check("an agent estimate can be withdrawn (v4)",
          one("SELECT status FROM agent_estimates WHERE id = ?", (ae,)) == "withdrawn")
    rejects("and stays withdrawn (v4)",
            "UPDATE agent_estimates SET status = 'recorded', withdrawn_at = NULL WHERE id = ?", (ae,))
    con.execute("ROLLBACK TO v4")
    con.execute("RELEASE v4")

# =====================================================================
# Housekeeping
# =====================================================================
run2 = one("INSERT INTO sync_runs (app, source_id, stream) VALUES ('odin',1,'issues') RETURNING id")
con.execute("UPDATE work_items SET run_id = ? WHERE id = ?", (run2, WI))
con.execute("DELETE FROM sync_runs WHERE id = ?", (run2,))
check("pruning a run nulls run_id on facts", one("SELECT run_id FROM work_items WHERE id=?", (WI,)) is None)
try:
    con.execute("DELETE FROM sync_runs WHERE id = ?", (run,))
    check("a run that events reference can still be pruned", True)
except sqlite3.DatabaseError as e:
    check("a run that events reference can still be pruned", False, str(e))

# A second connection reads while the first holds the write lock (WAL).
con.execute("BEGIN IMMEDIATE")
con.execute("UPDATE meta SET value = value WHERE key = 'none'")
reader = connect()
try:
    n = reader.execute("SELECT count(*) FROM work_items").fetchone()[0]
    check("readers are not blocked by a writer", n == 3, n)
finally:
    reader.close()
    con.execute("COMMIT")

check("foreign_key_check is clean", rows("PRAGMA foreign_key_check") == [])

# =====================================================================
# Index use
# =====================================================================
plans = {
    "Freya: my resolved items in a period": (
        "SELECT key FROM work_items WHERE is_mine = 1 AND resolved_at BETWEEN '2025-10-01' AND '2026-09-30T23:59:59Z'",
        "ix_work_items_mine_resolved"),
    "Odin: children of a tracked parent": (
        "SELECT key FROM work_items WHERE parent_key = 'XYZ-1'", "ix_work_items_parent"),
    "Odin: deletion sweep": (
        "SELECT id FROM work_items WHERE source_id = 1 AND last_seen_at < '2026-10-02T00:00:00Z' AND deleted_at IS NULL",
        "ix_work_items_last_seen"),
    "Odin: all keys of one issue": (
        "SELECT key FROM work_item_aliases WHERE work_item_id = 1", "ix_aliases_item"),
    "Odin: one day's worklogs on an issue (v_day_status)": (
        "SELECT sum(seconds) FROM worklogs WHERE work_item_id = 1 AND state IN ('sending','posted') "
        "AND started_at >= '2026-10-01T04:00:00Z' AND started_at < '2026-10-02T04:00:00Z'",
        "ix_worklogs_item_started"),
    "Odin: has this approval been posted": (
        "SELECT 1 FROM worklogs WHERE proposal_id = 3 AND state IN ('sending','posted','deleted')",
        "ix_worklogs_proposal"),
    "Baldur: my commits in a window": (
        "SELECT sha FROM commits WHERE is_mine = 1 AND authored_at BETWEEN '2026-10-01T00:00:00Z' AND '2026-10-02T00:00:00Z'",
        "ix_commits_mine_authored"),
    "Baldur: commits for a ticket": (
        "SELECT commit_id FROM commit_work_items WHERE work_item_key = 'XYZ-45'", "ix_commit_items_key"),
    "Baldur: reviews requested of you": (
        "SELECT id FROM pull_requests WHERE state = 'open' AND review_requested = 1 ORDER BY updated_at",
        "ix_prs_review_requested"),
    "Baldur: PRs for a ticket": (
        "SELECT id FROM pull_requests WHERE work_item_key = 'XYZ-45'", "ix_prs_key"),
    "Baldur: days waiting for review": (
        "SELECT local_date FROM day_proposals WHERE status = 'proposed' AND local_date >= '2026-09-01'",
        "ux_day_proposals_open"),
    "Baldur: approval for a day and ticket": (
        "SELECT id FROM day_proposals WHERE status = 'approved' AND local_date = '2026-10-01' AND work_item_key = 'XYZ-45'",
        "ux_day_proposals_approved"),
    "Freya: accomplishments in a period": (
        "SELECT work_item_key FROM accomplishments WHERE state = 'done' "
        "AND last_done_at BETWEEN '2026-10-01' AND '2027-09-30T23:59:59Z'",
        "ix_accomplishments_done"),
    "Freya: new events since the cursor": (
        "SELECT id FROM events WHERE id > 5 AND kind IN ('work_item.done','work_item.reopened')",
        "ix_events_kind (kind=? AND rowid>?)"),
    "Bifrost/Heimdall: poll queue": (
        "SELECT id FROM submissions WHERE state IN ('submitted','in_review') AND next_poll_at <= '2026-10-02T15:00:00Z'",
        "ix_submissions_poll"),
    "Ysildir: history of one row": (
        "SELECT * FROM events WHERE entity_type = 'work_items' AND entity_id = 1", "ix_events_entity"),
}
if one("PRAGMA user_version") >= 4:
    plans["Baldur: agent estimates for a day (v4)"] = (
        "SELECT id FROM agent_estimates WHERE status = 'recorded' AND local_date BETWEEN '2026-10-01' AND '2026-10-02'",
        "ix_agent_estimates_day")
    plans["Baldur: agent estimates citing a commit (v4)"] = (
        "SELECT estimate_id FROM agent_estimate_commits WHERE sha = 'b1c2d3e'", "ix_agent_estimate_commits_sha")
for name, (sql, want) in plans.items():
    plan = " | ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql))
    check("index used: " + name, want in plan, plan)

con.close()
fails = [r for r in results if not r[1]]
print("\n%d checks, %d failed" % (len(results), len(fails)))
sys.exit(1 if fails else 0)
