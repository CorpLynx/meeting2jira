"""Odin's files live under Asgard's folder: %LOCALAPPDATA%\\Asgard\\odin (Brandon, Oct 2026).

- The folder honours ASGARD_HOME, as Asgard does, and Odin still needs no Asgard installed.
- A folder from before (%LOCALAPPDATA%\\meeting2jira) moves there once, whole, in one rename, so the
  DPAPI token files, state.db, logs and exports come across unchanged.
- A move that fails stops with a message instead of starting an empty folder beside the old one.
- Odin and its two exporters (graph-app, playwright-app) and the scripts agree on the folder.
"""
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "apps" / "odin"
for folder in (ROOT, APP):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from odin import config  # noqa: E402

ODIN = ROOT.parent / "Odin"                  # the exporters that stay outside Asgard


def load(name, path, extra_path=None):
    if extra_path and str(extra_path) not in sys.path:
        sys.path.insert(0, str(extra_path))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DataDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = {k: os.environ.get(k) for k in ("LOCALAPPDATA", "ASGARD_HOME")}
        os.environ["LOCALAPPDATA"] = str(self.tmp)
        os.environ.pop("ASGARD_HOME", None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def legacy(self):
        old = self.tmp / "meeting2jira"
        (old / "logs").mkdir(parents=True)
        (old / "config.json").write_text("{}", encoding="utf-8")
        (old / "jira_token.dpapi").write_bytes(b"\x01\x00\x00\x00ciphertext")
        (old / "state.db").write_bytes(b"SQLite format 3\x00")
        return old

    def test_odins_folder_is_under_asgards(self):
        self.assertEqual(config.default_data_dir(), self.tmp / "Asgard" / "odin")
        self.assertEqual(config.default_config_path(), self.tmp / "Asgard" / "odin" / "config.json")
        os.environ["ASGARD_HOME"] = str(self.tmp / "elsewhere")
        self.assertEqual(config.default_data_dir(), self.tmp / "elsewhere" / "odin")

    def test_the_old_folder_moves_once_with_everything_in_it(self):
        old = self.legacy()
        new = config.default_data_dir()
        self.assertFalse(old.exists())
        self.assertEqual(sorted(p.name for p in new.iterdir()), ["config.json", "jira_token.dpapi", "logs", "state.db"])
        self.assertEqual((new / "jira_token.dpapi").read_bytes(), b"\x01\x00\x00\x00ciphertext", "the same bytes")
        self.assertIsNone(config.move_legacy_data(new), "the second time there is nothing to move")

    def test_an_existing_new_folder_wins_and_the_old_one_is_left_alone(self):
        old = self.legacy()
        new = self.tmp / "Asgard" / "odin"
        new.mkdir(parents=True)
        (new / "state.db").write_bytes(b"SQLite format 3\x00")
        config.default_data_dir()
        self.assertTrue((old / "jira_token.dpapi").exists())

    def test_history_left_behind_stops_odin_rather_than_duplicate_every_meeting(self):
        """An export script that made the new folder first must not orphan state.db."""
        old = self.legacy()
        (self.tmp / "Asgard" / "odin" / "exports").mkdir(parents=True)
        with self.assertRaisesRegex(config.ConfigError, "re-create every meeting already synced"):
            config.default_data_dir()
        self.assertTrue((old / "state.db").exists(), "nothing is moved or lost")

    def test_asgard_home_never_moves_a_real_folder(self):
        old = self.legacy()
        os.environ["ASGARD_HOME"] = str(self.tmp / "test-home")
        config.default_data_dir()
        self.assertTrue(old.exists())

    def test_a_move_that_fails_says_what_to_do(self):
        self.legacy()
        with mock.patch.object(config.os, "replace", side_effect=PermissionError("in use")):
            with self.assertRaisesRegex(config.ConfigError, "couldn't be moved .*Close anything using it"):
                config.default_data_dir()

    def test_the_other_apps_use_the_same_folder_and_move_it_too(self):
        graph_config = load("odin_graph_config", ODIN / "graph-app" / "graph" / "config.py")
        export_owa = load("odin_export_owa", ODIN / "playwright-app" / "export_owa.py", ODIN / "playwright-app")
        expected = self.tmp / "Asgard" / "odin"
        for name, data_dir in (("graph-app", graph_config.data_dir), ("playwright-app", export_owa.data_dir)):
            with self.subTest(name):
                old = self.legacy()
                self.assertEqual(data_dir(), expected)
                self.assertFalse(old.exists())
                self.assertTrue((expected / "jira_token.dpapi").exists())
                shutil.rmtree(expected)
                os.environ["ASGARD_HOME"] = str(self.tmp / "h")
                self.assertEqual(data_dir(), self.tmp / "h" / "odin")
                os.environ.pop("ASGARD_HOME")

    def test_no_script_still_writes_to_the_old_folder(self):
        """Only the move itself may name %LOCALAPPDATA%\\meeting2jira."""
        offenders = []
        for path in sorted(ODIN.rglob("*")) + sorted(APP.rglob("*")):
            if path.suffix.lower() not in (".py", ".ps1", ".cmd") or "tests" in path.parts or not path.is_file():
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if re.search(r"LOCALAPPDATA%?\\?\)?[\\/ ,'\"]+\\?'?meeting2jira", line, re.I) and not re.search(
                        r"legacy|move|before|exist|rem |#|\"\"\"|^\s*\(", line, re.I):
                    offenders.append(f"{path}:{n}: {line.strip()}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
