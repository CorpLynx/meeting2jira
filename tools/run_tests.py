#!/usr/bin/env python3
"""Compact pytest runner for AI agents (Kiro). Development support only; never shipped.

Runs the app/ test suite through pytest (config in pyproject.toml), keeps the full output in
.test-output/last-run.log, and prints a short summary with failures grouped by root cause, so the
model reads ~20 lines instead of hundreds.

The tests themselves stay plain unittest so they still run on the workstation without pip; this
runner is the dev path. Needs `python -m pip install -r requirements-dev.txt`.

Usage (run from anywhere; anything it doesn't recognize is passed straight to pytest):
    python tools/run_tests.py                          # full suite
    python tools/run_tests.py test_pipeline            # one module (also app/tests/test_pipeline.py)
    python tools/run_tests.py test_state.StateTests.test_reopen     # unittest-style id works too
    python tools/run_tests.py "app/tests/test_state.py::StateTests::test_reopen"
    python tools/run_tests.py -k worklog               # pytest -k expression
    python tools/run_tests.py --lf                     # only last failures (subTests included)
    python tools/run_tests.py -x                       # stop at first failure
    python tools/run_tests.py --changed                # tests that cover git-changed files
    python tools/run_tests.py --cov                    # add a coverage report (pytest-cov)
    python tools/run_tests.py test_x --tb long --trace-lines 60

Exit code is pytest's exit code (0 pass, 1 failures, 5 no tests collected).
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent  # repo root
TESTS = ROOT / "app" / "tests"
TESTS_REL = "app/tests"

# Files that can't be mapped to tests by import. Keys are repo-relative globs, values are test
# files. Static PowerShell rules live in test_guardrails; PS runtime behavior can't be tested here
# at all (see app/tools/Test-PowerShellSyntax.ps1 and app/tools/Invoke-WindowsChecks.ps1).
EXTRA_TEST_MAP: Dict[str, List[str]] = {
    "app/src/windows/*.ps1": ["app/tests/test_guardrails.py"],
    "app/tools/*.ps1": ["app/tests/test_guardrails.py"],
    "app/config.example.json": ["app/tests/test_pipeline.py"],
    "app/tests/fixtures/*": ["app/tests/test_pipeline.py", "app/tests/test_recovery.py"],
}
# Changed files under these globs are never reported as "no test mapping".
UNMAPPED_IGNORE = ["tools/*", ".kiro/*", "infra/*", "playwright-app/*", "power-platform/*", "*.md"]

OUT_DIR = ROOT / ".test-output"
LOG_FILE = OUT_DIR / "last-run.log"
XML_FILE = OUT_DIR / "last-run.xml"
SUMMARY_FILE = OUT_DIR / "last-summary.txt"
# Our own last-failed list. pytest's --lf cache misses unittest subTest failures (pytest 9 records
# the parent test as passed), so --lf is implemented here from the junit XML instead.
FAILED_FILE = OUT_DIR / "last-failed.txt"

MAX_TESTS_PER_GROUP = 5
LOG_TAIL_ON_CRASH = 30

_HEX = re.compile(r"0x[0-9a-fA-F]+")
_TMP = re.compile(r"(?:/tmp/|[A-Za-z]:\\[^'\" ]*\\Temp\\)[^'\" ]+|pytest-of-[^\\/]+[\\/]pytest-\d+")
_LOCATION = re.compile(r"^(?P<loc>[^\s:][^:]*\.(?:py|ps1|psm1):\d+):?")
_CARETS = re.compile(r"^\s*[\^~]+\s*$")  # Python 3.11+ error-position markers: pure noise here
_IMPORT = re.compile(r"^\s*(?:from\s+meeting2jira(?:\.(\w+))?\s+import\s+([\w\s,()]+)|"
                     r"import\s+meeting2jira\.(\w+))", re.M)
_SUBFAILED = re.compile(r"^SUBFAILED\((?P<params>.*?)\) (?P<node>\S+)")
_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def _strip_root(text: str) -> str:
    """Make absolute paths inside the repo relative; saves tokens on every line."""
    for prefix in {str(ROOT), ROOT.as_posix()}:
        text = text.replace(prefix + "\\", "").replace(prefix + "/", "").replace(prefix, ".")
    return text


def _configure_stdout() -> None:
    # Windows consoles may not be UTF-8; never crash on an odd character.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


# --------------------------------------------------------------------------------------------
# Target selection
# --------------------------------------------------------------------------------------------

def normalize_target(arg: str) -> str:
    """Turn the short forms into pytest node ids; leave real paths and options alone.

    test_state                         -> app/tests/test_state.py
    test_state.StateTests.test_reopen  -> app/tests/test_state.py::StateTests::test_reopen
    tests/test_state.py::X             -> app/tests/test_state.py::X
    """
    if arg.startswith("-"):
        return arg
    fixed = arg.replace("\\", "/")
    path_part, sep, rest = fixed.partition("::")
    if path_part.endswith(".py"):
        if not (ROOT / path_part).exists() and (TESTS / Path(path_part).name).exists():
            path_part = f"{TESTS_REL}/{Path(path_part).name}"
        return path_part + sep + rest
    head, _, tail = fixed.partition(".")
    if re.fullmatch(r"test_\w+", head) and (TESTS / f"{head}.py").exists():
        return "::".join([f"{TESTS_REL}/{head}.py"] + [p for p in tail.split(".") if p])
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
    """meeting2jira module name -> test files that import it."""
    result: Dict[str, List[str]] = {}
    for test_file in sorted(TESTS.glob("test_*.py")):
        text = test_file.read_text(encoding="utf-8", errors="replace")
        target = f"{TESTS_REL}/{test_file.name}"
        for m in _IMPORT.finditer(text):
            if m.group(1) or m.group(3):
                mods = [m.group(1) or m.group(3)]
            else:  # from meeting2jira import a, b
                mods = [n.strip() for n in re.split(r"[,()\s]+", m.group(2) or "") if n.strip()]
            for mod in mods:
                result.setdefault(mod, [])
                if target not in result[mod]:
                    result[mod].append(target)
    return result


def changed_targets() -> Tuple[List[str], List[str]]:
    """Map git-changed files to test files. Returns (targets, unmapped_files)."""
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
        if rel.startswith(TESTS_REL + "/") and path.name.startswith("test_") and path.suffix == ".py":
            if path.exists():
                hit.append(rel)
        elif rel.startswith("app/src/meeting2jira/") and path.suffix == ".py":
            hit.extend(imports.get(path.stem, []))
            # __main__ and helpers are reached through the CLI; the pipeline test drives main().
            if not hit:
                hit.append(f"{TESTS_REL}/test_pipeline.py")
        if rel.startswith("app/src/"):
            hit.append(f"{TESTS_REL}/test_guardrails.py")  # cheap, and guards the non-negotiables
        if hit:
            targets.extend(hit)
        elif path.suffix in (".py", ".ps1", ".cmd") and not any(
            fnmatch.fnmatch(rel, pat) for pat in UNMAPPED_IGNORE
        ):
            unmapped.append(rel)
    return list(OrderedDict.fromkeys(targets)), unmapped


# --------------------------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------------------------

def _nodeid(case: ET.Element) -> str:
    name = case.get("name", "?")
    classname = case.get("classname", "") or ""
    file_attr = (case.get("file") or "").replace("\\", "/")
    if not classname:  # collection errors: name is the dotted module path
        as_path = name.replace(".", "/") + ".py"
        return as_path if (ROOT / as_path).exists() else name
    if file_attr:
        module = file_attr[:-3].replace("/", ".") if file_attr.endswith(".py") else file_attr
        cls = classname[len(module) + 1:] if classname.startswith(module + ".") else ""
        if not cls and "." in classname:  # module imported by basename (rootdir-relative path differs)
            cls = classname.rsplit(".", 1)[-1] if classname.split(".")[0].startswith("test_") else ""
        return "::".join(p for p in (file_attr, cls, name) if p)
    return f"{classname}::{name}"


def _group_key(err: str, loc: str) -> Tuple[str, str]:
    """Failures with the same key share a root cause.

    Assertions: same failing line + same shape of message (numbers/strings ignored), so subTest
    cases of one bug collapse into one group. Other exceptions: same message wherever they
    surfaced, so e.g. one ImportError across many test files is one group.
    """
    if err.startswith(("assert ", "AssertionError")):
        return ("A", loc + "|" + _NUM.sub("#", _QUOTED.sub("'?'", err)))
    return ("X", err)


def _signature(text: str, message: str) -> Tuple[str, str]:
    """Return (error line, deepest location) for a failure."""
    lines = text.splitlines()
    err = ""
    loc = ""
    for i, line in enumerate(lines):
        if line.startswith("E "):
            err = line[1:].strip()
            for back in range(i - 1, -1, -1):
                m = _LOCATION.match(lines[back].strip())
                if m:
                    loc = m.group("loc").replace("\\", "/")
                    break
            break
    if not err:
        err = message.strip().splitlines()[0] if message.strip() else "(no message)"
    err = _TMP.sub("<tmp>", _HEX.sub("0x?", err))
    if len(err) > 160:
        err = err[:157] + "..."
    return err, loc


def _subtest_labels() -> Dict[str, List[str]]:
    """node id -> subTest params, in order, from pytest's short summary in the log."""
    labels: Dict[str, List[str]] = {}
    for line in LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _SUBFAILED.match(line)
        if m:
            labels.setdefault(m.group("node"), []).append(m.group("params"))
    return labels


def summarize(xml_path: Path, args: argparse.Namespace, duration: float, rc: int, cmd: str) -> str:
    tree = ET.parse(xml_path)
    labels = _subtest_labels()
    failed_ids: List[str] = []
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    groups: "OrderedDict[Tuple[str, str], dict]" = OrderedDict()
    collection_errors = 0

    for case in tree.iter("testcase"):
        bads = case.findall("failure")
        kind = "failed"
        if not bads:
            bads = case.findall("error")
            kind = "errors"
        if not bads:
            if case.find("skipped") is not None:
                counts["skipped"] += 1
            else:
                counts["passed"] += 1
            continue
        counts[kind] += 1
        is_collection = not case.get("classname")
        collection_errors += is_collection
        node = _nodeid(case)
        failed_ids.append(node)
        params = labels.get(node, []) if len(bads) > 1 else []
        for i, bad in enumerate(bads):  # subTest failures put several entries on one testcase
            text = _strip_root(bad.text or "")
            err, loc = _signature(text, _strip_root(bad.get("message", "")))
            group = groups.setdefault(_group_key(err, loc),
                                      {"err": err, "loc": loc, "coll": is_collection, "cases": []})
            label = f"{node} [{params[i]}]" if i < len(params) else node
            group["cases"].append((label, text))

    if rc not in (2, 3, 4):  # an interrupted or broken run says nothing about what passed
        try:
            FAILED_FILE.write_text("\n".join(OrderedDict.fromkeys(failed_ids)) + "\n", encoding="utf-8")
        except OSError:
            pass

    if rc in (2, 3, 4):  # interrupted, internal error, usage error: the log says why
        return ""
    status = "PASS" if rc == 0 else ("NO TESTS" if rc == 5 else "FAIL")
    head = (f"RESULT: {status} | {counts['passed']} passed, {counts['failed']} failed, "
            f"{counts['errors']} errors, {counts['skipped']} skipped | {duration:.1f}s")
    out = [head]
    if status == "PASS":
        if args.cov:
            out += _coverage_lines()
        return "\n".join(out)

    out.append(f"cmd: {cmd}")
    out.append(f"full log: {LOG_FILE.relative_to(ROOT).as_posix()}")
    if status == "NO TESTS":
        out.append("No tests were collected. Check the path, -k expression or --changed mapping.")
        return "\n".join(out)
    if collection_errors:
        out.append(f"NOTE: {collection_errors} test file(s) failed to import. Fix those first; "
                   "they hide every test in the file.")

    ordered = sorted(groups.values(), key=lambda g: (not g["coll"], -len(g["cases"])))
    for idx, group in enumerate(ordered[: args.max_groups], start=1):
        err, loc, cases = group["err"], group["loc"], group["cases"]
        out.append("")
        out.append(f"[{idx}] {len(cases)} test{'s' if len(cases) != 1 else ''} | {err}")
        if loc:
            out.append(f"    at {loc}")
        for nodeid, _ in cases[:MAX_TESTS_PER_GROUP]:
            out.append(f"    {nodeid}")
        if len(cases) > MAX_TESTS_PER_GROUP:
            out.append(f"    (+{len(cases) - MAX_TESTS_PER_GROUP} more with the same error)")
        trace = [ln for ln in cases[0][1].splitlines() if ln.strip() and not _CARETS.match(ln)]
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


def _coverage_lines() -> List[str]:
    """The TOTAL line plus the least-covered modules, from the coverage table in the log."""
    rows = []
    total = ""
    for line in LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        pct = next((p for p in parts if re.fullmatch(r"\d+%", p)), None)
        if not parts or pct is None:
            continue
        if parts[0] == "TOTAL":
            total = pct
        elif parts[0].endswith(".py"):
            missing = " ".join(parts[parts.index(pct) + 1:])
            rows.append((int(pct.rstrip("%")), _strip_root(parts[0]), missing))
    out = [f"coverage: {total} total (branch); lowest:" if total
           else "coverage: no report (is pytest-cov installed?)"]
    for pct, name, missing in sorted(rows)[:5]:
        if len(missing) > 70:
            missing = missing[:67] + "..."
        out.append(f"  {pct:3d}%  {name}  missing {missing}" if missing else f"  {pct:3d}%  {name}")
    return out


def main() -> int:
    _configure_stdout()
    parser = argparse.ArgumentParser(
        description="Compact pytest runner. Unknown args go to pytest.", allow_abbrev=False)
    parser.add_argument("--changed", action="store_true",
                        help="run only tests that cover git-changed files")
    parser.add_argument("--lf", action="store_true", help="rerun only last run's failures")
    parser.add_argument("--cov", action="store_true", help="add a coverage summary (pytest-cov)")
    parser.add_argument("--tb", default="short", choices=["short", "long", "line", "native", "no"],
                        help="pytest traceback style (default: short)")
    parser.add_argument("--max-groups", type=int, default=8,
                        help="max failure groups to print (default 8)")
    parser.add_argument("--trace-lines", type=int, default=12,
                        help="trace lines shown per group (default 12, 0 to hide)")
    args, passthrough = parser.parse_known_args()

    if importlib.util.find_spec("pytest") is None:
        print("RESULT: ERROR | pytest is not installed for this Python\n"
              "fix: python -m pip install -r requirements-dev.txt\n"
              "(on a machine without pip, run the stdlib suite from app/ instead: "
              "python -m unittest discover -s tests)")
        return 4

    passthrough = [normalize_target(a) for a in passthrough]
    notes: List[str] = []
    if args.changed:
        targets, unmapped = changed_targets()
        if unmapped:
            notes.append("note: no test mapping for " + ", ".join(unmapped[:8])
                         + (" ..." if len(unmapped) > 8 else "")
                         + " (run the full suite before finishing)")
        if any(p.endswith((".ps1", ".cmd")) for p in _git("diff", "--name-only", "HEAD")):
            notes.append("note: PowerShell/batch changed; also run app/tools/Test-PowerShellSyntax.ps1 "
                         "and say what still needs target-machine verification")
        if not targets:
            print("RESULT: NOTHING TO RUN | no changed files map to tests")
            for n in notes:
                print(n)
            return 0
        passthrough = targets + passthrough
    if args.lf:
        try:
            previous = [ln for ln in FAILED_FILE.read_text(encoding="utf-8").splitlines() if ln]
        except OSError:
            previous = []
        previous = [n for n in previous if (ROOT / n.split("::", 1)[0]).exists()]  # files since deleted
        if not previous:
            print("RESULT: NOTHING TO RUN | no failures recorded from the last run")
            return 0
        passthrough = previous + passthrough
    if args.cov:
        passthrough += ["--cov", "--cov-report=term-missing:skip-covered"]

    OUT_DIR.mkdir(exist_ok=True)
    try:
        XML_FILE.unlink()
    except FileNotFoundError:
        pass

    cmd = [sys.executable, "-m", "pytest", *passthrough, f"--tb={args.tb}", "--color=no", "-q",
           f"--junitxml={XML_FILE}", "-o", "junit_family=xunit1"]
    shown = "pytest " + " ".join(passthrough) if passthrough else "pytest"

    start = time.monotonic()
    with LOG_FILE.open("w", encoding="utf-8", errors="replace") as log:
        log.write("$ " + " ".join(cmd) + "\n\n")
        log.flush()
        proc = subprocess.run(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    duration = time.monotonic() - start
    rc = proc.returncode

    summary = ""
    if XML_FILE.exists():
        try:
            summary = summarize(XML_FILE, args, duration, rc, shown)
        except ET.ParseError:
            summary = ""
    if not summary:
        tail = _strip_root(LOG_FILE.read_text(encoding="utf-8", errors="replace")).splitlines()
        tail = [ln for ln in tail[1:] if ln.strip()][-LOG_TAIL_ON_CRASH:]  # [1:] skips "$ cmd"
        summary = "\n".join([f"RESULT: ERROR | pytest exited {rc} before producing results | "
                             f"{duration:.1f}s", f"cmd: {shown}", "log tail:"]
                            + ["  " + ln for ln in tail])

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
