-- =====================================================================
-- Muninn schema v4  (PRAGMA user_version = 4), Asgard 0.4.0
--
-- Agent estimates, for Baldur's AI-assisted method. An AI coding agent
-- (Kiro, Copilot, Claude Code) that worked a change with you records what it
-- thinks your working time on that change was, through Baldur (its CLI, or
-- Ysildir's MCP tool). Baldur owns both tables.
--
--   * Evidence, never a number on its own. Baldur uses a report only to move
--     minutes between tickets or lower them; a day never rises because of one
--     (Baldur spec, "AI-assisted estimates"). A report without commits is
--     shown in the day report and never counted.
--   * A report is a fact, like a commit. It is never edited or deleted; a
--     newer report on the same change withdraws the older one, which stays for
--     audit (trigger agent_estimates_are_facts).
--   * Metadata only (Asgard rule 6): a one-line summary and commit SHAs. No
--     code, no diffs; Baldur refuses text that looks like either before it
--     gets here, and the summary is short by CHECK.
--   * Additive: no existing table, view or trigger changes, so apps tested
--     against v3 keep working. Baldur needs v4.
-- =====================================================================
BEGIN;

CREATE TABLE agent_estimates (
    id              INTEGER PRIMARY KEY,
    recorded_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                    CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', recorded_at) IS recorded_at),
    via             TEXT NOT NULL DEFAULT 'cli' CHECK (via IN ('cli','mcp','window')),
    agent           TEXT NOT NULL CHECK (length(agent) BETWEEN 1 AND 40),          -- 'kiro', 'copilot'
    model           TEXT CHECK (model IS NULL OR length(model) BETWEEN 1 AND 80),
    guide_version   TEXT CHECK (guide_version IS NULL OR length(guide_version) BETWEEN 1 AND 40),
    work_item_key   TEXT,                            -- the ticket the agent named; NULL: its commits' keys
    local_date      TEXT NOT NULL CHECK (date(local_date) IS local_date),          -- the day the work happened
    started_at      TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', started_at) IS started_at),
    ended_at        TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', ended_at) IS ended_at),
    minutes         INTEGER NOT NULL CHECK (minutes BETWEEN 1 AND 1440),          -- your working time, its estimate
    minutes_low     INTEGER CHECK (minutes_low BETWEEN 1 AND minutes),            -- the low end, when it gave a range
    confidence      TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    summary         TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 300),       -- one sentence; no code
    report_hash     TEXT NOT NULL UNIQUE,            -- the same report twice is stored once
    status          TEXT NOT NULL DEFAULT 'recorded' CHECK (status IN ('recorded','withdrawn')),
    withdrawn_at    TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', withdrawn_at) IS withdrawn_at),
    CHECK (ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at),
    CHECK ((status = 'withdrawn') = (withdrawn_at IS NOT NULL))
) STRICT;

-- The commits a report is about. A SHA may be short (at least 7 characters)
-- because the agent can record before Baldur has collected the commit; Baldur
-- matches it by prefix when it reads the report.
CREATE TABLE agent_estimate_commits (
    estimate_id  INTEGER NOT NULL REFERENCES agent_estimates(id),
    sha          TEXT NOT NULL CHECK (length(sha) BETWEEN 7 AND 64 AND sha NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY (estimate_id, sha)
) STRICT, WITHOUT ROWID;

CREATE INDEX ix_agent_estimates_day ON agent_estimates (local_date) WHERE status = 'recorded';
CREATE INDEX ix_agent_estimate_commits_sha ON agent_estimate_commits (sha);

-- The cross-app key rule from v3.
CREATE TRIGGER agent_estimates_key_ins BEFORE INSERT ON agent_estimates
WHEN new.work_item_key IS NOT NULL AND NOT (new.work_item_key GLOB '[A-Z]*-[1-9]*' AND new.work_item_key NOT GLOB '*[^A-Z0-9_-]*' AND new.work_item_key NOT GLOB '*-*-*' AND substr(new.work_item_key, instr(new.work_item_key, '-') + 1) NOT GLOB '*[^0-9]*')
BEGIN
    SELECT RAISE(ABORT, 'agent_estimates.work_item_key must be a Jira key like PROJ-123, in capitals');
END;

-- A report is a fact: the only change is recorded -> withdrawn, once, with its time.
CREATE TRIGGER agent_estimates_are_facts BEFORE UPDATE ON agent_estimates
WHEN NOT (old.status = 'recorded' AND new.status = 'withdrawn' AND new.withdrawn_at IS NOT NULL
          AND new.id IS old.id AND new.recorded_at IS old.recorded_at AND new.via IS old.via
          AND new.agent IS old.agent AND new.model IS old.model AND new.guide_version IS old.guide_version
          AND new.work_item_key IS old.work_item_key AND new.local_date IS old.local_date
          AND new.started_at IS old.started_at AND new.ended_at IS old.ended_at
          AND new.minutes IS old.minutes AND new.minutes_low IS old.minutes_low
          AND new.confidence IS old.confidence AND new.summary IS old.summary
          AND new.report_hash IS old.report_hash)
BEGIN
    SELECT RAISE(ABORT, 'agent estimates are facts: withdraw one and record a new one');
END;

CREATE TRIGGER agent_estimates_are_kept BEFORE DELETE ON agent_estimates
BEGIN
    SELECT RAISE(ABORT, 'agent estimates are kept for audit; withdraw one instead');
END;

CREATE TRIGGER agent_estimate_commits_are_facts BEFORE UPDATE ON agent_estimate_commits
BEGIN
    SELECT RAISE(ABORT, 'agent estimates are facts: withdraw one and record a new one');
END;

CREATE TRIGGER agent_estimate_commits_are_kept BEFORE DELETE ON agent_estimate_commits
BEGIN
    SELECT RAISE(ABORT, 'agent estimates are kept for audit; withdraw one instead');
END;

-- A report's commits are written with it and never added to later (review 2026-10-09, R14).
-- muninn.baldur.record_agent_estimate inserts the report, then its commits, then the report's
-- agent_estimate.recorded event, in one transaction. Once that event exists (events are never
-- deleted) the report is sealed. Before it, only the connection that just inserted the report
-- may add commits: an insert into a WITHOUT ROWID table leaves last_insert_rowid() alone, so it
-- still names the report. (A database made before this trigger is caught by the report's
-- digest, and --muninn repair adds the trigger.)
CREATE TRIGGER agent_estimate_commits_with_their_report BEFORE INSERT ON agent_estimate_commits
WHEN new.estimate_id IS NOT last_insert_rowid()
  OR EXISTS (SELECT 1 FROM events WHERE entity_type = 'agent_estimates' AND entity_id = new.estimate_id
             AND kind = 'agent_estimate.recorded')
BEGIN
    SELECT RAISE(ABORT, 'a report''s commits are recorded with it: withdraw it and record a new one');
END;

-- When a report was recorded is Muninn's to say (review 2026-10-09, P7): it defaults to now, and
-- an explicit time must be now, give or take two minutes, so a report can't be back-dated.
CREATE TRIGGER agent_estimates_recorded_now BEFORE INSERT ON agent_estimates
WHEN new.recorded_at NOT BETWEEN strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-2 minutes')
                             AND strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+2 minutes')
BEGIN
    SELECT RAISE(ABORT, 'agent_estimates.recorded_at is set by Muninn when a report is recorded');
END;

PRAGMA user_version = 4;
COMMIT;
