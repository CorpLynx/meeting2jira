"""Make Asgard's packaged build with PyInstaller, and check it: the steps the GitHub workflow runs.

    py -3.12 packaging\\build.py                 Windows (the build Brandon ships)
    python3 packaging/build.py                  macOS or Linux: a build for that system, to test the steps

    --skip-tests     don't run the unit tests first (the checks on the built folder still run)
    --no-venv        install into this Python instead of a fresh build/venv (CI's runner, say)
    --keep-venv      reuse build/venv as it is: faster when you rebuild after changing Asgard's code

Steps, each stopping the build if it fails:
  1. A fresh virtual environment, build/venv, with the pinned packages: requirements.txt without
     Playwright (asgard.spec says why), and packaging/requirements-build.txt. Wheels only.
  2. The unit tests, with those packages.
  3. PyInstaller, with packaging/asgard.spec: build/dist/Asgard/.
  4. Checks on the built folder, run through its own programs in a temporary ASGARD_HOME: the
     self-test, Muninn created and checked, Baldur from setup to a day's estimate (when git is
     here), Ysildir's server, Heimdall, setup copying the build into ASGARD_HOME\\app and its
     self-test there, a script from outside refused, and that running wrote nothing into the
     folder.
  5. payload.sha256 inside the folder (docs/updates.md), then build/Asgard-VERSION-PLATFORM.zip
     and its .sha256.

Standard library only: it runs before anything is installed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from typing import Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # Asgard/
BUILD = ROOT / "build"
DIST = BUILD / "dist"
BUNDLE = DIST / "Asgard"
LEFT_OUT = {"playwright"}                            # see asgard.spec
MIN_PYTHON = (3, 11)                                 # Muninn's SQLite on Windows; Ysildir needs 3.10+
EXE = ".exe" if os.name == "nt" else ""
PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s#;]+)")


class BuildError(Exception):
    """A step failed; the message says which and what to do."""


def say(text: str) -> None:
    print(text, flush=True)


def step(title: str) -> None:
    say(f"\n== {title}")


def run(argv: Sequence[str], *, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None,
        expect: int = 0, quiet: bool = False) -> subprocess.CompletedProcess:
    """Run a command; stop the build if its exit code isn't the expected one."""
    shown = " ".join(f'"{a}"' if " " in str(a) else str(a) for a in argv)
    say(f"   $ {shown}")
    done = subprocess.run([str(a) for a in argv], cwd=str(cwd) if cwd else None, env=env,
                          capture_output=quiet, text=True, encoding="utf-8", errors="replace")
    if done.returncode != expect:
        output = (done.stdout or "") + (done.stderr or "") if quiet else ""
        raise BuildError(f"{shown}\n   exited {done.returncode}, expected {expect}.\n{output}".rstrip())
    return done


def version() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def platform_tag() -> str:
    """windows-x64, macos-arm64, linux-x64: what the zip's name says it runs on."""
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    machine = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(machine, machine)
    return f"{system}-{machine}"


def runtime_pins(requirements: Path = ROOT / "requirements.txt") -> List[str]:
    """requirements.txt's pins, as name==version, without the packages the build leaves out."""
    pins = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        found = PIN.match(line)
        if found and found.group(1).lower() not in LEFT_OUT:
            pins.append(f"{found.group(1)}=={found.group(2)}")
    return pins


def venv_python(folder: Path) -> Path:
    return folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def prepare_python(no_venv: bool, keep_venv: bool) -> Path:
    step("1. Python and the pinned packages")
    if sys.version_info < MIN_PYTHON:
        raise BuildError(f"Build with Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer (3.12 is what the workflow "
                         f"uses); this is {platform.python_version()}.")
    if no_venv:
        python = Path(sys.executable)
    else:
        folder = BUILD / "venv"
        python = venv_python(folder)
        if keep_venv and python.exists():
            say(f"   Reusing {folder}")
            return python
        if folder.exists():
            shutil.rmtree(folder)
        say(f"   Creating {folder}")
        venv.EnvBuilder(with_pip=True, clear=True).create(folder)
    BUILD.mkdir(parents=True, exist_ok=True)
    frozen = BUILD / "requirements-frozen.txt"
    frozen.write_text("\n".join(runtime_pins()) + "\n", encoding="utf-8")
    run([python, "-m", "pip", "install", "--disable-pip-version-check", "--only-binary", ":all:",
         "-r", frozen, "-r", HERE / "requirements-build.txt"])
    return python


def unit_tests(python: Path) -> None:
    step("2. Unit tests")
    run([python, "-m", "unittest", "discover", "-s", "tests"], cwd=ROOT)


def freeze(python: Path) -> None:
    step("3. PyInstaller")
    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    run([python, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
         "--distpath", DIST, "--workpath", BUILD / "work", HERE / "asgard.spec"], cwd=ROOT)
    for name in ("Asgard" + EXE, "asgard-cli" + EXE, "Asgard.pyw", "asgard/launcher.py", "apps/baldur/cli.py"):
        if not (BUNDLE / name).exists():
            raise BuildError(f"The build has no {name}; check packaging/asgard.spec.")


def tree_hashes(folder: Path) -> Dict[str, str]:
    """{relative path with / : sha256} for every file under folder, except payload.sha256 itself."""
    out = {}
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        rel = path.relative_to(folder).as_posix()
        if rel != "payload.sha256":
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _git_day(repo: Path, env: Dict[str, str]) -> str:
    """A tiny repository with work on PROJ-42 two days ago; returns that day."""
    day = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    repo.mkdir(parents=True)

    def git(*args: str, when: str = "") -> None:
        extra = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when} if when else {}
        run(["git", *args], cwd=repo, env=dict(env, **extra), quiet=True)

    git("init", "-q", ".")
    git("symbolic-ref", "HEAD", "refs/heads/main")
    git("config", "user.email", "smoke@agency.gov")
    git("config", "user.name", "Smoke")
    for n, (time, subject) in enumerate([("09:00", "initial"), ("10:00", "retry on 503"), ("11:30", "backoff")]):
        if n == 1:
            git("checkout", "-q", "-b", "feature/PROJ-42-retry", when=f"{day}T09:30:00")
        (repo / f"f{n}.txt").write_text(subject, encoding="utf-8")
        git("add", f"f{n}.txt")
        git("commit", "-q", "-m", subject, when=f"{day}T{time}:00")
    return day


def loads_qt(python: Path) -> bool:
    """Whether the build's own Python can load Qt; a headless Linux machine often can't."""
    if os.name == "nt":
        return True             # never skip the check where the build ships
    done = subprocess.run([str(python), "-c", "import PySide6.QtGui, PySide6.QtQml"], capture_output=True)
    return done.returncode == 0


def smoke(python: Path, bundle: Path = BUNDLE) -> None:
    step("4. Checks on the built folder")
    cli, windowed = bundle / ("asgard-cli" + EXE), bundle / ("Asgard" + EXE)
    before = tree_hashes(bundle)
    with tempfile.TemporaryDirectory(prefix="asgard-build-") as tmp:
        home = Path(tmp) / "home"
        env = dict(os.environ, ASGARD_HOME=str(home), GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL=str(Path(tmp) / "gitconfig"), PYTHONIOENCODING="utf-8")
        env.pop("PYTHONPATH", None)                      # the build must stand on its own
        (Path(tmp) / "gitconfig").write_text("", encoding="utf-8")

        def check(*args: object, expect: int = 0, contains: str = "") -> None:
            done = run([cli, *args], cwd=Path(tmp), env=env, expect=expect, quiet=True)
            text = (done.stdout or "") + (done.stderr or "")
            if contains and contains not in text:
                raise BuildError(f"Expected {contains!r} in the output of {args}:\n{text}")
            say(f"     ok: {text.strip().splitlines()[-1] if text.strip() else 'exit ' + str(done.returncode)}")

        skip = [] if loads_qt(python) else ["Qt"]
        if skip:
            say("     note: this machine can't load Qt even outside the build (a headless system without its "
                "graphics libraries), so the self-test leaves Qt out. Windows builds always check it.")
        check("--self-test", *(["--skip", ",".join(skip)] if skip else []), contains="All parts load.")
        check(bundle / "Asgard.pyw", "--muninn", "prepare", contains="Muninn is ready")
        check(bundle / "Asgard.pyw", "--muninn", "check", contains="healthy")
        run([windowed, "--muninn", "status"], cwd=Path(tmp), env=env, quiet=True)
        say("     ok: Asgard (windowed) ran --muninn status")
        baldur = bundle / "apps" / "baldur" / "cli.py"
        check(baldur, "--help", contains="usage")
        if shutil.which("git"):
            day = _git_day(Path(tmp) / "src" / "repo", env)
            check(baldur, "setup", "--email", "smoke@agency.gov", "--project", "PROJ", "--root", Path(tmp) / "src")
            check(baldur, "collect")
            check(baldur, "estimate", "--from", day, "--to", day)
            check(baldur, "days", "--from", day, "--to", day, contains="PROJ-42")
        else:
            say("     skipped: Baldur from setup to estimate (git isn't on PATH)")
        check(bundle / "apps" / "ysildir" / "cli.py", "check", contains="baldur_record_estimate")
        check(bundle / "apps" / "heimdall" / "cli.py", "--help", contains="usage")
        if os.name != "nt" or os.environ.get("CI"):
            env["APPDATA"] = str(Path(tmp) / "appdata")      # shortcuts go to the temporary folder
            check(bundle / "asgard" / "install.py", "--no-launch", contains="Asgard is installed")
            # Setup copied the build into the data folder; the installed copy must work on its own.
            installed = home / "app" / ("asgard-cli" + EXE)
            done = run([installed, "--self-test", *(["--skip", ",".join(skip)] if skip else [])],
                       cwd=Path(tmp), env=env, quiet=True)
            # (Its Apps check confirms Asgard loads from that copy's own files; paths aren't compared,
            # because Windows may name the same temporary folder in its short 8.3 form.)
            if "All parts load." not in done.stdout:
                raise BuildError(f"The installed copy's self-test didn't pass from {home / 'app'}:\n{done.stdout}")
            say(f"     ok: setup copied the build to {home / 'app'}, and its self-test passes there")
        else:   # setup writes Asgard's Settings > Apps entry, which would replace your real one
            say("     skipped: setup (Windows, outside CI: it would replace your Settings > Apps entry)")
        outside = Path(tmp) / "outside.py"
        outside.write_text("print('ran')\n", encoding="utf-8")
        check(outside, expect=2, contains="isn't part of Asgard")
    changed = sorted(k for k, v in tree_hashes(bundle).items() if before.get(k) != v)
    if changed:
        raise BuildError("Running the build wrote into its own folder, which may be read-only on the workstation: "
                         + ", ".join(changed[:10]))
    say("     ok: running wrote nothing into the build's folder")


def package(bundle: Path = BUNDLE) -> Path:
    step("5. payload.sha256 and the zip")
    hashes = tree_hashes(bundle)
    (bundle / "payload.sha256").write_text("".join(f"{h}  {p}\n" for p, h in hashes.items()), encoding="utf-8")
    name = f"Asgard-{version()}-{platform_tag()}"
    archive = Path(shutil.make_archive(str(BUILD / name), "zip", root_dir=str(DIST), base_dir="Asgard"))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    size = archive.stat().st_size / 1e6
    say(f"   {len(hashes)} files; {archive} ({size:.0f} MB)\n   sha256 {digest}")
    return archive


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="build.py", description="Make and check Asgard's packaged build.",
                                     allow_abbrev=False)
    parser.add_argument("--skip-tests", action="store_true", help="don't run the unit tests first")
    parser.add_argument("--no-venv", action="store_true", help="install into this Python, not build/venv")
    parser.add_argument("--keep-venv", action="store_true", help="reuse build/venv without reinstalling")
    args = parser.parse_args(argv)
    say(f"Asgard {version()} packaged build, {platform_tag()}, Python {platform.python_version()}")
    try:
        python = prepare_python(args.no_venv, args.keep_venv)
        if not args.skip_tests:
            unit_tests(python)
        freeze(python)
        smoke(python)
        package()
    except BuildError as exc:
        say(f"\nBuild failed: {exc}")
        return 1
    say("\nDone. Install it: run setup-Asgard.cmd in build/dist/Asgard (or in the unzipped zip). Setup copies it to "
        "%LOCALAPPDATA%\\Asgard\\app.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
