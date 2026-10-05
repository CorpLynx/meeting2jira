"""Muninn: Asgard's shared SQLite database, and the one way apps write to it.

    from asgard import muninn
    con = muninn.open_app("odin", supported=(1, 2))      # checks the schema version, never migrates
    jira = muninn.ensure_source(con, "jira", "jira-dc", "https://jira.example.gov")
    with muninn.Run(con, "odin", jira, "issues") as run:
        ...

Asgard calls muninn.prepare() when it starts, which creates the database,
applies migrations and keeps a daily backup. Apps outside Asgard (Odin)
import this package from Asgard's install folder; see README.md.
"""
from .db import (MuninnError, NotReady, Status, VersionError, ago, available_migrations, backup, connect,
                 daily_backup, default_path, from_ts, latest_version, migrate, open_app, prepare, sqlite_problems,
                 to_ts, transaction, user_version, utcnow)
from .sync import Event, EventBatch, Run, add_identity, consume, emit, ensure_source, identities
from .badges import Badge, tile_badges
from . import baldur, odin

SCHEMA_VERSION = latest_version()

__all__ = [
    "Badge", "Event", "EventBatch", "MuninnError", "NotReady", "Run", "SCHEMA_VERSION", "Status", "VersionError",
    "add_identity", "ago", "available_migrations", "backup", "baldur", "connect", "consume", "daily_backup",
    "default_path", "emit", "ensure_source", "from_ts", "identities", "latest_version", "migrate", "odin",
    "open_app", "prepare", "sqlite_problems", "tile_badges", "to_ts", "transaction", "user_version", "utcnow",
]
