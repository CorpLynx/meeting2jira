"""Odin: Asgard's Jira app. It turns attended, finished meetings into Jira sub-tasks, keeps Jira in
Muninn for Asgard's other apps, and posts the time you approve in Baldur. It is the only app that
writes to Jira. Jira Data Center only.

Module map (docs/integration/odin.md has the flow)
    cli         commands: init, set-token, check, daily, push, sync, post, status, forget
    config      DEFAULTS, load/merge/validate, parse_hhmm
    models      the Meeting record every calendar export produces; dedupe identity
    sources     schema-v1 JSON and Outlook CSV readers
    rules       filters, tour of duty, and parent routing - all the judgement
    sync        the meeting push loop, templates, and ambiguous-create recovery
    store       Muninn: the connection, sources, calendar, meeting_subtasks, the journal, the run lock
    collect     Jira into Muninn: issues, tracked parents, key lookups, worklogs
    posting     worklogs to Jira: meeting time, approved Baldur days, posts nobody saw finish
    history     state.db's records moved into Muninn once, then retired
    jira        stdlib Jira REST client; the only module that uses the network
    credstore   DPAPI token storage through ctypes

One-way where it matters: the calendar is never modified, and Jira issues and worklogs are never
edited or deleted.
"""

__version__ = "0.4.0"
