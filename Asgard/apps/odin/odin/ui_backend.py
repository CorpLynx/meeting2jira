"""Odin's views in Asgard's shared window, as plain Python: the shell's bridge calls these.

Every method returns JSON-like data (dicts, lists, text). Problems a person can fix raise
ConfigError, CredentialError or StoreError, which the window shows as a message.

Rules it keeps:
- It reads; the command line writes. Anything that touches Jira (the daily run, a sync, posting)
  runs `odin` in a child process (run_command), so the window and the scheduled task do exactly
  the same thing, under the same run lock, and Jira calls never block the window.
- The daily run goes through Invoke-MeetingSync.ps1, as the scheduled task does, because the
  Outlook export is PowerShell.
- Muninn is opened read-only for every view, and closed again straight away.

Standard library only.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from asgard import muninn
from asgard import paths as asgard_paths
from asgard.muninn import odin as mo
from asgard.muninn.db import console_python

from . import history, store
from .config import ConfigError, default_data_dir, load_config
from .credstore import ENV_VAR, TOKEN_FILE, save_token
from .models import parse_utc

APP = Path(__file__).resolve().parent.parent          # apps/odin
CLI = APP / "cli.py"
SYNC_PS = APP / "windows" / "Invoke-MeetingSync.ps1"
COMMANDS = {
    "preview": "Preview the daily run",
    "daily": "The daily run",
    "sync": "Read Jira into Muninn",
    "post": "Post approved Baldur days",
    "post_preview": "List approved days to post",
    "check": "Check the setup",
}


class Backend:
    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else None

    # ---------------------------------------------------------------- reading

    def _data(self) -> Path:
        return self.data_dir or default_data_dir()

    def _config_path(self) -> Path:
        return self._data() / "config.json"

    def _muninn(self) -> sqlite3.Connection:
        return store.open_muninn(readonly=True)

    def state(self) -> Dict[str, Any]:
        """Setup, the last run, and what waits: everything the Today view shows."""
        data = self._data()
        out: Dict[str, Any] = {
            "data_dir": str(data), "config_path": str(self._config_path()), "config_missing": False,
            "config_error": "", "base_url": "", "log_work": False, "token": False, "token_where": "",
            "last_run": None, "last_run_age_days": None, "alert": False, "muninn_error": "",
            "counts": {}, "legacy_records": 0, "windows": os.name == "nt", "commands": COMMANDS,
        }
        try:
            cfg = load_config(self._config_path())
            out["base_url"] = cfg["jira"]["base_url"].rstrip("/")
            out["log_work"] = bool(cfg["jira"].get("log_work"))
        except ConfigError as exc:
            out["config_missing"] = not self._config_path().exists()
            out["config_error"] = str(exc)
        if os.environ.get(ENV_VAR):
            out["token"], out["token_where"] = True, f"the {ENV_VAR} environment variable"
        elif (data / TOKEN_FILE).is_file():
            out["token"], out["token_where"] = True, str(data / TOKEN_FILE)
        out["last_run"] = _read_json(data / "last_run.json")
        if out["last_run"] and out["last_run"].get("finished_utc"):
            try:
                age = datetime.now(timezone.utc) - parse_utc(str(out["last_run"]["finished_utc"]))
                out["last_run_age_days"] = age.days
            except (ValueError, TypeError):
                pass
        out["alert"] = (data / "ATTENTION-Odin.txt").exists()
        try:
            out["legacy_records"] = len(history.Legacy.open(data) or [])
        except history.HistoryError as exc:
            out["muninn_error"] = str(exc)
        try:
            con = self._muninn()
        except muninn.MuninnError as exc:
            out["muninn_error"] = str(exc)
            return out
        try:
            out["counts"] = {
                "subtasks": con.execute("SELECT count(*) FROM meeting_subtasks").fetchone()[0],
                "posts_due": len(mo.posts_due(con)),
                "unpostable": con.execute("SELECT count(*) FROM v_unpostable_days").fetchone()[0],
                "worklogs_waiting": len(store.pending_meeting_worklogs(con)),
                "posts_in_doubt": len(mo.stuck_posts(con, older_than_seconds=0)),
                "assigned": len(mo.assigned_to_me(con)),
            }
        finally:
            con.close()
        return out

    def meetings(self, limit: int = 60) -> List[Dict[str, Any]]:
        """The latest meeting sub-tasks, newest first, with whether their time is in Jira."""
        base = self._base_url()
        con = self._muninn()
        try:
            rows = store.recent(con, int(limit))
        finally:
            con.close()
        out = []
        for r in rows:
            state = r["worklog_state"] or ("wanted" if r["worklog_wanted"] else "")
            out.append({"issue_key": r["issue_key"], "parent_key": r["parent_key"], "summary": r["summary"],
                        "started": _local(r["started_at"]), "minutes": r["minutes"], "worklog": state,
                        "origin": r["origin"], "url": f"{base}/browse/{r['issue_key']}" if base else ""})
        return out

    def issues(self, include_done: bool = False) -> List[Dict[str, Any]]:
        """Assigned to me: issues whose current assignee is you, as Muninn last saw them."""
        con = self._muninn()
        try:
            rows = mo.assigned_to_me(con, include_done=bool(include_done))
        finally:
            con.close()
        return [{"key": r["key"], "summary": r["summary"], "status": r["status"],
                 "category": r["status_category"], "type": r["issue_type"], "updated": _local(r["updated_at"]),
                 "url": r["url"]} for r in rows]

    def _base_url(self) -> str:
        try:
            return load_config(self._config_path())["jira"]["base_url"].rstrip("/")
        except ConfigError:
            return ""

    # ---------------------------------------------------------------- setting up

    def init_config(self) -> Dict[str, Any]:
        target = self._config_path()
        if target.exists():
            raise ConfigError(f"{target} already exists. Open it to edit it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(APP / "config.example.json", target)
        return {"path": str(target)}

    def save_token(self, token: str) -> Dict[str, Any]:
        """Store your Jira personal access token, DPAPI-encrypted for your Windows user."""
        token = str(token or "").strip()
        if not token or any(ch.isspace() for ch in token):
            raise ConfigError("Paste the whole personal access token, with no spaces.")
        path = save_token(self._data(), token)
        return {"path": str(path)}

    # ---------------------------------------------------------------- running

    def run_command(self, kind: str) -> List[str]:
        """The command line for one of COMMANDS, run by the window as a child process."""
        if kind not in COMMANDS:
            raise ConfigError(f"{kind!r} isn't something Odin's window can run.")
        if not self._config_path().exists():
            raise ConfigError("Create Odin's settings first (Today, Create settings).")
        python = console_python()
        config = ["--config", str(self._config_path())]
        if kind in ("preview", "daily"):
            if os.name != "nt":
                raise ConfigError("The daily run reads your calendar from Outlook, which needs Windows. "
                                  "Elsewhere run: python apps/odin/cli.py daily --input EXPORT.json")
            powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / \
                "v1.0" / "powershell.exe"
            argv = [str(powershell), "-NoProfile", "-NonInteractive", "-File", str(SYNC_PS), "-DaysBack", "1",
                    "-Config", str(self._config_path())]
            if not asgard_paths.FROZEN:
                argv += ["-Python", python]       # the packaged build is found by the script itself
            return argv + (["-DryRun"] if kind == "preview" else [])
        if kind == "post_preview":
            return [python, str(CLI), "post", *config, "--dry-run"]
        return [python, str(CLI), kind, *config]

    # ---------------------------------------------------------------- for the shell

    def dashboard(self) -> List[Dict[str, Any]]:
        st = self.state()
        if st["config_error"]:
            return [{"label": "Odin", "value": "Set up" if st["config_missing"] else "Fix",
                     "detail": "Create Odin's settings" if st["config_missing"] else "Odin's settings have a problem",
                     "tone": "warning", "view": "today"}]
        last = st["last_run"] or {}
        cards = [{"label": "Last run", "value": "OK" if last.get("exit_code") == 0 else ("Failed" if last else "None"),
                  "detail": last.get("finished_utc", "Odin hasn't run yet"),
                  "tone": "" if last.get("exit_code") == 0 else ("error" if last else "warning"), "view": "today"}]
        counts = st["counts"]
        if counts:
            cards.append({"label": "Approved days to post", "value": str(counts["posts_due"]),
                          "detail": "Posted by the next daily run", "view": "today"})
            cards.append({"label": "Assigned to me", "value": str(counts["assigned"]),
                          "detail": "Open issues in Jira", "view": "issues"})
        return cards

    def settings_files(self) -> List[Dict[str, str]]:
        return [{"label": "Odin's settings", "path": str(self._config_path())},
                {"label": "Odin's logs", "path": str(self._data() / "logs")}]


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _local(ts: Optional[str]) -> str:
    if not ts:
        return ""
    try:
        return muninn.from_ts(ts).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts
