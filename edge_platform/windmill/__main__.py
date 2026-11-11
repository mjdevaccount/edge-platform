"""`python -m edge_platform.windmill list | copy NAME ROOT SCRIPT_PATH [--check]` (exit 1 = drift under --check)."""

from __future__ import annotations

import argparse
import sys

from . import copy, names


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m edge_platform.windmill")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="the shipped template names")
    c = sub.add_parser("copy", help="write a template into a Windmill tree")
    c.add_argument("name")
    c.add_argument("root", help="the Windmill tree root (the directory holding u/, f/)")
    c.add_argument("script_path", help="the Windmill script path, e.g. u/admin/post_admin_run")
    c.add_argument("--check", action="store_true", help="write nothing; exit 1 if the tree's copy differs")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        print("\n".join(names()))
        return 0
    try:
        changed = copy(a.name, a.root, a.script_path, check=a.check)
    except (KeyError, ValueError) as e:
        print(f"edge_platform.windmill: {e}", file=sys.stderr)
        return 2
    for p in changed:
        print(f"{'DRIFT' if a.check else 'wrote'} {p}")
    return 1 if (a.check and changed) else 0


if __name__ == "__main__":
    sys.exit(main())
