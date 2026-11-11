"""`python -m edge_platform.config lint|schema|catalog module:ATTR [--check PATH | --write PATH]`.

ATTR names a sequence of `Domain` classes. Exit 0 = clean / up to date, 1 = lint findings or a
committed file that drifted from the render, 2 = could not import the domains or run (a checker that
did not finish looking never exits 0)."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from .render import catalog, exemptions, lint, schema


def _domains(target: str) -> list:
    mod, _, attr = target.partition(":")
    if not mod or not attr:
        raise ValueError(f"target {target!r} must be module:ATTR")
    return list(getattr(importlib.import_module(mod), attr))


def render(command: str, domains: list) -> str:
    if command == "schema":
        return json.dumps(schema(domains), sort_keys=True, indent=2) + "\n"
    return catalog(domains)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m edge_platform.config")
    ap.add_argument("command", choices=("lint", "schema", "catalog"))
    ap.add_argument("target", help="module:ATTR naming a sequence of Domain classes")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", type=Path, help="exit 1 when this committed file differs from the render")
    mode.add_argument("--write", type=Path, help="write the render here")
    a = ap.parse_args(argv)
    try:
        domains = _domains(a.target)
        if a.command == "lint":
            findings = lint(domains)
            for f in findings:
                print(f"config-lint: {f}")
            for e in exemptions(domains):
                print(f"config-lint: exempt (not_secret): {e}")
            print(f"config-lint: {len(findings)} finding(s) over {len(domains)} domain(s)")
            return 1 if findings else 0
        text = render(a.command, domains)
        if a.write is not None:
            a.write.write_text(text, encoding="utf-8", newline="\n")
            return 0
        if a.check is not None:
            if a.check.read_text(encoding="utf-8") != text:
                print(f"config-{a.command}: {a.check} is stale -- re-render with --write", file=sys.stderr)
                return 1
            return 0
        sys.stdout.write(text)
        return 0
    except Exception as exc:  # did not finish looking
        print(f"config-{a.command}: could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
