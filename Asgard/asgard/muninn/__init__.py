"""Muninn: Asgard's shared SQLite database, and the one way apps write to it.

    from asgard import muninn
    con = muninn.open_app("odin", supported=(1, 3))      # checks the schema version, never migrates
    jira = muninn.ensure_source(con, "jira", "jira-dc", "https://jira.example.gov")
    with muninn.Run(con, "odin", jira, "issues") as run:
        ...

Asgard calls muninn.prepare() when it starts, which creates the database,
applies migrations, checks the file and keeps a daily backup. Apps outside
Asgard (Odin) import this package from Asgard's install folder; see README.md.
The connection open_app() returns refuses writes to tables the app doesn't
own (guard.py). `Asgard.pyw --muninn check|repair|backup|restore` looks after
the file by hand (cli.py, docs/muninn-operations.md).
"""
from .db import (BusyError, CorruptError, MuninnError, NotReady, Status, VersionError, ago, available_migrations,
                 backup, connect, daily_backup, default_path, from_ts, latest_version, list_backups, migrate,
                 newest_backup, open_app, prepare, restore, retry_busy, sqlite_problems, to_ts, transaction,
                 user_version, utcnow)
from .keys import is_key, normalize_key
from .redact import redact_url, scrub, scrub_value
from .sync import Event, EventBatch, Run, add_identity, consume, emit, ensure_source, identities
from .badges import Badge, tile_badges
from . import baldur, guard, integrity, odin

SCHEMA_VERSION = latest_version()

__all__ = [
    "Badge", "BusyError", "CorruptError", "Event", "EventBatch", "MuninnError", "NotReady", "Run", "SCHEMA_VERSION",
    "Status", "VersionError", "add_identity", "ago", "available_migrations", "backup", "baldur", "connect", "consume",
    "daily_backup", "default_path", "emit", "ensure_source", "from_ts", "guard", "identities", "integrity", "is_key",
    "latest_version", "list_backups", "migrate", "newest_backup", "normalize_key", "odin", "open_app", "prepare",
    "redact_url", "restore", "retry_busy", "scrub", "scrub_value", "sqlite_problems", "tile_badges", "to_ts",
    "transaction", "user_version", "utcnow",
]
