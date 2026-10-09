"""Ysildir: Asgard's MCP server, so an approved AI client can use Baldur and Muninn.

The MCP SDK speaks the protocol (server.py). The rules stay Baldur's and Muninn's: every write is
one existing function in asgard.muninn.baldur or Baldur's assist, on a connection opened as
baldur (baldur_tools.py), and every read is on a read-only connection opened as ysildir. Ysildir
adds only the switches (config.py), the teaching (teach.py) and the shapes of the tools
(models.py), and owns no table. Spec: .kiro/specs/ysildir-mcp/ in the repository, and
docs/integration/ysildir.md.
"""
import contextlib
import datetime as dt
import sqlite3
from typing import Iterator, Optional, Tuple

from asgard import muninn

SCHEMA = (4, 4)     # the Muninn versions Ysildir understands; equal to Baldur's cli.SCHEMA (a test checks)
MAX_DAYS = 31       # the most days one call covers


class Refused(ValueError):
    """A call Ysildir can't answer as asked; the message says why and what to do instead."""


@contextlib.contextmanager
def reader() -> Iterator[sqlite3.Connection]:
    """Muninn for one call, read-only, as ysildir: query_only is on and the guard refuses every write.

    One connection per call: opening one is cheap, a new one picks up an Asgard upgrade between
    calls, nothing is held open while the agent thinks, and no connection crosses the SDK's worker
    threads (sqlite3 connections stay on the thread that opened them).
    """
    con = muninn.open_app("ysildir", supported=SCHEMA, readonly=True)
    try:
        yield con
    finally:
        con.close()


def parse_day(text: Optional[str]) -> dt.date:
    """A YYYY-MM-DD day (the models check the shape), or today on this computer's calendar."""
    if text is None:
        return dt.date.today()
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise Refused(f"{text} isn't a day on the calendar; give one like 2026-10-01.") from None


def day_range(first: Optional[str], last: Optional[str], default_days: int) -> Tuple[dt.date, dt.date]:
    """first to last, both included; last defaults to today and first to default_days before it."""
    end = parse_day(last)
    start = parse_day(first) if first else end - dt.timedelta(days=default_days - 1)
    if start > end:
        raise Refused("date_from is after date_to.")
    if (end - start).days + 1 > MAX_DAYS:
        raise Refused(f"Ask for at most {MAX_DAYS} days at a time.")
    return start, end
