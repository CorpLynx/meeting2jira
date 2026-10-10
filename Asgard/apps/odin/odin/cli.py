"""Odin from the command line:  odin <command>   (apps\\odin\\odin.cmd, or python apps\\odin\\cli.py)

Position in the flow
    Wiring, not logic. Parses arguments, sets up logging, loads config, opens Muninn, builds a Jira
    client, and calls the pieces in order. The decisions live in rules.py, sync.py, collect.py and
    posting.py; Muninn's half is asgard.muninn.odin and store.py.

    The command names, flags and exit codes are a compatibility surface: Invoke-MeetingSync.ps1,
    the scheduled task and Asgard's window depend on them.

Exit codes
    0    fine
    1    the run finished but some items failed (a meeting, an approved day Jira refused), or a check
         reported FAIL
    2    configuration, usage, credential or Muninn problem; also another Odin run in progress
    130  interrupted

A daily run, in order (each write its own short Muninn transaction; none held across a Jira call)
    1. Records Muninn couldn't take last time (the journal), then state.db's history, once.
    2. Posts an earlier run never heard back about: found in Jira by their marker, or offered again.
    3. Your calendar into Muninn, then meeting worklogs an earlier run couldn't log.
    4. Meetings to sub-tasks (sync.py).
    5. Jira into Muninn: your issues, tracked parents, keys other apps mention, your worklogs.
    6. Days you approved in Baldur that Jira is missing, once the worklog sync has finished.
    7. last_run.json, and the Desktop alert raised or taken down.

Operational breadcrumb
    Every real run writes last_run.json with the time, counts, exit code and first error, including
    a run that stopped early (an expired token, Muninn not ready). `status` and Test-Environment.ps1
    read it, so a scheduled task that quietly started failing is visible without opening a log.

Commands
  init        create %LOCALAPPDATA%\\Asgard\\odin\\config.json from config.example.json
  set-token   store your Jira PAT, DPAPI-encrypted for your Windows user
  check       verify config, token, Muninn, Jira reachability, parent issues, and sub-task type
  daily       steps 1-7 from an export (--input JSON or --csv); --dry-run to preview
  push        steps 1-4 and 7 only: meetings to sub-tasks
  sync        steps 1-2, 5 and 7: Jira into Muninn
  post        steps 1-2, the key lookups and worklog sync, then 6 and 7; --dry-run to list
  status      the last run, then recent sub-tasks and what waits to be posted
  forget      drop a sub-task's record so its meeting can be pushed again
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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

if sys.version_info < (3, 9):  # pragma: no cover
    sys.exit("Odin needs Python 3.9 or newer (3.11 or newer for Muninn on Windows).")

from asgard import muninn  # noqa: E402
from asgard.muninn import odin as mo  # noqa: E402

from . import __version__, collect, history, posting, store  # noqa: E402
from .config import ConfigError, default_config_path, default_data_dir, load_config  # noqa: E402
from .credstore import CredentialError, load_token, save_token  # noqa: E402
from .jira import JiraClient, JiraError  # noqa: E402
from .models import iso_utc, parse_utc  # noqa: E402
from .rules import Router  # noqa: E402
from .sources import load_outlook_csv, read_export  # noqa: E402
from .sync import RunResult, run  # noqa: E402

log = logging.getLogger("odin")
# apps/odin: this file is apps/odin/odin/cli.py.
APP_ROOT = Path(__file__).resolve().parents[1]
TASK_NAME = "Asgard Odin daily"


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
        fh = logging.handlers.RotatingFileHandler(logs / "odin.log", maxBytes=1_000_000,
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


@dataclass
class Outcome:
    """Everything one run did, for the summary, last_run.json and the alert."""
    command: str
    source: Optional[str] = None
    push: Optional[RunResult] = None
    retries: posting.PostResult = field(default_factory=posting.PostResult)
    settle: posting.PostResult = field(default_factory=posting.PostResult)
    collected: Optional[collect.CollectResult] = None
    posts: Optional[posting.PostResult] = None
    imported: Optional[history.ImportResult] = None
    calendar: Optional[store.CalendarResult] = None
    replayed: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def all_errors(self) -> List[str]:
        out = list(self.errors)
        if self.push:
            out += self.push.errors
        if self.posts:
            out += [f"approved day refused by Jira: {line}" for line in self.posts.failed]
        return out

    def all_warnings(self) -> List[str]:
        out = list(self.warnings)
        if self.push:
            out += self.push.warnings
        out += [f"meeting worklog: {line}" for line in self.retries.failed + self.retries.unknown]
        if self.settle.unsettled:
            out.append(f"{self.settle.unsettled} interrupted post(s) couldn't be checked; checked next run")
        if self.collected:
            out += self.collected.problems
        if self.posts:
            out += [f"approved day: {line}" for line in self.posts.unknown]
        if self.imported:
            out += [f"state.db: {line}" for line in self.imported.failed]
        if self.calendar:
            out += [f"calendar: {line}" for line in self.calendar.problems]
        return out


def _meetings(args: argparse.Namespace, cfg: Dict[str, Any]):
    """The export's meetings, its source name, the window it read in full, and a label for the log."""
    if getattr(args, "input", None):
        meetings, info = read_export(args.input)
        return meetings, str(info.get("source") or "unknown"), store.export_window(info), args.input
    meetings = load_outlook_csv(args.csv, cfg["csv"]["datetime_formats"])
    return meetings, "outlook-csv", None, args.csv


def _run(args: argparse.Namespace, command: str, meetings_wanted: bool, collect_wanted: bool,
         post_wanted: bool) -> int:
    cfg = load_config(args.config)
    data_dir = Path(cfg["data_dir"])
    if getattr(args, "max", None) is not None:
        cfg["jira"]["max_creates_per_run"] = args.max
    if getattr(args, "max_posts", None) is not None:
        cfg["muninn"]["max_posts_per_run"] = args.max_posts
    dry_run = bool(getattr(args, "dry_run", False))

    meetings, export_source, window, label = [], None, None, None
    if meetings_wanted:
        meetings, export_source, window, label = _meetings(args, cfg)
        log.info("Loaded %d calendar item(s) from %s%s", len(meetings), label,
                 "  [DRY RUN - nothing will be created]" if dry_run else "")

    outcome = Outcome(command, source=label)
    try:
        con = store.open_muninn(readonly=dry_run)
    except muninn.MuninnError as exc:
        if not dry_run:
            _record_failure(cfg, data_dir, outcome, exc)
        raise
    try:
        if dry_run:
            return _preview(cfg, con, data_dir, meetings, meetings_wanted, post_wanted)
        with store.RunLock(data_dir):
            try:
                _live(cfg, con, data_dir, outcome, meetings, export_source, window, meetings_wanted,
                      collect_wanted, post_wanted)
            except (JiraError, CredentialError, muninn.MuninnError, history.HistoryError, sqlite3.DatabaseError) as exc:
                _record_failure(cfg, data_dir, outcome, exc)
                raise
    finally:
        con.close()
    return _finish(cfg, data_dir, outcome)


def _live(cfg: Dict[str, Any], con: sqlite3.Connection, data_dir: Path, out: Outcome, meetings: list,
          export_source: Optional[str], window: Any, meetings_wanted: bool, collect_wanted: bool,
          post_wanted: bool) -> None:
    j, settings = cfg["jira"], cfg["muninn"]
    token, _ = load_token(data_dir)
    client = JiraClient.from_config(j, token)
    _warn_token_expiry(client, j)
    me = client.myself()
    jira_sid = store.jira_source(con, j["base_url"])
    store.remember_me(con, jira_sid, me)

    # 1. What only Odin knew: the journal, then state.db, once.
    out.replayed = store.Journal(data_dir).replay(con)
    if out.replayed:
        log.info("Recorded %d sub-task(s) an earlier run kept in its journal.", out.replayed)
    out.imported = history.import_state_db(con, data_dir)
    legacy = history.Legacy.open(data_dir)        # only if some rows couldn't move
    ctx = collect.context(con, client, jira_sid)

    # 2. Posts an earlier run never heard back about.
    out.settle = posting.settle_stuck(con, client, float(j.get("timeout_seconds") or 30))

    # 3 and 4. The calendar, then meetings to sub-tasks.
    if meetings_wanted:
        calendar_sid = store.calendar_source(con, export_source or "unknown")
        event_ids, out.calendar = store.store_calendar(con, calendar_sid, meetings, window)
        if j.get("log_work"):
            posting.retry_meetings(con, client, jira_sid, ctx,
                                   lambda run_, key, ctx_: collect.collect_issue(run_, key, ctx_, client), out.retries)
        out.push = run(meetings, cfg, con, client, data_dir=data_dir, event_ids=event_ids, ctx=ctx, me=me,
                       seen_before=legacy.find if legacy else None)
        out.push.worklogs_retried = len(out.retries.posted)

    # 5. Jira into Muninn. Posting needs the keys resolved and the worklogs current.
    if collect_wanted or post_wanted:
        got = out.collected = collect.CollectResult()
        days, limit = int(settings["history_days"]), int(settings["max_issues_per_run"])
        if collect_wanted and settings["sync_issues"]:
            collect.sync_issues(con, client, ctx, sorted(Router(cfg).parents()), days, limit, got)
            collect.refresh_open(con, client, ctx, got)
            collect.check_unseen(con, client, ctx, got)
        if (collect_wanted and settings["sync_issues"]) or post_wanted:
            collect.lookup_keys(con, client, ctx, got)
        if (collect_wanted and settings["sync_worklogs"]) or post_wanted:
            collect.sync_worklogs(con, client, ctx, days, limit, got)
            _classify_old_meeting_worklogs(con, cfg)

    # 6. Approved Baldur days, only once Muninn knows what Jira already holds.
    if post_wanted and (out.command == "post" or settings["post_approved"]):
        if out.collected is not None and out.collected.worklogs_ok:
            out.posts = posting.post_approved(con, client, int(settings["max_posts_per_run"]))
        else:
            out.errors.append("Approved Baldur days weren't posted: the worklog sync didn't finish, so Odin can't "
                              "tell what Jira already holds. Fix what the sync reported; the days wait for the next run.")


def _classify_old_meeting_worklogs(con: sqlite3.Connection, cfg: Dict[str, Any]) -> None:
    """Mark meeting worklogs Odin wrote before Muninn as meeting time, so they never count as development
    time. Strict: same start, same length, a comment like the template's, and one match each way."""
    prefix = str(cfg["templates"].get("worklog_comment") or "").split("{", 1)[0]
    if len(prefix.strip()) < 3 or "%" in prefix or "_" in prefix:
        return
    try:
        adopted = mo.classify_meeting_worklogs(con, prefix + "%")
    except (ValueError, sqlite3.DatabaseError, muninn.MuninnError) as exc:
        log.debug("Meeting worklogs from before Muninn weren't classified: %s", exc)
        return
    if adopted:
        log.info("Marked %d meeting worklog(s) from before Muninn as meeting time.", adopted)


def _warn_token_expiry(client: JiraClient, j: Dict[str, Any]) -> None:
    # One extra GET, and only a nicety: a token that expires unnoticed turns into a run of 401s.
    # Any failure here is swallowed, because this must never be why a sync does not happen.
    try:
        warning = token_expiry_warning(client.personal_access_tokens(), int(j.get("warn_token_expiry_days") or 0))
        if warning:
            log.warning("TOKEN: %s", warning)
    except (JiraError, ValueError, TypeError, AttributeError) as exc:
        # AttributeError included on purpose: a stubbed or older client without this method
        # must not be able to stop a sync over an advisory warning.
        log.debug("Token expiry check skipped: %s", exc)


def _preview(cfg: Dict[str, Any], con: sqlite3.Connection, data_dir: Path, meetings: list,
             meetings_wanted: bool, post_wanted: bool) -> int:
    """A dry run: reads Muninn (and state.db, if it hasn't moved in yet), writes nothing anywhere."""
    legacy = history.Legacy.open(data_dir)
    if legacy is not None:
        log.info("state.db holds %d sub-task record(s); the first real run moves them into Muninn.", len(legacy))
    errors = 0
    if meetings_wanted:
        result = run(meetings, cfg, con, None, data_dir=data_dir, dry_run=True,
                     seen_before=legacy.find if legacy else None)
        skipped = sum(result.skipped.values())
        log.info("\nWould create %d, already synced %d, skipped %d.", result.planned, result.existing, skipped)
        for reason, count in result.skipped.most_common():
            log.debug("  skipped %3d  %s", count, reason)
        errors += len(result.errors)
    if post_wanted:
        posts = posting.post_approved(con, None, int(cfg["muninn"]["max_posts_per_run"]), dry_run=True)
        log.info("Would post %d approved Baldur day(s), going by Muninn as of the last sync.", len(posts.planned))
    return 1 if errors else 0


def _summarize(out: Outcome) -> None:
    if out.push is not None:
        r = out.push
        skipped = sum(r.skipped.values())
        log.info("\nCreated %d, already synced %d, skipped %d, errors %d, warnings %d.",
                 len(r.created), r.existing, skipped, len(r.errors), len(r.warnings))
        if r.recovered:
            log.info("Recovered %d sub-task(s) that a failed create had already made: %s",
                     len(r.recovered), ", ".join(r.recovered))
        if r.worklogs_retried:
            log.info("Logged work for %d sub-task(s) left over from an earlier run.", r.worklogs_retried)
        for reason, count in r.skipped.most_common():
            log.debug("  skipped %3d  %s", count, reason)
    if out.imported and out.imported.owed:
        log.info("state.db had %d meeting worklog(s) that never reached Jira; Odin tries them for 14 days after "
                 "their sub-task was made: %s", len(out.imported.owed), ", ".join(out.imported.owed[:10]))
    if out.settle.settled:
        log.info("Settled %d post(s) an earlier run never heard back about.", out.settle.settled)
    if out.collected is not None:
        c = out.collected
        log.info("Jira into Muninn: %d issue(s) (%d changed), %d key(s) looked up, %d worklog(s) stored, "
                 "%d removed, %d issue(s) deleted in Jira.", c.issues, c.changed, c.looked_up, c.worklogs,
                 c.worklogs_removed, c.deleted)
    if out.posts is not None:
        log.info("Approved Baldur days: posted %d, refused %d, unsure %d.",
                 len(out.posts.posted), len(out.posts.failed), len(out.posts.unknown))
    for line in out.all_warnings():
        log.warning("WARNING: %s", line)


def _finish(cfg: Dict[str, Any], data_dir: Path, out: Outcome) -> int:
    _summarize(out)
    errors = out.all_errors()
    exit_code = 1 if errors else 0
    r = out.push or RunResult()
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
        "command": out.command,
        "source": out.source,
        "created": len(r.created),
        "recovered": len(r.recovered),
        "existing": r.existing,
        "skipped": sum(r.skipped.values()),
        "errors": len(errors),
        "warnings": len(out.all_warnings()),
        "worklogs_retried": r.worklogs_retried,
        "worklogs_logged": len(r.posts.posted) + len(out.retries.posted),
        "approved_posted": len(out.posts.posted) if out.posts else 0,
        "issues_synced": out.collected.issues if out.collected else 0,
        "consecutive_failures": streak,
        "first_error": errors[0] if errors else None,
    })
    # Push the outcome into view, or take the notice down now that it is working again.
    if exit_code == 0:
        clear_alert(data_dir)
    else:
        write_alert(cfg, data_dir, summary=f"The last Odin run ({out.command}) finished with {len(errors)} error(s).",
                    detail=errors[0], consecutive_failures=streak)
    return exit_code


def _record_failure(cfg: Dict[str, Any], data_dir: Path, out: Outcome, exc: BaseException) -> None:
    """A run that stopped early (an expired token, Muninn not ready) still leaves its breadcrumb and alert."""
    previous = _read_last_run(data_dir) or {}
    streak = int(previous.get("consecutive_failures") or 0) + 1
    _write_last_run(data_dir, {
        "finished_utc": iso_utc(datetime.now(timezone.utc)), "exit_code": 2, "command": out.command,
        "source": out.source, "created": 0, "recovered": 0, "existing": 0, "skipped": 0, "errors": 1,
        "warnings": 0, "consecutive_failures": streak, "first_error": str(exc)[:1000],
    })
    write_alert(cfg, data_dir, summary=f"The last Odin run ({out.command}) stopped before it finished.",
                detail=str(exc), consecutive_failures=streak)


def cmd_daily(args: argparse.Namespace) -> int:
    return _run(args, "daily", meetings_wanted=True, collect_wanted=True, post_wanted=True)


def cmd_push(args: argparse.Namespace) -> int:
    return _run(args, "push", meetings_wanted=True, collect_wanted=False, post_wanted=False)


def cmd_sync(args: argparse.Namespace) -> int:
    return _run(args, "sync", meetings_wanted=False, collect_wanted=True, post_wanted=False)


def cmd_post(args: argparse.Namespace) -> int:
    return _run(args, "post", meetings_wanted=False, collect_wanted=False, post_wanted=True)


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

    try:
        con = store.open_muninn(readonly=True)
        try:
            report("OK", f"Muninn version {muninn.user_version(con)} at {muninn.default_path()}")
        finally:
            con.close()
    except muninn.MuninnError as exc:
        report("FAIL", f"Muninn: {exc}")

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


LAST_RUN_FILE = "last_run.json"
ALERT_FILE = "ATTENTION-Odin.txt"
OLD_ALERT_FILE = "ATTENTION-meeting2jira.txt"   # from before Odin moved into Asgard; cleared too
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
        "Odin needs attention\n"
        "====================\n\n"
        f"{summary}\n\n"
        f"Consecutive failed runs: {consecutive_failures}\n"
        f"Last attempt (UTC):      {iso_utc(datetime.now(timezone.utc))}\n\n"
        "Details\n-------\n"
        f"{detail}\n\n"
        "What to do\n----------\n"
        "  1. Open a Command Prompt in Odin's folder (apps\\odin in Asgard's)\n"
        "  2. Run:  odin check\n"
        "     That reports the specific problem: an expired token, a moved parent issue,\n"
        "     a proxy intercepting the API, or a certificate that is not trusted.\n"
        "  3. Fix what it names, then:  odin\n\n"
        "Meetings are not lost. Nothing has been pushed twice either: re-running is safe,\n"
        "because already-synced meetings and posted worklogs are recognised and skipped.\n\n"
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
        subprocess.run(["msg.exe", "*", f"Odin: {summary}"],
                       timeout=10, capture_output=True, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("msg.exe unavailable: %s", exc)


def clear_alert(data_dir: Path) -> None:
    """Remove the alert once a run succeeds, so a stale file never causes a false alarm."""
    for folder in (d for d in (_desktop_dir(), data_dir) if d):
        for name in (ALERT_FILE, OLD_ALERT_FILE):
            path = folder / name
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
    log.info("Last run (%s) %s at %s: created %s, existing %s, skipped %s, errors %s.",
             last.get("command", "push"), verdict, finished, last.get("created"), last.get("existing"),
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
    legacy = history.Legacy.open(data_dir)
    if legacy is not None:
        log.info("state.db still holds %d sub-task record(s); the next real run moves them into Muninn.", len(legacy))
    try:
        con = store.open_muninn(readonly=True)
    except muninn.MuninnError as exc:
        log.info("Muninn: %s", exc)
        return 0
    try:
        rows = store.recent(con, args.limit)
        due = mo.posts_due(con)
        stuck = mo.stuck_posts(con, older_than_seconds=0)
        waiting = store.pending_meeting_worklogs(con)
        unpostable = con.execute("SELECT count(*) FROM v_unpostable_days").fetchone()[0]
    finally:
        con.close()
    if not rows:
        log.info("No meeting sub-tasks yet.")
    shown = {"posted": "logged", "sending": "sending", "failed": "refused"}
    for row in rows:
        state = shown.get(row["worklog_state"] or "", "wanted" if row["worklog_wanted"] else "-")
        log.info("%-12s %s  %4dm  worklog=%-7s  %s", row["issue_key"], row["started_at"], row["minutes"],
                 state, row["summary"])
    log.info("")
    log.info("Approved Baldur days waiting to be posted: %d%s", len(due),
             f" (and {unpostable} Jira can't take)" if unpostable else "")
    if waiting:
        log.info("Meeting worklogs to retry: %d", len(waiting))
    if stuck:
        log.info("Posts waiting for an answer from Jira: %d (settled at the start of the next run)", len(stuck))
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args)
    con = store.open_muninn()
    try:
        removed = store.forget(con, args.issue_key)
    finally:
        con.close()
    removed += history.forget_legacy(data_dir, args.issue_key)
    log.info("Removed %d record(s) for %s. Its meeting is pushed again on the next run; time already logged "
             "for that meeting isn't logged a second time.", removed, args.issue_key)
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


def _source_args(p: argparse.ArgumentParser) -> None:
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", help="JSON written by Export-OutlookMeetings.ps1 (or the Graph or OWA exporter)")
    src.add_argument("--csv", help="Outlook calendar CSV export")
    p.add_argument("--max", type=_positive_int,
                   help="override jira.max_creates_per_run for this run (must be 1 or more)")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help=f"config file (default: {default_config_path()})")
    common.add_argument("-v", "--verbose", action="store_true", help="more detail on the console")

    parser = argparse.ArgumentParser(prog="odin", description="Odin: meetings to Jira sub-tasks, and Jira into Muninn.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", parents=[common], help="create a config file from the example")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    sub.add_parser("set-token", parents=[common], help="store Jira PAT (DPAPI)").set_defaults(func=cmd_set_token)
    sub.add_parser("check", parents=[common], help="verify config, Muninn and Jira access").set_defaults(func=cmd_check)

    p = sub.add_parser("daily", parents=[common], help="the scheduled run: meetings, Jira into Muninn, approved days")
    _source_args(p)
    p.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    p.add_argument("--max-posts", type=_positive_int, help="override muninn.max_posts_per_run for this run")
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("push", parents=[common], help="create sub-tasks from an export")
    _source_args(p)
    p.add_argument("--dry-run", action="store_true", help="show what would happen; create nothing")
    p.set_defaults(func=cmd_push)

    sub.add_parser("sync", parents=[common], help="read your issues and worklogs from Jira into Muninn") \
        .set_defaults(func=cmd_sync)

    p = sub.add_parser("post", parents=[common], help="post the days you approved in Baldur that Jira is missing")
    p.add_argument("--dry-run", action="store_true", help="list what would be posted; post nothing")
    p.add_argument("--max-posts", type=_positive_int, help="override muninn.max_posts_per_run for this run")
    p.set_defaults(func=cmd_post)

    p = sub.add_parser("status", parents=[common], help="the last run, recent sub-tasks, what waits to be posted")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("forget", parents=[common], help="drop a sub-task's record so its meeting is pushed again")
    p.add_argument("issue_key")
    p.set_defaults(func=cmd_forget)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(_data_dir(args), args.verbose)
    try:
        return args.func(args)
    except store.StoreError as exc:
        log.error("ERROR: %s", exc)
        return 2
    except muninn.MuninnError as exc:
        log.error("ERROR: Muninn: %s", exc)
        return 2
    except history.HistoryError as exc:
        log.error("ERROR: %s", exc)
        return 2
    except (ConfigError, CredentialError, JiraError, ValueError, OSError) as exc:
        log.error("ERROR: %s", exc)
        return 2
    except sqlite3.DatabaseError as exc:
        log.error("ERROR: Muninn problem: %s", exc)
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            log.error("Another Asgard app is writing to Muninn. Wait a moment, then try again.")
        else:
            log.error("Open Asgard and run Muninn's check (Asgard.pyw --muninn check) to see what's wrong.")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
