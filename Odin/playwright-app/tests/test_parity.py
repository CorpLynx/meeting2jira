"""Keep this exporter's entry point in step with Odin's (Asgard/apps/odin/odin.cmd) and graph-app's.

The dependency runs one way: the exporters know about Odin, never the reverse. So these checks
live here rather than in Asgard's tests.

Two things are asserted:

1. The command surface covers Odin's, so muscle memory transfers and documentation does not
   quietly become wrong.
2. The Python discovery block matches graph-app's. That logic was rewritten after it failed on a
   real agency install, and a copy that drifts back to the brittle version would fail the same
   way - except only on this path, which is harder to notice. (Odin's own discovery differs on
   purpose since it moved into Asgard: it looks for Asgard's Python, one that can run Muninn;
   the exporters need one with their own packages.)

Skips cleanly when Odin is not present, so this folder remains usable on its own.
"""
import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
OWA_CMD = HERE / "meeting2jira-owa.cmd"
APP_CMD = HERE.parent.parent / "Asgard" / "apps" / "odin" / "odin.cmd"
GRAPH_CMD = HERE.parent / "graph-app" / "meeting2jira-graph.cmd"

# Outlook-specific, so the OWA entry point is not expected to offer them.
NOT_APPLICABLE = {
    "run",       # Invoke-MeetingSync.ps1 passthrough; the OWA path has its own exporter
}


def actions(path: Path):
    """Every `if /i "%ACTION%"=="x"` dispatch target in a batch entry point."""
    text = path.read_text(encoding="utf-8")
    found = set(re.findall(r'if /i "%ACTION%"=="([a-z0-9-]+)"', text))
    return {a for a in found if a not in ("-h", "--help", "/?")}


def discovery_logic(path: Path):
    """The Python-discovery logic, with prose stripped so wording differences don't trip this.

    Only the lines that decide *which* interpreters are considered and how each is proved are
    compared: the candidate loops and the :trypython body. Echo text legitimately differs, since
    the two entry points tell the user to run different commands.
    """
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^:resolvepython\b(.*?)^:usage\b", text, re.DOTALL | re.MULTILINE)
    if not match:
        raise AssertionError(f"{path.name}: no :resolvepython block found")

    kept = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        if low.startswith("rem ") or low == "echo." or low == "echo":
            continue
        # Drop user-facing messages, but NOT `echo ... | find ...`, which is how the batch version
        # tests a string. An earlier revision of this filter discarded that line, which made the
        # drift comparison blind to the Store-alias guard - the exact thing it is meant to protect.
        if low.startswith("echo ") and "|" not in stripped and "&&" not in stripped:
            continue
        kept.append(re.sub(r"\s+", " ", stripped))
    return kept


@unittest.skipUnless(APP_CMD.is_file(), f"Odin not found at {APP_CMD}")
class EntryPointParityTests(unittest.TestCase):
    def test_command_surface_covers_the_com_app(self):
        app_actions = actions(APP_CMD) - NOT_APPLICABLE
        owa_actions = actions(OWA_CMD)
        missing = sorted(app_actions - owa_actions)
        self.assertEqual(missing, [],
                         f"meeting2jira-owa.cmd is missing commands Odin has: {missing}")

    def test_owa_specific_commands_exist(self):
        """The three things this path needs that the COM path does not."""
        owa_actions = actions(OWA_CMD)
        for expected in ("login", "discover", "export"):
            self.assertIn(expected, owa_actions)

    @unittest.skipUnless(GRAPH_CMD.is_file(), f"graph-app not found at {GRAPH_CMD}")
    def test_python_discovery_matches_graph_app(self):
        """The fix for brittle discovery must not exist in only one of the two exporters."""
        self.assertEqual(discovery_logic(OWA_CMD), discovery_logic(GRAPH_CMD))

    def test_it_hands_off_to_odins_daily_run(self):
        text = OWA_CMD.read_text(encoding="utf-8")
        self.assertIn('cli daily --input "%EXPORTFILE%"', text)
        self.assertNotIn("cli push", text)

    def test_discovery_proves_candidates_by_running_them(self):
        """Guards the specific regression: accepting a candidate because it merely exists.

        `where py.exe` succeeding does not mean `py -3` works - the launcher is routinely installed
        with no 3.x registered. Every candidate must go through :trypython, which executes it.
        """
        logic = discovery_logic(OWA_CMD)
        candidate_lines = [line for line in logic if line.lower().startswith(("for /f", "for /d"))]
        self.assertGreaterEqual(len(candidate_lines), 6, "expected PATH, registry and directory probes")
        for line in candidate_lines:
            self.assertIn("call :trypython", line,
                          f"candidate is used without being proved to run: {line}")
        joined = " ".join(logic)
        self.assertIn("PYPROBE", joined, "no probe script: candidates are not being executed")
        self.assertIn("WindowsApps", joined, "the Microsoft Store alias stub is not being skipped")
        # The quoting bug that broke any install under "C:\Program Files\...".
        self.assertIn('set "PYCMD="%CAND%" %PRE%"', joined,
                      "PYCMD must be quoted or paths containing spaces break")


class ExporterContractTests(unittest.TestCase):
    """The entry point and the exporter have to agree on flags, or the .cmd silently breaks."""

    def test_flags_the_entry_point_passes_all_exist(self):
        import sys
        sys.path.insert(0, str(HERE))
        from export_owa import build_parser

        known = set()
        for action in build_parser()._actions:      # noqa: SLF001 - reading argparse's own registry
            known.update(action.option_strings)

        cmd_text = OWA_CMD.read_text(encoding="utf-8")
        used = set(re.findall(r'"%EXPORTER%" (--[a-z-]+)', cmd_text))
        used.update(re.findall(r'--days-back', cmd_text))
        unknown = sorted(flag for flag in used if flag not in known)
        self.assertEqual(unknown, [], f"entry point passes flags export_owa.py does not define: {unknown}")


if __name__ == "__main__":
    unittest.main()
