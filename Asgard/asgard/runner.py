"""Start apps, watch them, and explain failures in plain words.

Output from each app goes to %LOCALAPPDATA%\\Asgard\\logs\\<id>.log, so a
crash at startup can be shown to the user instead of vanishing.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Dict, List, Optional, Set

from .catalog import LaunchSpec

CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
LOG_LIMIT_BYTES = 1_000_000
QUICK_EXIT_SECONDS = 15


class LaunchError(Exception):
    """An app could not be started; the message is meant for the user."""


@dataclass
class Running:
    app_id: str
    name: str
    proc: subprocess.Popen
    started: float
    log_path: Optional[Path]
    log_file: Optional[IO[str]]


@dataclass
class Finished:
    app_id: str
    name: str
    returncode: int
    seconds: float
    log_path: Optional[Path]

    @property
    def crashed_early(self) -> bool:
        return self.returncode != 0 and self.seconds < QUICK_EXIT_SECONDS


def explain_oserror(exc: OSError, name: str, program: str) -> str:
    winerror = getattr(exc, "winerror", None)
    if winerror == 1260:
        return (f"Windows blocked {name}. A group policy (AppLocker or App Control) "
                f"doesn't allow:\n{program}\n\nAsk IT to allow it, or install it "
                f"somewhere your policy allows.")
    if isinstance(exc, FileNotFoundError) or winerror in (2, 3):
        return f"Couldn't find:\n{program}"
    if isinstance(exc, PermissionError) or winerror == 5:
        return f"Windows denied access to:\n{program}"
    if winerror == 193:
        return f"This isn't a program Windows can run:\n{program}"
    return f"Couldn't start {name}: {exc}"


def log_tail(path: Optional[Path], lines: int = 12) -> str:
    if not path or not path.exists():
        return ""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - 8192))
        text = fh.read().decode("utf-8", errors="replace")
    all_lines = text.splitlines()
    starts = [i for i, ln in enumerate(all_lines) if ln.startswith("=== ") and " starting " in ln]
    body = all_lines[starts[-1] + 1:] if starts else all_lines  # only the latest run
    kept = [ln for ln in body if not ln.startswith("=== exited")]
    return "\n".join(kept[-lines:]).strip()


class Runner:
    def __init__(self, log_dir: Path, extra_env: Optional[Dict[str, str]] = None) -> None:
        self.log_dir = log_dir
        self.extra_env = extra_env or {}
        self._running: Dict[str, List[Running]] = {}

    def is_running(self, app_id: str) -> bool:
        return bool(self._running.get(app_id))

    def running_ids(self) -> Set[str]:
        return {k for k, v in self._running.items() if v}

    def _open_log(self, app_id: str) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{app_id}.log"
        try:
            if path.exists() and path.stat().st_size > LOG_LIMIT_BYTES:
                os.replace(path, path.with_suffix(".log.1"))
        except OSError:
            pass
        return path

    def start(self, app_id: str, name: str, spec: LaunchSpec) -> Running:
        if spec.kind != "process":
            raise ValueError("Runner only starts process launches")
        log_path: Optional[Path] = None
        log_file: Optional[IO[str]] = None
        if not spec.console:
            log_path = self._open_log(app_id)
            log_file = open(log_path, "a", encoding="utf-8", errors="replace")
            log_file.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} starting {name}: "
                           f"{subprocess.list2cmdline(spec.argv)}\n")
            log_file.flush()
        env = dict(os.environ)
        env.update(self.extra_env)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        kwargs: Dict[str, object] = dict(
            cwd=spec.cwd or None, env=env, stdin=subprocess.DEVNULL,
            stdout=log_file, stderr=subprocess.STDOUT if log_file else None, close_fds=True)
        if os.name == "nt":
            kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | (
                CREATE_NEW_CONSOLE if spec.console else CREATE_NO_WINDOW)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(spec.argv, **kwargs)  # type: ignore[call-overload]
        except OSError as exc:
            if log_file:
                log_file.write(f"Could not start: {exc}\n")
                log_file.close()
            raise LaunchError(explain_oserror(exc, name, spec.argv[0])) from exc
        run = Running(app_id, name, proc, time.monotonic(), log_path, log_file)
        self._running.setdefault(app_id, []).append(run)
        return run

    def poll(self) -> List[Finished]:
        done: List[Finished] = []
        for app_id, runs in list(self._running.items()):
            for run in list(runs):
                code = run.proc.poll()
                if code is None:
                    continue
                runs.remove(run)
                if run.log_file:
                    run.log_file.write(f"=== exited with code {code}\n")
                    run.log_file.close()
                done.append(Finished(app_id, run.name, code,
                                     time.monotonic() - run.started, run.log_path))
            if not runs:
                del self._running[app_id]
        return done

    def close(self) -> None:
        """Stop watching. Apps keep running after the launcher closes."""
        for runs in self._running.values():
            for run in runs:
                if run.log_file and not run.log_file.closed:
                    run.log_file.close()
        self._running.clear()
