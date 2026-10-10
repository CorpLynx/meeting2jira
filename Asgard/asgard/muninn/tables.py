"""What each of Muninn's tables and views holds, in one line, for people and AI agents.

Ysildir's muninn_catalog serves these with each table's owner (guard.OWNERS) and row count, so an
agent knows what it can ask about. Every table and view in the schema needs a line here, and a
line here needs a table or view (tests/test_muninn.py checks both), so a migration that adds one
says what it is. FTS5's own tables (guard.FTS_SHADOW) are left out: they are the search index's
insides.
"""
from __future__ import annotations

from typing import Dict, List

MEANINGS: Dict[str, str] = {
    # Shared
    "meta": "Muninn's own facts: the owner, when it was created, and the Asgard version; written only by Asgard.",
    "sources": "Where data comes from: one row per git, Jira, calendar or GitHub source.",
    "identities": "The person's own identities (git emails, Jira user, GitHub login), which mark what is theirs.",
    "sync_runs": "One row per collection run by an app: when, what, how many items, and whether it finished.",
    "sync_cursors": "How far each source's last collection got, so the next one starts there.",
    "events": "The change log: one row per change any app made, in time order. Never edited.",
    "event_cursors": "How far each app has read the change log.",
    # Odin
    "work_items": "Jira issues Odin collected: key, summary, status, type, epic and dates.",
    "work_item_aliases": "Every key an issue has had, so an old key still finds the issue after it moved.",
    "work_item_transitions": "Each issue's status changes, from Jira's changelog.",
    "calendar_events": "The person's calendar events, from Odin's meeting sync: times, titles and responses.",
    "worklogs": "Time logged in Jira: what Odin posted, and what it read back from Jira.",
    "meeting_subtasks": "The Jira sub-task Odin made for each meeting, so a re-run never makes a second one.",
    # Baldur
    "repos": "The git repositories Baldur reads, and whether each one counts.",
    "commits": "Commit metadata from those repositories: SHA, time, subject and line counts. No code or bodies.",
    "commit_work_items": "Which Jira tickets each commit belongs to, and how Baldur knows.",
    "reflog_entries": "Branch checkouts from git's reflog, which show when work on a ticket started.",
    "pull_requests": "GitHub pull requests the person wrote or reviews: title, state and dates.",
    "pull_request_commits": "Which commits each pull request holds.",
    "pr_reviews": "Reviews on those pull requests, with when each was submitted.",
    "estimate_runs": "One row per Baldur estimate, with the settings it used.",
    "work_sessions": "The work sessions an estimate found: start, end and minutes.",
    "session_commits": "Which commits each work session holds.",
    "session_allocations": "How each session's minutes were split between tickets.",
    "day_proposals": "Baldur's proposed minutes per day and ticket, and the person's decision on each.",
    "calibration_runs": "Calibrations the person accepted: the estimating settings they set, and the error before "
                        "and after.",
    "time_actuals": "Real hours the person noted during a calibration trial, which estimates are compared with.",
    "agent_estimates": "AI coding agents' estimates of the person's time on a change, as reports like r12.",
    "agent_estimate_commits": "Which commits each agent estimate cites.",
    # Loki, Freya, Heimdall and Bifrost (not built yet)
    "meetings": "Loki's meeting recaps (not built yet).",
    "action_items": "Action items from Loki's meeting recaps (not built yet).",
    "blufs": "Loki's bottom-line-up-front summaries (not built yet).",
    "citations": "What a review draft or a summary cites as its evidence (not built yet).",
    "accomplishments": "Freya's accomplishments, for performance reviews (not built yet).",
    "review_periods": "Freya's performance review periods (not built yet).",
    "review_drafts": "Freya's review drafts (not built yet).",
    "submissions": "Forms Heimdall filled and Bifrost tracks (not built yet).",
    "submission_status_history": "Each submission's status changes (not built yet).",
    # Search
    "search": "The full-text search index over issues, commits, pull requests and later apps' text.",
    # Views
    "v_activity": "The person's activity in time order: commits, checkouts, reviews and their own Jira changes.",
    "v_busy_meetings": "Calendar events that count as meetings for Baldur: busy or tentative, not declined or cancelled.",
    "v_day_status": "Per day and ticket: the minutes the person approved beside the development time Jira holds.",
    "v_double_posts": "Time posted to Jira twice, for the person to delete in Jira.",
    "v_review_evidence": "What Freya may cite for a review period (not built yet).",
    "v_tile_badges": "The counts Asgard's launcher shows on its tiles, such as reviews requested.",
    "v_time_by_item_month": "Approved minutes per ticket per month.",
    "v_unknown_keys": "Ticket keys other apps use that Odin hasn't resolved, or couldn't find.",
    "v_unpostable_days": "Approved time Odin can't post, because Jira no longer has the issue or never had the key.",
    "v_unverified_citations": "Citations nobody has checked yet (not built yet).",
    "v_worklogs_to_post": "What Odin should post next: approved time Jira doesn't hold yet.",
}

RULES: List[str] = [
    "Each app writes only the tables it owns; shared tables take only the writes every app needs (guard.py).",
    "Only Asgard changes the schema, and every app checks the schema version it was written for.",
    "Facts are never edited in place: decisions are new rows, and the change log is append-only.",
    "Metadata only: commit subjects, times, line counts and keys; never code, diffs, commit bodies or "
    "Jira descriptions.",
    "Times are UTC (2026-10-01T14:05:00Z); days are the person's local calendar days (2026-10-01).",
]
