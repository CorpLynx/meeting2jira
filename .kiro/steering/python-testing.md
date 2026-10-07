---
inclusion: fileMatch
fileMatchPattern: ["Odin/app/tests/**/*.py"]
---
# Writing and fixing tests in Odin/app/tests

## Style
- Write tests as stdlib `unittest` (`unittest.TestCase`, `self.assert*`). They ship with the app and
  must still run on the workstation without dev tools (`python -m unittest discover -s tests`) to prove the
  install. In dev, pytest runs them (`tools/run_tests.py`) for better output, timeouts and coverage.
- So in `Odin/app/tests`: no `import pytest`, no pytest fixtures, `parametrize`, marks or `pytest.raises`.
  Anything that needs those isn't a shipped test; ask before adding a dev-only test folder.
- Each test has a 60s timeout (pytest-timeout); a test that waits on a socket or lock must not hang.
- Use `self.subTest(...)` instead of copy-pasted near-duplicate tests.
- Temporary files: `tempfile.TemporaryDirectory()` or `tempfile.mkdtemp()` cleaned up in `tearDown`.
  Never write into `Odin/app/` or the real `%LOCALAPPDATA%\meeting2jira`.
- Patch with `unittest.mock.patch("meeting2jira.<module>.<name>")`, e.g. `_desktop_dir`,
  `load_token`, `JiraClient.from_config`, `jira.time.sleep` (keeps retry tests instant).
- Python 3.8 compatible, same as the app (see tech.md).

## Shared pieces worth reusing
- `tests/fixtures/sample_export.json` (12 items covering recurring, declined, private, all-day,
  cancelled, future, duplicate) and `tests/fixtures/sample_outlook.csv` (multi-line description,
  malformed row). Add new fixtures there.
- `FakeJira` in `test_pipeline.py` for CLI/sync tests (`from tests.test_pipeline import FakeJira`).
- `test_jira_client.py` runs a local `http.server` on 127.0.0.1 for real HTTP behavior. No real
  network anywhere.

## What can't be unit-tested here
- Outlook COM, DPAPI, Task Scheduler, CLM enforcement and real PS 5.1 behavior. Those belong in
  `Odin/app/tools/Invoke-WindowsChecks.ps1` and the HANDOFF.md checklist, not in a mocked test that
  would pass without proving anything.
- Static PowerShell rules (CLM-safe patterns, no execution-policy bypass, guarded Outlook
  properties) are checked by `test_guardrails.py`. If you move files, keep `GuardrailWiringTests`
  in step so the guardrails can't pass vacuously.

## When a test fails
- Read the assertion line and the deepest frame in our code first; that's usually enough.
- Check whether the test or the code is wrong before editing either one.
- Keep each test focused on one behavior so a failure names the cause.
