"""`edge-fence` -- is a change allowed now, and until when?

  edge-fence check --tier T [--need 45m] [--at INSTANT] [--override "<reason>"]
  edge-fence status                     which source answers right now, its horizon and age
  edge-fence path [policy|bundled]      the packaged file's path

check prints ONE JSON line (the decision record) on stdout and exits 0 allow · 1 refuse ·
2 could not evaluate; a caller treats 2 as refuse. A refusal is a deferral, never a failed run.

Sources (first that answers): --dsn / $EDGE_FENCE_DSN (quant.market_sessions; a good read
is written to --write-cache / $EDGE_FENCE_WRITE_CACHE when given -- never the box's export file,
which edge-clock-export.timer owns) -> --cache / $CLOCK_SESSIONS_FILE (default /var/lib/edge-clock/sessions.json,
trusted up to $CLOCK_MAX_AGE_H, 336h) -> the bundled snapshot -> the weekday superset.

Telemetry: --textfile PATH (or $EDGE_FENCE_TEXTFILE) keeps node_exporter textfile counters
edge_fence_decisions_total{tier,result,source} and edge_fence_last_decision_timestamp_seconds{tier}.
A counter that cannot be written never changes the verdict: it warns on stderr, and the stale
timestamp is what an outside rule sees. Any crash is exit 2 with a record, never rc 1 or 0.
--need defaults to the tier's need_min in the policy.
INSTANT: ISO 8601 with an offset, `@EPOCH`, or a naive value read as ET wall time (lib-clock's rule).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
from importlib import resources
from pathlib import Path

from . import fence, sessions

NL = chr(10)


def parse_at(s: str | None) -> dt.datetime:
    if not s:
        return dt.datetime.now(dt.UTC)
    if s.startswith("@"):
        return dt.datetime.fromtimestamp(int(s[1:]), dt.UTC)
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=sessions.et())


def parse_need(s: str) -> dt.timedelta:
    m = re.fullmatch(r"(\d+)\s*([mh]?)", s.strip())
    if not m:
        raise ValueError(f"--need wants minutes like 45m or 2h, got {s!r}")
    n = int(m.group(1))
    return dt.timedelta(hours=n) if m.group(2) == "h" else dt.timedelta(minutes=n)


def _p(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="edge-fence", description=(__doc__ or "").split(NL)[0])
    ap.add_argument("--dsn", default=os.environ.get("EDGE_FENCE_DSN"))
    ap.add_argument("--cache", type=Path, default=Path(os.environ.get("CLOCK_SESSIONS_FILE") or sessions.CACHE_FILE))
    ap.add_argument("--max-age-h", default=os.environ.get("CLOCK_MAX_AGE_H") or str(sessions.MAX_AGE_H))
    ap.add_argument("--write-cache", type=Path, default=os.environ.get("EDGE_FENCE_WRITE_CACHE") or None)
    ap.add_argument("--policy", type=Path, default=None)
    ap.add_argument("--textfile", type=Path, default=os.environ.get("EDGE_FENCE_TEXTFILE") or None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--tier", required=True)
    c.add_argument("--need", default=None)
    c.add_argument("--at", default=None)
    c.add_argument("--override", default=None)
    sub.add_parser("status")
    sub.add_parser("path").add_argument("which", nargs="?", default="policy", choices=["policy", "bundled"])
    return ap.parse_args(argv)


_LINE = re.compile(r"^(edge_fence_\w+)\{(.*)\} (\S+)$")


def _num(v: float) -> str:
    """Exact sample text. `:g` keeps six significant digits, so an epoch timestamp printed as
    1.7929e+09 -- up to ~80 min off, which a staleness rule would read as truth (v0.3.3)."""
    return str(int(v)) if v.is_integer() else repr(v)


def bump_textfile(path: Path, tier: str, result: str, source: str, now: float) -> None:
    """Read-modify-write the counter file atomically. A torn or foreign file restarts at zero
    (Prometheus reads a counter reset as a reset, not as a loss of the fence's decision)."""
    vals: dict[tuple[str, str], float] = {}
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            m = _LINE.match(ln)
            if m:
                vals[(m.group(1), m.group(2))] = float(m.group(3))
    except (OSError, ValueError):
        vals = {}
    k = ("edge_fence_decisions_total", f'tier="{tier}",result="{result}",source="{source}"')
    vals[k] = vals.get(k, 0.0) + 1
    vals[("edge_fence_last_decision_timestamp_seconds", f'tier="{tier}"')] = now
    out = [
        "# HELP edge_fence_decisions_total Fence decisions by tier, result (allow|refuse|blind) and source.",
        "# TYPE edge_fence_decisions_total counter",
        "# HELP edge_fence_last_decision_timestamp_seconds When the fence last decided for a tier.",
        "# TYPE edge_fence_last_decision_timestamp_seconds gauge",
    ]
    out += [f"{n}{{{lab}}} {_num(v)}" for (n, lab), v in sorted(vals.items())]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".edge_fence.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(NL.join(out) + NL)
    os.replace(tmp, path)


def _count(textfile: Path | None, tier: str, result: str, source: str) -> None:
    if not textfile:
        return
    try:
        bump_textfile(textfile, tier, result, source, time.time())
    except OSError as e:
        print(f"edge-fence: WARNING -- counter not written to {textfile} ({e}); the verdict stands", file=sys.stderr)


def _blind(textfile: Path | None, tier: str, why: str) -> int:
    rec = {"allowed": False, "tier": tier, "reason": f"could not evaluate: {why}", "source": "none"}
    print(json.dumps(rec, separators=(",", ":"), sort_keys=True))
    _count(textfile, tier, "blind", "none")
    return 2


def run(a: argparse.Namespace) -> int:
    if a.cmd == "path":
        name = fence.POLICY if a.which == "policy" else sessions.BUNDLED
        print(resources.files(fence.__package__).joinpath(name))
        return 0
    loaded = sessions.load_sources(dsn=a.dsn, cache=a.cache, max_age_h=float(a.max_age_h), write_to=a.write_cache)
    if a.cmd == "status":
        s = loaded.sessions
        rec = {"source": loaded.source, "blind": loaded.blind, "tz_ok": sessions.tz_ok()}
        if s is not None:
            rec |= {
                "first": s.first.isoformat(),
                "horizon": s.horizon.isoformat(),
                "generated_at": s.doc["generated_at"],
                "sessions": len(s.rows),
            }
        print(json.dumps(rec, separators=(",", ":"), sort_keys=True))
        return 0 if s is not None and rec["tz_ok"] else 2
    d = fence.decide(
        a.tier,
        at=parse_at(a.at),
        need=parse_need(a.need) if a.need is not None else None,
        loaded=loaded,
        policy=fence.load_policy(a.policy),
        override=a.override,
    )
    print(d.as_json())
    for b in d.blind:
        print(f"edge-fence: BLIND source skipped -- {b}", file=sys.stderr)
    _count(a.textfile, d.tier, "allow" if d.allowed else "refuse", d.source)
    return 0 if d.allowed else 1


def main(argv: list[str] | None = None) -> int:
    a = _p(argv)
    try:
        return run(a)
    except Exception as e:
        return _blind(a.textfile, getattr(a, "tier", None) or a.cmd, f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
