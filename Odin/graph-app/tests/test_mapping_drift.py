"""graph/mapping.py is a duplicate of playwright-app/owa/mapping.py. Prove it has not drifted.

The duplication is deliberate - each top-level folder must be independently copyable, so neither may
import from the other - but a duplicate with no guardrail is just a bug waiting to be fixed in one
place. The repo already settled this pattern for the Python-discovery logic, which lives in five
copies with `test_resolve_python_copies_are_identical`.

Exactly two kinds of difference are permitted, and both are mechanical:

    1. The module docstring. It is prose, and each copy should explain its own side.
    2. The SOURCE and KEY_PREFIX constants, which are the whole point of having two copies.

Anything else fails. If you are here because this test is red: the fix is almost never to loosen the
comparison, it is to apply your change to both files.

Skips cleanly when playwright-app/ is absent, so this folder stays usable on its own - including
after playwright-app/ is eventually deleted, which is planned once Graph is verified.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
MINE = HERE / "graph" / "mapping.py"
THEIRS = HERE.parent / "playwright-app" / "owa" / "mapping.py"

# The constants that are allowed to differ, and what each copy must say.
EXPECTED = {
    MINE: {"SOURCE": "graph-msal", "KEY_PREFIX": "graph"},
    THEIRS: {"SOURCE": "owa-playwright", "KEY_PREFIX": "owa"},
}


def strip_docstring(text: str) -> str:
    """Remove the module docstring, leaving the code."""
    module = ast.parse(text)
    if (module.body and isinstance(module.body[0], ast.Expr)
            and isinstance(module.body[0].value, ast.Constant)
            and isinstance(module.body[0].value.value, str)):
        # end_lineno is 3.8+, which is the floor this project targets anyway.
        return "\n".join(text.splitlines()[module.body[0].end_lineno:])
    return text


def normalize(text: str) -> str:
    body = strip_docstring(text)
    body = re.sub(r'^SOURCE = ".*"$', "SOURCE = <per-copy>", body, flags=re.MULTILINE)
    body = re.sub(r'^KEY_PREFIX = ".*"$', "KEY_PREFIX = <per-copy>", body, flags=re.MULTILINE)
    return body.strip()


@unittest.skipUnless(THEIRS.is_file(), "playwright-app/ is not present")
class MappingDriftTests(unittest.TestCase):

    def test_the_copies_are_identical_apart_from_two_constants(self):
        mine = normalize(MINE.read_text(encoding="utf-8"))
        theirs = normalize(THEIRS.read_text(encoding="utf-8"))
        if mine != theirs:
            # A diff, not just "not equal": this test is read by someone who has to go and fix it.
            import difflib
            diff = "\n".join(difflib.unified_diff(
                theirs.splitlines(), mine.splitlines(),
                fromfile="playwright-app/owa/mapping.py", tofile="graph-app/graph/mapping.py",
                lineterm=""))
            self.fail("The mapping copies have drifted. Apply the change to BOTH files.\n\n"
                      + diff)

    def test_each_copy_declares_its_own_identity(self):
        """The permitted differences must actually be there, and be right.

        Without this, the normalization above would happily accept two files that had both drifted
        to the same wrong value - or a Graph copy still claiming to be the OWA one, which would put
        `owa:` keys in a Graph export and quietly defeat nothing at all except future debugging.
        """
        for path, expected in EXPECTED.items():
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for name, value in expected.items():
                self.assertIn('{} = "{}"'.format(name, value), text,
                              "{} should declare {} = {!r}".format(path.name, name, value))


class KeyPrefixTests(unittest.TestCase):
    """The prefix has to reach the key, not just exist as a constant."""

    def test_graph_keys_are_prefixed_graph(self):
        import sys
        sys.path.insert(0, str(HERE))
        from graph import mapping
        key = mapping.occurrence_key(
            {"iCalUId": "abc"},
            mapping.parse_graph_datetime({"dateTime": "2026-09-22T13:00:00", "timeZone": "UTC"}))
        self.assertEqual(key, "graph:abc|2026-09-22T13:00:00Z")


if __name__ == "__main__":
    unittest.main()
