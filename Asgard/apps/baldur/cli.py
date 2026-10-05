"""Baldur from the command line:  python apps\\baldur\\cli.py --help

Finds the asgard package two folders up (or at ASGARD_APP, which Asgard sets
when it starts an app), so it runs from the installed copy or a checkout.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = os.environ.get("ASGARD_APP") or str(HERE.parent.parent)
for folder in (str(HERE), ROOT):
    if folder not in sys.path:
        sys.path.insert(0, folder)

from baldur.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
