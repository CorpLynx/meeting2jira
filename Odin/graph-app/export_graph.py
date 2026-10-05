"""Export your own calendar from Microsoft Graph to meeting2jira's schema-v1 JSON.

The intended primary calendar source: it works with classic Outlook and "new Outlook" alike, needs
no COM, no browser automation, and reads only documented APIs. The output is consumed by the
existing, unmodified pipeline:

    python export_graph.py --days-back 1 --out week.json
    cd ../app && PYTHONPATH=src python -m meeting2jira push --input ../graph-app/week.json --dry-run

First run:

    python export_graph.py --init          write graph.json, then edit in the client_id from IT
    python export_graph.py --login         sign in once; later runs are silent
    python export_graph.py --check         prove the token and the permission work

Exit codes match the main CLI so a scheduler can treat them the same way:
    0 ok        2 config/auth problem        130 interrupted
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from graph import auth, client as graph_client, config as cfgmod, mapping

log = logging.getLogger("graph-export")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="export_graph.py",
        description="Export your own calendar from Microsoft Graph to schema-v1 JSON.")
    p.add_argument("--days-back", type=int, default=1,
                   help="midnight this many days ago through now (default 1). Wider is safe: "
                        "re-runs cannot duplicate, because the pipeline dedupes.")
    p.add_argument("--out", help="output file (default: %%LOCALAPPDATA%%\\meeting2jira\\exports\\graph_<stamp>.json)")
    p.add_argument("--config", help="path to graph.json (default: in %%LOCALAPPDATA%%\\meeting2jira)")
    p.add_argument("--init", action="store_true",
                   help="write a starter graph.json and exit")
    p.add_argument("--login", action="store_true",
                   help="authenticate interactively and cache the token, then exit")
    p.add_argument("--check", action="store_true",
                   help="verify config, token and calendar permission, then exit")
    p.add_argument("--forget", action="store_true",
                   help="delete the cached Graph token and exit")
    p.add_argument("--include-organizer", action="store_true",
                   help="include the organizer name (extra personal data; off by default)")
    p.add_argument("--attendee-count", action="store_true",
                   help="request attendees so appointments can be told from meetings. Only the "
                        "count is used and no name is ever stored, but the names do cross the "
                        "network; off by default. Without it every item is treated as a meeting.")
    p.add_argument("--retention-days", type=int, default=7,
                   help="delete exports older than this many days (default 7, 0 disables). They "
                        "contain meeting subjects, so they should not pile up indefinitely.")
    p.add_argument("--raw-out", help="also write the unmapped events, for debugging the mapping")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def default_out_path() -> Path:
    return (cfgmod.data_dir() / "exports" /
            "graph_{}.json".format(datetime.now().strftime("%Y%m%d_%H%M%S")))


def prune_old_exports(retention_days: int, now=None) -> int:
    """Delete exports older than retention_days. Returns how many were removed.

    Exports hold calendar data. The entry point deletes them after a successful push, but a failed
    push keeps one for diagnosis and `export` keeps them on purpose, so without this they accumulate
    indefinitely. Shares the directory with the COM and OWA paths, so it prunes all three.

    Best effort: a file that cannot be removed must never stop an export.
    """
    if retention_days <= 0:
        return 0
    folder = cfgmod.data_dir() / "exports"
    if not folder.is_dir():
        return 0
    cutoff = (now or datetime.now()) - timedelta(days=retention_days)
    removed = 0
    for path in folder.glob("*.json"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:
            log.debug("Could not remove %s: %s", path, exc)
    if removed:
        log.info("Removed %d export(s) older than %d day(s).", removed, retention_days)
    return removed


def write_last_export(ok: bool, detail: str, captured: int = 0, exported: int = 0) -> None:
    """Record the outcome of the export itself, separately from the push.

    Without this there is a silent-failure hole: the push writes last_run.json, but if the *export*
    fails the push never runs, so last_run.json keeps yesterday's success and `status` looks
    healthy. A scheduled run whose token expired would lose meetings for days before the staleness
    warning noticed.

    Best effort: failing to write a breadcrumb must never change the exit code.
    """
    try:
        path = cfgmod.data_dir() / "last_export.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "finished_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ok": ok,
                "source": mapping.SOURCE,
                "captured": captured,
                "exported": exported,
                "detail": detail,
            }, fh, indent=2)
    except OSError:
        pass


def do_init(path: Path) -> int:
    if path.is_file():
        log.info("%s already exists; leaving it alone.", path)
        log.info("Edit it by hand, or delete it and re-run --init.")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cfgmod.example(), encoding="utf-8")
    log.info("Wrote %s", path)
    log.info("")
    log.info("Now fill in client_id, and set cloud if you are not in a commercial tenant.")
    log.info("Ask IT for: a public-client Entra app registration (no secret), redirect URI")
    log.info("http://localhost, delegated %s, and admin consent.", cfgmod.DEFAULT_SCOPE)
    log.info("Then:  meeting2jira-graph login")
    return 0


def do_check(cfg) -> int:
    """Prove the whole chain before trusting it on a schedule: token, identity, then a real read."""
    token = auth.acquire_token(cfg, interactive_ok=False)
    api = graph_client.GraphClient(cfgmod.graph_base(cfg), token,
                                   timeout=cfg["timeout_seconds"], ca_bundle=cfg["ca_bundle"])
    me = api.whoami()
    log.info("Signed in as %s <%s>",
             me.get("displayName") or "?", me.get("userPrincipalName") or me.get("mail") or "?")

    # A token that works for /me does not prove the calendar scope was consented, so read a tiny
    # window too. Yesterday, because an empty result is then still meaningful.
    start, end = mapping.window(1)
    events = api.calendar_view(start, end)
    log.info("Calendar read OK: %d event(s) in the last day.", len(events))
    log.info("Cloud: %s (%s)", cfg["cloud"], cfgmod.graph_base(cfg))
    log.info("Scope: %s", cfgmod.scopes(cfg)[0])
    log.info("Auth mode: %s", cfg["auth_mode"])
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s", stream=sys.stdout)

    config_file = Path(args.config) if args.config else cfgmod.config_path()

    if args.init:
        return do_init(config_file)

    if args.forget:
        log.info("Deleted the cached Graph token." if auth.forget()
                 else "No cached Graph token to delete.")
        return 0

    try:
        cfg = cfgmod.load(config_file)
    except cfgmod.GraphConfigError as exc:
        log.error("ERROR: %s", exc)
        return 2

    try:
        if args.login:
            auth.acquire_token(cfg, interactive_ok=True)
            log.info("Signed in. The token is cached; later runs are silent.")
            log.info("Next:  meeting2jira-graph check")
            return 0

        if args.check:
            return do_check(cfg)

        # Housekeeping first, so it still happens on a run that then fails to export.
        prune_old_exports(args.retention_days)

        start, end = mapping.window(args.days_back)
        log.info("Window: %s to %s", mapping.iso_utc(start), mapping.iso_utc(end))

        token = auth.acquire_token(cfg, interactive_ok=False)
        api = graph_client.GraphClient(cfgmod.graph_base(cfg), token,
                                       timeout=cfg["timeout_seconds"],
                                       ca_bundle=cfg["ca_bundle"])
        events = api.calendar_view(start, end, with_attendee_count=args.attendee_count)

    except auth.AuthError as exc:
        log.error("ERROR: %s", exc)
        write_last_export(False, "auth: {}".format(exc))
        return 2
    except graph_client.GraphError as exc:
        log.error("ERROR: %s", exc)
        write_last_export(False, "graph: {}".format(exc))
        return 2
    except KeyboardInterrupt:
        write_last_export(False, "interrupted")
        return 130

    if args.raw_out:
        Path(args.raw_out).write_text(json.dumps(events, indent=2), encoding="utf-8")
        log.info("Raw events -> %s", args.raw_out)

    if not args.attendee_count:
        # Said once, plainly, because the consequence is a filter quietly not applying rather than
        # anything visibly breaking.
        log.debug("attendees were not requested, so every item counts as a meeting. Pass "
                  "--attendee-count to distinguish personal appointments.")

    skipped = []
    document = mapping.build_export(
        events, start, end, include_organizer=args.include_organizer,
        on_skip=lambda i, why: skipped.append("#{}: {}".format(i, why)))

    out_path = Path(args.out) if args.out else default_out_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    write_last_export(True, "exported {} meeting(s)".format(len(document["meetings"])),
                      captured=len(events), exported=len(document["meetings"]))
    log.info("Captured %d event(s), exported %d meeting(s) -> %s",
             len(events), len(document["meetings"]), out_path)
    for line in skipped:
        log.warning("  skipped %s", line)
    log.info("")
    log.info("Next: push it (dry run first)")
    log.info("  cd ../app && PYTHONPATH=src python -m meeting2jira push --input %s --dry-run",
             out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
