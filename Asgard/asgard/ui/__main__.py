"""python -m asgard.ui [--app ID]: the shared window, for every app with views or for one."""
import argparse
import sys

from asgard.ui import run


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m asgard.ui", description="Asgard's desktop window.")
    parser.add_argument("--app", metavar="ID", help="show only this app's views (for example heimdall)")
    args, rest = parser.parse_known_args()
    return run(app=args.app, argv=[sys.argv[0]] + rest)


if __name__ == "__main__":
    sys.exit(main())
