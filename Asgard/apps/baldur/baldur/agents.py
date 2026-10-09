"""Putting Baldur's agent files (steering and hooks) into a workspace's .kiro folder.

    agents.install(Path("C:/src/my-service"))      # cli.py ai kiro --into C:\\src\\my-service

The files come from apps/baldur/agents/ (see its README). Paths to this computer's Python and
to the scripts are filled in at install time. A command starts with an unquoted launcher (py -3
on Windows) and quotes only the script path, so it runs the same in PowerShell, where a quoted
program path would need '&' first, and in cmd. Files that are already there are kept unless you
ask to replace them: they may be your own edits.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Tuple

AGENTS = Path(__file__).resolve().parent.parent / "agents"
CLI = Path(__file__).resolve().parent.parent / "cli.py"
SHELL_TOOLS = "shell|bash|pwsh|powershell|execute"


def launcher() -> str:
    """How to start Python from a hook or an agent's shell on this computer."""
    if os.name == "nt":
        return "py -3" if shutil.which("py") else "python"
    return sys.executable if " " not in sys.executable else "python3"


def baldur_command() -> str:
    return f'{launcher()} "{CLI}"'


def _hook(name: str, description: str, trigger: str, script: str) -> str:
    hook = {"name": name, "description": description, "trigger": trigger, "matcher": SHELL_TOOLS, "timeout": 15,
            "action": {"type": "command", "command": f'{launcher()} "{AGENTS / "hooks" / script}"'}}
    return json.dumps({"version": "v1", "hooks": [hook]}, indent=2) + "\n"


def kiro_files() -> Dict[str, str]:
    """{path under .kiro: content} for this computer."""
    steering = (AGENTS / "kiro-steering.md").read_text(encoding="utf-8").replace("{baldur}", baldur_command())
    return {
        "steering/baldur-estimates.md": steering,
        "hooks/baldur-guard.json": _hook(
            "Baldur: the person decides",
            "Blocks an agent from approving, rejecting or changing time in Baldur, noting real hours, accepting a "
            "calibration, changing Baldur's settings, keys or repositories, switching Ysildir's tools or "
            "connecting clients to it, and from touching muninn.db or ysildir.json. Recording its own estimate "
            "(ai record) and reading are allowed.", "PreToolUse", "guard_baldur.py"),
        "hooks/baldur-after-commit.json": _hook(
            "Baldur: record the estimate after a commit",
            "After a git commit, adds one line telling the agent how to record its estimate of the person's time "
            "on the change in Baldur. Silent otherwise.", "PostToolUse", "after_commit.py"),
    }


def install(workspace: Path, force: bool = False) -> List[Tuple[Path, str]]:
    """Write the files into workspace/.kiro. Returns (path, 'written', 'replaced' or 'kept') for each."""
    workspace = Path(workspace).expanduser()
    if not workspace.is_dir():
        raise ValueError(f"{workspace} isn't a folder.")
    done = []
    for rel, text in kiro_files().items():
        path = workspace / ".kiro" / rel
        existed = path.exists()
        if existed and not force:
            done.append((path, "kept"))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\r\n" if os.name == "nt" else "\n") as fh:
            fh.write(text)
        done.append((path, "replaced" if existed else "written"))
    return done
