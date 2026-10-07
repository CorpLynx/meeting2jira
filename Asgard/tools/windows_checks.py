"""Asgard on Windows: the checks macOS and Linux can't run. Development tooling; never installed.

Run from an administrator prompt (the lab VM in ../../infra/windows-test-vm runs it as SYSTEM
over SSM; see run-checks.sh asgard):

    py -3 tools\\windows_checks.py            exit code 1 if any check fails

1. Facts: Python, its SQLite (3.37+ with FTS5 and JSON, which Muninn needs), Tcl/Tk, git, code page.
2. The unit tests in several Windows time zones, set with tzutil. Windows Python has no
   time.tzset, so the tests run in the machine's own zone, and every zone has to pass.
3. As the lab's standard (non-admin) account, through the m2j-as-user task the VM's bootstrap
   registers: setup-Asgard.cmd from a folder with spaces, Muninn created the way the launcher
   creates it, Baldur's installed CLI on the spec's worked example (repository in a folder with
   spaces, output captured as a pipe in code page 1252), `baldur.cmd schedule` and its removal,
   and uninstalling with the Settings > Apps quiet uninstall string.

The standard-account part runs this same file as `windows_checks.py as-user OUTDIR`.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
GIT_DIRS = (r"C:\Program Files\Git\cmd", r"C:\Program Files\Git\bin")
ZONES = ("UTC", "Eastern Standard Time", "India Standard Time", "Line Islands Standard Time",
         "Newfoundland Standard Time")
AS_USER_DIR = Path(r"C:\m2j\asuser")
AS_USER_TASK = "m2j-as-user"
DEMO_EMAIL = "brandon@agency.gov"

results: List[Dict[str, Any]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + (f" :: {detail}" if detail else ""), flush=True)
    return bool(ok)


def run(cmd: Any, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None, timeout: int = 900,
        stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a command; output decoded the way a Windows pipe carries it (the ANSI code page)."""
    done = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, input=(stdin or "").encode(),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    text = done.stdout.decode("mbcs" if os.name == "nt" else "utf-8", errors="replace")
    done.text = text.replace("\r\n", "\n")  # type: ignore[attr-defined]
    return done


def with_git(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The SSM agent started before git was installed, so its PATH doesn't have git yet."""
    env = dict(env or os.environ)
    extra = [d for d in GIT_DIRS if os.path.isdir(d) and d.lower() not in env.get("PATH", "").lower()]
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def tail(text: str, lines: int = 25) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


# --------------------------------------------------------------------------
# 1. Facts
# --------------------------------------------------------------------------

def facts() -> None:
    import sqlite3
    sys.path.insert(0, str(ROOT))
    from asgard import muninn
    print(f"Python {sys.version.split()[0]} at {sys.executable}; SQLite {sqlite3.sqlite_version}")
    check("Python is 3.9 or newer", sys.version_info >= (3, 9), sys.version.split()[0])
    problems = muninn.sqlite_problems()
    check("this Python's SQLite has what Muninn needs (3.37+, FTS5, JSON)", not problems, "; ".join(problems))
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        version = root.tk.call("info", "patchlevel")
        root.destroy()
        check("Tcl/Tk opens a window", True, f"Tk {version}")
    except Exception as exc:              # noqa: BLE001 - any failure is the finding
        check("Tcl/Tk opens a window", False, f"{type(exc).__name__}: {exc}")
    git = run(["git", "--version"], env=with_git())
    check("git is installed", git.returncode == 0, git.text.strip())
    chcp = run(["cmd.exe", "/d", "/c", "chcp"])
    print(f"Console code page: {chcp.text.strip()}; preferred encoding: {__import__('locale').getpreferredencoding()}")


# --------------------------------------------------------------------------
# 2. The suite in several time zones
# --------------------------------------------------------------------------

_RAN = re.compile(r"^Ran (\d+) tests? in", re.MULTILINE)


def current_zone() -> str:
    return run(["tzutil", "/g"]).text.strip()


def set_zone(zone: str) -> bool:
    return run(["tzutil", "/s", zone]).returncode == 0


def suite_in_zones(logs: Path) -> None:
    before = current_zone()
    try:
        for zone in ZONES:
            if not set_zone(zone):
                check(f"tests in {zone}", False, "tzutil couldn't set the zone")
                continue
            # A new process reads the new zone; this one keeps the zone it started with.
            seen = run([sys.executable, "-c", "import time; print(time.tzname[0], -time.timezone // 60)"]).text.strip()
            done = run([sys.executable, "-m", "unittest", "discover", "-v", "-s", "tests"], cwd=ROOT,
                       env=with_git(), timeout=1500)
            (logs / f"suite-{zone.replace(' ', '-')}.log").write_text(done.text, encoding="utf-8")
            ran = _RAN.search(done.text)
            summary = (ran.group(0).split(" in")[0] if ran else "no test count") + "; " + tail(done.text, 1)
            skips = sorted({m.group(1) for m in re.finditer(r"\.\.\. skipped '(.*)'$", done.text, re.MULTILINE)})
            if skips and zone == ZONES[0]:
                summary += "\n    skipped because: " + "\n    skipped because: ".join(skips)
            ok = done.returncode == 0 and ran is not None
            check(f"tests in {zone} ({seen})", ok, summary if ok else summary + "\n" + failures(done.text))
    finally:
        set_zone(before)


def failures(text: str) -> str:
    """The FAIL/ERROR headers and their last lines, so a failing zone says why without the whole log."""
    blocks = re.split(r"^={50,}$", text, flags=re.MULTILINE)
    out = []
    for b in blocks[1:]:
        lines = [x for x in b.strip().splitlines() if x.strip()]
        if lines:
            out.append(lines[0] + "\n    " + "\n    ".join(lines[-3:]))
    return "\n".join(out[:12])


# --------------------------------------------------------------------------
# 3. As the standard account
# --------------------------------------------------------------------------

def as_user(logs: Path) -> None:
    out = AS_USER_DIR / "result"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    script = (f'@echo off\r\n"{sys.executable}" "{Path(__file__).resolve()}" as-user "{out}" '
              f'> "{AS_USER_DIR / "out.txt"}" 2>&1\r\n')
    (AS_USER_DIR / "run.cmd").write_text(script, encoding="ascii")
    before = current_zone()
    set_zone("Eastern Standard Time")      # Brandon's zone, so local days and UTC differ
    try:
        started = run(["schtasks", "/Run", "/TN", AS_USER_TASK])
        if not check("the standard-account task starts", started.returncode == 0, started.text.strip()):
            return
        deadline = time.monotonic() + 1500
        done_file = out / "results.json"
        while time.monotonic() < deadline and not done_file.exists():
            time.sleep(5)
        transcript = (AS_USER_DIR / "out.txt").read_text(encoding="utf-8", errors="replace") \
            if (AS_USER_DIR / "out.txt").exists() else ""
        (logs / "as-user.log").write_text(transcript, encoding="utf-8")
        if not done_file.exists():
            check("the standard-account checks finished", False, tail(transcript, 40))
            return
        for r in json.loads(done_file.read_text(encoding="utf-8")):
            check("[standard user] " + r["name"], r["ok"], r["detail"])
    finally:
        set_zone(before)


def user_phase(out: Path) -> int:
    """Runs as the non-admin account. Every step records a result; nothing here raises past main()."""
    out.mkdir(parents=True, exist_ok=True)
    env = with_git()
    try:
        _user_steps(out, env)
    except Exception as exc:              # noqa: BLE001 - report it, don't lose the rest
        import traceback
        check("the standard-account checks ran to the end", False, traceback.format_exc()[-1500:])
        del exc
    (out / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


def _user_steps(out: Path, env: Dict[str, str]) -> None:
    groups = run(["whoami", "/groups"]).text
    check("this account isn't an administrator", "S-1-5-32-544" not in groups,
          run(["whoami"]).text.strip())

    # Install from an extracted zip in a folder with spaces, as Brandon would.
    home = Path(os.environ["USERPROFILE"])
    extracted = home / "Downloads" / "Asgard test copy" / "Asgard"
    if extracted.exists():
        shutil.rmtree(extracted)
    shutil.copytree(ROOT, extracted, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    setup = run(["cmd.exe", "/d", "/c", "setup-Asgard.cmd", "--no-launch"], cwd=extracted, stdin="\r\n")
    (out / "setup.log").write_text(setup.text, encoding="utf-8")
    local = Path(os.environ["LOCALAPPDATA"]) / "Asgard"
    app = local / "app"
    check("setup-Asgard.cmd installs without admin rights", setup.returncode == 0 and (app / "Asgard.pyw").exists(),
          tail(setup.text, 12))
    lnk = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Asgard.lnk"
    check("a Start menu shortcut", lnk.exists(), str(lnk))
    key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\Asgard"
    reg = run(["reg", "query", key, "/v", "QuietUninstallString"])
    quiet = next((line.split("REG_SZ", 1)[1].strip() for line in reg.text.splitlines() if "REG_SZ" in line), "")
    check("listed in Settings > Apps", reg.returncode == 0 and bool(quiet), quiet or reg.text.strip())

    # Muninn, created the way the launcher does at start.
    made = run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); from asgard import muninn; "
                "st = muninn.prepare(); print(st.version, st.path)", str(app)])
    check("Muninn is created at schema v2", made.returncode == 0 and made.text.strip().startswith("2 "),
          made.text.strip()[-400:])

    day = _demo_repo(home / "Lab Repos" / "asgard demo", env)
    _demo_calendar(app, day)

    baldur = app / "apps" / "baldur"
    repos = home / "Lab Repos"
    steps = [("setup", f'setup --email {DEMO_EMAIL} --project PROJ --root "{repos}"'),
             ("collect", "collect"),
             ("estimate", f"estimate --from {day} --to {day} --report"),
             ("days", f"days --from {day - dt.timedelta(days=2)} --to {day + dt.timedelta(days=1)}"),
             ("approve", f"approve --date {day}"),
             ("report", f"report {day}")]
    texts: Dict[str, str] = {}
    for name, args in steps:
        # chcp 1252 and a pipe: the redirected output a scheduled task or a log file gets.
        done = run(f'cmd.exe /d /s /c "chcp 1252 >nul && baldur.cmd {args}"', cwd=baldur, env=env)
        texts[name] = done.text
        (out / f"baldur-{name}.log").write_text(done.text, encoding="utf-8")
        check(f"baldur.cmd {name}", done.returncode == 0 and "Traceback" not in done.text, tail(done.text, 8))
    report = texts.get("report", "")
    line42 = next((x for x in report.splitlines() if x.strip().startswith("PROJ-42")), "")
    line51 = next((x for x in report.splitlines() if x.strip().startswith("PROJ-51")), "")
    check("the worked example: PROJ-42 1h30m and PROJ-51 30m, approved",
          "1h30m" in line42 and "approved 1h30m" in line42 and " 30m" in line51 and "approved 30m" in line51,
          f"{line42.strip()} | {line51.strip()}")
    check("sessions 09:20-12:25 and 14:00-15:05", "09:20-12:25" in report and "14:00-15:05" in report,
          tail(report, 20))

    # The weekly collection task: created as this user, interactive-only, then removed.
    made = run('cmd.exe /d /s /c "baldur.cmd schedule --day MON --time 09:00"', cwd=baldur, env=env)
    (out / "schedule.log").write_text(made.text, encoding="utf-8")
    check("baldur.cmd schedule adds the weekly task", made.returncode == 0, tail(made.text, 6))
    query = run(["schtasks", "/Query", "/TN", "Asgard Baldur collect", "/V", "/FO", "LIST"])
    (out / "schedule-query.log").write_text(query.text, encoding="utf-8")
    fields = {k.strip(): v.strip() for k, _, v in (line.partition(":") for line in query.text.splitlines()) if v}
    check("the task runs collect --quiet with pythonw, only while you're signed in",
          query.returncode == 0 and "pythonw.exe" in fields.get("Task To Run", "")
          and "collect --quiet" in fields.get("Task To Run", "")
          and "Interactive only" in fields.get("Logon Mode", ""),
          "; ".join(f"{k}: {fields.get(k, '?')}" for k in ("Task To Run", "Logon Mode", "Run As User", "Days")))
    gone = run('cmd.exe /d /s /c "baldur.cmd schedule --remove"', cwd=baldur, env=env)
    still = run(["schtasks", "/Query", "/TN", "Asgard Baldur collect"])
    check("baldur.cmd schedule --remove deletes it", gone.returncode == 0 and still.returncode != 0,
          tail(gone.text, 4))

    # The GitHub token in Credential Manager, as this user: saved, read back, removed.
    code = ("import sys; sys.path[:0] = [sys.argv[1], sys.argv[1] + '\\\\apps\\\\baldur']; "
            "from baldur import github as g; h = 'github.lab.invalid'; g.save_token(h, 'ghp_lab_check'); "
            "print(g.load_token(h) == 'ghp_lab_check', g.delete_token(h), g.load_token(h))")
    plain = {k: v for k, v in env.items() if k != "BALDUR_GITHUB_TOKEN"}
    cred = run([sys.executable, "-c", code, str(app)], env=plain)
    check("the GitHub token is saved in, read from and removed from Credential Manager",
          cred.returncode == 0 and cred.text.strip() == "True True None", cred.text.strip()[-400:])

    # Uninstall the way Settings > Apps does it quietly, then check nothing is left.
    if quiet:
        undo = run(f"{quiet} --purge", cwd=home)
        (out / "uninstall.log").write_text(undo.text, encoding="utf-8")
        left = [str(p) for p in (app, lnk) if p.exists()]
        reg_left = run(["reg", "query", key]).returncode == 0
        check("the quiet uninstall removes the app, the shortcut and the Settings entry",
              undo.returncode == 0 and not left and not reg_left,
              tail(undo.text, 8) + (f"\nleft: {left}" if left else "") + ("\nregistry key left" if reg_left else ""))
        check("--purge deletes your data too, leaving no Asgard folder", not local.exists(),
              ", ".join(p.name for p in local.iterdir()) if local.exists() else "")


def _demo_repo(repo: Path, env: Dict[str, str]) -> dt.date:
    """The spec's worked example as real commits and checkouts, three days ago in local time."""
    if repo.exists():
        shutil.rmtree(repo, onerror=lambda f, p, _: (os.chmod(p, 0o700), f(p)))
    repo.mkdir(parents=True)
    day = dt.date.today() - dt.timedelta(days=3)
    before = day - dt.timedelta(days=1)

    def git(*args: str, at: Optional[str] = None, who: Optional[tuple] = None) -> None:
        e = dict(env)
        if at:
            e.update(GIT_AUTHOR_DATE=at, GIT_COMMITTER_DATE=at)
        if who:
            e.update(GIT_AUTHOR_NAME=who[0], GIT_AUTHOR_EMAIL=who[1])
        done = run(["git", "-C", str(repo), *args], env=e)
        if done.returncode:
            raise RuntimeError(f"git {' '.join(args)}: {done.text}")

    n = [0]

    def commit(hhmm: str, message: str) -> None:
        n[0] += 1
        (repo / f"f{n[0]}.txt").write_text(f"{n[0]}\n")
        git("add", f"f{n[0]}.txt")
        git("commit", "-q", "-m", message, at=f"{day}T{hhmm}:00")

    git("init", "-q")
    git("symbolic-ref", "HEAD", "refs/heads/main")
    git("config", "user.email", DEMO_EMAIL)
    git("config", "user.name", "Brandon")
    git("config", "commit.gpgsign", "false")
    (repo / "a.txt").write_text("a\n")
    git("add", "a.txt")
    git("commit", "-q", "-m", "initial", at=f"{before}T15:00:00", who=("Sam", "sam@agency.gov"))
    git("checkout", "-q", "-b", "feature/PROJ-42-retry", at=f"{before}T15:30:00")
    git("checkout", "-q", "main", at=f"{before}T15:31:00")
    git("checkout", "-q", "-b", "feature/PROJ-51-x", at=f"{before}T15:32:00")
    git("checkout", "-q", "feature/PROJ-42-retry", at=f"{before}T15:33:00")
    for hhmm, msg in (("09:50", "retry on 503"), ("10:20", "backoff"), ("10:55", "jitter"), ("11:40", "tests")):
        commit(hhmm, msg)
    git("checkout", "-q", "feature/PROJ-51-x", at=f"{day}T12:05:00")
    commit("12:10", "form layout")
    commit("12:25", "form validation")
    git("checkout", "-q", "feature/PROJ-42-retry", at=f"{day}T12:27:00")
    commit("14:30", "retry logging")
    commit("15:05", "docs")
    return day


def _demo_calendar(app: Path, day: dt.date) -> None:
    """Meetings 09:00-12:00 and 12:30-16:30 local, as Odin's calendar sync would store them."""
    code = (
        "import sys, datetime as dt; sys.path.insert(0, sys.argv[1]); from asgard import muninn; "
        "from asgard.muninn import odin; d = dt.date.fromisoformat(sys.argv[2]); "
        "z = lambda h, m: muninn.to_ts(dt.datetime(d.year, d.month, d.day, h, m).astimezone()); "
        "con = muninn.connect(); src = muninn.ensure_source(con, 'calendar', 'outlook'); "
        "run = muninn.Run(con, 'odin', src, 'calendar')\n"
        "with run:\n"
        "    for i, (a, b) in enumerate([((9, 0), (12, 0)), ((12, 30), (16, 30))]):\n"
        "        odin.upsert_calendar_event(run, {'external_id': 'm%d' % i, 'title': 'Meeting %d' % i, "
        "'starts_at': z(*a), 'ends_at': z(*b), 'is_all_day': 0, 'show_as': 'busy', 'response': 'accepted', "
        "'is_cancelled': 0})\n")
    done = run([sys.executable, "-c", code, str(app), day.isoformat()])
    check("Odin-style calendar events stored", done.returncode == 0, done.text.strip()[-600:])


# --------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    if argv[:1] == ["as-user"]:
        return user_phase(Path(argv[1]))
    if os.name != "nt":
        print("These checks are for Windows.")
        return 2
    logs = Path(os.environ.get("TEMP", ".")) / "asgard-windows-checks"
    logs.mkdir(parents=True, exist_ok=True)
    facts()
    if "--no-suite" not in argv:
        suite_in_zones(logs)
    if "--no-user" not in argv:
        as_user(logs)
    fails = [r for r in results if not r["ok"]]
    print(f"\n{len(results)} checks, {len(fails)} failed. Logs: {logs}")
    (logs / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
