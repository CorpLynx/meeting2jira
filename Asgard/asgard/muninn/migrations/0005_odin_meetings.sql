-- =====================================================================
-- Muninn schema v5  (PRAGMA user_version = 5), Asgard 0.4.0
--
-- Odin's meeting sub-tasks. Odin turns each attended, finished meeting into
-- a Jira sub-task under a parent issue, and logs the meeting's time on it.
-- This table is the record of which meetings already have one: it is all
-- that stands between a re-run and a pile of duplicate sub-tasks, and it
-- replaces the `synced` table of Odin's own state.db, which Odin imports
-- once and then retires.
--
--   * One row per meeting Odin made a sub-task for. A meeting is recognised
--     by its calendar item's own key (Outlook's id and start, csv:<hash>) or,
--     from any other source, by content_hash: sha256 of its normalised
--     subject, start and end. Changing either formula would re-create every
--     past meeting, so both are stored, not derived.
--   * The meeting's time is a worklog on the sub-task, written through the
--     posting protocol in `worklogs` (origin 'meeting', its calendar event),
--     so it is never posted twice. calendar_event_id links the two; it is
--     NULL for history imported from state.db, whose calendar items Muninn
--     never saw.
--   * Odin owns it. Removing a row (`odin forget KEY`) lets that meeting be
--     pushed again, which is the one deliberate way to re-create a sub-task.
--   * Additive: no existing table, view or trigger changes. Baldur and
--     Ysildir read nothing here and accept v5; Odin needs it.
-- =====================================================================
BEGIN;

CREATE TABLE meeting_subtasks (
    id                 INTEGER PRIMARY KEY,
    meeting_key        TEXT NOT NULL UNIQUE CHECK (length(meeting_key) BETWEEN 1 AND 1000),
    content_hash       TEXT NOT NULL CHECK (length(content_hash) = 32 AND content_hash NOT GLOB '*[^0-9a-f]*'),
    calendar_event_id  INTEGER REFERENCES calendar_events(id) ON DELETE SET NULL,
    issue_key          TEXT NOT NULL,                -- the sub-task, as Jira named it when Odin made it
    parent_key         TEXT NOT NULL,                -- the issue it was made under
    summary            TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 255),
    started_at         TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', started_at) IS started_at),
    minutes            INTEGER NOT NULL CHECK (minutes BETWEEN 0 AND 1440),
    worklog_wanted     INTEGER NOT NULL DEFAULT 0 CHECK (worklog_wanted IN (0, 1)),   -- log_work was on
    worklog_comment    TEXT,                         -- rendered when it was made, so a retry needs no calendar
    origin             TEXT NOT NULL DEFAULT 'odin' CHECK (origin IN ('odin', 'state_db')),
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                       CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at)
) STRICT;

CREATE INDEX ix_meeting_subtasks_hash ON meeting_subtasks (content_hash);
CREATE INDEX ix_meeting_subtasks_issue ON meeting_subtasks (issue_key);
CREATE INDEX ix_meeting_subtasks_event ON meeting_subtasks (calendar_event_id) WHERE calendar_event_id IS NOT NULL;

-- The cross-app key rule from v3, for both keys.
CREATE TRIGGER meeting_subtasks_keys_ins BEFORE INSERT ON meeting_subtasks
WHEN NOT (new.issue_key GLOB '[A-Z]*-[1-9]*' AND new.issue_key NOT GLOB '*[^A-Z0-9_-]*' AND new.issue_key NOT GLOB '*-*-*' AND substr(new.issue_key, instr(new.issue_key, '-') + 1) NOT GLOB '*[^0-9]*')
  OR NOT (new.parent_key GLOB '[A-Z]*-[1-9]*' AND new.parent_key NOT GLOB '*[^A-Z0-9_-]*' AND new.parent_key NOT GLOB '*-*-*' AND substr(new.parent_key, instr(new.parent_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'meeting_subtasks keys must be Jira keys like PROJ-123, in capitals');
END;

-- What a meeting was turned into doesn't change afterwards; only its link to
-- a calendar event may be filled in (or cleared when the event goes).
CREATE TRIGGER meeting_subtasks_are_facts BEFORE UPDATE ON meeting_subtasks
WHEN NOT (new.id IS old.id AND new.meeting_key IS old.meeting_key AND new.content_hash IS old.content_hash
          AND new.issue_key IS old.issue_key AND new.parent_key IS old.parent_key AND new.summary IS old.summary
          AND new.started_at IS old.started_at AND new.minutes IS old.minutes
          AND new.worklog_wanted IS old.worklog_wanted AND new.worklog_comment IS old.worklog_comment
          AND new.origin IS old.origin AND new.created_at IS old.created_at)
BEGIN
    SELECT RAISE(ABORT, 'a meeting sub-task record only gains or loses its calendar event; forget it to push the meeting again');
END;

PRAGMA user_version = 5;
COMMIT;
