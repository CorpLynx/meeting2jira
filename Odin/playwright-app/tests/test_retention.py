"""Exports must not accumulate.

An export holds meeting subjects, locations, and sometimes the organizer. The entry point deletes it
after a successful push, but a failed push keeps one for diagnosis and `export` keeps them on
purpose. Without pruning, a run of failures leaves an unbounded pile of calendar data in the profile
- which the security notes would then be wrong about.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import export_owa


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = str(self.tmp)
        self.exports = self.tmp / "meeting2jira" / "exports"
        self.exports.mkdir(parents=True)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, name, age_days):
        path = self.exports / name
        path.write_text('{"schema_version": 1, "meetings": []}', encoding="utf-8")
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))
        return path

    def test_removes_only_what_is_past_retention(self):
        fresh = self.make("owa_fresh.json", 1)
        stale = self.make("owa_stale.json", 30)
        edge = self.make("owa_edge.json", 6)          # inside a 7-day window

        removed = export_owa.prune_old_exports(7)

        self.assertEqual(removed, 1)
        self.assertTrue(fresh.exists())
        self.assertTrue(edge.exists())
        self.assertFalse(stale.exists())

    def test_prunes_com_exports_too(self):
        """Both paths write to the same directory, so either pruner cleans both."""
        com = self.make("outlook_20260101_090000.json", 20)
        export_owa.prune_old_exports(7)
        self.assertFalse(com.exists())

    def test_zero_disables(self):
        stale = self.make("owa_stale.json", 90)
        self.assertEqual(export_owa.prune_old_exports(0), 0)
        self.assertTrue(stale.exists())

    def test_leaves_non_json_alone(self):
        keep = self.exports / "notes.txt"
        keep.write_text("not an export", encoding="utf-8")
        old = time.time() - 90 * 86400
        os.utime(keep, (old, old))
        export_owa.prune_old_exports(7)
        self.assertTrue(keep.exists())

    def test_missing_directory_is_harmless(self):
        shutil.rmtree(self.exports)
        self.assertEqual(export_owa.prune_old_exports(7), 0)

    def test_unremovable_file_does_not_raise(self):
        """A locked file must never stop an export."""
        self.make("owa_stale.json", 30)
        from unittest import mock
        with mock.patch.object(Path, "unlink", side_effect=OSError("in use")):
            self.assertEqual(export_owa.prune_old_exports(7), 0)

    def test_explicit_now_makes_the_boundary_testable(self):
        self.make("owa_stale.json", 10)
        later = datetime.now() + timedelta(days=5)
        self.assertEqual(export_owa.prune_old_exports(20, now=later), 0)
        self.assertEqual(export_owa.prune_old_exports(10, now=later), 1)


if __name__ == "__main__":
    unittest.main()
