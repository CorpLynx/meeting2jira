"""Baldur's command line, until its window arrives.

    cli.py setup --from-git --project PROJ --root C:\\src
    cli.py collect                 read your repositories into Muninn
    cli.py estimate                estimate the last 14 days and store proposals
    cli.py days                    one line per day
    cli.py report 2026-10-01       the day report, with the basis of every number
    cli.py approve --date 2026-10-01
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from asgard import muninn, paths
from asgard.muninn import baldur as approvals

from . import collect, desk, github, gitread, report, store
from . import estimate as E
from . import settings as config

SCHEMA = (2, 3)     # 2: squash copies are stored, and v_activity leaves them out; 3: hardening, nothing to change
MAX_DAYS_BACK = 3650
TASK_NAME = "Asgard Baldur collect"
ENTRY = Path(__file__).resolve().parent.parent / "cli.py"
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]+-[1-9][0-9]*$")


class CliError(Exception):
    """Something you can fix; the message says how."""


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse_minutes(text: str) -> int:
    """90, 1h30m, 1h, 45m, 1:30 or 1.5h as minutes."""
    t = text.strip().lower().replace(" ", "")
    if re.fullmatch(r"\d+", t):
        return int(t)
    m = re.fullmatch(r"(\d+):([0-5]\d)", t)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.fullmatch(r"(\d+\.\d+)h", t)
    if m:
        return int(round(float(m.group(1)) * 60))
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", t)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    raise CliError(f"{text!r} isn't a length of time; try 90, 1h30m or 1:30")


def parse_day_minutes(text: str) -> int:
    """A length of time that fits in one day."""
    minutes = parse_minutes(text)
    if minutes > 24 * 60:
        raise CliError(f"{text} is more than a day.")
    return minutes


def parse_day(text: str, today: Optional[dt.date] = None) -> dt.date:
    """2026-10-01, today, yesterday, or -3 for three days ago."""
    today = today or dt.date.today()
    t = text.strip().lower()
    if t == "today":
        return today
    if t == "yesterday":
        return today - dt.timedelta(days=1)
    if re.fullmatch(r"-\d{1,4}", t) and int(t[1:]) <= MAX_DAYS_BACK:
        return today - dt.timedelta(days=int(t[1:]))
    try:
        return dt.date.fromisoformat(t)
    except ValueError:
        raise CliError(f"{text!r} isn't a date; use 2026-10-01, today, yesterday or -3") from None


def date_range(args: argparse.Namespace, default_days: int) -> Tuple[dt.date, dt.date]:
    today = dt.date.today()
    last = parse_day(args.to, today) if args.to else today
    if args.days is not None and not 1 <= args.days <= 366:
        raise CliError("--days is from 1 to 366.")
    if args.start:
        first = parse_day(args.start, today)
    else:
        first = last - dt.timedelta(days=(args.days or default_days) - 1)
    if first > last:
        raise CliError("--from is after --to")
    if (last - first).days > 366:
        raise CliError("Pick at most a year at a time.")
    return first, last


def parse_value(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return text


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_setup(con: sqlite3.Connection, s: config.Settings, args: argparse.Namespace) -> int:
    add = [e.strip() for e in args.email if e.strip()]
    if args.from_git:
        email, _ = gitread.configured_identity()          # read git before taking Muninn's write lock
        if not email:
            raise CliError("git config has no user.email. Use --email you@agency.gov instead.")
        add.append(email)
    for e in add:
        if "@" not in e:
            raise CliError(f"{e!r} isn't an email address. Baldur knows your commits by your git email.")
    removed = [e.strip() for e in args.remove_email if e.strip()]
    if add or removed:
        with muninn.transaction(con):
            for e in add:
                muninn.add_identity(con, "git_email", e)
            for e in removed:
                con.execute("DELETE FROM identities WHERE kind = 'git_email' AND value = ?", (e,))
            dropped = collect.refresh_ownership(con, removed) if removed else 0
        if dropped:
            print(f"{store.plural(dropped, 'commit')} no longer count as yours. Run cli.py estimate to update your days.")

    changes: Dict[str, object] = {}
    if args.project or args.remove_project:
        drop = {k.strip().upper() for k in args.remove_project}
        changes["project_keys"] = sorted({k for k in s.project_keys if k not in drop} |
                                         {k.strip().upper() for k in args.project})
    if args.root or args.remove_root:
        drop = {r for r in args.remove_root} | {str(Path(os.path.expandvars(r)).expanduser().resolve())
                                                 for r in args.remove_root}
        roots = [r for r in s.repo_roots if r not in drop]
        for r in args.root:
            folder = Path(os.path.expandvars(r)).expanduser()
            if not folder.is_dir():
                raise CliError(f"{r} isn't a folder.")
            if str(folder.resolve()) not in roots:
                roots.append(str(folder.resolve()))
        changes["repo_roots"] = roots
    if args.policy:
        changes["policy"] = args.policy
    for item in args.set:
        name, sep, value = item.partition("=")
        if not sep or name.strip() not in config.DEFAULTS:
            raise CliError(f"--set takes NAME=VALUE with a Baldur setting name, like policy=overlap; got {item!r}")
        changes[name.strip()] = parse_value(value)
    if changes:
        s = config.update(changes)
    print_setup(con, s)
    return 0


def print_setup(con: sqlite3.Connection, s: config.Settings) -> None:
    emails = sorted(r[0] for r in con.execute("SELECT value FROM identities WHERE kind = 'git_email'"))
    found = gitread.discover(s.repo_roots, s.max_depth) if s.repo_roots else []
    weight = {"ambient": f" (weight {s.ambient_weight:g})", "overlap": f" (fraction {s.concurrent_fraction:g})"}
    print("Baldur setup")
    print(f"  Your git emails      {', '.join(emails) or '-'}")
    print(f"  Jira projects        {', '.join(s.project_keys) or '-'}")
    print(f"  Repository folders   {', '.join(s.repo_roots) or '-'}"
          + (f"  ({len(found)} {'repository' if len(found) == 1 else 'repositories'})" if s.repo_roots else ""))
    print(f"  Policy               {s.policy}{weight.get(s.policy, '')}")
    print(f"  Settings file        {config.settings_path()}")
    todo = []
    if not emails:
        hint = collect.identity_hint()
        todo.append(f"cli.py setup --from-git   (git config says {hint})" if hint
                    else "cli.py setup --email you@agency.gov")
    if not s.project_keys:
        todo.append("cli.py setup --project PROJ   (each Jira project you work in)")
    if not s.repo_roots:
        todo.append("cli.py setup --root C:\\src   (the folder your repositories are in)")
    print()
    if todo:
        print("Still to do:")
        for t in todo:
            print(f"  {t}")
    else:
        print("Next: cli.py collect")


def cmd_collect(con: sqlite3.Connection, s: config.Settings, args: argparse.Namespace) -> int:
    only = [Path(p) for p in args.paths] if args.paths else None
    if only is None and not s.repo_roots:
        raise CliError("Tell Baldur where your repositories are first: cli.py setup --root C:\\src")
    for p in only or ():
        if not (p / ".git").exists():
            raise CliError(f"{p} isn't a git repository.")
    result = collect.collect(con, s, only=only, full=args.full)
    new = sum(r.commits_new for r in result.repos)
    _log(f"collect: {len(result.repos)} repositories, {new} new commits"
         + "".join(f"; {r.name} failed: {r.error}" for r in result.failed))
    if not args.quiet:
        if not result.repos:
            print("No repositories found under " + ", ".join(s.repo_roots))
        for r in result.repos:
            if r.error:
                print(f"  {r.name:<24} failed: {r.error}")
                continue
            bits = [f"{store.plural(r.commits_seen - r.skipped_copies, 'commit')} ({r.commits_new} new)",
                    f"{r.keyed} with Jira keys"]
            if r.coauthored:
                bits.append(f"{r.coauthored} co-authored")
            if r.skipped_copies:
                bits.append(f"{r.skipped_copies} squash copies skipped")
            bits.append(f"{r.reflog_new} new reflog entries")
            print(f"  {r.name:<24} " + ", ".join(bits))
        if result.repos and not result.failed:
            print("\nNext: cli.py estimate")
    for r in result.failed:
        print(f"Baldur: {r.name} wasn't collected: {r.error}", file=sys.stderr)
    return 1 if result.failed else 0


def cmd_repos(con: sqlite3.Connection, s: config.Settings, args: argparse.Namespace) -> int:
    for target, active in [(t, 0) for t in args.off] + [(t, 1) for t in args.on]:
        path = str(Path(target).expanduser().resolve()) if Path(target).expanduser().exists() else None
        rows = con.execute("SELECT id FROM repos WHERE name = ? OR local_path = ?", (target, path)).fetchall()
        if len(rows) != 1:
            raise CliError(f"{target!r} matches {len(rows)} repositories; use its folder instead.")
        with muninn.transaction(con):
            con.execute("UPDATE repos SET active = ? WHERE id = ?", (active, rows[0][0]))
    rows = con.execute("SELECT r.*, (SELECT count(*) FROM commits c WHERE c.repo_id = r.id AND c.is_mine = 1 "
                       "AND c.is_merge = 0) AS n "
                       "FROM repos r ORDER BY r.name").fetchall()
    if not rows:
        print("No repositories yet: cli.py collect")
        return 0
    for r in rows:
        where = r["local_path"] or f"GitHub {r['github_repo']} (review only)"
        scanned = r["last_scanned_at"] or "never"
        print(f"  {'on ' if r['active'] else 'off'}  {r['name']:<24} {r['n']:>5} commits   scanned {scanned}   {where}")
    return 0


def cmd_estimate(con: sqlite3.Connection, s: config.Settings, args: argparse.Namespace) -> int:
    first, last = date_range(args, 14)
    result = store.run(con, s, first, last, dry_run=args.dry_run)
    est = result.estimate
    print(f"Estimated {first.isoformat()} to {last.isoformat()}, policy {s.policy}"
          + (" (dry run, nothing stored)" if args.dry_run else f" (run {result.run_id})" if result.run_id else ""))
    by_day: Dict[dt.date, List[store.Change]] = {}
    for c in result.changes:
        by_day.setdefault(c.local_date, []).append(c)
    shown = 0
    for day in store.days_between(first, last):
        changes = [c for c in by_day.get(day, []) if c.minutes or c.action == "withdrawn"]
        if not changes:
            continue
        shown += 1
        text = ", ".join(f"{c.key or 'untracked'} {E.fmt(c.minutes)}"
                         + ("" if c.action in ("computed",) else f" {c.action}") for c in changes)
        print(f"  {report.day_name(day)}   {text}")
    if not shown:
        print("  No commits of yours in this range." if not est.parts else "  Nothing over the rounding step.")
    for c, why in est.excluded:
        if why.startswith("dated in the future"):
            print(f"  Left out {c.sha[:10]} ({c.label}): {why}")
    if args.report:
        days = sorted({c.local_date for c in result.changes if c.action in ("new", "updated", "computed")})
        todo = store.plan(con, s, est, first, last) if not args.dry_run else None
        for day in days:
            print()
            print(report.render_day(con, est, day, s, plan=todo, dry_run=args.dry_run))
    waiting = con.execute("SELECT count(DISTINCT local_date) FROM day_proposals WHERE status = 'proposed' "
                          "AND work_item_key IS NOT NULL").fetchone()[0]
    if waiting and not args.dry_run:
        print(f"\n{waiting} {'day' if waiting == 1 else 'days'} to review: cli.py days, then cli.py report DATE")
    return 0


def _latest_day(con: sqlite3.Connection) -> dt.date:
    row = con.execute(f"SELECT max(c.authored_at) {store._MINE} AND c.authored_at <= ?", (muninn.utcnow(),)).fetchone()
    return E.local_day(muninn.from_ts(row[0])) if row and row[0] else dt.date.today()


def cmd_report(con: sqlite3.Connection, s: config.Settings, args: argparse.Namespace) -> int:
    today = dt.date.today()
    days = sorted({parse_day(d, today) for d in args.dates}) or [_latest_day(con)]
    est = store.compute(con, s, days[0], days[-1])
    todo = store.plan(con, s, est, days[0], days[-1])
    for n, day in enumerate(days):
        if n:
            print()
        print(report.render_day(con, est, day, s, plan=todo))
    return 0


def cmd_days(con: sqlite3.Connection, s: config.Settings, args: argparse.Namespace) -> int:
    first, last = date_range(args, 7)
    est = store.compute(con, s, first, last)
    todo = store.plan(con, s, est, first, last)
    print(report.render_days(con, est, store.days_between(first, last), todo))
    if todo.would_change():
        print("\nSome days changed since they were estimated: cli.py estimate")
    return 0


def _proposal_line(con: sqlite3.Connection, pid: int) -> str:
    r = con.execute("SELECT * FROM day_proposals WHERE id = ?", (pid,)).fetchone()
    minutes = r["minutes_final"] if r["minutes_final"] is not None else r["minutes_proposed"]
    return f"{r['work_item_key']} {E.fmt(minutes)} on {report.day_name(dt.date.fromisoformat(r['local_date']))}"


def cmd_approve(con: sqlite3.Connection, s: config.Settings, args: argparse.Namespace) -> int:
    if bool(args.ids) == bool(args.date):
        raise CliError("Give proposal ids (from cli.py report) or --date, not both.")
    if args.date:
        overrides = {}
        for item in args.set:
            key, sep, value = item.partition("=")
            if not sep:
                raise CliError(f"--set takes KEY=MINUTES, like PROJ-42=1h15m; got {item!r}")
            overrides[key.strip().upper()] = parse_day_minutes(value)
        day = parse_day(args.date)
        ids = approvals.approve_day(con, day.isoformat(), overrides)
        if not ids:
            raise CliError(f"Nothing to approve on {day.isoformat()}. See cli.py report {day.isoformat()}")
    else:
        if args.set:
            raise CliError("--set goes with --date; for one proposal use --minutes")
        if args.minutes and len(args.ids) > 1:
            raise CliError("--minutes applies to one proposal at a time.")
        minutes = parse_day_minutes(args.minutes) if args.minutes else None
        ids = [approvals.approve(con, pid, minutes) for pid in args.ids]
    for pid in ids:
        print(f"Approved {_proposal_line(con, pid)}")
    print("Odin posts approved time on its next run.")
    return 0


def cmd_reject(con: sqlite3.Connection, s: config.Settings, args: argparse.Namespace) -> int:
    if bool(args.ids) == bool(args.date):
        raise CliError("Give proposal ids (from cli.py report) or --date, not both.")
    if args.date:
        ids = approvals.reject_day(con, parse_day(args.date).isoformat())
    else:
        ids = list(args.ids)
        for pid in ids:
            approvals.reject(con, pid)
    for pid in ids:
        print(f"Rejected {_proposal_line(con, pid)}")
    if not ids:
        print("Nothing to reject.")
    return 0


def cmd_change(con: sqlite3.Connection, s: config.Settings, args: argparse.Namespace) -> int:
    new_id = approvals.change_approval(con, args.id, parse_day_minutes(args.minutes))
    print(f"Approved {_proposal_line(con, new_id)} instead (proposal {new_id}).")
    return 0


def cmd_keys(con: sqlite3.Connection, s: config.Settings, args: argparse.Namespace) -> int:
    sha = args.sha.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{4,64}", sha):
        raise CliError(f"{args.sha!r} isn't a commit SHA.")
    rows = con.execute("SELECT c.id, c.sha, c.subject, c.is_merge FROM commits c WHERE c.is_mine = 1 "
                       "AND c.sha LIKE ? ORDER BY c.authored_at, c.id", (sha + "%",)).fetchall()
    shas = {r["sha"] for r in rows}
    if len(shas) != 1:
        raise CliError(f"{args.sha} matches {len(shas)} of your commits" + ("; give more of the SHA." if shas else "."))
    head = rows[0]
    wanted = list(dict.fromkeys(k.strip().upper() for k in args.keys if k.strip()))
    if wanted and all(r["is_merge"] for r in rows):
        raise CliError(f"{head['sha'][:10]} is a squash copy of commits already counted, so it never counts "
                       "toward an estimate. Give the key to the commits it copies instead.")
    if not wanted:
        current = sorted({r[0] for r in con.execute(
            "SELECT work_item_key || ' (' || method || ')' FROM commit_work_items WHERE commit_id IN "
            "(SELECT id FROM commits WHERE sha = ?)", (head["sha"],))})
        print(f"{head['sha'][:10]} {head['subject']}: {', '.join(current) or 'no Jira key'}")
        return 0
    for k in wanted:
        if not _KEY_RE.match(k):
            raise CliError(f"{k!r} isn't a Jira key like PROJ-123.")
        if k.split("-")[0] not in s.project_keys:
            print(f"Note: {k.split('-')[0]} isn't in your project_keys.", file=sys.stderr)
    collect.set_keys(con, [r["id"] for r in rows], wanted)     # every clone's copy of it
    print(f"{head['sha'][:10]} {head['subject']} now counts toward {', '.join(wanted)}. "
          "Run cli.py estimate to update its day.")
    return 0


def github_client(s: config.Settings) -> "github.Client":
    """A client for your GitHub, or CliError saying what's missing."""
    host = s.github_host()
    if not host:
        raise CliError("GitHub is off. Name your server first: cli.py setup --set github_api=github.agency.gov")
    token = github.load_token(host)
    if not token:
        raise CliError(f"No GitHub token for {host} yet. Create a read-only token on {host}, then run: "
                       "cli.py github token")
    return github.Client(s.github_api, token)


def cmd_github(con: sqlite3.Connection, s: config.Settings, args: argparse.Namespace) -> int:
    host = s.github_host()
    if args.action == "token":
        if not host:
            raise CliError("GitHub is off. Name your server first: cli.py setup --set github_api=github.agency.gov")
        if args.remove:
            gone = github.delete_token(host)
            print(f"Removed the token for {host}." if gone else f"There was no token for {host}.")
            return 0
        import getpass
        token = getpass.getpass(f"Paste a read-only token for {host} (it isn't shown): ")
        github.save_token(host, token)
        print(f"Saved in Windows Credential Manager as \"{github.token_target(host)}\". Try: cli.py github sync")
        return 0
    if args.action == "sync":
        res = github.sync(con, s, github_client(s))
        print(f"GitHub {host} as {res.login}: {res.repos} {'repository' if res.repos == 1 else 'repositories'}, "
              f"{res.pulls_changed} pull requests changed, {res.reviews_new} new reviews, "
              f"{res.requested} reviews requested of you, {res.keyed_commits} commits keyed from pull requests "
              f"({res.requests} requests, {res.unchanged} unchanged).")
        for problem in res.problems:
            print(f"  {problem}", file=sys.stderr)
        return 1 if res.problems else 0
    # status
    if not host:
        print("GitHub is off (github_api is empty). Baldur works from local git alone.")
        return 0
    token = github.load_token(host)
    last = con.execute("SELECT max(finished_at) FROM sync_runs WHERE app = 'baldur' AND stream LIKE 'github:%' "
                       "AND status IN ('ok', 'partial')").fetchone()[0]
    print(f"GitHub          {s.github_api}")
    print(f"Token           {'saved' if token else 'none: cli.py github token'}")
    print(f"Last sync       {last or 'never'}")
    for line in desk.review_lines(con):
        print(line)
    return 0


def schedule_command(day: str, at: str, remove: bool = False, python: Optional[str] = None) -> List[str]:
    """The schtasks command that adds (or removes) the weekly collection for this user."""
    if remove:
        return ["schtasks", "/Delete", "/F", "/TN", TASK_NAME]
    exe = Path(python or sys.executable)
    quiet = exe.with_name("pythonw.exe")
    exe = quiet if quiet.exists() else exe
    action = f'"{exe}" "{ENTRY}" collect --quiet'
    if len(action) > 261:
        raise CliError("The scheduled command would be longer than Windows allows (261 characters).")
    # /IT: run only while you're signed in, so no password is stored and no admin rights are needed.
    return ["schtasks", "/Create", "/F", "/SC", "WEEKLY", "/D", day, "/ST", at, "/IT", "/TN", TASK_NAME, "/TR", action]


def cmd_schedule(con: sqlite3.Connection, s: config.Settings, args: argparse.Namespace) -> int:
    if os.name != "nt":
        raise CliError("Scheduled collection uses Windows Task Scheduler. Run cli.py collect weekly here instead.")
    day = args.day.upper()[:3]
    if day not in DAYS:
        raise CliError(f"--day is one of {', '.join(DAYS)}")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", args.time):
        raise CliError("--time is like 09:00")
    cmd = schedule_command(day, args.time, args.remove)
    done = subprocess.run(cmd, capture_output=True, text=True, creationflags=0x08000000)
    if done.returncode != 0:
        raise CliError("Task Scheduler refused: " + (done.stderr or done.stdout).strip()
                       + "\nIf your computer blocks scheduled tasks, run cli.py collect at least weekly yourself.")
    print("Removed the weekly collection." if args.remove
          else f"Baldur will collect every {day} at {args.time} while you're signed in. "
               f"Results go to {paths.log_dir() / 'baldur.log'}.")
    return 0


def _log(text: str) -> None:
    try:
        folder = paths.log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "baldur.log", "a", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli.py", description="Baldur: work estimates from git, for you to review.")
    sub = parser.add_subparsers(dest="command", metavar="command")

    def add(name: str, func, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    def ranged(p: argparse.ArgumentParser) -> None:
        p.add_argument("--from", dest="start", metavar="DATE", help="first day (2026-10-01, yesterday, -7)")
        p.add_argument("--to", metavar="DATE", help="last day (default: today)")
        p.add_argument("--days", type=int, metavar="N", help="the last N days")

    p = add("setup", cmd_setup, "Show or change who you are and what Baldur reads")
    p.add_argument("--email", action="append", default=[], help="a git email you commit with (add each one)")
    p.add_argument("--from-git", action="store_true", help="add the email in your git config")
    p.add_argument("--remove-email", action="append", default=[], metavar="EMAIL")
    p.add_argument("--project", action="append", default=[], metavar="KEY", help="a Jira project you work in")
    p.add_argument("--remove-project", action="append", default=[], metavar="KEY")
    p.add_argument("--root", action="append", default=[], metavar="DIR", help="a folder with repositories in it")
    p.add_argument("--remove-root", action="append", default=[], metavar="DIR")
    p.add_argument("--policy", choices=config.POLICIES, help="how coding during meetings counts")
    p.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="any other setting")

    p = add("collect", cmd_collect, "Read your commits and reflog from git into Muninn")
    p.add_argument("paths", nargs="*", metavar="REPO", help="only these repositories")
    p.add_argument("--full", action="store_true", help="read all of each repository's history, not just the last history_days")
    p.add_argument("--quiet", action="store_true", help="print only problems (for a scheduled task)")

    p = add("repos", cmd_repos, "List repositories, or switch one off or on")
    p.add_argument("--off", action="append", default=[], metavar="REPO", help="leave out of estimates")
    p.add_argument("--on", action="append", default=[], metavar="REPO")

    p = add("estimate", cmd_estimate, "Estimate days and store proposals for review (default: the last 14 days)")
    ranged(p)
    p.add_argument("--dry-run", action="store_true", help="show the numbers without storing them")
    p.add_argument("--report", action="store_true", help="print the day report for each day with new numbers")

    p = add("report", cmd_report, "Print the day report (default: your latest day with commits)")
    p.add_argument("dates", nargs="*", metavar="DATE")

    p = add("days", cmd_days, "One line per day (default: the last 7 days)")
    ranged(p)

    p = add("approve", cmd_approve, "Approve proposals, by id or a whole day")
    p.add_argument("ids", nargs="*", type=int, metavar="ID")
    p.add_argument("--date", metavar="DATE", help="approve every open ticket that day")
    p.add_argument("--minutes", metavar="TIME", help="approve one proposal at another figure, like 1h15m")
    p.add_argument("--set", action="append", default=[], metavar="KEY=TIME", help="with --date: another figure")

    p = add("reject", cmd_reject, "Reject proposals, by id or a whole day")
    p.add_argument("ids", nargs="*", type=int, metavar="ID")
    p.add_argument("--date", metavar="DATE")

    p = add("change", cmd_change, "Approve a different figure for an approved proposal")
    p.add_argument("id", type=int, metavar="ID")
    p.add_argument("minutes", metavar="TIME")

    p = add("keys", cmd_keys, "Show a commit's Jira keys, or set them by hand")
    p.add_argument("sha", metavar="SHA")
    p.add_argument("keys", nargs="*", metavar="KEY")

    p = add("github", cmd_github, "Your pull requests and the reviews requested of you (read-only)")
    p.add_argument("action", nargs="?", choices=("status", "sync", "token"), default="status")
    p.add_argument("--remove", action="store_true", help="with token: remove the saved token")

    p = add("schedule", cmd_schedule, "Collect weekly with a scheduled task for you (Windows)")
    p.add_argument("--day", default="MON", help="MON to SUN (default MON)")
    p.add_argument("--time", default="09:00", help="24-hour time (default 09:00)")
    p.add_argument("--remove", action="store_true")
    return parser


def _tolerant_output() -> None:
    """Print what can't be encoded as '?', instead of failing, when output goes to a file or pipe on
    a Windows code page that lacks a character in a commit subject."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    _tolerant_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    s = config.load()
    if s.broken:
        print(f"Baldur: {config.settings_path()} has a mistake in it, so Baldur won't run until it's fixed "
              f"({s.warnings[0]}). Fix it, or delete it to start again from the defaults.", file=sys.stderr)
        return 1
    for warning in s.warnings:
        print(f"Baldur: {warning}", file=sys.stderr)
    try:
        con = muninn.open_app("baldur", supported=SCHEMA)
    except muninn.MuninnError as exc:
        print(f"Baldur: {exc}", file=sys.stderr)
        return 1
    try:
        return int(args.func(con, s, args) or 0)
    except (CliError, collect.CollectError, muninn.MuninnError, config.SettingsError, gitread.GitError,
            github.GitHubError, desk.DeskError) as exc:
        print(f"Baldur: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"Baldur: Muninn couldn't do that ({exc}). If another Asgard app is busy, try again in a moment.",
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Baldur: stopped; nothing half-done was saved.", file=sys.stderr)
        return 130
    finally:
        con.close()
