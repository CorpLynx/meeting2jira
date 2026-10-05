"""Command-line entry point:  py -3 -m meeting2jira <command>

Position in the flow
    Wiring, not logic. Parses arguments, sets up logging, loads config, picks a source, opens state,
    builds a Jira client, and calls sync.run. The decisions live in rules.py and sync.py.

    Invoked either directly or through the `meeting2jira.cmd` / `m2j` entry points, which is why the
    command names, flags, and exit codes below are a compatibility surface: `Invoke-MeetingSync.ps1`
    and the scheduled task depend on them.

Exit codes
    0    fine
    1    the push finished but individual items failed, or a check reported FAIL
    2    configuration, usage, or credential problem; also a locked or corrupt state database
    130  interrupted

Operational breadcrumb
    Every real (non-dry-run) push writes last_run.json with the timestamp, counts, exit code, and
    first error. `status` and Test-Environment.ps1 read it, so a scheduled task that quietly started
    failing is visible without opening a log file.

Commands
  init        create %LOCALAPPDATA%\\meeting2jira\\config.json from config.example.json
  set-token   store your Jira PAT, DPAPI-encrypted for your Windows user
  check       verify config, token, Jira reachability, parent issues, and sub-task type
  push        read an export (--input JSON or --csv) and create sub-tasks (--dry-run to preview)
  status      show recently created sub-tasks
  forget      drop an issue from local state so its meeting can be pushed again
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import logging.handlers
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

if sys.version_info < (3, 8):  # pragma: no cover
    sys.exit("meeting2jira needs Python 3.8 or newer.")

from . import __version__
from .config import ConfigError, default_config_path, default_data_dir, load_config
from .credstore import CredentialError, load_token, save_token
from .jira import JiraClient, JiraError
from .models import iso_utc, parse_utc
from .rules import Router
from .sources import load_export, load_outlook_csv
from .state import State
from .sync import run

log = logging.getLogger("meeting2jira")
# app/ root: this file is app/src/meeting2jira/__main__.py, so climb three parents.
APP_ROOT = Path(__file__).resolve().parents[2]


def _data_dir(args: argparse.Namespace) -> Path:
    return Path(args.config).parent if args.config else default_data_dir()


def _setup_logging(data_dir: Path, verbose: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # don't crash on emoji in subjects under cp1252
        except AttributeError:
            pass
    log.setLevel(logging.DEBUG)
    for handler in log.handlers[:]:      # close, don't just drop: a bare clear() leaks the file handle
        log.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(console)
    try:
        logs = data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(logs / "meeting2jira.log", maxBytes=1_000_000,
                                                  backupCount=3, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        log.addHandler(fh)
    except OSError as exc:
        log.warning("File logging disabled: %s", exc)


# ---- commands ----------------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.config) if args.config else default_config_path()
    if target.exists() and not args.force:
        log.info("Config already exists: %s (use --force to overwrite)", target)
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(APP_ROOT / "config.example.json", target)
    log.info("Created %s", target)
    log.info("Next: edit jira.base_url, jira.default_parent and rules, then run `set-token` and `check`.")
    return 0


def cmd_set_token(args: argparse.Namespace) -> int:
    token = getpass.getpass("Paste your Jira personal access token (input hidden): ").strip()
    if not token:
        log.error("No token entered.")
        return 2
    path = save_token(_data_dir(args), token)
    log.info("Token encrypted with DPAPI (your user, this machine) and saved to %s", path)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    ok = True

    def report(status: str, message: str) -> None:
        nonlocal ok
        ok = ok and status != "FAIL"
        log.info("[%-4s] %s", status, message)

    cfg = load_config(args.config)
    j = cfg["jira"]
    router = Router(cfg)
    report("OK", f"config {Path(cfg['data_dir']) / 'config.json'}")

    token, where = load_token(Path(cfg["data_dir"]))
    report("OK", f"token from {where}")
    client = JiraClient.from_config(j, token)

    try:
        info = client.server_info()
        deployment = info.get("deploymentType", "?")
        report("OK", f"reached {j['base_url']} (Jira {info.get('version', '?')}, {deployment})")
        if deployment == "Cloud":
            report("FAIL", "that is a Jira Cloud site; this tool supports Jira Data Center only")
    except JiraError as exc:
        report("FAIL", f"server info: {exc}")
        return 1

    try:
        me = client.myself()
        report("OK", f"authenticated as {me.get('displayName')} ({me.get('name')})")
    except JiraError as exc:
        report("FAIL", f"authentication: {exc}")
        return 1

    # Token expiry. Absent on older Data Center versions, so "not available" is reported as INFO
    # rather than as a problem: it is a nicety, not a requirement.
    try:
        tokens = client.personal_access_tokens()
        if tokens is None:
            report("INFO", "this Jira does not expose token expiry dates; check them in Jira directly")
        else:
            warning = token_expiry_warning(tokens, int(j.get("warn_token_expiry_days") or 0))
            if warning:
                report("WARN", warning)
            else:
                report("OK", f"{len(tokens)} personal access token(s), none expiring soon")
    except JiraError as exc:
        report("INFO", f"could not read token expiry: {exc}")

    for parent in sorted(router.parents()):
        try:
            issue = client.get_issue(parent)
            fields = issue.get("fields", {})
            if (fields.get("issuetype") or {}).get("subtask"):
                report("FAIL", f"{parent} is itself a sub-task; parents must be standard issues")
            else:
                report("OK", f"parent {parent}: {fields.get('summary')}")
        except JiraError as exc:
            report("FAIL", f"parent {parent}: {exc}")

    for project in sorted({p.rsplit('-', 1)[0] for p in router.parents()}):
        try:
            types = [t["name"] for t in client.get_project(project).get("issueTypes", []) if t.get("subtask")]
            if j["subtask_type"] in types:
                report("OK", f"project {project} has sub-task type '{j['subtask_type']}'")
            else:
                report("FAIL", f"project {project} has no '{j['subtask_type']}' type; sub-task types: {types or 'none'}")
        except JiraError as exc:
            report("FAIL", f"project {project}: {exc}")

    log.info("All checks passed." if ok else "Some checks failed; fix the items marked FAIL.")
    return 0 if ok else 1


def cmd_push(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    data_dir = Path(cfg["data_dir"])
    if args.max is not None:
        cfg["jira"]["max_creates_per_run"] = args.max

    if args.input:
        meetings = load_export(args.input)
    else:
        meetings = load_outlook_csv(args.csv, cfg["csv"]["datetime_formats"])
    log.info("Loaded %d calendar item(s) from %s%s", len(meetings), args.input or args.csv,
             "  [DRY RUN - nothing will be created]" if args.dry_run else "")

    client = None
    if not args.dry_run:
        token, _ = load_token(data_dir)
        client = JiraClient.from_config(cfg["jira"], token)
        # One extra GET, and only a nicety: a token that expires unnoticed turns into a run of 401s.
        # Any failure here is swallowed, because this must never be why a sync does not happen.
        try:
            warning = token_expiry_warning(client.personal_access_tokens(),
                                           int(cfg["jira"].get("warn_token_expiry_days") or 0))
            if warning:
                log.warning("TOKEN: %s", warning)
        except (JiraError, ValueError, TypeError, AttributeError) as exc:
            # AttributeError included on purpose: a stubbed or older client without this method
            # must not be able to stop a sync over an advisory warning.
            log.debug("Token expiry check skipped: %s", exc)

    with State(data_dir / "state.db") as state:
        result = run(meetings, cfg, state, client, dry_run=args.dry_run)

    skipped = sum(result.skipped.values())
    if args.dry_run:
        log.info("\nWould create %d, already synced %d, skipped %d.", result.planned, result.existing, skipped)
    else:
        log.info("\nCreated %d, already synced %d, skipped %d, errors %d, warnings %d.",
                 len(result.created), result.existing, skipped, len(result.errors), len(result.warnings))
        if result.recovered:
            log.info("Recovered %d sub-task(s) that a failed create had already made: %s",
                     len(result.recovered), ", ".join(result.recovered))
        if result.worklogs_retried:
            log.info("Logged work for %d sub-task(s) left over from an earlier run.", result.worklogs_retried)
    for reason, count in result.skipped.most_common():
        log.debug("  skipped %3d  %s", count, reason)

    exit_code = 1 if result.errors else 0
    if not args.dry_run:
        # Carry the failure streak forward, so a single network blip does not have to raise an
        # alarm while a run of failures does.
        previous = _read_last_run(data_dir) or {}
        streak = int(previous.get("consecutive_failures") or 0)
        streak = streak + 1 if exit_code != 0 else 0

        # A scheduled run is hidden; this is what `status` and Test-Environment.ps1 read to tell
        # you it stopped working, without anyone having to open a log file.
        _write_last_run(data_dir, {
            "finished_utc": iso_utc(datetime.now(timezone.utc)),
            "exit_code": exit_code,
            "source": args.input or args.csv,
            "created": len(result.created),
            "recovered": len(result.recovered),
            "existing": result.existing,
            "skipped": skipped,
            "errors": len(result.errors),
            "warnings": len(result.warnings),
            "worklogs_retried": result.worklogs_retried,
            "consecutive_failures": streak,
            "first_error": result.errors[0] if result.errors else None,
        })

        # Push the outcome into view, or take the notice down now that it is working again.
        if exit_code == 0:
            clear_alert(data_dir)
        else:
            write_alert(cfg, data_dir,
                        summary=f"The last sync finished with {len(result.errors)} error(s).",
                        detail=result.errors[0] if result.errors else "No detail recorded.",
                        consecutive_failures=streak)
    return exit_code


LAST_RUN_FILE = "last_run.json"
ALERT_FILE = "ATTENTION-meeting2jira.txt"
STALE_AFTER_DAYS = 4   # a weekday schedule can legitimately be quiet over a long weekend


def token_expiry_warning(tokens: Optional[list], within_days: int,
                         now: Optional[datetime] = None) -> Optional[str]:
    """Build a warning if a personal access token is expiring soon, else None.

    Pure so it can be tested without a Jira. `tokens` is whatever
    JiraClient.personal_access_tokens returned, including None for "this Jira does not expose them".

    The response never says which token is the one in use, so the message must not pretend to know.
    With a single token that is unambiguous; with several, the soonest expiry is reported and named.
    """
    if not tokens or within_days <= 0:
        return None
    now = now or datetime.now(timezone.utc)

    soonest = None
    for token in tokens:
        raw = token.get("expiringAt") or token.get("expiringAtMillis")
        if not raw:
            continue          # a token with no expiry cannot expire
        try:
            expires = parse_utc(str(raw))
        except (ValueError, TypeError):
            continue
        if soonest is None or expires < soonest[0]:
            soonest = (expires, str(token.get("name") or "unnamed"))

    if not soonest:
        return None
    expires, name = soonest
    days = (expires - now).days
    if days > within_days:
        return None

    which = "your token" if len(tokens) == 1 else f"the soonest-expiring of your {len(tokens)} tokens, {name!r},"
    if days < 0:
        return (f"{which} expired on {expires:%Y-%m-%d}. Create a new one in Jira "
                "(Profile > Personal Access Tokens) and run set-token.")
    when = "expires today" if days == 0 else f"expires in {days} day(s), on {expires:%Y-%m-%d}"
    tail = "" if len(tokens) == 1 else " Jira does not reveal which token a request used, so check the name."
    return (f"{which} {when}. Create a replacement in Jira (Profile > Personal Access Tokens) "
            f"and run set-token before then.{tail}")


def _write_last_run(data_dir: Path, summary: Dict[str, Any]) -> None:
    """Record the outcome of a push so a hidden scheduled task can't fail silently.

    Best effort: failing to write the breadcrumb must never change the command's exit code.
    """
    try:
        path = data_dir / LAST_RUN_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
    except OSError as exc:
        log.debug("Could not write %s: %s", LAST_RUN_FILE, exc)


def _desktop_dir() -> Optional[Path]:
    """The user's Desktop, if it can be found. Nothing here is worth failing a run over."""
    candidates = []
    profile = os.environ.get("USERPROFILE") or str(Path.home())
    if profile:
        candidates.append(Path(profile) / "Desktop")
        # OneDrive Known Folder Move relocates the Desktop, which is common on managed machines.
        for key in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
            root = os.environ.get(key)
            if root:
                candidates.append(Path(root) / "Desktop")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def write_alert(cfg: Dict[str, Any], data_dir: Path, summary: str, detail: str,
                consecutive_failures: int) -> None:
    """Put a failure somewhere the user cannot miss it, and stop once it is fixed.

    A file on the Desktop is unglamorous, but it is the only mechanism that works for a standard
    user, under Constrained Language Mode, with no modules, and without using a binary that
    endpoint protection treats as an attack tool. See config.DEFAULTS["notify"] for what was
    rejected and why.
    """
    notify = cfg.get("notify") or {}
    if not notify.get("desktop_alert"):
        return
    threshold = max(1, int(notify.get("alert_after_failures") or 1))
    if consecutive_failures < threshold:
        log.debug("Not alerting yet: %d consecutive failure(s), threshold %d",
                  consecutive_failures, threshold)
        return

    body = (
        "meeting2jira needs attention\n"
        "============================\n\n"
        f"{summary}\n\n"
        f"Consecutive failed runs: {consecutive_failures}\n"
        f"Last attempt (UTC):      {iso_utc(datetime.now(timezone.utc))}\n\n"
        "Details\n-------\n"
        f"{detail}\n\n"
        "What to do\n----------\n"
        "  1. Open a PowerShell window in the meeting2jira folder\n"
        "  2. Run:  .\\meeting2jira check\n"
        "     That reports the specific problem: an expired token, a moved parent issue,\n"
        "     a proxy intercepting the API, or a certificate that is not trusted.\n"
        "  3. Fix what it names, then:  .\\meeting2jira\n\n"
        "Meetings are not lost. Nothing has been pushed twice either: re-running is safe,\n"
        "because already-synced meetings are recognised and skipped.\n\n"
        "This file is deleted automatically on the next successful run.\n"
        f"Logs: {data_dir / 'logs'}\n"
    )

    for folder in (d for d in (_desktop_dir(), data_dir) if d):
        try:
            (folder / ALERT_FILE).write_text(body, encoding="utf-8")
            log.info("Wrote %s", folder / ALERT_FILE)
            break
        except OSError as exc:
            log.debug("Could not write the alert to %s: %s", folder, exc)

    if notify.get("use_msg_exe"):
        _try_msg_exe(summary)


def _try_msg_exe(summary: str) -> None:
    """Best-effort console message. msg.exe is absent on some Windows builds, so failure is fine."""
    import subprocess
    try:
        subprocess.run(["msg.exe", "*", f"meeting2jira: {summary}"],
                       timeout=10, capture_output=True, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("msg.exe unavailable: %s", exc)


def clear_alert(data_dir: Path) -> None:
    """Remove the alert once a run succeeds, so a stale file never causes a false alarm."""
    for folder in (d for d in (_desktop_dir(), data_dir) if d):
        path = folder / ALERT_FILE
        try:
            if path.exists():
                path.unlink()
                log.info("Cleared %s", path)
        except OSError as exc:
            log.debug("Could not remove %s: %s", path, exc)


def _read_last_run(data_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(data_dir / LAST_RUN_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _report_last_run(data_dir: Path) -> None:
    last = _read_last_run(data_dir)
    if not last:
        log.info("No run recorded yet (%s absent).", LAST_RUN_FILE)
        return
    finished = last.get("finished_utc", "?")
    verdict = "OK" if last.get("exit_code") == 0 else "FAILED"
    log.info("Last run %s at %s: created %s, existing %s, skipped %s, errors %s.",
             verdict, finished, last.get("created"), last.get("existing"),
             last.get("skipped"), last.get("errors"))
    if last.get("first_error"):
        log.info("  first error: %s", last["first_error"])
    try:
        age = datetime.now(timezone.utc) - parse_utc(finished)
    except (ValueError, TypeError):
        return
    if age > timedelta(days=STALE_AFTER_DAYS):
        log.warning("  that was %d days ago; the scheduled task may not be running.", age.days)
    streak = int(last.get("consecutive_failures") or 0)
    if streak > 1:
        log.warning("  %d consecutive failed runs.", streak)
    for folder in (d for d in (_desktop_dir(), data_dir) if d):
        if (folder / ALERT_FILE).exists():
            log.warning("  an alert is outstanding: %s", folder / ALERT_FILE)
            break


def cmd_status(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args)
    _report_last_run(data_dir)
    log.info("")
    with State(data_dir / "state.db") as state:
        rows = state.recent(args.limit)
    if not rows:
        log.info("Nothing synced yet.")
    for row in rows:
        log.info("%-12s %s  %4dm  worklog=%s  %s", row["issue_key"], row["start_utc"], row["minutes"],
                 "yes" if row["worklog_logged"] else "no ", row["summary"])
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    with State(_data_dir(args) / "state.db") as state:
        removed = state.forget(args.issue_key)
    log.info("Removed %d record(s) for %s.", removed, args.issue_key)
    return 0


# ---- parser ------------------------------------------------------------------------------------
def _positive_int(text: str) -> int:
    """argparse type for --max. Rejects 0, which used to quietly mean 'no cap at all'."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError(
            f"{value} would remove the safety cap; pass 1 or more (use --dry-run to create nothing)")
    return value


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help=f"config file (default: {default_config_path()})")
    common.add_argument("-v", "--verbose", action="store_true", help="more detail on the console")

    parser = argparse.ArgumentParser(prog="meeting2jira", description="Push calendar meetings into Jira as sub-tasks.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", parents=[common], help="create a config file from the example")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    sub.add_parser("set-token", parents=[common], help="store Jira PAT (DPAPI)").set_defaults(func=cmd_set_token)
    sub.add_parser("check", parents=[common], help="verify config and Jira access").set_defaults(func=cmd_check)

    p = sub.add_parser("push", parents=[common], help="create sub-tasks from an export")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", help="JSON written by Export-OutlookMeetings.ps1")
    src.add_argument("--csv", help="Outlook calendar CSV export")
    p.add_argument("--dry-run", action="store_true", help="show what would happen; create nothing")
    p.add_argument("--max", type=_positive_int,
                   help="override jira.max_creates_per_run for this run (must be 1 or more)")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("status", parents=[common], help="list recently created sub-tasks")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("forget", parents=[common], help="remove an issue from local state")
    p.add_argument("issue_key")
    p.set_defaults(func=cmd_forget)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(_data_dir(args), args.verbose)
    try:
        return args.func(args)
    except (ConfigError, CredentialError, JiraError, ValueError, OSError) as exc:
        log.error("ERROR: %s", exc)
        return 2
    except sqlite3.OperationalError as exc:
        # Most often "database is locked": a manual run overlapping the scheduled one. Nothing is
        # damaged, and the state database is what prevents duplicates, so just say so plainly.
        log.error("ERROR: local state database problem: %s", exc)
        if "locked" in str(exc).lower():
            log.error("Another meeting2jira run is probably in progress. Wait for it to finish, "
                      "or check Task Scheduler for 'meeting2jira-daily', then try again.")
        return 2
    except sqlite3.DatabaseError as exc:
        log.error("ERROR: the local state database looks corrupt: %s", exc)
        log.error("It only caches which meetings were already pushed. If it cannot be repaired, "
                  "move %s aside; the next run will rebuild it, but meetings already in Jira may "
                  "be created a second time, so review before pushing.",
                  _data_dir(args) / "state.db")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
