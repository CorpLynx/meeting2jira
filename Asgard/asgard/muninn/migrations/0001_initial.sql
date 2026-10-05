-- =====================================================================
-- Muninn schema v1  (PRAGMA user_version = 1)
-- Asgard's shared data layer: one SQLite file per user at
--   %LOCALAPPDATA%\Asgard\muninn.db
--
-- Requires SQLite 3.37+ (STRICT tables), FTS5, JSON functions and window
-- functions. The file carries its own BEGIN/COMMIT: all or nothing.
--
-- Owners (only the owner writes a table; every app may read every table):
--   shared    meta, sources, identities, sync_runs, sync_cursors,
--             events (append-only), event_cursors (one row per app)
--   Odin      work_items, work_item_transitions, work_item_aliases,
--             calendar_events, worklogs
--   Baldur    repos, commits, commit_work_items, reflog_entries,
--             pull_requests, pr_reviews, estimate_runs, work_sessions,
--             session_commits, session_allocations, day_proposals,
--             calibration_runs, time_actuals
--   Loki      meetings, action_items, blufs
--   Freya     accomplishments, review_periods, review_drafts, citations
--   Heimdall, Bifrost   submissions, submission_status_history
--
-- Jira issue keys (PROJ-123) are the join between apps. Baldur stores the
-- keys it sees; Odin resolves them in work_item_aliases, which also keeps
-- the old keys of issues that moved between projects.
--
-- Conventions
--   ts columns    TEXT, UTC, 'YYYY-MM-DDTHH:MM:SSZ'
--                 CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', col) IS col)
--   date columns  TEXT, 'YYYY-MM-DD'   CHECK (date(col) IS col)
--   booleans      INTEGER 0/1
--   JSON          TEXT, CHECK (json_valid(col) ...)
--   keys          id INTEGER PRIMARY KEY; source-system IDs are UNIQUE
--   run_id        last sync run that touched the row; SET NULL when
--                 old sync_runs rows are pruned
--
-- Connection settings live in the writer module, not here:
--   once per file:     PRAGMA journal_mode = WAL;
--   every connection:  PRAGMA foreign_keys = ON;
--                      PRAGMA busy_timeout = 5000;
--                      PRAGMA synchronous = NORMAL;
-- =====================================================================

BEGIN;

-- =====================================================================
-- Shared: provenance, sync and events
-- =====================================================================

-- Key/value settings: owner_upn, created_at, asgard_version.
CREATE TABLE meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
) STRICT, WITHOUT ROWID;

-- One row per external system Asgard reads from or writes to.
CREATE TABLE sources (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('jira','confluence','git','github','calendar','teams','secchm','manual')),
    name        TEXT NOT NULL UNIQUE,              -- 'jira-dc', 'github', 'local-git', 'outlook', 'paste'
    base_url    TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at)
) STRICT;

-- The owner's identities in each system; drives every is_mine flag.
CREATE TABLE identities (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('git_email','git_name','jira_user','github_login','m365_upn','display_name')),
    value      TEXT NOT NULL COLLATE NOCASE,
    source_id  INTEGER REFERENCES sources(id),
    UNIQUE (kind, value)
) STRICT;

CREATE TABLE sync_runs (
    id             INTEGER PRIMARY KEY,
    app            TEXT NOT NULL CHECK (app IN ('muninn','huginn','odin','baldur','loki','freya','heimdall','bifrost','ysildir','valkyrie')),
    source_id      INTEGER REFERENCES sources(id),
    stream         TEXT NOT NULL,                  -- 'issues', 'commits:<repo>', 'pulls', 'calendar'
    mode           TEXT NOT NULL DEFAULT 'incremental' CHECK (mode IN ('incremental','full')),
    started_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                   CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', started_at) IS started_at),
    finished_at    TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', finished_at) IS finished_at),
    status         TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','ok','partial','failed')),
    items_seen     INTEGER NOT NULL DEFAULT 0 CHECK (items_seen >= 0),
    items_changed  INTEGER NOT NULL DEFAULT 0 CHECK (items_changed >= 0),
    cursor_before  TEXT,
    cursor_after   TEXT,
    error          TEXT
) STRICT;

-- High-water mark per stream; advanced only after a run commits.
CREATE TABLE sync_cursors (
    source_id   INTEGER NOT NULL REFERENCES sources(id),
    stream      TEXT NOT NULL,
    cursor      TEXT NOT NULL,
    updated_at  TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
    run_id      INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL,
    PRIMARY KEY (source_id, stream)
) STRICT, WITHOUT ROWID;

-- Append-only change log; every app writes here.
CREATE TABLE events (
    id           INTEGER PRIMARY KEY,
    at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                 CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', at) IS at),
    app          TEXT NOT NULL CHECK (app IN ('muninn','huginn','odin','baldur','loki','freya','heimdall','bifrost','ysildir','valkyrie')),
    kind         TEXT NOT NULL CHECK (kind GLOB '[a-z]*.[a-z]*'),   -- 'work_item.done'
    entity_type  TEXT NOT NULL,
    entity_id    INTEGER,
    ref          TEXT,                             -- external ID: Jira key, SHA, meeting ID
    run_id       INTEGER,                          -- no FK on purpose: events outlive pruned sync_runs
    payload      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload) AND json_type(payload) = 'object')
) STRICT;

CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

-- How far each consuming app has read the event log. Advance it in the
-- same transaction as the writes it caused, so nothing is handled twice.
CREATE TABLE event_cursors (
    app            TEXT PRIMARY KEY CHECK (app IN ('muninn','huginn','odin','baldur','loki','freya','heimdall','bifrost','ysildir','valkyrie')),
    last_event_id  INTEGER NOT NULL DEFAULT 0 CHECK (last_event_id >= 0),
    updated_at     TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at)
) STRICT, WITHOUT ROWID;

-- =====================================================================
-- Odin: Jira, calendar, worklogs
-- =====================================================================

-- Jira issues. jira_id is stable; key changes if an issue moves projects.
CREATE TABLE work_items (
    id               INTEGER PRIMARY KEY,
    source_id        INTEGER NOT NULL REFERENCES sources(id),
    jira_id          TEXT NOT NULL,
    key              TEXT NOT NULL,
    project_key      TEXT NOT NULL,
    issue_type       TEXT NOT NULL,
    summary          TEXT NOT NULL,
    status           TEXT NOT NULL,
    status_category  TEXT NOT NULL CHECK (status_category IN ('todo','in_progress','done')),
    resolution       TEXT,                         -- 'Done', 'Won't Do', 'Duplicate'; NULL while open
    priority         TEXT,
    epic_key         TEXT,
    parent_key       TEXT,
    assignee         TEXT,
    reporter         TEXT,
    is_mine          INTEGER NOT NULL DEFAULT 0 CHECK (is_mine IN (0,1)),   -- assigned to me now or ever
    labels           TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(labels) AND json_type(labels) = 'array'),
    components       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(components) AND json_type(components) = 'array'),
    story_points     REAL CHECK (story_points >= 0),
    sprint           TEXT,
    url              TEXT NOT NULL,
    created_at       TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    updated_at       TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
    resolved_at      TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', resolved_at) IS resolved_at),
    due_on           TEXT CHECK (date(due_on) IS due_on),
    first_seen_at    TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', first_seen_at) IS first_seen_at),
    last_seen_at     TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_seen_at) IS last_seen_at),
    deleted_at       TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', deleted_at) IS deleted_at),
    run_id           INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL,
    UNIQUE (source_id, jira_id),
    UNIQUE (source_id, key)
) STRICT;

-- Every key an issue has had, plus keys Odin looked up and couldn't find.
-- Other apps store keys; this table turns any of them into a work item.
CREATE TABLE work_item_aliases (
    key           TEXT PRIMARY KEY,
    work_item_id  INTEGER REFERENCES work_items(id) ON DELETE CASCADE,
    status        TEXT NOT NULL CHECK (status IN ('current','moved','not_found')),
    checked_at    TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', checked_at) IS checked_at),
    CHECK ((status = 'not_found') = (work_item_id IS NULL))
) STRICT, WITHOUT ROWID;

-- Status changes from the Jira changelog.
CREATE TABLE work_item_transitions (
    id            INTEGER PRIMARY KEY,
    work_item_id  INTEGER NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    changelog_id  TEXT NOT NULL,
    at            TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', at) IS at),
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    to_category   TEXT NOT NULL CHECK (to_category IN ('todo','in_progress','done')),
    author        TEXT,
    by_me         INTEGER NOT NULL DEFAULT 0 CHECK (by_me IN (0,1)),
    UNIQUE (work_item_id, changelog_id)
) STRICT;

-- Your calendar, from Odin's meeting sync. Loki's meetings table holds recaps.
CREATE TABLE calendar_events (
    id             INTEGER PRIMARY KEY,
    source_id      INTEGER NOT NULL REFERENCES sources(id),
    external_id    TEXT NOT NULL,
    title          TEXT NOT NULL,
    starts_at      TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', starts_at) IS starts_at),
    ends_at        TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', ends_at) IS ends_at),
    is_all_day     INTEGER NOT NULL DEFAULT 0 CHECK (is_all_day IN (0,1)),
    show_as        TEXT NOT NULL DEFAULT 'busy' CHECK (show_as IN ('busy','tentative','free','oof','unknown')),
    response       TEXT NOT NULL DEFAULT 'none' CHECK (response IN ('organizer','accepted','tentative','declined','none')),
    is_cancelled   INTEGER NOT NULL DEFAULT 0 CHECK (is_cancelled IN (0,1)),
    logged_as_key  TEXT,                           -- Jira key Odin logs this meeting's time against, if any
    first_seen_at  TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', first_seen_at) IS first_seen_at),
    last_seen_at   TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_seen_at) IS last_seen_at),
    deleted_at     TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', deleted_at) IS deleted_at),
    run_id         INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL,
    UNIQUE (source_id, external_id),
    CHECK (ends_at >= starts_at)
) STRICT;

-- Your Jira worklogs, whoever created them. origin says who:
--   jira     found in Jira, made outside Asgard (by hand in the browser)
--   baldur   posted by Odin from an approved Baldur day proposal
--   meeting  posted by Odin for a calendar event
--   manual   typed into Odin
-- Odin writes a 'sending' row, with a marker in the comment, before each
-- Jira call, so a crash can't post the same time twice.
CREATE TABLE worklogs (
    id                 INTEGER PRIMARY KEY,
    work_item_id       INTEGER NOT NULL REFERENCES work_items(id),
    jira_worklog_id    TEXT,
    origin             TEXT NOT NULL CHECK (origin IN ('jira','baldur','meeting','manual')),
    state              TEXT NOT NULL CHECK (state IN ('sending','posted','failed','deleted')),
    started_at         TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', started_at) IS started_at),
    seconds            INTEGER NOT NULL CHECK (seconds > 0),
    comment            TEXT,
    proposal_id        INTEGER REFERENCES day_proposals(id),
    calendar_event_id  INTEGER REFERENCES calendar_events(id),
    posted_at          TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', posted_at) IS posted_at),
    error              TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                       CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    UNIQUE (work_item_id, jira_worklog_id),
    CHECK ((state IN ('posted','deleted')) = (jira_worklog_id IS NOT NULL)),
    CHECK (state <> 'posted' OR posted_at IS NOT NULL),
    CHECK (origin <> 'jira' OR state IN ('posted','deleted')),
    CHECK ((origin = 'baldur') = (proposal_id IS NOT NULL)),
    CHECK ((origin = 'meeting') = (calendar_event_id IS NOT NULL))
) STRICT;

-- =====================================================================
-- Baldur: git, GitHub, time estimates
-- =====================================================================

CREATE TABLE repos (
    id               INTEGER PRIMARY KEY,
    source_id        INTEGER REFERENCES sources(id),
    name             TEXT NOT NULL,
    github_repo      TEXT UNIQUE,                  -- 'owner/name'; set for repos on GitHub
    local_path       TEXT UNIQUE,                  -- NULL for repos you only review on GitHub
    remote_url       TEXT,
    default_branch   TEXT,
    active           INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    last_scanned_at  TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_scanned_at) IS last_scanned_at),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                     CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    CHECK (github_repo IS NOT NULL OR local_path IS NOT NULL)
) STRICT;

-- Commit metadata only: no bodies, no diffs.
CREATE TABLE commits (
    id             INTEGER PRIMARY KEY,
    repo_id        INTEGER NOT NULL REFERENCES repos(id),
    sha            TEXT NOT NULL CHECK (length(sha) IN (40, 64)),
    patch_id       TEXT,                           -- git patch-id: one change counted once across cherry-picks
    author_name    TEXT NOT NULL,
    author_email   TEXT NOT NULL COLLATE NOCASE,
    authored_at    TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', authored_at) IS authored_at),
    committed_at   TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', committed_at) IS committed_at),
    subject        TEXT NOT NULL,
    files_changed  INTEGER NOT NULL DEFAULT 0 CHECK (files_changed >= 0),
    additions      INTEGER NOT NULL DEFAULT 0 CHECK (additions >= 0),
    deletions      INTEGER NOT NULL DEFAULT 0 CHECK (deletions >= 0),
    is_merge       INTEGER NOT NULL DEFAULT 0 CHECK (is_merge IN (0,1)),
    is_mine        INTEGER NOT NULL DEFAULT 0 CHECK (is_mine IN (0,1)),
    branch_hint    TEXT,                           -- the branch it was made on, saved at collection
    first_seen_at  TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', first_seen_at) IS first_seen_at),
    run_id         INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL,
    UNIQUE (repo_id, sha)
) STRICT;

-- Which tickets a commit belongs to, and how Baldur knows.
CREATE TABLE commit_work_items (
    commit_id      INTEGER NOT NULL REFERENCES commits(id) ON DELETE CASCADE,
    work_item_key  TEXT NOT NULL,
    method         TEXT NOT NULL CHECK (method IN ('reflog','branch','pr','message','manual')),
    PRIMARY KEY (commit_id, work_item_key)
) STRICT, WITHOUT ROWID;

CREATE TABLE reflog_entries (
    id       INTEGER PRIMARY KEY,
    repo_id  INTEGER NOT NULL REFERENCES repos(id),
    ref      TEXT NOT NULL DEFAULT 'HEAD',         -- HEAD or the branch whose reflog it came from
    at       TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', at) IS at),
    action   TEXT NOT NULL,
    sha      TEXT NOT NULL,
    message  TEXT,
    UNIQUE (repo_id, ref, at, sha, action)
) STRICT;

CREATE TABLE pull_requests (
    id                INTEGER PRIMARY KEY,
    repo_id           INTEGER NOT NULL REFERENCES repos(id),
    number            INTEGER NOT NULL CHECK (number > 0),
    title             TEXT NOT NULL,
    author            TEXT NOT NULL,
    is_mine           INTEGER NOT NULL DEFAULT 0 CHECK (is_mine IN (0,1)),
    head_ref          TEXT NOT NULL,               -- branch name; GitHub keeps it after the branch is deleted
    work_item_key     TEXT,
    state             TEXT NOT NULL CHECK (state IN ('open','closed','merged')),
    is_draft          INTEGER NOT NULL DEFAULT 0 CHECK (is_draft IN (0,1)),
    review_requested  INTEGER NOT NULL DEFAULT 0 CHECK (review_requested IN (0,1)),   -- your review, now
    notified_at       TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', notified_at) IS notified_at),
    created_at        TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    updated_at        TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
    merged_at         TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', merged_at) IS merged_at),
    closed_at         TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', closed_at) IS closed_at),
    additions         INTEGER CHECK (additions >= 0),
    deletions         INTEGER CHECK (deletions >= 0),
    url               TEXT NOT NULL,
    first_seen_at     TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', first_seen_at) IS first_seen_at),
    last_seen_at      TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_seen_at) IS last_seen_at),
    run_id            INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL,
    UNIQUE (repo_id, number),
    CHECK (state <> 'merged' OR merged_at IS NOT NULL)
) STRICT;

CREATE TABLE pr_reviews (
    id             INTEGER PRIMARY KEY,
    pr_id          INTEGER NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
    github_id      TEXT NOT NULL,
    reviewer       TEXT NOT NULL,
    is_mine        INTEGER NOT NULL DEFAULT 0 CHECK (is_mine IN (0,1)),
    state          TEXT NOT NULL CHECK (state IN ('approved','changes_requested','commented','dismissed')),
    submitted_at   TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', submitted_at) IS submitted_at),
    comment_count  INTEGER NOT NULL DEFAULT 0 CHECK (comment_count >= 0),
    notified_at    TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', notified_at) IS notified_at),   -- reviews of your PRs
    UNIQUE (pr_id, github_id)
) STRICT;

-- Each fit of the estimation settings against your real hours; one is active.
CREATE TABLE calibration_runs (
    id                  INTEGER PRIMARY KEY,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    gap_minutes         INTEGER NOT NULL CHECK (gap_minutes > 0),
    lead_in_minutes     INTEGER NOT NULL CHECK (lead_in_minutes >= 0),
    ambient_weight      REAL NOT NULL CHECK (ambient_weight BETWEEN 0 AND 1),
    days_used           INTEGER NOT NULL CHECK (days_used > 0),
    mean_abs_error_min  REAL NOT NULL CHECK (mean_abs_error_min >= 0),
    bias_min            REAL NOT NULL CHECK (bias_min <= 0),          -- calibration may only bias low
    is_active           INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0,1))
) STRICT;

-- One estimation run: the frozen settings every number in it came from.
CREATE TABLE estimate_runs (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                    CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    date_from       TEXT NOT NULL CHECK (date(date_from) IS date_from),
    date_to         TEXT NOT NULL CHECK (date(date_to) IS date_to),
    model_version   TEXT NOT NULL,                 -- 'baldur-1'
    params          TEXT NOT NULL CHECK (json_valid(params) AND json_type(params) = 'object'),
    params_hash     TEXT NOT NULL,
    calibration_id  INTEGER REFERENCES calibration_runs(id),
    CHECK (date_to >= date_from)
) STRICT;

-- Sessions as one run computed them. Recomputed, never edited.
CREATE TABLE work_sessions (
    id                INTEGER PRIMARY KEY,
    estimate_run_id   INTEGER NOT NULL REFERENCES estimate_runs(id) ON DELETE CASCADE,
    local_date        TEXT NOT NULL CHECK (date(local_date) IS local_date),
    started_at        TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', started_at) IS started_at),
    ended_at          TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', ended_at) IS ended_at),
    start_basis       TEXT NOT NULL CHECK (start_basis IN ('reflog','lead_in')),
    policy            TEXT NOT NULL CHECK (policy IN ('ambient','overlap','independent')),
    focused_minutes   REAL NOT NULL CHECK (focused_minutes >= 0),
    ambient_minutes   REAL NOT NULL CHECK (ambient_minutes >= 0),
    counted_minutes   REAL NOT NULL CHECK (counted_minutes >= 0),   -- after weighting and the day cap
    commit_count      INTEGER NOT NULL CHECK (commit_count > 0),
    CHECK (ended_at >= started_at),
    CHECK (counted_minutes <= focused_minutes + ambient_minutes)
) STRICT;

CREATE TABLE session_commits (
    session_id  INTEGER NOT NULL REFERENCES work_sessions(id) ON DELETE CASCADE,
    commit_id   INTEGER NOT NULL REFERENCES commits(id),
    PRIMARY KEY (session_id, commit_id)
) STRICT, WITHOUT ROWID;

-- A session's counted minutes split across tickets. NULL key = untracked.
CREATE TABLE session_allocations (
    id             INTEGER PRIMARY KEY,
    session_id     INTEGER NOT NULL REFERENCES work_sessions(id) ON DELETE CASCADE,
    work_item_key  TEXT,
    minutes        REAL NOT NULL CHECK (minutes >= 0)
) STRICT;

-- The unit you review: minutes per ticket per local day. NULL key = untracked.
-- A decided row never changes: to change an approved number, Baldur marks
-- it superseded and inserts the new one in the same transaction.
CREATE TABLE day_proposals (
    id                INTEGER PRIMARY KEY,
    estimate_run_id   INTEGER NOT NULL REFERENCES estimate_runs(id) ON DELETE CASCADE,
    local_date        TEXT NOT NULL CHECK (date(local_date) IS local_date),
    work_item_key     TEXT,
    minutes_raw       REAL NOT NULL CHECK (minutes_raw >= 0),
    minutes_proposed  INTEGER NOT NULL CHECK (minutes_proposed >= 0),
    minutes_final     INTEGER CHECK (minutes_final >= 0),
    first_started_at  TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', first_started_at) IS first_started_at),
    basis             TEXT NOT NULL,
    basis_hash        TEXT NOT NULL,
    review            TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(review) AND json_type(review) = 'object'),
    status            TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','approved','rejected','superseded')),
    decided_at        TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', decided_at) IS decided_at),
    CHECK (minutes_proposed <= minutes_raw),                                   -- rounding only goes down
    CHECK (status <> 'approved' OR minutes_final IS NOT NULL),
    CHECK (status NOT IN ('approved','rejected') OR decided_at IS NOT NULL),
    CHECK (work_item_key IS NOT NULL OR status IN ('proposed','superseded'))   -- untracked is never approved
) STRICT;

CREATE TRIGGER day_proposals_decided_are_final BEFORE UPDATE ON day_proposals
WHEN old.status <> 'proposed'
 AND NOT (old.status = 'approved' AND new.status = 'superseded'
          AND new.minutes_final IS old.minutes_final AND new.decided_at IS old.decided_at
          AND new.work_item_key IS old.work_item_key AND new.local_date IS old.local_date)
BEGIN
    SELECT RAISE(ABORT, 'decided proposals are final: supersede and insert a new one');
END;

CREATE TRIGGER day_proposals_decisions_are_kept BEFORE DELETE ON day_proposals
WHEN old.decided_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'decided proposals are kept for audit; prune only undecided ones');
END;

-- Real hours you noted during a calibration trial.
CREATE TABLE time_actuals (
    id             INTEGER PRIMARY KEY,
    on_date        TEXT NOT NULL CHECK (date(on_date) IS on_date),
    minutes        INTEGER NOT NULL CHECK (minutes BETWEEN 0 AND 1440),
    work_item_key  TEXT,                            -- NULL = the whole day's development total
    note           TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                   CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at)
) STRICT;

-- =====================================================================
-- Loki: meetings and BLUFs
-- =====================================================================

CREATE TABLE meetings (
    id                 INTEGER PRIMARY KEY,
    source_id          INTEGER NOT NULL REFERENCES sources(id),
    external_id        TEXT NOT NULL,
    calendar_event_id  INTEGER REFERENCES calendar_events(id) ON DELETE SET NULL,
    title              TEXT NOT NULL,
    starts_at          TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', starts_at) IS starts_at),
    ends_at            TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', ends_at) IS ends_at),
    organizer          TEXT,
    attendee_count     INTEGER CHECK (attendee_count >= 0),
    recap_origin       TEXT NOT NULL CHECK (recap_origin IN ('graph_insights','graph_transcript','paste','file')),
    source_link        TEXT,
    notes_summary      TEXT,
    first_seen_at      TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', first_seen_at) IS first_seen_at),
    run_id             INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL,
    UNIQUE (source_id, external_id),
    CHECK (ends_at IS NULL OR ends_at >= starts_at)
) STRICT;

CREATE TABLE action_items (
    id             INTEGER PRIMARY KEY,
    meeting_id     INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    text           TEXT NOT NULL,
    owner          TEXT,
    is_mine        INTEGER NOT NULL DEFAULT 0 CHECK (is_mine IN (0,1)),
    due_on         TEXT CHECK (date(due_on) IS due_on),
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','dropped')),
    work_item_key  TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                   CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at)
) STRICT;

CREATE TABLE blufs (
    id              INTEGER PRIMARY KEY,
    subject_type    TEXT NOT NULL CHECK (subject_type IN ('meeting','commit_range','pull_request','work_item','adhoc')),
    subject_ref     TEXT NOT NULL,
    bottom_line     TEXT NOT NULL,
    body_md         TEXT NOT NULL,
    sections        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(sections) AND json_type(sections) = 'object'),
    tier            TEXT NOT NULL CHECK (tier IN ('api','mcp','clipboard','manual')),
    model           TEXT,
    prompt_version  TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','approved','posted','rejected')),
    sent_via        TEXT CHECK (sent_via IN ('clipboard','mailto','graph_mail','teams')),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                    CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    approved_at     TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', approved_at) IS approved_at),
    posted_at       TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', posted_at) IS posted_at),
    CHECK (state <> 'approved' OR approved_at IS NOT NULL),
    CHECK (state <> 'posted'   OR (posted_at IS NOT NULL AND sent_via IS NOT NULL))
) STRICT;

-- =====================================================================
-- Freya: accomplishments, reviews, citations
-- =====================================================================

-- A durable record of each issue you finished, written when Odin reports
-- work_item.done. Stats refresh until the review period is frozen; your
-- impact note is the context no data source has.
CREATE TABLE accomplishments (
    id                  INTEGER PRIMARY KEY,
    jira_id             TEXT NOT NULL UNIQUE,
    work_item_key       TEXT NOT NULL,              -- key at the last refresh
    summary             TEXT NOT NULL,
    issue_type          TEXT NOT NULL,
    epic_key            TEXT,
    url                 TEXT NOT NULL,
    state               TEXT NOT NULL CHECK (state IN ('done','reopened')),
    resolution          TEXT,
    first_done_at       TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', first_done_at) IS first_done_at),
    last_done_at        TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_done_at) IS last_done_at),
    reopened_count      INTEGER NOT NULL DEFAULT 0 CHECK (reopened_count >= 0),
    story_points        REAL CHECK (story_points >= 0),
    approved_minutes    INTEGER NOT NULL DEFAULT 0 CHECK (approved_minutes >= 0),
    commit_count        INTEGER NOT NULL DEFAULT 0 CHECK (commit_count >= 0),
    merged_pr_count     INTEGER NOT NULL DEFAULT 0 CHECK (merged_pr_count >= 0),
    first_activity_at   TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', first_activity_at) IS first_activity_at),
    last_activity_at    TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_activity_at) IS last_activity_at),
    impact_note         TEXT,
    highlight           INTEGER NOT NULL DEFAULT 0 CHECK (highlight IN (0,1)),
    stats_refreshed_at  TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', stats_refreshed_at) IS stats_refreshed_at),
    frozen_at           TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', frozen_at) IS frozen_at),
    CHECK (last_done_at >= first_done_at)
) STRICT;

CREATE TABLE review_periods (
    id         INTEGER PRIMARY KEY,
    label      TEXT NOT NULL UNIQUE,
    starts_on  TEXT NOT NULL CHECK (date(starts_on) IS starts_on),
    ends_on    TEXT NOT NULL CHECK (date(ends_on) IS ends_on),
    rubric     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(rubric) AND json_type(rubric) = 'array'),
    closed_at  TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', closed_at) IS closed_at),
    CHECK (ends_on >= starts_on)
) STRICT;

CREATE TABLE review_drafts (
    id              INTEGER PRIMARY KEY,
    period_id       INTEGER NOT NULL REFERENCES review_periods(id),
    element         TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    text            TEXT NOT NULL,
    tier            TEXT NOT NULL CHECK (tier IN ('api','mcp','clipboard','manual')),
    model           TEXT,
    prompt_version  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','final','discarded')),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                    CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    UNIQUE (period_id, element, version)
) STRICT;

CREATE TABLE citations (
    id             INTEGER PRIMARY KEY,
    owner_type     TEXT NOT NULL CHECK (owner_type IN ('bluf','review_draft')),
    owner_id       INTEGER NOT NULL,
    evidence_type  TEXT NOT NULL CHECK (evidence_type IN ('accomplishment','work_item','commit','pull_request','meeting','bluf')),
    evidence_ref   TEXT NOT NULL,
    evidence_id    INTEGER,
    verified       INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0,1)),
    UNIQUE (owner_type, owner_id, evidence_type, evidence_ref),
    CHECK (verified = 0 OR evidence_id IS NOT NULL)
) STRICT;

-- =====================================================================
-- Heimdall, Bifrost: submissions
-- =====================================================================

CREATE TABLE submissions (
    id                  INTEGER PRIMARY KEY,
    system              TEXT NOT NULL CHECK (system IN ('secchm','bears')),
    title               TEXT NOT NULL,
    state               TEXT NOT NULL DEFAULT 'draft'
                        CHECK (state IN ('draft','approved','submitted','in_review','accepted','returned','withdrawn')),
    external_id         TEXT,
    external_url        TEXT,
    fields              TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(fields) AND json_type(fields) = 'object'),
    artifact_path       TEXT,
    attachment_id       TEXT,
    attachment_version  INTEGER CHECK (attachment_version >= 1),
    work_item_key       TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    approved_at         TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', approved_at) IS approved_at),
    submitted_at        TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', submitted_at) IS submitted_at),
    last_polled_at      TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_polled_at) IS last_polled_at),
    next_poll_at        TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', next_poll_at) IS next_poll_at),
    last_status_raw     TEXT,
    UNIQUE (system, external_id),
    CHECK (state = 'draft' OR approved_at IS NOT NULL),
    CHECK (state IN ('draft','approved','withdrawn') OR submitted_at IS NOT NULL)
) STRICT;

CREATE TABLE submission_status_history (
    id             INTEGER PRIMARY KEY,
    submission_id  INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                   CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', at) IS at),
    from_state     TEXT,
    to_state       TEXT NOT NULL,
    raw_status     TEXT
) STRICT;

CREATE TRIGGER submissions_state_history AFTER UPDATE OF state ON submissions
WHEN old.state IS NOT new.state
BEGIN
    INSERT INTO submission_status_history (submission_id, from_state, to_state, raw_status)
    VALUES (new.id, old.state, new.state, new.last_status_raw);
END;

CREATE TRIGGER submissions_first_history AFTER INSERT ON submissions
BEGIN
    INSERT INTO submission_status_history (submission_id, from_state, to_state, raw_status)
    VALUES (new.id, NULL, new.state, new.last_status_raw);
END;

-- =====================================================================
-- Search: one FTS5 index across entity types
--   rowid = entity_id * 16 + type code; entity_id = rowid >> 4, code = rowid & 15
--   1 work_items  2 commits  3 pull_requests  4 meetings
--   5 action_items  6 blufs  7 submissions  8 accomplishments
-- =====================================================================

CREATE VIRTUAL TABLE search USING fts5(
    title,
    body,
    kind UNINDEXED,
    tokenize = 'porter unicode61'
);

CREATE TRIGGER work_items_search_ins AFTER INSERT ON work_items BEGIN
    INSERT INTO search (rowid, title, body, kind)
    VALUES (new.id * 16 + 1, new.key || ' ' || new.summary,
            coalesce(new.epic_key, '') || ' ' || new.issue_type || ' ' || new.labels || ' ' || new.components, 'work_items');
END;
CREATE TRIGGER work_items_search_upd AFTER UPDATE OF key, summary, epic_key, issue_type, labels, components ON work_items BEGIN
    UPDATE search SET title = new.key || ' ' || new.summary,
                      body = coalesce(new.epic_key, '') || ' ' || new.issue_type || ' ' || new.labels || ' ' || new.components
    WHERE rowid = new.id * 16 + 1;
END;
CREATE TRIGGER work_items_search_del AFTER DELETE ON work_items BEGIN
    DELETE FROM search WHERE rowid = old.id * 16 + 1;
END;

CREATE TRIGGER commits_search_ins AFTER INSERT ON commits BEGIN
    INSERT INTO search (rowid, title, body, kind)
    VALUES (new.id * 16 + 2, new.subject, substr(new.sha, 1, 12) || ' ' || coalesce(new.branch_hint, ''), 'commits');
END;
CREATE TRIGGER commits_search_upd AFTER UPDATE OF subject, branch_hint ON commits BEGIN
    UPDATE search SET title = new.subject, body = substr(new.sha, 1, 12) || ' ' || coalesce(new.branch_hint, '')
    WHERE rowid = new.id * 16 + 2;
END;
CREATE TRIGGER commits_search_del AFTER DELETE ON commits BEGIN
    DELETE FROM search WHERE rowid = old.id * 16 + 2;
END;

CREATE TRIGGER pull_requests_search_ins AFTER INSERT ON pull_requests BEGIN
    INSERT INTO search (rowid, title, body, kind)
    VALUES (new.id * 16 + 3, new.title, '#' || new.number || ' ' || new.author || ' ' || new.head_ref, 'pull_requests');
END;
CREATE TRIGGER pull_requests_search_upd AFTER UPDATE OF title, head_ref ON pull_requests BEGIN
    UPDATE search SET title = new.title, body = '#' || new.number || ' ' || new.author || ' ' || new.head_ref
    WHERE rowid = new.id * 16 + 3;
END;
CREATE TRIGGER pull_requests_search_del AFTER DELETE ON pull_requests BEGIN
    DELETE FROM search WHERE rowid = old.id * 16 + 3;
END;

CREATE TRIGGER meetings_search_ins AFTER INSERT ON meetings BEGIN
    INSERT INTO search (rowid, title, body, kind)
    VALUES (new.id * 16 + 4, new.title, coalesce(new.notes_summary, ''), 'meetings');
END;
CREATE TRIGGER meetings_search_upd AFTER UPDATE OF title, notes_summary ON meetings BEGIN
    UPDATE search SET title = new.title, body = coalesce(new.notes_summary, '')
    WHERE rowid = new.id * 16 + 4;
END;
CREATE TRIGGER meetings_search_del AFTER DELETE ON meetings BEGIN
    DELETE FROM search WHERE rowid = old.id * 16 + 4;
END;

CREATE TRIGGER action_items_search_ins AFTER INSERT ON action_items BEGIN
    INSERT INTO search (rowid, title, body, kind)
    VALUES (new.id * 16 + 5, new.text, coalesce(new.owner, '') || ' ' || coalesce(new.work_item_key, ''), 'action_items');
END;
CREATE TRIGGER action_items_search_upd AFTER UPDATE OF text, owner, work_item_key ON action_items BEGIN
    UPDATE search SET title = new.text, body = coalesce(new.owner, '') || ' ' || coalesce(new.work_item_key, '')
    WHERE rowid = new.id * 16 + 5;
END;
CREATE TRIGGER action_items_search_del AFTER DELETE ON action_items BEGIN
    DELETE FROM search WHERE rowid = old.id * 16 + 5;
END;

CREATE TRIGGER blufs_search_ins AFTER INSERT ON blufs BEGIN
    INSERT INTO search (rowid, title, body, kind)
    VALUES (new.id * 16 + 6, new.bottom_line, new.body_md, 'blufs');
END;
CREATE TRIGGER blufs_search_upd AFTER UPDATE OF bottom_line, body_md ON blufs BEGIN
    UPDATE search SET title = new.bottom_line, body = new.body_md WHERE rowid = new.id * 16 + 6;
END;
CREATE TRIGGER blufs_search_del AFTER DELETE ON blufs BEGIN
    DELETE FROM search WHERE rowid = old.id * 16 + 6;
END;

CREATE TRIGGER submissions_search_ins AFTER INSERT ON submissions BEGIN
    INSERT INTO search (rowid, title, body, kind)
    VALUES (new.id * 16 + 7, new.title, new.system || ' ' || coalesce(new.external_id, '') || ' ' || coalesce(new.work_item_key, ''), 'submissions');
END;
CREATE TRIGGER submissions_search_upd AFTER UPDATE OF title, external_id, work_item_key ON submissions BEGIN
    UPDATE search SET title = new.title,
                      body = new.system || ' ' || coalesce(new.external_id, '') || ' ' || coalesce(new.work_item_key, '')
    WHERE rowid = new.id * 16 + 7;
END;
CREATE TRIGGER submissions_search_del AFTER DELETE ON submissions BEGIN
    DELETE FROM search WHERE rowid = old.id * 16 + 7;
END;

CREATE TRIGGER accomplishments_search_ins AFTER INSERT ON accomplishments BEGIN
    INSERT INTO search (rowid, title, body, kind)
    VALUES (new.id * 16 + 8, new.work_item_key || ' ' || new.summary,
            coalesce(new.epic_key, '') || ' ' || coalesce(new.impact_note, ''), 'accomplishments');
END;
CREATE TRIGGER accomplishments_search_upd AFTER UPDATE OF work_item_key, summary, epic_key, impact_note ON accomplishments BEGIN
    UPDATE search SET title = new.work_item_key || ' ' || new.summary,
                      body = coalesce(new.epic_key, '') || ' ' || coalesce(new.impact_note, '')
    WHERE rowid = new.id * 16 + 8;
END;
CREATE TRIGGER accomplishments_search_del AFTER DELETE ON accomplishments BEGIN
    DELETE FROM search WHERE rowid = old.id * 16 + 8;
END;

-- =====================================================================
-- Views
-- =====================================================================

-- Calendar events that count as meetings for Baldur. Tentative counts: it lowers estimates.
CREATE VIEW v_busy_meetings AS
    SELECT * FROM calendar_events
     WHERE deleted_at IS NULL AND is_all_day = 0 AND is_cancelled = 0
       AND response <> 'declined' AND show_as IN ('busy', 'tentative');

-- Keys other apps use that Odin hasn't resolved yet, or couldn't find a week ago.
CREATE VIEW v_unknown_keys AS
    SELECT DISTINCT k.key
      FROM (SELECT work_item_key AS key FROM commit_work_items
            UNION SELECT work_item_key FROM pull_requests WHERE work_item_key IS NOT NULL
            UNION SELECT work_item_key FROM day_proposals WHERE work_item_key IS NOT NULL
            UNION SELECT work_item_key FROM action_items WHERE work_item_key IS NOT NULL
            UNION SELECT work_item_key FROM submissions WHERE work_item_key IS NOT NULL) AS k
      LEFT JOIN work_item_aliases a ON a.key = k.key
     WHERE a.key IS NULL
        OR (a.status = 'not_found' AND a.checked_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days'));

-- Everything you did, in time order: Baldur's input for session building.
CREATE VIEW v_activity AS
    SELECT c.authored_at AS at, 'commit' AS kind, c.repo_id, c.id AS ref_id, c.subject AS label
      FROM commits c WHERE c.is_mine = 1
    UNION ALL
    SELECT r.at, 'reflog', r.repo_id, r.id, r.action
      FROM reflog_entries r
    UNION ALL
    SELECT v.submitted_at, 'pr_review', p.repo_id, v.id, p.title
      FROM pr_reviews v JOIN pull_requests p ON p.id = v.pr_id WHERE v.is_mine = 1
    UNION ALL
    SELECT t.at, 'transition', NULL, t.id, w.key || ' -> ' || t.to_status
      FROM work_item_transitions t JOIN work_items w ON w.id = t.work_item_id WHERE t.by_me = 1;

-- Per local day and Jira issue: the minutes you approved in Baldur and
-- the development time Jira already holds for you that day. Meeting
-- worklogs are left out (Baldur's estimate excludes meeting time), and so
-- are failed and deleted ones. Keys resolve through work_item_aliases, so
-- an old and a new key for one issue count as one; the newest approval wins.
-- day_start and day_end are local midnight in UTC, so the worklog index is used.
CREATE VIEW v_day_status AS
    WITH approved AS (
        SELECT p.id, p.local_date, p.work_item_key, al.work_item_id, p.minutes_final,
               p.first_started_at, p.basis, p.basis_hash, p.decided_at,
               strftime('%Y-%m-%dT%H:%M:%SZ', p.local_date, 'utc') AS day_start,
               strftime('%Y-%m-%dT%H:%M:%SZ', p.local_date, '+1 day', 'utc') AS day_end,
               row_number() OVER (PARTITION BY p.local_date, coalesce(al.work_item_id, p.work_item_key)
                                  ORDER BY p.decided_at DESC, p.id DESC) AS rn
          FROM day_proposals p
          LEFT JOIN work_item_aliases al ON al.key = p.work_item_key AND al.status <> 'not_found'
         WHERE p.status = 'approved'
    )
    SELECT a.local_date, a.work_item_key, a.work_item_id, a.id AS proposal_id,
           a.minutes_final AS approved_minutes, a.first_started_at, a.basis, a.basis_hash, a.decided_at,
           a.day_start, a.day_end,
           (coalesce((SELECT sum(w.seconds) FROM worklogs w
                       WHERE w.work_item_id = a.work_item_id
                         AND w.state IN ('sending', 'posted')
                         AND w.origin <> 'meeting'
                         AND w.started_at >= a.day_start
                         AND w.started_at <  a.day_end), 0)
            + 59) / 60 AS logged_minutes
      FROM approved a
     WHERE a.rn = 1;

-- What Odin should post: the shortfall for each approval, at most once per
-- approval. A newer approval is new consent; a worklog you deleted in Jira
-- is not re-posted for the approval it came from. While any post for that
-- issue and day is still in doubt ('sending'), nothing more is offered, so a
-- post that later turns out to have failed can't leave approved time behind.
CREATE VIEW v_worklogs_to_post AS
    SELECT d.*, d.approved_minutes - d.logged_minutes AS minutes_to_post
      FROM v_day_status d
      JOIN work_items wi ON wi.id = d.work_item_id AND wi.deleted_at IS NULL
     WHERE d.approved_minutes > d.logged_minutes
       AND NOT EXISTS (SELECT 1 FROM worklogs w
                        WHERE w.proposal_id = d.proposal_id
                          AND w.state IN ('sending', 'posted', 'deleted'))
       AND NOT EXISTS (SELECT 1 FROM worklogs s
                        WHERE s.work_item_id = d.work_item_id
                          AND s.state = 'sending'
                          AND s.origin <> 'meeting'
                          AND s.started_at >= d.day_start
                          AND s.started_at <  d.day_end);

-- Approved time Odin can't post because Jira no longer has the issue, or
-- never had the key. Baldur shows these so you can re-key or reject the day.
CREATE VIEW v_unpostable_days AS
    SELECT d.local_date, d.work_item_key, d.proposal_id, d.approved_minutes, d.logged_minutes,
           CASE WHEN d.work_item_id IS NULL THEN 'not found in Jira' ELSE 'deleted in Jira' END AS reason
      FROM v_day_status d
      LEFT JOIN work_items wi ON wi.id = d.work_item_id
     WHERE d.approved_minutes > d.logged_minutes
       AND ((d.work_item_id IS NOT NULL AND wi.deleted_at IS NOT NULL)
         OR (d.work_item_id IS NULL AND EXISTS (SELECT 1 FROM work_item_aliases a
                                                 WHERE a.key = d.work_item_key AND a.status = 'not_found')));

-- Approved minutes per ticket per month: Freya's "where the hours went".
CREATE VIEW v_time_by_item_month AS
    SELECT substr(local_date, 1, 7) AS month, work_item_key, sum(approved_minutes) AS minutes
      FROM v_day_status
     GROUP BY 1, 2;

-- Everything Freya may cite for a review period.
CREATE VIEW v_review_evidence AS
    SELECT p.id AS period_id, 'accomplishment' AS evidence_type, a.work_item_key AS evidence_ref,
           a.summary AS label, a.last_done_at AS at
      FROM review_periods p
      JOIN accomplishments a ON a.state = 'done'
                            AND a.last_done_at BETWEEN p.starts_on AND p.ends_on || 'T23:59:59Z'
    UNION ALL
    SELECT p.id, 'pull_request', r.name || '#' || pr.number, pr.title, pr.merged_at
      FROM review_periods p
      JOIN pull_requests pr ON pr.is_mine = 1
                           AND pr.merged_at BETWEEN p.starts_on AND p.ends_on || 'T23:59:59Z'
      JOIN repos r ON r.id = pr.repo_id
    UNION ALL
    SELECT p.id, 'bluf', b.subject_ref, b.bottom_line, b.posted_at
      FROM review_periods p
      JOIN blufs b ON b.state = 'posted'
                  AND b.posted_at BETWEEN p.starts_on AND p.ends_on || 'T23:59:59Z';

CREATE VIEW v_unverified_citations AS
    SELECT * FROM citations WHERE verified = 0;

-- Counts the Asgard launcher shows on tiles; rows with nothing to show are left out.
CREATE VIEW v_tile_badges AS
    SELECT * FROM (
        SELECT 'baldur' AS app, 1 AS priority, count(*) AS n, 'reviews requested' AS label
          FROM pull_requests WHERE state = 'open' AND review_requested = 1
        UNION ALL
        SELECT 'baldur', 2, count(DISTINCT local_date), 'days to review'
          FROM day_proposals WHERE status = 'proposed' AND work_item_key IS NOT NULL
        UNION ALL
        SELECT 'baldur', 3, count(*), 'days that can''t be posted' FROM v_unpostable_days
        UNION ALL
        SELECT 'odin', 1, count(*), 'worklogs to post' FROM v_worklogs_to_post
        UNION ALL
        SELECT 'odin', 2, count(*), 'keys to look up' FROM v_unknown_keys
        UNION ALL
        SELECT 'odin', 3, count(*), 'posts to check'
          FROM worklogs WHERE state = 'sending' AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-10 minutes')
        UNION ALL
        SELECT 'freya', 1, count(*), 'citations to check' FROM citations WHERE verified = 0
        UNION ALL
        SELECT 'freya', 2, count(*), 'wins without a note'
          FROM accomplishments WHERE state = 'done' AND impact_note IS NULL AND frozen_at IS NULL
    ) WHERE n > 0;

-- =====================================================================
-- Indexes (UNIQUE constraints and primary keys already index their columns)
-- =====================================================================

CREATE INDEX ix_events_at                ON events (at);
CREATE INDEX ix_events_entity            ON events (entity_type, entity_id);
CREATE INDEX ix_events_kind              ON events (kind);         -- + rowid: consumers read past their cursor
CREATE INDEX ix_sync_runs_app_started    ON sync_runs (app, started_at);

CREATE INDEX ix_work_items_mine_resolved ON work_items (resolved_at) WHERE is_mine = 1;
CREATE INDEX ix_work_items_open_mine     ON work_items (status_category, updated_at) WHERE is_mine = 1 AND deleted_at IS NULL;
CREATE INDEX ix_work_items_updated       ON work_items (source_id, updated_at);
CREATE INDEX ix_work_items_epic          ON work_items (epic_key) WHERE epic_key IS NOT NULL;
CREATE INDEX ix_work_items_parent        ON work_items (parent_key) WHERE parent_key IS NOT NULL;
CREATE INDEX ix_work_items_last_seen     ON work_items (source_id, last_seen_at) WHERE deleted_at IS NULL;
CREATE INDEX ix_aliases_item             ON work_item_aliases (work_item_id);
CREATE UNIQUE INDEX ux_aliases_current     ON work_item_aliases (work_item_id) WHERE status = 'current';
CREATE INDEX ix_transitions_mine_at      ON work_item_transitions (at) WHERE by_me = 1;
CREATE INDEX ix_calendar_events_starts   ON calendar_events (starts_at) WHERE deleted_at IS NULL;
CREATE INDEX ix_worklogs_item_started    ON worklogs (work_item_id, started_at);
CREATE INDEX ix_worklogs_sending         ON worklogs (created_at) WHERE state = 'sending';
CREATE INDEX ix_worklogs_proposal        ON worklogs (proposal_id) WHERE proposal_id IS NOT NULL;
CREATE INDEX ix_worklogs_calendar_event  ON worklogs (calendar_event_id) WHERE calendar_event_id IS NOT NULL;

CREATE INDEX ix_commits_mine_authored    ON commits (authored_at) WHERE is_mine = 1;
CREATE INDEX ix_commits_repo_authored    ON commits (repo_id, authored_at);
CREATE INDEX ix_commits_patch_id         ON commits (patch_id) WHERE patch_id IS NOT NULL;
CREATE INDEX ix_commit_items_key         ON commit_work_items (work_item_key);
CREATE INDEX ix_reflog_at                ON reflog_entries (at);
CREATE INDEX ix_prs_mine_merged          ON pull_requests (merged_at) WHERE is_mine = 1;
CREATE INDEX ix_prs_review_requested     ON pull_requests (updated_at) WHERE state = 'open' AND review_requested = 1;
CREATE INDEX ix_prs_key                  ON pull_requests (work_item_key) WHERE work_item_key IS NOT NULL;
CREATE INDEX ix_pr_reviews_mine_at       ON pr_reviews (submitted_at) WHERE is_mine = 1;
CREATE INDEX ix_pr_reviews_unnotified    ON pr_reviews (pr_id) WHERE notified_at IS NULL AND is_mine = 0;
CREATE UNIQUE INDEX ux_calibration_active ON calibration_runs (is_active) WHERE is_active = 1;
CREATE INDEX ix_sessions_run_day         ON work_sessions (estimate_run_id, local_date);
CREATE UNIQUE INDEX ux_allocations       ON session_allocations (session_id, coalesce(work_item_key, ''));
CREATE INDEX ix_allocations_key          ON session_allocations (work_item_key);
CREATE INDEX ix_day_proposals_run        ON day_proposals (estimate_run_id);
CREATE UNIQUE INDEX ux_day_proposals_open ON day_proposals (local_date, coalesce(work_item_key, '')) WHERE status = 'proposed';
CREATE UNIQUE INDEX ux_day_proposals_approved ON day_proposals (local_date, work_item_key) WHERE status = 'approved';
CREATE UNIQUE INDEX ux_time_actuals_day_item ON time_actuals (on_date, coalesce(work_item_key, ''));

CREATE INDEX ix_meetings_starts          ON meetings (starts_at);
CREATE INDEX ix_meetings_calendar_event  ON meetings (calendar_event_id) WHERE calendar_event_id IS NOT NULL;
CREATE INDEX ix_action_items_meeting     ON action_items (meeting_id);
CREATE INDEX ix_action_items_open        ON action_items (due_on) WHERE status = 'open';
CREATE INDEX ix_blufs_subject            ON blufs (subject_type, subject_ref);
CREATE INDEX ix_blufs_posted             ON blufs (posted_at) WHERE state = 'posted';

CREATE INDEX ix_accomplishments_done     ON accomplishments (last_done_at) WHERE state = 'done';
CREATE INDEX ix_accomplishments_key      ON accomplishments (work_item_key);
CREATE INDEX ix_citations_evidence       ON citations (evidence_type, evidence_ref);
CREATE INDEX ix_citations_unverified     ON citations (owner_type, owner_id) WHERE verified = 0;
CREATE INDEX ix_submissions_poll         ON submissions (next_poll_at) WHERE state IN ('submitted','in_review');
CREATE INDEX ix_submission_history       ON submission_status_history (submission_id, at);

PRAGMA user_version = 1;

COMMIT;
