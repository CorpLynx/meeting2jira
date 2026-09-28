"""meeting2jira: push attended, already-ended calendar meetings into Jira as sub-tasks.

Standard library only, so it runs on a locked-down machine with no pip, no admin rights, and no
approved packages. Jira Data Center only.

Module map (the project repo's ARCHITECTURE.md has the diagram)
    __main__    CLI: init, set-token, check, push, status, forget. Wiring and exit codes.
    config      DEFAULTS, load/merge/validate, parse_hhmm
    models      the Meeting record every source produces; dedupe identity
    sources     schema-v1 JSON and Outlook CSV readers
    rules       filters, tour of duty, and parent routing - all the judgement
    sync        the push loop, templates, and ambiguous-create recovery
    jira        stdlib Jira REST client; the only module that uses the network
    state       sqlite dedupe and worklog bookkeeping; what makes re-runs safe
    credstore   DPAPI token storage through ctypes

One-way by design: calendar -> Jira. The calendar is never modified and Jira issues are never
deleted.
"""

__version__ = "0.1.0"
