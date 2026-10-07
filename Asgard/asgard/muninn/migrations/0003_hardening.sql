-- =====================================================================
-- Muninn schema v3  (PRAGMA user_version = 3), Asgard 0.4.0
--
-- Hardening. Rules that held only in Python now hold in the database, so
-- an app's bug (or someone with plain sqlite3) can't break them:
--
--   1. Jira keys. Every column where one app writes a key another app
--      resolves takes only a key as Muninn stores it: PROJ-123, capitals.
--      A key in another case never resolves, so its approved time could
--      never be posted. Triggers, not CHECKs: changing a CHECK needs a
--      table rebuild, and rows written before v3 stay readable (run
--      `Asgard.pyw --muninn check` to list any).
--   2. Size limits. An approval is at most 1440 minutes, and so are all
--      of a day's approvals together; a worklog Asgard creates is at most
--      24 hours. Worklogs read from Jira are stored as Jira has them.
--   3. Posted time. A posted worklog can only become deleted, never go
--      back to sending or failed. A worklog Asgard made is never deleted
--      unless it failed: its row is the memory that stops a second post.
--   4. Consent. Baldur time is posted only for an approved day.
--   5. Events. A cursor can't pass the newest event (it would skip the
--      next ones).
--   6. Search. The pull request and submission entries now follow every
--      column they show (author, number, system), and existing entries
--      are brought up to date.
--   7. A tile badge for time posted twice (v_double_posts), including the
--      same meeting synced from two calendars.
--   8. Pull request commits and merge commits for Baldur (additive).
--   9. Indexes on run_id, so pruning old sync runs doesn't scan every
--      fact table once per run (9 s for 500 runs with 100,000 commits,
--      0.01 s with the index).
-- =====================================================================
BEGIN;

-- ---------------------------------------------------------------------
-- 1. Jira keys: capital letter, then capitals, digits or _, a hyphen, a
--    number with no leading zero (asgard/muninn/keys.py has the same rule).
-- ---------------------------------------------------------------------

CREATE TRIGGER commit_work_items_key_ins BEFORE INSERT ON commit_work_items
WHEN new.work_item_key IS NOT NULL AND NOT (new.work_item_key GLOB '[A-Z]*-[1-9]*' AND new.work_item_key NOT GLOB '*[^A-Z0-9_-]*' AND new.work_item_key NOT GLOB '*-*-*' AND substr(new.work_item_key, instr(new.work_item_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'commit_work_items.work_item_key must be a Jira key like PROJ-123, in capitals');
END;
CREATE TRIGGER commit_work_items_key_upd BEFORE UPDATE OF work_item_key ON commit_work_items
WHEN new.work_item_key IS NOT NULL AND NOT (new.work_item_key GLOB '[A-Z]*-[1-9]*' AND new.work_item_key NOT GLOB '*[^A-Z0-9_-]*' AND new.work_item_key NOT GLOB '*-*-*' AND substr(new.work_item_key, instr(new.work_item_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'commit_work_items.work_item_key must be a Jira key like PROJ-123, in capitals');
END;

CREATE TRIGGER pull_requests_key_ins BEFORE INSERT ON pull_requests
WHEN new.work_item_key IS NOT NULL AND NOT (new.work_item_key GLOB '[A-Z]*-[1-9]*' AND new.work_item_key NOT GLOB '*[^A-Z0-9_-]*' AND new.work_item_key NOT GLOB '*-*-*' AND substr(new.work_item_key, instr(new.work_item_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'pull_requests.work_item_key must be a Jira key like PROJ-123, in capitals');
END;
CREATE TRIGGER pull_requests_key_upd BEFORE UPDATE OF work_item_key ON pull_requests
WHEN new.work_item_key IS NOT NULL AND NOT (new.work_item_key GLOB '[A-Z]*-[1-9]*' AND new.work_item_key NOT GLOB '*[^A-Z0-9_-]*' AND new.work_item_key NOT GLOB '*-*-*' AND substr(new.work_item_key, instr(new.work_item_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'pull_requests.work_item_key must be a Jira key like PROJ-123, in capitals');
END;

CREATE TRIGGER day_proposals_key_ins BEFORE INSERT ON day_proposals
WHEN new.work_item_key IS NOT NULL AND NOT (new.work_item_key GLOB '[A-Z]*-[1-9]*' AND new.work_item_key NOT GLOB '*[^A-Z0-9_-]*' AND new.work_item_key NOT GLOB '*-*-*' AND substr(new.work_item_key, instr(new.work_item_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'day_proposals.work_item_key must be a Jira key like PROJ-123, in capitals');
END;
CREATE TRIGGER day_proposals_key_upd BEFORE UPDATE OF work_item_key ON day_proposals
WHEN new.work_item_key IS NOT NULL AND NOT (new.work_item_key GLOB '[A-Z]*-[1-9]*' AND new.work_item_key NOT GLOB '*[^A-Z0-9_-]*' AND new.work_item_key NOT GLOB '*-*-*' AND substr(new.work_item_key, instr(new.work_item_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'day_proposals.work_item_key must be a Jira key like PROJ-123, in capitals');
END;

CREATE TRIGGER session_allocations_key_ins BEFORE INSERT ON session_allocations
WHEN new.work_item_key IS NOT NULL AND NOT (new.work_item_key GLOB '[A-Z]*-[1-9]*' AND new.work_item_key NOT GLOB '*[^A-Z0-9_-]*' AND new.work_item_key NOT GLOB '*-*-*' AND substr(new.work_item_key, instr(new.work_item_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'session_allocations.work_item_key must be a Jira key like PROJ-123, in capitals');
END;
CREATE TRIGGER session_allocations_key_upd BEFORE UPDATE OF work_item_key ON session_allocations
WHEN new.work_item_key IS NOT NULL AND NOT (new.work_item_key GLOB '[A-Z]*-[1-9]*' AND new.work_item_key NOT GLOB '*[^A-Z0-9_-]*' AND new.work_item_key NOT GLOB '*-*-*' AND substr(new.work_item_key, instr(new.work_item_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'session_allocations.work_item_key must be a Jira key like PROJ-123, in capitals');
END;

CREATE TRIGGER time_actuals_key_ins BEFORE INSERT ON time_actuals
WHEN new.work_item_key IS NOT NULL AND NOT (new.work_item_key GLOB '[A-Z]*-[1-9]*' AND new.work_item_key NOT GLOB '*[^A-Z0-9_-]*' AND new.work_item_key NOT GLOB '*-*-*' AND substr(new.work_item_key, instr(new.work_item_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'time_actuals.work_item_key must be a Jira key like PROJ-123, in capitals');
END;
CREATE TRIGGER time_actuals_key_upd BEFORE UPDATE OF work_item_key ON time_actuals
WHEN new.work_item_key IS NOT NULL AND NOT (new.work_item_key GLOB '[A-Z]*-[1-9]*' AND new.work_item_key NOT GLOB '*[^A-Z0-9_-]*' AND new.work_item_key NOT GLOB '*-*-*' AND substr(new.work_item_key, instr(new.work_item_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'time_actuals.work_item_key must be a Jira key like PROJ-123, in capitals');
END;

CREATE TRIGGER action_items_key_ins BEFORE INSERT ON action_items
WHEN new.work_item_key IS NOT NULL AND NOT (new.work_item_key GLOB '[A-Z]*-[1-9]*' AND new.work_item_key NOT GLOB '*[^A-Z0-9_-]*' AND new.work_item_key NOT GLOB '*-*-*' AND substr(new.work_item_key, instr(new.work_item_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'action_items.work_item_key must be a Jira key like PROJ-123, in capitals');
END;
CREATE TRIGGER action_items_key_upd BEFORE UPDATE OF work_item_key ON action_items
WHEN new.work_item_key IS NOT NULL AND NOT (new.work_item_key GLOB '[A-Z]*-[1-9]*' AND new.work_item_key NOT GLOB '*[^A-Z0-9_-]*' AND new.work_item_key NOT GLOB '*-*-*' AND substr(new.work_item_key, instr(new.work_item_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'action_items.work_item_key must be a Jira key like PROJ-123, in capitals');
END;

CREATE TRIGGER submissions_key_ins BEFORE INSERT ON submissions
WHEN new.work_item_key IS NOT NULL AND NOT (new.work_item_key GLOB '[A-Z]*-[1-9]*' AND new.work_item_key NOT GLOB '*[^A-Z0-9_-]*' AND new.work_item_key NOT GLOB '*-*-*' AND substr(new.work_item_key, instr(new.work_item_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'submissions.work_item_key must be a Jira key like PROJ-123, in capitals');
END;
CREATE TRIGGER submissions_key_upd BEFORE UPDATE OF work_item_key ON submissions
WHEN new.work_item_key IS NOT NULL AND NOT (new.work_item_key GLOB '[A-Z]*-[1-9]*' AND new.work_item_key NOT GLOB '*[^A-Z0-9_-]*' AND new.work_item_key NOT GLOB '*-*-*' AND substr(new.work_item_key, instr(new.work_item_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'submissions.work_item_key must be a Jira key like PROJ-123, in capitals');
END;

CREATE TRIGGER calendar_events_key_ins BEFORE INSERT ON calendar_events
WHEN new.logged_as_key IS NOT NULL AND NOT (new.logged_as_key GLOB '[A-Z]*-[1-9]*' AND new.logged_as_key NOT GLOB '*[^A-Z0-9_-]*' AND new.logged_as_key NOT GLOB '*-*-*' AND substr(new.logged_as_key, instr(new.logged_as_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'calendar_events.logged_as_key must be a Jira key like PROJ-123, in capitals');
END;
CREATE TRIGGER calendar_events_key_upd BEFORE UPDATE OF logged_as_key ON calendar_events
WHEN new.logged_as_key IS NOT NULL AND NOT (new.logged_as_key GLOB '[A-Z]*-[1-9]*' AND new.logged_as_key NOT GLOB '*[^A-Z0-9_-]*' AND new.logged_as_key NOT GLOB '*-*-*' AND substr(new.logged_as_key, instr(new.logged_as_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'calendar_events.logged_as_key must be a Jira key like PROJ-123, in capitals');
END;

-- ---------------------------------------------------------------------
-- 2. Size limits
-- ---------------------------------------------------------------------

CREATE TRIGGER day_proposals_fit_in_a_day_ins BEFORE INSERT ON day_proposals
WHEN new.minutes_final > 1440
BEGIN
    SELECT RAISE(ABORT, 'a day has 1440 minutes; an approval can''t be for more');
END;
CREATE TRIGGER day_proposals_fit_in_a_day_upd BEFORE UPDATE OF minutes_final ON day_proposals
WHEN new.minutes_final > 1440
BEGIN
    SELECT RAISE(ABORT, 'a day has 1440 minutes; an approval can''t be for more');
END;

-- All of a day's approvals together fit in the day too.
CREATE TRIGGER day_proposals_day_total_ins BEFORE INSERT ON day_proposals
WHEN new.status = 'approved'
 AND new.minutes_final + (SELECT coalesce(sum(minutes_final), 0) FROM day_proposals
                           WHERE local_date = new.local_date AND status = 'approved') > 1440
BEGIN
    SELECT RAISE(ABORT, 'a day''s approvals can''t add up to more than 1440 minutes');
END;
CREATE TRIGGER day_proposals_day_total_upd BEFORE UPDATE OF status, minutes_final ON day_proposals
WHEN new.status = 'approved'
 AND new.minutes_final + (SELECT coalesce(sum(minutes_final), 0) FROM day_proposals
                           WHERE local_date = new.local_date AND status = 'approved' AND id <> new.id) > 1440
BEGIN
    SELECT RAISE(ABORT, 'a day''s approvals can''t add up to more than 1440 minutes');
END;

CREATE TRIGGER worklogs_at_most_a_day BEFORE INSERT ON worklogs
WHEN new.origin <> 'jira' AND new.seconds > 86400
BEGIN
    SELECT RAISE(ABORT, 'Asgard posts at most 24 hours in one worklog');
END;

-- ---------------------------------------------------------------------
-- 3. Posted time
-- ---------------------------------------------------------------------

CREATE TRIGGER worklogs_posted_stay_posted BEFORE UPDATE OF state ON worklogs
WHEN old.state = 'posted' AND new.state IN ('sending', 'failed')
BEGIN
    SELECT RAISE(ABORT, 'a posted worklog is in Jira; it can only become deleted');
END;

CREATE TRIGGER worklogs_asgard_rows_are_kept BEFORE DELETE ON worklogs
WHEN old.origin <> 'jira' AND old.state <> 'failed'
BEGIN
    SELECT RAISE(ABORT, 'worklogs Asgard sent are kept: the row is what stops the time being posted twice');
END;

-- ---------------------------------------------------------------------
-- 4. Consent
-- ---------------------------------------------------------------------

CREATE TRIGGER worklogs_need_an_approval BEFORE INSERT ON worklogs
WHEN new.proposal_id IS NOT NULL
 AND (SELECT status FROM day_proposals WHERE id = new.proposal_id) IS NOT 'approved'
BEGIN
    SELECT RAISE(ABORT, 'Baldur time is posted only for an approved day');
END;

-- ---------------------------------------------------------------------
-- 5. Event cursors
-- ---------------------------------------------------------------------

CREATE TRIGGER event_cursors_within_log_ins BEFORE INSERT ON event_cursors
WHEN new.last_event_id > (SELECT coalesce(max(id), 0) FROM events)
BEGIN
    SELECT RAISE(ABORT, 'an event cursor can''t pass the newest event');
END;
CREATE TRIGGER event_cursors_within_log_upd BEFORE UPDATE OF last_event_id ON event_cursors
WHEN new.last_event_id > (SELECT coalesce(max(id), 0) FROM events)
BEGIN
    SELECT RAISE(ABORT, 'an event cursor can''t pass the newest event');
END;

-- ---------------------------------------------------------------------
-- 6. Search entries follow every column they show
-- ---------------------------------------------------------------------

DROP TRIGGER pull_requests_search_upd;
CREATE TRIGGER pull_requests_search_upd AFTER UPDATE OF title, head_ref, author, number ON pull_requests BEGIN
    UPDATE search SET title = new.title, body = '#' || new.number || ' ' || new.author || ' ' || new.head_ref
    WHERE rowid = new.id * 16 + 3;
END;

DROP TRIGGER submissions_search_upd;
CREATE TRIGGER submissions_search_upd AFTER UPDATE OF title, system, external_id, work_item_key ON submissions BEGIN
    UPDATE search SET title = new.title,
                      body = new.system || ' ' || coalesce(new.external_id, '') || ' ' || coalesce(new.work_item_key, '')
    WHERE rowid = new.id * 16 + 7;
END;

UPDATE search SET body = (SELECT '#' || p.number || ' ' || p.author || ' ' || p.head_ref
                            FROM pull_requests p WHERE p.id * 16 + 3 = search.rowid)
 WHERE rowid IN (SELECT id * 16 + 3 FROM pull_requests);
UPDATE search SET body = (SELECT s.system || ' ' || coalesce(s.external_id, '') || ' ' || coalesce(s.work_item_key, '')
                            FROM submissions s WHERE s.id * 16 + 7 = search.rowid)
 WHERE rowid IN (SELECT id * 16 + 7 FROM submissions);

-- ---------------------------------------------------------------------
-- 7. Time posted twice
-- ---------------------------------------------------------------------

-- An approval or a meeting with more than one worklog in Jira (or on its
-- way there). Odin never offers either twice, so every row here is a
-- double post to delete in Jira. A meeting is matched by title and times,
-- so the same meeting synced from two calendars (Outlook and Graph) counts
-- as one.
CREATE VIEW v_double_posts AS
    SELECT 'proposal' AS kind, w.proposal_id AS ref_id, count(*) AS n, min(i.key) AS key,
           group_concat(coalesce(w.jira_worklog_id, 'sending'), ', ') AS jira_worklog_ids
      FROM worklogs w JOIN work_items i ON i.id = w.work_item_id
     WHERE w.proposal_id IS NOT NULL AND w.state IN ('sending', 'posted')
     GROUP BY w.proposal_id HAVING count(*) > 1
    UNION ALL
    SELECT 'meeting', min(e.id), count(*), min(i.key), group_concat(coalesce(w.jira_worklog_id, 'sending'), ', ')
      FROM worklogs w
      JOIN calendar_events e ON e.id = w.calendar_event_id
      JOIN work_items i ON i.id = w.work_item_id
     WHERE w.state IN ('sending', 'posted')
     GROUP BY lower(trim(e.title)), e.starts_at, e.ends_at HAVING count(*) > 1;

DROP VIEW v_tile_badges;

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
        SELECT 'odin', 0, count(*), 'worklogs posted twice' FROM v_double_posts
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

-- ---------------------------------------------------------------------
-- 8. Pull request commits (Baldur). Which commits GitHub lists in each
--    pull request, so a commit's 'pr' key comes from every PR that holds
--    it (the smallest wins) and never from whichever was synced last; and
--    the commit GitHub made when it merged, so a squash whose title was
--    edited is still known to be a copy.
-- ---------------------------------------------------------------------

CREATE TABLE pull_request_commits (
    pr_id  INTEGER NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
    sha    TEXT NOT NULL CHECK (length(sha) IN (40, 64)),
    PRIMARY KEY (pr_id, sha)
) STRICT, WITHOUT ROWID;
CREATE INDEX ix_pr_commits_sha ON pull_request_commits (sha);

ALTER TABLE pull_requests ADD COLUMN merge_commit_sha TEXT CHECK (merge_commit_sha IS NULL OR length(merge_commit_sha) IN (40, 64));
ALTER TABLE pull_requests ADD COLUMN commits_listed INTEGER NOT NULL DEFAULT 0 CHECK (commits_listed IN (0,1));
CREATE INDEX ix_prs_merge_commit ON pull_requests (merge_commit_sha) WHERE merge_commit_sha IS NOT NULL;

-- ---------------------------------------------------------------------
-- 9. Indexes for pruning sync runs (ON DELETE SET NULL looks rows up by run_id)
-- ---------------------------------------------------------------------

CREATE INDEX ix_work_items_run      ON work_items (run_id)      WHERE run_id IS NOT NULL;
CREATE INDEX ix_calendar_events_run ON calendar_events (run_id) WHERE run_id IS NOT NULL;
CREATE INDEX ix_commits_run         ON commits (run_id)         WHERE run_id IS NOT NULL;
CREATE INDEX ix_pull_requests_run   ON pull_requests (run_id)   WHERE run_id IS NOT NULL;
CREATE INDEX ix_meetings_run        ON meetings (run_id)        WHERE run_id IS NOT NULL;

PRAGMA user_version = 3;
COMMIT;
