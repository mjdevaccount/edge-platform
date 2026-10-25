"""Regenerate edge_platform/fence/sessions-bundled.json: XNYS sessions from exchange_calendars,
the same library and document trade-data's service/market_sessions.py writes (schema 1).

    uv run python scripts/gen_bundled_sessions.py [--years 2]

The bundled table is the fence's third source (after quant.market_sessions and the box cache).
Its age is never checked, only its horizon: tests/fence/test_cli.py::test_bundled_horizon_has_room fails CI once fewer than
180 days remain, which is the signal to re-run this and cut a release.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from importlib.metadata import version
from pathlib import Path

import exchange_calendars as xcals

OUT = Path(__file__).resolve().parents[1] / "edge_platform" / "fence" / "sessions-bundled.json"


def _z(t) -> str:
    return t.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=2)
    a = ap.parse_args()
    today = dt.datetime.now(dt.UTC).date()
    first = today.replace(day=1)
    horizon = dt.date(today.year + a.years, today.month, 1) - dt.timedelta(days=1)
    cal = xcals.get_calendar("XNYS", start=str(first - dt.timedelta(days=7)), end=str(horizon + dt.timedelta(days=7)))
    rows = []
    for s in cal.sessions_in_range(str(first), str(horizon)):
        o, c = cal.session_open(s), cal.session_close(s)
        rows.append(
            {
                "date": s.date().isoformat(),
                "open_utc": _z(o),
                "close_utc": _z(c),
                "early_close": c.tz_convert("America/New_York").hour < 16,
            }
        )
    doc = {
        "schema": 1,
        "calendar": "XNYS",
        "generated_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": f"exchange_calendars {version('exchange_calendars')}",
        "first": first.isoformat(),
        "horizon": horizon.isoformat(),
        "sessions": rows,
    }
    OUT.write_text(json.dumps(doc, separators=(",", ":")) + chr(10), encoding="utf-8", newline=chr(10))
    print(f"{OUT.name}: {len(rows)} sessions {first}..{horizon} ({doc['source']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
