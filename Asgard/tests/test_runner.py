import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from asgard.catalog import LaunchSpec  # noqa: E402
from asgard.runner import LaunchError, Runner, explain_oserror, log_tail  # noqa: E402


class FakeBlocked(OSError):
    winerror = 1260


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.runner = Runner(self.dir / "logs")

    def tearDown(self) -> None:
        self.runner.close()
        self.tmp.cleanup()

    def script(self, body: str) -> LaunchSpec:
        path = self.dir / "app.py"
        path.write_text(body, encoding="utf-8")
        return LaunchSpec("process", str(path), [sys.executable, str(path)], str(self.dir), False)

    def wait(self):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            done = self.runner.poll()
            if done:
                return done
            time.sleep(0.05)
        self.fail("app did not finish")

    def test_crash_is_reported_with_the_latest_log_lines(self) -> None:
        spec = self.script("import sys\nprint('first run')\nsys.exit(3)\n")
        self.runner.start("odin", "Odin", spec)
        self.assertTrue(self.runner.is_running("odin"))
        self.wait()
        spec = self.script("import sys\nprint('loading config')\n"
                           "print('ModuleNotFoundError: No module named jira', file=sys.stderr)\nsys.exit(3)\n")
        self.runner.start("odin", "Odin", spec)
        (done,) = self.wait()
        self.assertFalse(self.runner.is_running("odin"))
        self.assertEqual(done.returncode, 3)
        self.assertTrue(done.crashed_early)
        tail = log_tail(done.log_path)
        self.assertIn("loading config", tail)
        self.assertIn("No module named jira", tail)
        self.assertNotIn("first run", tail)
        self.assertNotIn("===", tail)

    def test_clean_exit_is_not_a_crash(self) -> None:
        self.runner.start("odin", "Odin", self.script("print('bye')\n"))
        (done,) = self.wait()
        self.assertEqual(done.returncode, 0)
        self.assertFalse(done.crashed_early)

    def test_missing_program_gives_a_plain_message(self) -> None:
        spec = LaunchSpec("process", "x", [str(self.dir / "no-such-program")], str(self.dir), False)
        with self.assertRaises(LaunchError) as caught:
            self.runner.start("x", "Thing", spec)
        self.assertIn("Couldn't find", str(caught.exception))
        self.assertIn("Could not start", (self.dir / "logs" / "x.log").read_text(encoding="utf-8"))
        self.assertFalse(self.runner.is_running("x"))

    def test_policy_block_is_explained(self) -> None:
        message = explain_oserror(FakeBlocked("blocked"), "Odin", r"C:\Tools\odin.exe")
        self.assertIn("group policy", message)
        self.assertIn(r"C:\Tools\odin.exe", message)

    def test_child_gets_asgard_environment(self) -> None:
        runner = Runner(self.dir / "logs", extra_env={"ASGARD_DATA": "here"})
        out = self.dir / "env.txt"
        spec = self.script(f"import os\nopen({str(out)!r}, 'w').write(os.environ.get('ASGARD_DATA', ''))\n")
        runner.start("e", "Env", spec)
        deadline = time.monotonic() + 20
        while not runner.poll() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(out.read_text(), "here")


if __name__ == "__main__":
    unittest.main()
