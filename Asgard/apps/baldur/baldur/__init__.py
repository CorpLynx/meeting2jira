"""Baldur: work-time estimates from git, for you to review before Odin posts them.

    collect   read your repositories into Muninn (commits, reflog, Jira keys)
    estimate  turn a date range into sessions and per-ticket day proposals
    report    print the day report with the basis of every number

Run it with apps/baldur/cli.py. The rules it follows are in the Baldur spec.
"""
MODEL_VERSION = "baldur-1"
