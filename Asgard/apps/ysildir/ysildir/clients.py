"""Connecting an AI client to Ysildir: `ysildir.cmd setup`.

| Option          | The client's own command (preferred)                         | Otherwise, the file setup edits         |
| --kiro DIR      | none known                                                   | DIR\\.kiro\\settings\\mcp.json             |
| --kiro-user     | none known                                                   | %USERPROFILE%\\.kiro\\settings\\mcp.json   |
| --vscode DIR    | -                                                            | DIR\\.vscode\\mcp.json                    |
| --vscode-user   | code --add-mcp JSON (your VS Code profile)                   | -                                       |
| --claude DIR    | claude mcp add --scope project ysildir -- ..., run in DIR    | DIR\\.mcp.json                           |

The client starts this computer's Python directly with cli.py serve, never through ysildir.cmd: a
batch file in between can stop on Ctrl+C with "Terminate batch job (Y/N)?" and break stdio. It is
the Python running setup (sys.executable), because that's the one with the MCP SDK installed: py -3
could pick another. Run setup again after changing Python.

When setup edits a file itself, it parses it with json and refuses one it can't parse (VS Code's
files allow comments, which a JSON parser would drop) and prints the snippet to paste instead. It
adds or replaces only the ysildir entry, and replaces it only with --force; every other key stays;
and it writes through a temporary file and a rename, so a crash can't leave half a file.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from asgard import paths

from . import Refused
from .config import Config

CLI = Path(__file__).resolve().parent.parent / "cli.py"
NAME = "ysildir"


def launch() -> Tuple[str, List[str]]:
    """The command and arguments an AI client runs to start Ysildir.

    In the packaged build that's its console program, asgard-cli.exe, which runs cli.py as Python
    would (stdio needs a console program, never the windowless Asgard.exe).
    """
    if paths.FROZEN:
        return paths.frozen_programs()[0], [str(CLI), "serve"]
    return sys.executable, [str(CLI), "serve"]


def auto_approve(config: Config, read_only: List[str]) -> List[str]:
    """Kiro's autoApprove: only the read-only tools that are on. The person may add more by hand."""
    return [n for n in read_only if config.on(n)]


def kiro_entry(config: Config, read_only: List[str]) -> Dict[str, Any]:
    command, args = launch()
    return {"command": command, "args": args, "env": {}, "disabled": False,
            "autoApprove": auto_approve(config, read_only)}


def stdio_entry() -> Dict[str, Any]:
    command, args = launch()
    return {"type": "stdio", "command": command, "args": args}


def snippet(top: str, entry: Dict[str, Any]) -> str:
    return json.dumps({top: {NAME: entry}}, indent=2)


def merge(path: Path, top: str, entry: Dict[str, Any], force: bool = False) -> str:
    """Add entry as top.ysildir in the JSON file at path: 'written', 'replaced' or 'kept'."""
    data: Dict[str, Any] = {}
    if path.exists():
        text = path.read_text(encoding="utf-8-sig")
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError as exc:
            raise Refused(f"{path} isn't plain JSON ({getattr(exc, 'msg', exc)}): it may have comments, which setup "
                          f"would lose. Add this to it yourself:\n{snippet(top, entry)}") from None
        if not isinstance(data, dict) or not isinstance(data.get(top, {}), dict):
            raise Refused(f"{path} doesn't hold a JSON object with {top!r} in it. Add this to it yourself:\n"
                          f"{snippet(top, entry)}")
    servers = data.setdefault(top, {})
    existed = NAME in servers
    if existed and not force:
        return "kept"
    servers[NAME] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\r\n" if os.name == "nt" else "\n") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return "replaced" if existed else "written"


def _run(argv: List[str], cwd: Optional[Path] = None) -> Optional[subprocess.CompletedProcess]:
    """Run a client's own command line; None when it isn't on PATH."""
    exe = shutil.which(argv[0])
    if exe is None:
        return None
    return subprocess.run([exe] + argv[1:], cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                          timeout=120, check=False)


def _has_entry(path: Path, top: str) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and isinstance(data.get(top), dict) and NAME in data[top]


def _folder(text: str) -> Path:
    path = Path(text).expanduser()
    if not path.is_dir():
        raise Refused(f"{text} isn't a folder.")
    return path


def setup(config: Config, read_only: List[str], *, kiro: Optional[str] = None, kiro_user: bool = False,
          vscode: Optional[str] = None, vscode_user: bool = False, claude: Optional[str] = None,
          force: bool = False, print_only: bool = False) -> List[str]:
    """Connect each client asked for; returns what was done, one line each."""
    out: List[str] = []
    command, args = launch()
    if kiro or kiro_user:
        entry = kiro_entry(config, read_only)
        targets = ([("Kiro, this workspace", _folder(kiro) / ".kiro" / "settings" / "mcp.json")] if kiro else []) + \
            ([("Kiro, every workspace", Path.home() / ".kiro" / "settings" / "mcp.json")] if kiro_user else [])
        for label, path in targets:
            if print_only:
                out.append(f"{label}: add to {path}\n{snippet('mcpServers', entry)}")
            else:
                out.append(f"{label}: {merge(path, 'mcpServers', entry, force)} {path}")
    if vscode:
        path = _folder(vscode) / ".vscode" / "mcp.json"
        if print_only:
            out.append(f"VS Code, this workspace: add to {path}\n{snippet('servers', stdio_entry())}")
        else:
            out.append(f"VS Code, this workspace: {merge(path, 'servers', stdio_entry(), force)} {path}")
    if vscode_user:
        argv = ["code", "--add-mcp", json.dumps({"name": NAME, "command": command, "args": args})]
        if print_only:
            out.append("VS Code, your profile: run\n" + subprocess.list2cmdline(argv))
        else:
            done = _run(argv)
            if done is None:
                raise Refused("VS Code's command line (code) isn't on PATH. Use --vscode DIR for one workspace, or "
                              "--print and add the server in VS Code (MCP: Add Server).")
            if done.returncode:
                raise Refused(f"code --add-mcp failed ({(done.stderr or done.stdout).strip()[:300]}).")
            out.append("VS Code, your profile: added with code --add-mcp")
    if claude:
        folder = _folder(claude)
        path = folder / ".mcp.json"
        argv = ["claude", "mcp", "add", "--scope", "project", NAME, "--", command] + args
        if print_only:
            out.append(f"Claude Code, this workspace: run in {folder}\n{subprocess.list2cmdline(argv)}\n"
                       f"or add to {path}\n{snippet('mcpServers', stdio_entry())}")
        elif _has_entry(path, "mcpServers") and not force:
            out.append(f"Claude Code, this workspace: kept {path}")
        elif shutil.which("claude"):
            if force:
                _run(["claude", "mcp", "remove", "--scope", "project", NAME], cwd=folder)
            done = _run(argv, cwd=folder)
            if done is None or done.returncode:
                why = (done.stderr or done.stdout).strip()[:300] if done else "not found"
                raise Refused(f"claude mcp add failed ({why}). Use --print to add it by hand.")
            out.append(f"Claude Code, this workspace: added with claude mcp add ({path})")
        else:
            out.append(f"Claude Code, this workspace: {merge(path, 'mcpServers', stdio_entry(), force)} {path}")
    if not out:
        raise Refused("Say which client to connect: --kiro DIR, --kiro-user, --vscode DIR, --vscode-user or "
                      "--claude DIR.")
    return out
