#!/usr/bin/env python3
"""Compact unittest runner for AI agents (Kiro). Development support only; never shipped.

Runs the app/ test suite the same way the verify command in .kiro/steering/tech.md does (cwd app/,
app/src on PYTHONPATH, stdlib unittest), keeps the full output in .test-output/last-run.log, and
prints a short summary with failures grouped by root cause, so the model reads ~20 lines instead of
hundreds. Standard library only, so it also works on a workstation with no pip.

Usage (run from anywhere; paths are resolved against the repo):
    python tools/run_tests.py                          # full suite
    python tools/run_tests.py test_pipeline            # one module (also app/tests/test_pipeline.py)
    python tools/run_tests.py test_state.StateTests.test_reopen
    python tools/run_tests.py -k worklog               # unittest -k: substring or fnmatch pattern
    python tools/run_tests.py --lf                     # only the tests that failed last run
    python tools/run_tests.py -x                       # stop at the first failure
    python tools/run_tests.py --changed                # tests that cover git-changed files
    python tools/run_tests.py test_x --trace-lines 60  # more traceback for one failure

Exit code: 0 all passed, 1 failures/errors, 5 nothing ran.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import unittest
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent  # repo root
APP = ROOT / "app"
SRC = APP / "src"
TESTS = APP / "tests"
PACKAGE = SRC / "meeting2jira"

OUT_DIR = ROOT / ".test-output"
LOG_FILE = OUT_DIR / "last-run.log"
RESULT_FILE = OUT_DIR / "last-run.json"
SUMMARY_FILE = OUT_DIR / "last-summary.txt"

# Files that can't be mapped to tests by import. Keys are repo-relative globs, values are test
# module names. Static PowerShell rules live in test_guardrails; PS runtime behavior can't be
# tested here at all (see tools/Test-PowerShellSyntax.ps1 and tools/Invoke-WindowsChecks.ps1).
EXTRA_TEST_MAP: Dict[str, List[str]] = {
    "app/src/windows/*.ps1": ["test_guardrails"],
    "app/tools/*.ps1": ["test_guardrails"],
    "app/config.example.json": ["test_pipeline"],
    "app/tests/fixtures/*": ["test_pipeline", "test_recovery"],
}
# Changed files under these globs are never reported as "no test mapping".
UNMAPPED_IGNORE = ["tools/*", ".kiro/*", "infra/*", "playwright-app/*", "power-platform/*", "*.md"]

MAX_TESTS_PER_GROUP = 5
LOG_TAIL_ON_CRASH = 30

_HEX = re.compile(r"0x[0-9a-fA-F]+")
_TMP = re.compile(r"(?:/tmp/|[A-Za-z]:\\[^'\" ]*\\Temp\\)[^'\" ]+")
_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)')
_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_IMPORT = re.compile(r"^\s*(?:from\s+meeting2jira(?:\.(\w+))?\s+import\s+([\w\s,()]+)|"
                     r"import\s+meeting2jira\.(\w+))", re.M)


# --------------------------------------------------------------------------------------------
# Worker: runs inside a child process so test output and logging go to the log file.
# --------------------------------------------------------------------------------------------

class _BadName(unittest.TestCase):
    """Stands in for a test name that can't be loaded, so it's reported like an import error."""

    def __init__(self, name: str, exc: Exception) -> None:
        super().__init__("runTest")
        self._name, self._exc = name, exc

    def runTest(self) -> None:
        raise ImportError(f"cannot load test {self._name!r}: {self._exc}")

    def id(self) -> str:
        return f"unittest.loader._FailedTest.{self._name}"


def _rerun_id(test: unittest.TestCase) -> str:
    """A name loadTestsFromName accepts, for --lf. Subtests rerun their parent test."""
    inner = getattr(test, "test_case", test)
    tid = inner.id()
    if tid.startswith("unittest.loader._FailedTest."):
        return tid.rsplit(".", 1)[-1]  # import/collection error: rerun the whole module
    return tid


def worker(names: List[str], patterns: List[str], failfast: bool) -> int:
    os.chdir(APP)
    sys.path[:0] = [str(SRC), str(APP), str(TESTS)]
    loader = unittest.TestLoader()
    if patterns:
        loader.testNamePatterns = [p if "*" in p else f"*{p}*" for p in patterns]
    if names:
        suite = unittest.TestSuite()
        for name in names:
            try:
                suite.addTests(loader.loadTestsFromName(name))
            except Exception as exc:  # bad name: report it as a collection error, not a crash
                suite.addTest(_BadName(name, exc))
    else:
        suite = loader.discover("tests", top_level_dir=None)

    runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=2, failfast=failfast)
    result = runner.run(suite)

    bad = []
    for kind, items in (("failure", result.failures), ("error", result.errors)):
        for test, tb in items:
            collection = test.id().startswith("unittest.loader._FailedTest.")
            bad.append({"id": test.id(), "rerun": _rerun_id(test), "kind": kind,
                        "collection": collection, "tb": tb})
    for test in result.unexpectedSuccesses:
        bad.append({"id": test.id(), "rerun": _rerun_id(test), "kind": "unexpected success",
                    "collection": False, "tb": "AssertionError: unexpected success (expectedFailure)"})
    payload = {
        "run": result.testsRun,
        "skipped": len(result.skipped),
        "xfail": len(result.expectedFailures),
        "bad": bad,
    }
    RESULT_FILE.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    if result.testsRun == 0:
        return 5
    return 0 if result.wasSuccessful() else 1


# --------------------------------------------------------------------------------------------
# Target selection
# --------------------------------------------------------------------------------------------

def _normalize_target(arg: str) -> str:
    """Accept test_x, test_x.Class.test_m, app/tests/test_x.py, tests/test_x.py::Class::test_m."""
    arg = arg.replace("\\", "/")
    path_part, _, rest = arg.partition("::")
    if path_part.endswith(".py"):
        module = Path(path_part).stem
        return ".".join([module] + [p for p in rest.split("::") if p])
    return arg


def _git(*args: str) -> List[str]:
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _import_map() -> Dict[str, List[str]]:
    """meeting2jira module name -> test modules that import it."""
    result: Dict[str, List[str]] = {}
    for test_file in sorted(TESTS.glob("test_*.py")):
        text = test_file.read_text(encoding="utf-8", errors="replace")
        for m in _IMPORT.finditer(text):
            if m.group(1) or m.group(3):
                mods = [m.group(1) or m.group(3)]
            else:  # from meeting2jira import a, b
                mods = [n.strip() for n in re.split(r"[,()\s]+", m.group(2) or "") if n.strip()]
            for mod in mods:
                result.setdefault(mod, [])
                if test_file.stem not in result[mod]:
                    result[mod].append(test_file.stem)
    return result


def changed_targets() -> Tuple[List[str], List[str]]:
    """Map git-changed files to test modules. Returns (targets, unmapped_files)."""
    changed = set(_git("diff", "--name-only", "HEAD"))
    changed |= set(_git("ls-files", "--others", "--exclude-standard"))
    imports = _import_map()
    targets: List[str] = []
    unmapped: List[str] = []

    for rel in sorted(changed):
        rel = rel.replace("\\", "/")
        path = ROOT / rel
        hit: List[str] = []
        for pattern, mapped in EXTRA_TEST_MAP.items():
            if fnmatch.fnmatch(rel, pattern):
                hit.extend(mapped)
        if rel.startswith("app/tests/") and path.name.startswith("test_") and path.suffix == ".py":
            if path.exists():
                hit.append(path.stem)
        elif rel.startswith("app/src/meeting2jira/") and path.suffix == ".py":
            hit.extend(imports.get(path.stem, []))
            # __main__ and helpers are reached through the CLI; the pipeline test drives main().
            if not hit:
                hit.append("test_pipeline")
        if rel.startswith("app/src/"):
            hit.append("test_guardrails")  # cheap, and guards the non-negotiables
        if hit:
            targets.extend(hit)
        elif path.suffix in (".py", ".ps1", ".cmd") and not any(
            fnmatch.fnmatch(rel, pat) for pat in UNMAPPED_IGNORE
        ):
            unmapped.append(rel)
    return list(OrderedDict.fromkeys(targets)), unmapped


def last_failed() -> List[str]:
    try:
        data = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return list(OrderedDict.fromkeys(b["rerun"] for b in data.get("bad", [])))


# --------------------------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------------------------

def _strip_root(text: str) -> str:
    """Make absolute paths inside the repo relative; saves tokens on every line."""
    for prefix in {str(ROOT), ROOT.as_posix()}:
        text = text.replace(prefix + "\\", "").replace(prefix + "/", "").replace(prefix, ".")
    return text


def _signature(tb: str) -> Tuple[str, str]:
    """Return (exception line, deepest frame in our code) for a traceback."""
    lines = tb.rstrip().splitlines()
    last_frame = -1
    loc = ""
    for i, line in enumerate(lines):
        m = _FRAME.match(line)
        if m:
            last_frame = i
            path = Path(m.group("file"))
            try:
                rel = path.resolve().relative_to(ROOT).as_posix()
            except (ValueError, OSError):
                continue
            loc = f"{rel}:{m.group('line')}"
    err = ""
    for line in lines[last_frame + 1:]:
        if line and not line[0].isspace():
            err = line.strip()
            break
    if not err:
        err = lines[-1].strip() if lines else "(no message)"
    err = _TMP.sub("<tmp>", _HEX.sub("0x?", err))
    if len(err) > 160:
        err = err[:157] + "..."
    return err, loc


def _group_key(err: str, loc: str) -> Tuple[str, str]:
    """Failures with the same key share a root cause.

    Assertions: same failing line + same shape of message (numbers/strings ignored), so subTest
    cases of one bug collapse into one group. Other exceptions: same message wherever they
    surfaced, so e.g. one ImportError across many test modules is one group.
    """
    if err.startswith("AssertionError"):
        return ("A", loc + "|" + _NUM.sub("#", _QUOTED.sub("'?'", err)))
    return ("X", err)


def summarize(data: dict, args: argparse.Namespace, duration: float, rc: int, shown: str) -> str:
    bad = data.get("bad", [])
    failed = sum(1 for b in bad if b["kind"] != "error")
    errors = len(bad) - failed
    # testsRun counts test methods, but each failing subTest is its own entry in `bad`.
    broken = len({b["rerun"] for b in bad})
    passed = max(0, data["run"] - broken - data["skipped"] - data["xfail"])
    status = "PASS" if rc == 0 else ("NO TESTS" if rc == 5 else "FAIL")
    head = (f"RESULT: {status} | {passed} passed, {failed} failed, {errors} errors, "
            f"{data['skipped']} skipped | {duration:.1f}s")
    out = [head]
    if status == "PASS":
        return "\n".join(out)
    out.append(f"cmd: {shown}")
    out.append(f"full log: {LOG_FILE.relative_to(ROOT).as_posix()}")
    if status == "NO TESTS":
        out.append("No tests ran. Check the module name, -k pattern, or --changed mapping.")
        return "\n".join(out)

    groups: "OrderedDict[Tuple[str, str], dict]" = OrderedDict()
    for b in bad:
        tb = _strip_root(b["tb"])
        err, loc = _signature(tb)
        group = groups.setdefault(_group_key(err, loc),
                                  {"err": err, "loc": loc, "coll": b["collection"], "cases": []})
        group["cases"].append((b["id"], tb))
    if any(g["coll"] for g in groups.values()):
        out.append("NOTE: a test module failed to import. Fix that group first; it hides every "
                   "test in the module.")

    ordered = sorted(groups.values(), key=lambda g: (not g["coll"], -len(g["cases"])))
    for idx, group in enumerate(ordered[: args.max_groups], start=1):
        cases = group["cases"]
        out.append("")
        out.append(f"[{idx}] {len(cases)} test{'s' if len(cases) != 1 else ''} | {group['err']}")
        if group["loc"]:
            out.append(f"    at {group['loc']}")
        for tid, _ in cases[:MAX_TESTS_PER_GROUP]:
            out.append(f"    {tid}")
        if len(cases) > MAX_TESTS_PER_GROUP:
            out.append(f"    (+{len(cases) - MAX_TESTS_PER_GROUP} more with the same error)")
        trace = [ln for ln in cases[0][1].splitlines()
                 if ln.strip() and not ln.startswith("Traceback (most recent call last)")
                 and not re.match(r"^\s*[\^~]+\s*$", ln)]
        if trace and args.trace_lines > 0:
            out.append(f"    trace ({cases[0][0]}):")
            for ln in trace[-args.trace_lines:]:
                out.append("      " + ln.rstrip())
    hidden = len(ordered) - args.max_groups
    if hidden > 0:
        out.append("")
        out.append(f"(+{hidden} more group{'s' if hidden != 1 else ''} not shown; "
                   f"rerun with --max-groups {len(ordered)})")
    return "\n".join(out)


def _configure_stdout() -> None:
    # Windows consoles may not be UTF-8; never crash on an odd character.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _configure_stdout()
    parser = argparse.ArgumentParser(description="Compact unittest runner for app/tests.",
                                     allow_abbrev=False)
    parser.add_argument("targets", nargs="*", help="test modules, ids, or test file paths")
    parser.add_argument("-k", dest="patterns", action="append", default=[],
                        help="only tests whose name matches (unittest -k semantics)")
    parser.add_argument("-x", "--failfast", action="store_true", help="stop at the first failure")
    parser.add_argument("--lf", action="store_true", help="rerun only last run's failures")
    parser.add_argument("--changed", action="store_true",
                        help="run only tests that cover git-changed files")
    parser.add_argument("--max-groups", type=int, default=8,
                        help="max failure groups to print (default 8)")
    parser.add_argument("--trace-lines", type=int, default=12,
                        help="traceback lines shown per group (default 12, 0 to hide)")
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    targets = [_normalize_target(t) for t in args.targets]
    if args._worker:
        return worker(targets, args.patterns, args.failfast)

    notes: List[str] = []
    if args.lf:
        previous = last_failed()
        if not previous:
            print("RESULT: NOTHING TO RUN | no failures recorded from the last run")
            return 0
        targets += previous
    if args.changed:
        mapped, unmapped = changed_targets()
        if unmapped:
            notes.append("note: no test mapping for " + ", ".join(unmapped[:8])
                         + (" ..." if len(unmapped) > 8 else "")
                         + " (run the full suite before finishing)")
        if any(p.endswith((".ps1", ".cmd")) for p in _git("diff", "--name-only", "HEAD")):
            notes.append("note: PowerShell/batch changed; also run tools\\Test-PowerShellSyntax.ps1 "
                         "and say what still needs target-machine verification")
        if not mapped and not targets:
            print("RESULT: NOTHING TO RUN | no changed files map to tests")
            for n in notes:
                print(n)
            return 0
        targets += mapped
    targets = list(OrderedDict.fromkeys(targets))

    OUT_DIR.mkdir(exist_ok=True)
    try:
        RESULT_FILE.unlink()
    except FileNotFoundError:
        pass

    passthrough: List[str] = list(targets)
    for p in args.patterns:
        passthrough += ["-k", p]
    if args.failfast:
        passthrough.append("-x")
    cmd = [sys.executable, str(Path(__file__).resolve()), "--_worker", *passthrough]
    shown = "run_tests.py " + " ".join(passthrough) if passthrough else "run_tests.py (full suite)"

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(SRC)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    start = time.monotonic()
    with LOG_FILE.open("w", encoding="utf-8", errors="replace") as log:
        log.write("$ " + " ".join(cmd) + "\n\n")
        log.flush()
        proc = subprocess.run(cmd, cwd=APP, stdout=log, stderr=subprocess.STDOUT, env=env)
    duration = time.monotonic() - start
    rc = proc.returncode

    summary = ""
    try:
        data = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
        summary = summarize(data, args, duration, rc, shown)
    except (OSError, ValueError):
        pass
    if not summary:
        tail = _strip_root(LOG_FILE.read_text(encoding="utf-8", errors="replace")).splitlines()
        tail = [ln for ln in tail[1:] if ln.strip()][-LOG_TAIL_ON_CRASH:]
        summary = "\n".join([f"RESULT: ERROR | the test process exited {rc} before reporting "
                             f"results | {duration:.1f}s", f"cmd: {shown}", "log tail:"]
                            + ["  " + ln for ln in tail])
        rc = rc or 1

    if notes:
        summary += "\n" + "\n".join(notes)
    print(summary)
    try:
        SUMMARY_FILE.write_text(summary.splitlines()[0]
                                + f"  (at {time.strftime('%Y-%m-%d %H:%M')})\n", encoding="utf-8")
    except OSError:
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
