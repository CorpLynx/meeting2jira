"""Ysildir from the command line:  py -3 apps\\ysildir\\cli.py --help  (or ysildir.cmd help)

    cli.py serve                 what an AI client starts (stdio); you never run it by hand
    cli.py check                 start the server in memory and list what an agent would see
    cli.py tools                 each tool, on or off, and what it sends to the AI client
    cli.py tools --on NAME ...   switch tools on, or --off NAME ... (you run this; an agent can't)
    cli.py setup --kiro DIR      connect an AI client; also --kiro-user, --vscode DIR, --vscode-user
                                 and --claude DIR. --print shows what it would do; --force replaces

Finds the asgard package two folders up (or at ASGARD_APP, which Asgard sets when it starts an
app) and Baldur beside Ysildir, so it runs from the installed copy or a checkout. Needs Python
3.10+ and the MCP SDK (mcp and pydantic, MODULES.md); without them it says what's missing and what
still works, and exits 2. While serving, nothing but the SDK writes to standard output: it's the
protocol's channel.
"""
import argparse
import importlib.util
import os
import platform
import sys
from pathlib import Path
from typing import List, Optional, Sequence

HERE = Path(__file__).resolve().parent
ROOT = os.environ.get("ASGARD_APP") or str(HERE.parent.parent)
for folder in (str(HERE.parent / "baldur"), str(HERE), ROOT):
    if folder not in sys.path:
        sys.path.insert(0, folder)

FALLBACK = ("Without Ysildir, an agent can still record its estimates with baldur.cmd ai record ... --json, and "
            "AI review still works through the clipboard: baldur.cmd ai pack DATE, then baldur.cmd ai review DATE "
            "ANSWER.json.")


def missing() -> Optional[str]:
    """What Ysildir needs that this Python doesn't have, or None."""
    if sys.version_info < (3, 10):
        return f"Ysildir needs Python 3.10 or newer, for the MCP SDK; this is Python {platform.python_version()}."
    lost = [name for name in ("mcp", "pydantic") if importlib.util.find_spec(name) is None]
    if lost:
        return (f"Ysildir needs the MCP SDK, and {' and '.join(lost)} isn't installed for this Python "
                f"({sys.executable}). Ask IT for Asgard's Ysildir packages (requirements.txt: mcp, pydantic).")
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ysildir", description="Ysildir, Asgard's MCP server for AI clients.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="what an AI client starts (stdio); not for running by hand")
    check = sub.add_parser("check", help="start the server in memory and list what an agent would see")
    check.add_argument("--all", action="store_true", help="as if every tool were switched on")
    tools = sub.add_parser("tools", help="list the tools, or switch them on or off")
    tools.add_argument("--list", action="store_true", help="list them (the default)")
    tools.add_argument("--on", nargs="+", metavar="NAME", default=[], help="switch these on")
    tools.add_argument("--off", nargs="+", metavar="NAME", default=[], help="switch these off")
    setup = sub.add_parser("setup", help="connect an AI client to Ysildir")
    setup.add_argument("--kiro", metavar="DIR", help="this Kiro workspace (.kiro/settings/mcp.json)")
    setup.add_argument("--kiro-user", action="store_true", help="every Kiro workspace (~/.kiro/settings/mcp.json)")
    setup.add_argument("--vscode", metavar="DIR", help="this VS Code workspace (.vscode/mcp.json)")
    setup.add_argument("--vscode-user", action="store_true", help="your VS Code profile (code --add-mcp)")
    setup.add_argument("--claude", metavar="DIR", help="this Claude Code workspace (claude mcp add, or .mcp.json)")
    setup.add_argument("--print", dest="print_only", action="store_true", help="show what it would do; change nothing")
    setup.add_argument("--force", action="store_true", help="replace a ysildir entry that's already there")
    return parser


def show_tools(config) -> List[str]:          # config: ysildir.config.Config
    from ysildir.config import SWITCHES
    out = [f"Switches: {config.path}" + ("" if config.path.exists() else " (no file yet: the defaults)")]
    if config.problem:
        out.append(f"  It has a mistake in it, so only asgard_guide is on: {config.problem}. Fix it, or delete it "
                   "to start again from the defaults.")
    for s in SWITCHES:
        out.append(f"  {'on ' if config.on(s.name) else 'off'}  {s.name:<26} sends: {s.sends}")
    if config.unknown:
        out.append(f"  Ignored, not Ysildir tools: {', '.join(config.unknown)}")
    out.append("Each tool's data flow needs your ISSO's approval. Switch one on with: ysildir.cmd tools --on NAME, "
               "then restart the MCP server in your AI client.")
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    problem = missing()
    if problem:
        print(f"Ysildir: {problem}\n{FALLBACK}", file=sys.stderr)
        return 2
    try:
        from ysildir import Refused, clients, config, server
    except ImportError as exc:                 # installed, but a compiled part won't load (App Control?)
        print(f"Ysildir: the MCP SDK is installed but won't load ({exc}). If this computer only runs approved "
              f"programs, IT needs to approve its compiled parts (Asgard MODULES.md, \"mcp\").\n{FALLBACK}",
              file=sys.stderr)
        return 2
    try:
        if args.command == "serve":
            server.serve()
            return 0
        if args.command == "check":
            for line in server.check(config.everything_on() if args.all else None):
                print(line)
            return 0
        if args.command == "tools":
            both = sorted(set(args.on) & set(args.off))
            if both:
                raise Refused(f"{', '.join(both)} can't be switched both on and off.")
            current = config.load()
            if args.off:
                current = config.switch(args.off, False)
            if args.on:
                current = config.switch(args.on, True)
            for line in show_tools(current):
                print(line)
            return 0
        read_only = [t.name for t in server.TOOLS if t.read_only]
        for line in clients.setup(config.load(), read_only, kiro=args.kiro, kiro_user=args.kiro_user,
                                  vscode=args.vscode, vscode_user=args.vscode_user, claude=args.claude,
                                  force=args.force, print_only=args.print_only):
            print(line)
        if not args.print_only:
            print("Restart the MCP server in your AI client (or reload its window) to use it. An entry that was "
                  "kept stays as it was; add --force to replace it.")
        return 0
    except Refused as exc:
        print(f"Ysildir: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
