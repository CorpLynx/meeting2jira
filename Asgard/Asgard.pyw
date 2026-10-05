"""Start the Asgard launcher: double-click, or run  pythonw Asgard.pyw
Uninstall:  pythonw Asgard.pyw --uninstall
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from asgard.launcher import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
