"""edge-host tests/lib-clock.test.sh, sections 1-5, driven through edge_platform.fence.sessions.

The fixture is edge-host's tests/fixtures/clock/sessions.json (origin/main 0598b6e), copied
verbatim: exchange_calendars 4.13.2's answers, not ones written to agree with this code.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from edge_platform.fence.sessions import ClockBlind, Sessions, doc_from_rows, load_sources
from edge_platform.fence.sessions import et as _et

FX = Path(__file__).parent / "fixtures" / "lib-clock-sessions.json"
B, A = dt.timedelta(minutes=15), dt.timedelta(minutes=35)


def _doc() -> dict:
    return json.loads(FX.read_text(encoding="utf-8"))


def _now(doc: dict, days_old: float = 0) -> float:
    return dt.datetime.fromisoformat(doc["generated_at"].replace("Z", "+00:00")).timestamp() + days_old * 86400


@pytest.fixture
def s() -> Sessions:
    d = _doc()
    return Sessions(d, now=_now(d))


def et(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=_et())


def z(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def fence_active(s: Sessions, t: dt.datetime) -> bool:
    return s.window_end(t, B, A) is not None


def fence_minutes(s: Sessions, t: dt.datetime) -> int:
    if fence_active(s, t):
        return 0
    return int((s.next_window_start(t, B) - t).total_seconds() // 60)


# -- 1. the half day (2025-11-28, bell 13:00 ET) -------------------------------------------------
def test_half_day(s: Sessions) -> None:
    assert s.is_rth(et("2025-11-28T12:30"))
    assert not s.is_rth(et("2025-11-28T13:30")), "no phantom 16:00"
    assert s.close_utc(et("2025-11-28T10:00")) == z("2025-11-28T18:00:00Z")
    assert fence_active(s, et("2025-11-28T13:30"))
    assert not fence_active(s, et("2025-11-28T13:40"))
    assert s.window_end(et("2025-11-28T13:00"), B, A) == z("2025-11-28T18:35:00Z")
    assert s.next_open(et("2025-11-28T14:00")) == z("2025-12-01T14:30:00Z")


# -- 2. the holiday (Thanksgiving 2025-11-27) and the weekend ------------------------------------
def test_holiday_and_weekend(s: Sessions) -> None:
    assert not s.is_trading_day(et("2025-11-27T11:00"))
    assert s.is_trading_day(et("2025-11-26T11:00"))
    assert not fence_active(s, et("2025-11-27T11:00"))
    assert fence_minutes(s, et("2025-11-27T11:00")) == 1335
    assert s.close_utc(et("2025-11-27T11:00")) is None
    assert not s.is_trading_day(et("2025-11-29T12:00"))
    assert s.next_open(et("2025-11-29T12:00")) == z("2025-12-01T14:30:00Z")
    assert s.is_rth(et("2025-11-26T16:00")), "the bell is inclusive"
    assert not s.is_rth(et("2025-11-26T16:01"))
    assert fence_active(s, et("2025-11-26T09:15"))
    assert not fence_active(s, et("2025-11-26T09:14"))
    assert fence_active(s, et("2025-11-26T16:34"))
    assert not fence_active(s, et("2025-11-26T16:35")), "half-open"


# -- 3. DST, both directions ---------------------------------------------------------------------
def test_dst(s: Sessions) -> None:
    assert s.next_open(et("2026-03-06T08:00")) == z("2026-03-06T14:30:00Z")
    assert s.next_open(et("2026-03-09T08:00")) == z("2026-03-09T13:30:00Z")
    assert fence_minutes(s, et("2027-03-13T20:00")) == 36 * 60 + 15
    assert fence_minutes(s, et("2026-10-31T20:00")) == 38 * 60 + 15
    assert s.is_rth(z("2026-11-02T14:31:00Z"))
    assert not s.is_rth(z("2026-11-02T13:31:00Z"))
    wide_b, wide_a = dt.timedelta(minutes=30), dt.timedelta(minutes=60)
    assert s.window_end(et("2025-11-26T09:00"), wide_b, wide_a) is not None
    assert s.window_end(et("2025-11-26T17:01"), wide_b, wide_a) is None


# -- 4. BLIND is an exception, never a guess -----------------------------------------------------
def test_blind() -> None:
    d = _doc()
    with pytest.raises(ClockBlind, match="stale"):
        Sessions(d, now=_now(d, 15))
    assert Sessions(d, now=_now(d, 15), max_age_h=720).is_rth(et("2025-11-26T10:00"))
    assert Sessions(d, now=_now(d, 900), max_age_h=None).is_rth(et("2025-11-26T10:00")), "db/bundled: no age"
    with pytest.raises(ClockBlind, match="schema"):
        Sessions(d | {"schema": 2}, now=_now(d))
    with pytest.raises(ClockBlind, match="malformed"):
        Sessions(d | {"generated_at": 5}, now=_now(d))
    ok = Sessions(d, now=_now(d))
    with pytest.raises(ClockBlind, match="outside"):
        ok.is_trading_day(et("2027-06-01T10:00"))
    with pytest.raises(ClockBlind, match="outside"):
        ok.is_trading_day(et("2025-10-01T10:00"))
    with pytest.raises(ClockBlind, match="no open"):
        ok.next_open(et("2027-03-31T17:00"))


# -- sources: db rows -> document, cache, bundled, superset, in order ----------------------------
def test_doc_from_rows_round_trips(s: Sessions) -> None:
    rows = [(d, o, c, False) for d, o, c in s.rows]
    doc = doc_from_rows(rows, source="quant.market_sessions", now=dt.datetime.now(dt.UTC))
    t = Sessions(doc, now=None, max_age_h=None)
    assert t.rows == s.rows
    assert (t.first, t.horizon) == (s.rows[0][0], s.rows[-1][0])


def test_load_order(tmp_path: Path) -> None:
    d = _doc()
    fresh = tmp_path / "fresh.json"
    fresh.write_text(json.dumps(d), encoding="utf-8")
    got = load_sources(dsn=None, cache=fresh, now=_now(d))
    assert (got.source, got.blind) == ("cache", [])
    got = load_sources(dsn=None, cache=fresh, now=_now(d, 15))
    assert got.source == "bundled" and "stale" in got.blind[0]
    got = load_sources(dsn=None, cache=tmp_path / "none.json", now=_now(d), use_bundled=False)
    assert got.source == "superset" and got.sessions is None
    torn = tmp_path / "torn.json"
    torn.write_text('{"schema":1,', encoding="utf-8")
    assert load_sources(dsn=None, cache=torn, use_bundled=False).source == "superset"


def test_unreachable_db_falls_through(tmp_path: Path) -> None:
    got = load_sources(dsn="postgresql://nobody@127.0.0.1:1/none", cache=None, use_bundled=False)
    assert got.source == "superset"
    assert got.blind and got.blind[0].startswith("db:")
