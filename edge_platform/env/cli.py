"""edge-env -- the .env reader's CLI, and the check every vendored copy of it runs in CI.

  edge-env get KEY [FILE]           print KEY's value; exit 0 assigned, 1 not assigned, 2 BLIND
  edge-env digest FILE KEY...       `KEY sha256[:12]` per key (`absent` when missing), never a value
  edge-env path sh|py               where this install keeps lib-env.sh / lib_env.py
  edge-env check-copy FILE...       exit 1 unless each FILE is byte-identical to the twin it is
                                    named after (lib-env.sh / lib_env.py) in THIS release

check-copy compares EXACT bytes, no line-ending normalisation: a CRLF lib-env.sh is a broken one
(bash reads the \\r into every value), so it must not pass. Run it from the consumer's pinned wheel,
`uvx --from <wheel URL> edge-env check-copy ...`, and the copy is proven equal to the release the
consumer pins -- which ci-kit's `ci-pin-drift` holds to the newest.

Exit 0 = clean, 1 = findings, 2 = BLIND (unreadable file, a name that is not a twin, bad usage):
a crashed check must never look like a passing one.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from . import lib_env

HERE = Path(__file__).resolve().parent
TWINS = {"lib-env.sh": HERE / "lib-env.sh", "lib_env.py": HERE / "lib_env.py"}
KIND = {"sh": "lib-env.sh", "py": "lib_env.py"}
USAGE = "usage: edge-env get KEY [FILE] | digest FILE KEY... | path sh|py | check-copy FILE..."


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def check_copy(files: list[str]) -> tuple[list[str], list[str]]:
    """(findings, blind) for vendored copies: each FILE against the packaged twin of its name."""
    found: list[str] = []
    blind: list[str] = []
    for f in files:
        p = Path(f)
        twin = TWINS.get(p.name)
        if twin is None:
            blind.append(f"{f}: not a lib-env twin (want a file named {' or '.join(TWINS)})")
            continue
        try:
            got = _sha(p)
        except OSError as e:
            blind.append(f"{f}: cannot read ({type(e).__name__})")
            continue
        want = _sha(twin)
        if got != want:
            found.append(f"{f} sha256 {got[:12]} != released {p.name} {want[:12]} -- re-vendor it")
    return found, blind


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    cmd, rest = args[0], args[1:]
    if cmd in ("get", "digest"):
        return lib_env.main(args)
    if cmd == "path" and len(rest) == 1 and rest[0] in KIND:
        print(TWINS[KIND[rest[0]]])
        return 0
    if cmd == "check-copy" and rest:
        found, blind = check_copy(rest)
        for line in found + blind:
            print(f"  {line}")
        if blind:
            print(f"lib-env copy: BLIND ({len(blind)} not checked)")
            return 2
        if found:
            print(f"lib-env copy: {len(found)} finding(s)")
            return 1
        print(f"lib-env copy: OK ({len(rest)} file(s) == this release)")
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
