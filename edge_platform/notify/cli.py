"""`edge-notify` -- the policy for consumers that are not Python, and the gates they run.

  edge-notify path [policy|cases|publishers]   print the packaged file's path (cp it, hash it)
  edge-notify shell [POLICY]                   `_ENP_<NAME>='<v>'` lines for lib-notify.sh to eval
  edge-notify rows [CASES]                     the shared cases as TAB rows for a bash test
  edge-notify check-pin [POLICY [CASES]]       exit 1 unless CASES pins POLICY's LF sha256
  edge-notify check-copy FILE                  exit 1 unless FILE is byte-identical (LF) to the release's policy
  edge-notify am-night ALERTMANAGER_YML        exit 1 unless the AM night mute == policy.night
  edge-notify senders                          validate the registry against the policy; list it

Exit 0 = clean, 1 = findings, 2 = BLIND (unreadable / invalid input): a crashed check must never
look like a passing one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import policy as pol
from . import publishers as pubs

NL = chr(10)


def _p(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="edge-notify", description=__doc__.split(NL)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("path").add_argument("which", nargs="?", default="policy", choices=["policy", "cases", "publishers"])
    sub.add_parser("shell").add_argument("policy", nargs="?", type=Path, default=pol.POLICY_PATH)
    sub.add_parser("rows").add_argument("cases", nargs="?", type=Path, default=pol.CASES_PATH)
    cp = sub.add_parser("check-pin")
    cp.add_argument("policy", nargs="?", type=Path, default=pol.POLICY_PATH)
    cp.add_argument("cases", nargs="?", type=Path, default=pol.CASES_PATH)
    sub.add_parser("check-copy").add_argument("file", type=Path)
    am = sub.add_parser("am-night")
    am.add_argument("alertmanager_yml", type=Path)
    am.add_argument("--interval", default="quiet-night")
    sub.add_parser("senders")
    return ap.parse_args(argv)


def _report(name: str, found: list[str], ok: str) -> int:
    for f in found:
        print(f"  {f}")
    print(f"{name}: {len(found)} finding(s)" if found else f"{name}: OK ({ok})")
    return 1 if found else 0


def run(a: argparse.Namespace) -> int:
    if a.cmd == "path":
        print({"policy": pol.POLICY_PATH, "cases": pol.CASES_PATH, "publishers": pubs.REGISTRY_PATH}[a.which])
        return 0
    if a.cmd == "shell":
        print(NL.join(pol.shell_lines(pol.load(a.policy))))
        return 0
    if a.cmd == "rows":
        print(NL.join(pol.case_rows(json.loads(a.cases.read_text(encoding="utf-8")))))
        return 0
    if a.cmd == "check-pin":
        pol.load(a.policy)
        msg = pol.check_pin(a.policy, a.cases)
        return _report("ntfy policy pin", [msg] if msg else [], pol.sha256_lf(a.policy)[:12])
    if a.cmd == "check-copy":
        pol.load(a.file)
        got, want = pol.sha256_lf(a.file), pol.sha256_lf(pol.POLICY_PATH)
        found = [] if got == want else [f"{a.file} sha256 {got[:12]} != released policy {want[:12]}"]
        return _report("ntfy policy copy", found, want[:12])
    if a.cmd == "am-night":
        from .alertmanager import am_night_findings

        p = pol.load()
        found = am_night_findings(a.alertmanager_yml.read_text(encoding="utf-8"), p, a.interval)
        return _report(
            "alertmanager night", found, f"{a.interval} == {p.night_start:02d}:00-{p.night_end:02d}:00 {p.night_tz}"
        )
    if a.cmd == "senders":
        senders = pubs.load()
        for s in senders:
            print(f"  {s.mode:9} {s.route:10} {s.name:34} {s.repo}")
        open_ = pubs.uncounted(senders)
        found = pubs.policy_findings(senders, pol.load())
        note = f"{len(senders)} senders, {len(open_)} uncounted: {', '.join(s.name for s in open_) or 'none'}"
        return _report("ntfy senders", found, note)
    raise AssertionError(a.cmd)


def main(argv: list[str] | None = None) -> int:
    # Windows python prints CRLF; bash evals these lines and a stray CR would ride into a value.
    sys.stdout.reconfigure(newline=NL)
    a = _p(argv)
    try:
        return run(a)
    except Exception as exc:  # ANY crash (bad JSON/YAML, missing key, unreadable file) is BLIND, never clean
        print(f"edge-notify {a.cmd}: BLIND -- {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
