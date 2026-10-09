"""How every Ysildir tool runs: refusals become the SDK's ToolError, answers fit the caps, calls are logged.

- A refusal (Ysildir's Refused, or Muninn's or Baldur's MuninnError, which ReviewRejected is) is
  raised as ToolError with its own message. The agent sees it after the SDK's prefix "Error
  executing tool NAME: " and shows it to the person.
- Anything else is a bug. Its traceback goes to logs\\ysildir.log, frames and type only: the
  exception's message is left out because it might repeat an argument. The agent gets a message
  saying where the details are.
- A list answer is cut to MAX_ITEMS items and MAX_BYTES of JSON, and says so (truncated, narrow).
- A prompt given an argument it can't use (a date that isn't one) answers with the client's
  invalid-params error and Ysildir's message, instead of the SDK's "Error rendering prompt".
- The call log, logs\\ysildir.log: the time, the tool, the outcome, how long it took and the ids
  it created or changed. Never arguments, summaries or results.

This module and server.py are the only ones that import the MCP SDK (MODULES.md, "mcp").
"""
import functools
import logging
import logging.handlers
import os
import sqlite3
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from mcp import MCPError                                 # mcp 1.x: McpError(ErrorData(...)) from mcp.shared.exceptions
from mcp.server.mcpserver.exceptions import ToolError    # mcp 1.x: from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import INVALID_PARAMS

from asgard import muninn, paths
from asgard.muninn import guard
from asgard.muninn.db import is_busy

from . import Refused

MAX_ITEMS = 200
MAX_BYTES = 64 * 1024
LOG_NAME = "ysildir.log"

_log = logging.getLogger("ysildir.calls")
_log.propagate = False              # the SDK's own logging goes to stderr; the call log only to its file
_log.setLevel(logging.INFO)
_lock = threading.Lock()
_handler: Optional[logging.Handler] = None


def log_path() -> Path:
    return paths.log_dir() / LOG_NAME


def _logger() -> logging.Logger:
    """The call log, in the Asgard folder in use now (tests move ASGARD_HOME between calls)."""
    global _handler
    path = os.path.abspath(log_path())
    with _lock:
        if _handler is None or getattr(_handler, "baseFilename", None) != path:
            if _handler is not None:
                _log.removeHandler(_handler)
                _handler.close()
                _handler = None
            try:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2,
                                                               encoding="utf-8", delay=True)
            except OSError:
                return _log             # no log folder: the call still runs, unlogged
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
            _log.addHandler(handler)
            _handler = handler
    return _log


def close_log() -> None:
    """Close the call log's file (the next call opens it again), so its folder can be removed."""
    global _handler
    with _lock:
        if _handler is not None:
            _log.removeHandler(_handler)
            _handler.close()
            _handler = None


def log_call(tool: str, outcome: str, started: Optional[float], ids: str = "", detail: str = "") -> None:
    took = f" {int((time.monotonic() - started) * 1000)}ms" if started is not None else ""
    line = f"{tool} {outcome}{took}" + (f" {ids}" if ids else "")
    if detail:
        _logger().error("%s\n%s", line, detail)
    else:
        _logger().info("%s", line)


def _trace(exc: BaseException) -> str:
    """The traceback without the exception's message, which might repeat an argument."""
    what = type(exc).__name__
    if isinstance(exc, sqlite3.Error) and "not authorized" in str(exc).lower():
        what += f": {guard.describe(exc)}"          # Muninn's guard refused a write: which, and why
    return "".join(traceback.format_tb(exc.__traceback__)) + what


def fit(result: Any) -> Any:
    """A list answer cut to MAX_ITEMS items and MAX_BYTES of JSON, marked truncated with how to narrow it."""
    field = getattr(type(result), "ITEMS", "")
    if not field:
        return result
    items = list(getattr(result, field))
    marked = {"truncated": True, "narrow": type(result).NARROW}
    if len(items) <= MAX_ITEMS and len(result.model_dump_json().encode("utf-8")) <= MAX_BYTES:
        return result
    room = MAX_BYTES - len(result.model_copy(update=dict(marked, **{field: []})).model_dump_json().encode("utf-8"))
    kept = []
    for item in items[:MAX_ITEMS]:
        size = len(item.model_dump_json().encode("utf-8")) + 1
        if size > room:
            break
        room -= size
        kept.append(item)
    return result.model_copy(update=dict(marked, **{field: kept}))


def tool(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """fn as the SDK calls it. functools.wraps keeps the signature the SDK builds the schemas from."""
    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            result = fit(fn(*args, **kwargs))
        except (Refused, muninn.MuninnError) as exc:
            log_call(name, "refused", started)
            raise ToolError(str(exc)) from None
        except sqlite3.Error as exc:
            if not is_busy(exc):
                return _crashed(name, started, exc)
            log_call(name, "busy", started)
            raise ToolError("Muninn is busy in another Asgard app. Wait a moment and try again.") from None
        except Exception as exc:                       # noqa: BLE001 - a bug: logged, and a plain message out
            return _crashed(name, started, exc)
        log_call(name, "ok", started, result.log_ids() if hasattr(result, "log_ids") else "")
        return result
    return run


def _crashed(name: str, started: float, exc: BaseException) -> Any:
    log_call(name, "error", started, detail=_trace(exc))
    raise ToolError(f"Ysildir hit a problem it didn't expect ({type(exc).__name__}). The details are in Asgard's "
                    f"logs folder ({LOG_NAME}). Show the person this message.") from None


def prompt(fn: Callable[..., str]) -> Callable[..., str]:
    """A prompt as the SDK calls it: an argument Ysildir refuses becomes invalid-params, with its message."""
    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except Refused as exc:
            raise MCPError(INVALID_PARAMS, str(exc)) from None
    return run
