"""Which Ysildir tools the person allows: %LOCALAPPDATA%\\Asgard\\settings\\ysildir.json.

Everything a tool returns goes to the AI client, and so to its model, so each tool's data flow is
the ISSO's decision and each tool has a switch (docs/integration/ysildir.md):
- No file: the defaults. Tools that return only Asgard's own text, or the agents' own reports,
  start on; tools that send commit subjects, Jira summaries or times start off.
- A broken file fails closed: every tool except asgard_guide is off, and the guide's tools topic
  says what's wrong, naming the bad entry.
- Names that aren't Ysildir tools are ignored, and listed in the tools topic.
The server reads the file when it starts and never writes it. `ysildir.cmd tools` writes it,
through a temporary file and a rename. The person runs that, not an agent: Kiro's guard hook
blocks it.

Python 3.9 syntax, but no `from __future__ import annotations`: pydantic reads the annotations.
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from asgard import paths

from . import Refused


class Switch(NamedTuple):
    name: str
    default: bool
    sends: str          # what the tool sends to the AI client: what the ISSO approves
    fallback: str       # the command-line way to do the same, when the tool is off or MCP isn't there


# The allow-list, in the documented order. server.TOOLS has exactly these names (a test checks), so
# adding a tool means adding its switch, its data flow and a spec change.
SWITCHES = (
    Switch("asgard_guide", True, "Asgard's own guides", "baldur.cmd ai guide"),
    Switch("muninn_catalog", True, "Muninn's table names, owners and row counts", "none"),
    Switch("baldur_record_estimate", True, "The id and status of the report it records",
           "baldur.cmd ai record ... --json"),
    Switch("baldur_withdraw_estimate", True, "The id of the report it withdraws", "baldur.cmd ai withdraw r12"),
    Switch("baldur_estimates", True, "The agents' own reports: dates, keys, minutes and summaries",
           "baldur.cmd ai list --json"),
    Switch("baldur_day", False, "One day's keys, minutes, AI reasons and flags; the day report (commit subjects) "
           "when asked", "baldur.cmd ai show DATE --json, baldur.cmd report DATE"),
    Switch("baldur_review_pack", False, "One day's evidence: commit subjects, times, line counts, keys and agent "
           "reports", "baldur.cmd ai pack DATE"),
    Switch("baldur_submit_review", False, "The checked figures and flags", "baldur.cmd ai review DATE ANSWER.json"),
    Switch("muninn_what_changed", False, "Event kinds, times, keys and allow-listed payload fields", "none yet"),
    Switch("muninn_day_status", False, "Approved and logged minutes per day and ticket", "baldur.cmd report DATE"),
    Switch("muninn_issue", False, "One Jira issue's key, summary, status, type and epic", "none yet"),
    Switch("muninn_search", False, "Jira keys and summaries, the person's commit subjects, pull request titles",
           "none yet"),
)
NAMES = tuple(s.name for s in SWITCHES)
ALWAYS_ON = "asgard_guide"      # stays on when the file is broken, so the agent can say what's wrong
VERSION = 1
HELP = ("Which Ysildir tools your AI client may use. Each tool's data flow needs your ISSO's approval (Asgard "
        "docs/integration/ysildir.md). Restart the MCP server in your client after a change.")


class SwitchesFile(BaseModel):
    """ysildir.json, as `ysildir.cmd tools` writes it."""
    model_config = ConfigDict(extra="ignore")

    version: int = Field(VERSION, ge=1, le=VERSION)
    tools: Dict[str, StrictBool] = Field(default_factory=dict)


@dataclass
class Config:
    tools: Dict[str, bool]              # every switch: on or off
    path: Path
    problem: Optional[str] = None       # why the file couldn't be used; then only asgard_guide is on
    unknown: List[str] = field(default_factory=list)     # names in the file that aren't Ysildir tools

    def on(self, name: str) -> bool:
        return bool(self.tools.get(name, False))

    def on_names(self) -> List[str]:
        return [n for n in NAMES if self.on(n)]


def settings_path() -> Path:
    return paths.data_dir() / "settings" / "ysildir.json"


def defaults() -> Dict[str, bool]:
    return {s.name: s.default for s in SWITCHES}


def everything_on(path: Optional[Path] = None) -> Config:
    """Every tool on: for `ysildir.cmd check --all` and the tests, never for serving."""
    return Config({n: True for n in NAMES}, path or settings_path())


def _problem(exc: Exception) -> str:
    """What's wrong with the file, naming the entry and never repeating its values."""
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors()[:3]:
            where = ".".join(str(x) for x in err.get("loc", ())) or "the file"
            parts.append(f"{where}: {err.get('msg', 'not valid')}")
        return "; ".join(parts)
    if isinstance(exc, json.JSONDecodeError):
        return f"it isn't JSON ({exc.msg} at line {exc.lineno})"
    if isinstance(exc, OSError):
        return f"it couldn't be read ({exc.strerror or type(exc).__name__})"
    return str(exc)


def load(path: Optional[Path] = None) -> Config:
    path = path or settings_path()
    if not path.exists():
        return Config(defaults(), path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise Refused('the file must hold a JSON object, like {"version": 1, "tools": {...}}')
        data = SwitchesFile.model_validate(raw)
    except (OSError, ValueError) as exc:          # JSONDecodeError and ValidationError are ValueErrors
        return Config({n: n == ALWAYS_ON for n in NAMES}, path, problem=_problem(exc))
    tools = defaults()
    tools.update({k: v for k, v in data.tools.items() if k in NAMES})
    return Config(tools, path, unknown=sorted(set(data.tools) - set(NAMES)))


def save(tools: Dict[str, bool], path: Optional[Path] = None) -> Path:
    """Write every switch, through a temporary file and a rename so a crash can't leave half a file."""
    path = path or settings_path()
    data = {"_help": HELP, "version": VERSION, "tools": {n: bool(tools.get(n, d)) for n, d in defaults().items()}}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\r\n" if os.name == "nt" else "\n") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def switch(names: Iterable[str], on: bool, path: Optional[Path] = None) -> Config:
    """Turn tools on or off, keeping the other switches. The person runs this (`ysildir.cmd tools`)."""
    names = list(names)
    bad = [n for n in names if n not in NAMES]
    if bad:
        raise Refused(f"{', '.join(bad)} isn't a Ysildir tool. The tools are: {', '.join(NAMES)}.")
    current = load(path)
    if current.problem:
        raise Refused(f"{current.path} has a mistake in it ({current.problem}). Fix it, or delete it to start "
                      "again from the defaults.")
    tools = dict(current.tools)
    tools.update({n: on for n in names})
    save(tools, current.path)
    return load(current.path)
