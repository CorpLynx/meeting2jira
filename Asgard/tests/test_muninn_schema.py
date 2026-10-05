"""Run tools/check_muninn_schema.py, the schema's own 125 checks, in a subprocess."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipIf(os.name == "nt", "the local-day checks switch time zones, which Windows Python can't do")
class SchemaChecks(unittest.TestCase):
    def test_schema_checks_pass(self):
        done = subprocess.run([sys.executable, str(ROOT / "tools" / "check_muninn_schema.py")],
                              capture_output=True, text=True, timeout=120)
        failures = [line for line in done.stdout.splitlines() if line.startswith("FAIL")]
        self.assertEqual((done.returncode, failures), (0, []), done.stdout[-2000:] + done.stderr[-2000:])
        self.assertIn("0 failed", done.stdout)


if __name__ == "__main__":
    unittest.main()
