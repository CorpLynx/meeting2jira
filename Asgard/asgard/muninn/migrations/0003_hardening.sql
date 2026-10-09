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
--   8. Pull request commits, merge commits and message keys for Baldur
--      (additive).
--   9. A Baldur worklog counts for the day it was approved for, so a
--      change of time zone between a post and a changed approval can't
--      post the same day's time again.
--  10. Indexes on run_id, so pruning old sync runs doesn't scan every
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

DROP TRIGGER IF EXISTS pull_requests_search_upd;
CREATE TRIGGER pull_requests_search_upd AFTER UPDATE OF title, head_ref, author, number ON pull_requests BEGIN
    UPDATE search SET title = new.title, body = '#' || new.number || ' ' || new.author || ' ' || new.head_ref
    WHERE rowid = new.id * 16 + 3;
END;

DROP TRIGGER IF EXISTS submissions_search_upd;
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

DROP VIEW IF EXISTS v_tile_badges;

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

-- The Jira keys collection found in each commit's whole message (the subject and the body, which
-- isn't stored). When pull request evidence for a commit goes away, its keys fall back to these,
-- even for commits older than history_days that collection no longer reads.
ALTER TABLE commits ADD COLUMN message_keys TEXT
    CHECK (message_keys IS NULL OR (json_valid(message_keys) AND json_type(message_keys) = 'array'));

-- Baldur's pull request lists are read again from the top once: rows stored before v3 have
-- no commit lists and no merge commit, and an unchanged list (an ETag answer) would never say
-- so. The cursors only save a request; dropping them loses nothing.
DELETE FROM sync_cursors WHERE stream LIKE 'github:pulls %';

-- ---------------------------------------------------------------------
-- 9. A Baldur worklog counts for the day it was approved for.
--     v_day_status found what Jira holds for a day by worklog start time
--     between local midnights. If the laptop's time zone changed between a
--     post and a changed approval, the earlier post fell outside the new
--     day's bounds and its time was posted again. Worklogs Odin posted for
--     an approval now count for that approval's local_date as well as for
--     the day they start on (moved in Jira, it still counts where it is);
--     counting a worklog for two days can only lower what is posted. Other
--     worklogs (from Jira, typed in Odin) count by when they start.
-- ---------------------------------------------------------------------

DROP VIEW IF EXISTS v_day_status;
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
                       LEFT JOIN day_proposals wp ON wp.id = w.proposal_id
                       WHERE w.work_item_id = a.work_item_id
                         AND w.state IN ('sending', 'posted')
                         AND w.origin <> 'meeting'
                         AND ((w.started_at >= a.day_start AND w.started_at < a.day_end)
                              OR wp.local_date = a.local_date)), 0)
            + 59) / 60 AS logged_minutes
      FROM approved a
     WHERE a.rn = 1;

DROP VIEW IF EXISTS v_worklogs_to_post;
-- What Odin should post: the shortfall for each approval, at most once per
-- approval, and nothing while a post for that issue and day is in doubt.
CREATE VIEW v_worklogs_to_post AS
    SELECT d.*, d.approved_minutes - d.logged_minutes AS minutes_to_post
      FROM v_day_status d
      JOIN work_items wi ON wi.id = d.work_item_id AND wi.deleted_at IS NULL
     WHERE d.approved_minutes > d.logged_minutes
       AND NOT EXISTS (SELECT 1 FROM worklogs w
                        WHERE w.proposal_id = d.proposal_id
                          AND w.state IN ('sending', 'posted', 'deleted'))
       AND NOT EXISTS (SELECT 1 FROM worklogs s
                        LEFT JOIN day_proposals sp ON sp.id = s.proposal_id
                        WHERE s.work_item_id = d.work_item_id
                          AND s.state = 'sending'
                          AND s.origin <> 'meeting'
                          AND ((s.started_at >= d.day_start AND s.started_at < d.day_end)
                               OR sp.local_date = d.local_date));

-- ---------------------------------------------------------------------
-- 10. Indexes for pruning sync runs (ON DELETE SET NULL looks rows up by run_id)
-- ---------------------------------------------------------------------

CREATE INDEX ix_work_items_run      ON work_items (run_id)      WHERE run_id IS NOT NULL;
CREATE INDEX ix_calendar_events_run ON calendar_events (run_id) WHERE run_id IS NOT NULL;
CREATE INDEX ix_commits_run         ON commits (run_id)         WHERE run_id IS NOT NULL;
CREATE INDEX ix_pull_requests_run   ON pull_requests (run_id)   WHERE run_id IS NOT NULL;
CREATE INDEX ix_meetings_run        ON meetings (run_id)        WHERE run_id IS NOT NULL;

PRAGMA user_version = 3;
COMMIT;
