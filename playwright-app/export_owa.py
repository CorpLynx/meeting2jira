"""Export your own Outlook on the web calendar to meeting2jira's schema-v1 JSON.

For machines where classic Outlook (and therefore COM) is unavailable - that is, "new Outlook".
The output is consumed by the existing, unmodified pipeline:

    python export_owa.py --days-back 1 --out week.json
    cd ../app && PYTHONPATH=src python -m meeting2jira push --input ../playwright-app/week.json --dry-run

First run needs a visible sign-in:

    python export_owa.py --login

After that the session lives in the profile directory and runs are headless and unattended.

Exit codes match the main CLI so a scheduler can treat them the same way:
    0 ok        2 setup/session problem        130 interrupted
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from owa import capture, mapping

log = logging.getLogger("owa-export")

DEFAULT_PROFILE = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "meeting2jira" / "owa-profile"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="export_owa.py",
        description="Export your own Outlook on the web calendar to schema-v1 JSON.")
    p.add_argument("--days-back", type=int, default=1,
                   help="midnight this many days ago through now (default 1). Wider is safe: "
                        "re-runs cannot duplicate, because the pipeline dedupes.")
    p.add_argument("--out", help="output file (default: %%LOCALAPPDATA%%\\meeting2jira\\exports\\owa_<stamp>.json)")
    p.add_argument("--profile", default=str(DEFAULT_PROFILE),
                   help="browser profile directory holding the signed-in session")
    p.add_argument("--login", action="store_true",
                   help="open a visible browser to sign in, then save the session and exit")
    p.add_argument("--headed", action="store_true", help="show the browser (for diagnosis)")
    p.add_argument("--channel", default="msedge",
                   help="browser channel; msedge uses the installed Edge and avoids a download")
    p.add_argument("--endpoint",
                   help="skip discovery and use this calendar API URL (see --debug-endpoints)")
    p.add_argument("--debug-endpoints", action="store_true",
                   help="print every JSON request the calendar page makes, then exit")
    p.add_argument("--include-organizer", action="store_true",
                   help="include the organizer name (extra personal data; off by default)")
    p.add_argument("--timeout", type=int, default=60,
                   help="seconds to wait for the calendar request (default 60)")
    p.add_argument("--raw-out", help="also write the unmapped events, for debugging the mapping")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "meeting2jira"


def default_out_path() -> Path:
    from datetime import datetime
    return data_dir() / "exports" / f"owa_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


def write_last_export(ok: bool, detail: str, captured: int = 0, exported: int = 0) -> None:
    """Record the outcome of the export itself, separately from the push.

    Without this there is a silent-failure hole. The push writes last_run.json, but if the *export*
    fails the push never runs, so last_run.json keeps yesterday's success and `status` looks healthy.
    A scheduled run with an expired browser session would lose meetings for days before the staleness
    warning noticed. `meeting2jira-owa status` reads this first.

    Best effort: failing to write a breadcrumb must never change the exit code.
    """
    from datetime import datetime, timezone
    try:
        path = data_dir() / "last_export.json"
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


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s", stream=sys.stdout)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        message = ("Playwright is not installed. From this folder:\n"
                   "  pip install -r requirements.txt\n"
                   "  playwright install msedge   (or: playwright install chromium)")
        log.error(message)
        # Recorded too: a scheduled run on a machine whose install broke would otherwise leave no
        # trace anywhere, and `status` would keep reporting the last successful push.
        write_last_export(False, "playwright is not installed")
        return 2

    profile_dir = Path(args.profile)
    profile_dir.mkdir(parents=True, exist_ok=True)

    # A visible browser for sign-in and for diagnosis; headless for normal runs.
    headless = not (args.login or args.headed or args.debug_endpoints)

    try:
        with sync_playwright() as playwright:
            # Resource blocking speeds up an unattended run, but it must be off for interactive
            # sign-in or the login page renders as unstyled HTML for the person using it.
            context = capture.open_session(playwright, str(profile_dir), headless=headless,
                                           channel=args.channel, timeout_ms=args.timeout * 1000,
                                           block_resources=not (args.login or args.debug_endpoints))
            try:
                if args.login:
                    page = context.new_page()
                    page.goto(capture.CALENDAR_URL, wait_until="domcontentloaded")
                    log.info("Sign in (including MFA) in the browser window, wait for the calendar "
                             "to appear, then close the window.")
                    # Closing the browser is the signal that sign-in is finished.
                    page.wait_for_event("close", timeout=0)
                    log.info("Session saved to %s. Future runs are headless.", profile_dir)
                    return 0

                if args.debug_endpoints:
                    for line in capture.debug_endpoints(context, seconds=min(args.timeout, 30)):
                        log.info(line)
                    log.info("\nPass the events URL to --endpoint if discovery is not finding it.")
                    return 0

                start, end = mapping.window(args.days_back)
                log.info("Window: %s to %s", mapping.iso_utc(start), mapping.iso_utc(end))

                # Observation happens even when --endpoint is given. The URL can be overridden by
                # hand, but the auth headers cannot: a bearer token is only obtainable by watching
                # the page ask for one. An --endpoint with no Authorization header would fail with a
                # 401 and look like a broken URL, which made the documented escape hatch useless.
                seed: list = []
                endpoint = None
                try:
                    endpoint, seed = capture.observe_calendar_endpoint(
                        context, timeout_ms=args.timeout * 1000)
                except capture.CaptureError:
                    if not args.endpoint:
                        raise
                    log.warning("Discovery found nothing, but --endpoint was given: continuing with "
                                "cookie authentication only. If this returns 401, the endpoint needs "
                                "a bearer token that only discovery can capture.")

                if args.endpoint:
                    headers = dict(endpoint.headers) if endpoint else {}
                    if endpoint and endpoint.url.split("?")[0] != args.endpoint.split("?")[0]:
                        log.info("Overriding the discovered endpoint with --endpoint, keeping the "
                                 "%d observed header(s).", len(headers))
                    endpoint = capture.ObservedEndpoint(args.endpoint, "GET", headers)

                try:
                    events = capture.fetch_window(context, endpoint, start, end)
                except capture.CaptureError as exc:
                    # A POST-shaped endpoint cannot take our window, but discovery's events are
                    # still real data. Better a narrower export than none.
                    if not seed:
                        raise
                    log.warning("%s", exc)
                    log.warning("Using the %d event(s) captured during discovery instead.", len(seed))
                    events = seed

                if not events and seed:
                    log.warning("Direct call returned nothing; falling back to the %d event(s) seen "
                                "during discovery. The window may not have been applied.", len(seed))
                    events = seed
            finally:
                context.close()
    except capture.CaptureError as exc:
        log.error("ERROR: %s", exc)
        write_last_export(False, str(exc))
        return 2
    except KeyboardInterrupt:
        write_last_export(False, "interrupted")
        return 130

    if args.raw_out:
        Path(args.raw_out).write_text(json.dumps(events, indent=2), encoding="utf-8")
        log.info("Raw events -> %s", args.raw_out)

    skipped = []
    document = mapping.build_export(
        events, start, end, include_organizer=args.include_organizer,
        on_skip=lambda i, why: skipped.append(f"#{i}: {why}"))

    out_path = Path(args.out) if args.out else default_out_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8 without a BOM; the reader accepts either, but this keeps the file clean.
    out_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    write_last_export(True, f"exported {len(document['meetings'])} meeting(s)",
                      captured=len(events), exported=len(document["meetings"]))
    log.info("Captured %d event(s), exported %d meeting(s) -> %s",
             len(events), len(document["meetings"]), out_path)
    for line in skipped:
        log.warning("  skipped %s", line)
    log.info("\nNext: push it (dry run first)\n"
             "  cd ../app && PYTHONPATH=src python -m meeting2jira push --input %s --dry-run",
             out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
