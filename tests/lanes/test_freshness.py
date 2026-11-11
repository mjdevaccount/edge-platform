"""The freshness verdict table (ported from trade-data service/usecases/producer_freshness_selftest.py):
every branch, each case a real reading from the estate's history. The session-clock cases run on a
test XNYS clock built from exchange_calendars and REGISTERED, exactly as a consumer would."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from edge_platform.lanes import freshness as f

ET = ZoneInfo("America/New_York")

_CASES: tuple[tuple[str, dict, str], ...] = (
    ("paused wins over everything", dict(paused=True, runs=0, age_h=None, ttl_h=None), "paused"),
    ("fresh output inside ttl", dict(paused=False, runs=5, age_h=10.0, ttl_h=24.0), "ok"),
    ("running but output frozen", dict(paused=False, runs=78, age_h=13 * 24.0, ttl_h=24.0), "STALE"),
    ("never runs, no output", dict(paused=False, runs=0, age_h=None, ttl_h=24.0), "idle"),
    ("runs but output unreadable", dict(paused=False, runs=40, age_h=None, ttl_h=24.0), "unknown"),
    ("short ttl respects the floor", dict(paused=False, runs=5, age_h=30.0, ttl_h=1.0), "ok"),
    ("event-driven, quiet a week, 168h declared", dict(paused=False, runs=5, age_h=7 * 24.0, ttl_h=None, max_silence_h=168), "ok"),
    ("event-driven, quiet a month, 168h declared", dict(paused=False, runs=5, age_h=30 * 24.0, ttl_h=None, max_silence_h=168), "STALE"),
    ("event-driven over a weekend, 72h declared", dict(paused=False, runs=5, age_h=66.0, ttl_h=None, max_silence_h=72), "ok"),
    ("event-driven, nothing declared, output fresh", dict(paused=False, runs=5, age_h=1.0, ttl_h=None), "unknown"),
    ("event-driven, nothing declared, three weeks quiet", dict(paused=False, runs=5, age_h=21 * 24.0, ttl_h=None), "unknown"),
    ("a declared ttl wins over max_silence", dict(paused=False, runs=5, age_h=100.0, ttl_h=24.0, max_silence_h=168), "STALE"),
    ("ran 148x then quit five days ago", dict(paused=False, runs=148, age_h=120.0, ttl_h=6.0, last_run_h=120.0), "STOPPED"),
    ("still firing, output frozen -- the real STALE", dict(paused=False, runs=148, age_h=120.0, ttl_h=6.0, last_run_h=0.5), "STALE"),
    ("weekly producer firing on schedule, output frozen", dict(paused=False, runs=4, age_h=600.0, ttl_h=168.0, last_run_h=100.0), "STALE"),
    ("...and the same producer once it also quits", dict(paused=False, runs=4, age_h=600.0, ttl_h=168.0, last_run_h=600.0), "STOPPED"),
    ("last run unreadable falls back to STALE", dict(paused=False, runs=9, age_h=999.0, ttl_h=6.0, last_run_h=None), "STALE"),
    ("never ran", dict(paused=False, runs=0, age_h=999.0, ttl_h=6.0, last_run_h=None), "idle"),
    ("abstained on its last run, output frozen",
     dict(paused=False, runs=10, age_h=244.0, ttl_h=24.0, last_run_h=10.0, last_status="abstained"), "abstaining"),
    ("abstained, then stopped firing",
     dict(paused=False, runs=10, age_h=244.0, ttl_h=6.0, last_run_h=200.0, last_status="abstained"), "STOPPED"),
    ("dialled 4 days ago, output aged with it -- still an operator decision",
     dict(paused=False, runs=148, age_h=110.0, ttl_h=6.0, last_run_h=100.0, held="richness 1 (template only)",
          held_since_h=100.0), "held"),
    ("dial exactly at the hold bound", dict(paused=False, runs=148, age_h=168.0, ttl_h=6.0, last_run_h=168.0,
                                             held="richness 1 (template only)", held_since_h=168.0), "held"),
    ("dialled to richness 1 and left there 50 days",
     dict(paused=False, runs=0, age_h=1215.0, ttl_h=4.0, last_run_h=1215.0, held="richness 1 (template only)",
          held_since_h=1215.0), "HELD_LONG"),
    ("dialled off (richness 0) past the bound",
     dict(paused=False, runs=0, age_h=200.0, ttl_h=4.0, last_run_h=200.0, held="richness 0 (off)",
          held_since_h=200.0), "HELD_LONG"),
    ("held 3 weeks while still writing template rows",
     dict(paused=False, runs=40, age_h=1.0, ttl_h=4.0, last_run_h=1.0, held="richness 1 (template only)",
          held_since_h=500.0), "HELD_LONG"),
    ("dialled 2h ago over a producer that quit 25 days ago",
     dict(paused=False, runs=10, age_h=600.0, ttl_h=6.0, last_run_h=600.0, held="richness 1 (template only)",
          held_since_h=2.0), "STOPPED"),
    ("the same dead producer, no runs left in 30d: the dial still ages",
     dict(paused=False, runs=0, age_h=1500.0, ttl_h=6.0, last_run_h=None, held="richness 1 (template only)",
          held_since_h=400.0), "HELD_LONG"),
    ("dial age unreadable stays a hold", dict(paused=False, runs=5, age_h=900.0, ttl_h=6.0, last_run_h=900.0,
                                              held="richness 1 (template only)"), "held"),
    ("held with no output ever stays a hold",
     dict(paused=False, runs=0, age_h=None, ttl_h=4.0, held="richness 1 (template only)", held_since_h=10.0), "held"),
    ("budget exhausted today is not a hold",
     dict(paused=False, runs=148, age_h=207.0, ttl_h=6.0, last_run_h=207.0, held="budget exhausted: 31.2/30.0 min"), "STOPPED"),
    ("last run said done, output frozen",
     dict(paused=False, runs=10, age_h=244.0, ttl_h=24.0, last_run_h=10.0, last_status="done"), "STALE"),
    ("nightly edition missed once, producer quit with the box",
     dict(paused=False, runs=4, age_h=75.1, ttl_h=24.0, last_run_h=75.1), "STOPPED"),
    ("nightly edition one day late is already a finding",
     dict(paused=False, runs=4, age_h=49.0, ttl_h=24.0, last_run_h=49.0), "STOPPED"),
    ("nightly edition read the next afternoon", dict(paused=False, runs=4, age_h=30.0, ttl_h=24.0), "ok"),
)  # fmt: skip


@pytest.mark.parametrize(("name", "kw", "want"), _CASES, ids=[c[0] for c in _CASES])
def test_the_verdict_table(name: str, kw: dict, want: str) -> None:
    assert f.verdict(**kw) == want


@pytest.mark.parametrize(
    ("ttl", "ms", "want"),
    [(None, None, None), (None, 72.0, 72.0), (None, 168.0, 168.0), (1.0, None, 36.0), (6.0, None, 36.0),
     (24.0, None, 48.0), (168.0, None, 192.0), (24.0, 168.0, 48.0)],
)  # fmt: skip
def test_the_limit_is_ttl_x3_or_ttl_plus_a_day_never_under_36h(ttl, ms, want) -> None:
    assert f.stale_limit_h(ttl, ms) == want


def test_the_state_list_is_closed_and_findings_are_named() -> None:
    want = {"STALE": True, "STOPPED": True, "NEVER_RAN": True, "unknown": True, "HELD_LONG": True,
            "ok": False, "idle": False, "paused": False, "abstaining": False, "held": False}  # fmt: skip
    assert set(want) == set(f.STATES)
    for state, finding in want.items():
        assert f.is_finding(state) is finding
    with pytest.raises(ValueError, match="not a freshness state"):
        f.is_finding("OK")  # a typo'd state is refused, never a silent non-finding


@pytest.mark.parametrize(
    ("raw", "want"), [("18h", 18.0), ("7d", 168.0), ("90m", 1.5), (None, None), ("nonsense", None), ("5x", None)]
)
def test_ttl_hours_parses_h_d_m(raw, want) -> None:
    assert f.ttl_hours(raw) == want


# --- the clock protocol ------------------------------------------------------------------------


class XnysSessionDays:
    """Hours of [start, end) on XNYS session DAYS (ET dates) -- trade-data's market_calendar.session_hours
    shape, built here only to prove the protocol carries it. None outside the calendar's window."""

    name = "test_session"

    def __init__(self) -> None:
        import exchange_calendars as xcals

        self.cal = xcals.get_calendar("XNYS", start="2026-01-01", end="2027-12-31")

    def hours(self, start: dt.datetime, end: dt.datetime) -> float | None:
        if end <= start:
            return 0.0
        day, last = start.astimezone(ET).date(), end.astimezone(ET).date()
        if day < self.cal.first_session.date() or last > self.cal.last_session.date():
            return None
        total = 0.0
        while day <= last:
            if self.cal.is_session(day.isoformat()):
                lo = max(start, dt.datetime.combine(day, dt.time(), tzinfo=ET))
                hi = min(end, dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=ET))
                total += max(0.0, (hi - lo).total_seconds() / 3600.0)
            day += dt.timedelta(days=1)
        return total


@pytest.fixture(scope="module")
def session_clock() -> XnysSessionDays:
    return f.register_clock(XnysSessionDays())  # type: ignore[return-value]


def _open(y: int, m: int, d: int) -> dt.datetime:
    return dt.datetime(y, m, d, 9, 30, tzinfo=ET)


def _close(y: int, m: int, d: int) -> dt.datetime:
    return dt.datetime(y, m, d, 16, 0, tzinfo=ET)


def _sess(now: dt.datetime, last: dt.datetime, **kw) -> dict:
    h = (now - last).total_seconds() / 3600.0
    return dict(paused=False, runs=50, age_h=h, last_run_h=h, now=now, **kw)


_MON = _open(2026, 11, 2)
_ADV = dict(ttl_h=0.25, clock="test_session")
_SESSION_CASES = (
    ("Monday open, last card at Friday close", _sess(_MON, _close(2026, 10, 30), **_ADV), "ok"),
    ("...the same reading on the wall clock (the false STOPPED)", _sess(_MON, _close(2026, 10, 30), ttl_h=0.25), "STOPPED"),
    ("missed Friday's session", _sess(_MON, _close(2026, 10, 29), **_ADV), "STOPPED"),
    ("missed Friday but still firing: the real STALE", {**_sess(_MON, _close(2026, 10, 29), **_ADV), "last_run_h": 0.5}, "STALE"),
    ("Tuesday open after MLK, last card at Friday close", _sess(_open(2027, 1, 19), _close(2027, 1, 15), **_ADV), "ok"),
    ("Black Friday open, Thanksgiving skipped", _sess(_open(2026, 11, 27), _close(2026, 11, 25), **_ADV), "ok"),
    ("Black Friday, Wednesday's session missed", _sess(_open(2026, 11, 27), _close(2026, 11, 24), **_ADV), "STOPPED"),
    ("dial set Saturday over a Friday card -- the weekend did not age it",
     {**_sess(_MON, _close(2026, 10, 30), **_ADV), "held": "richness 1 (template only)", "held_since_h": 40.0}, "held"),
)  # fmt: skip


@pytest.mark.parametrize(("name", "kw", "want"), _SESSION_CASES, ids=[c[0] for c in _SESSION_CASES])
def test_the_session_clock_table(session_clock, name: str, kw: dict, want: str) -> None:
    assert f.verdict(**kw) == want


def test_a_clock_object_works_without_registration() -> None:
    c = XnysSessionDays()
    kw = _sess(_MON, _close(2026, 10, 30), ttl_h=0.25)
    assert f.verdict(**{**kw, "clock": c}) == "ok"


def test_an_unregistered_clock_name_raises_never_falls_back_to_wall() -> None:
    with pytest.raises(ValueError, match="not registered"):
        f.verdict(paused=False, runs=5, age_h=10.0, ttl_h=24.0, clock="xnys_nobody_registered", now=_MON)


def test_a_non_wall_clock_without_now_raises(session_clock) -> None:
    with pytest.raises(ValueError, match="needs `now`"):
        f.on_clock(10.0, "test_session", None)


def test_an_uncountable_span_is_unknown_never_a_crash(session_clock) -> None:
    # the 1970 sentinel seed: outside the calendar's window -> None -> `unknown` for a producer that runs
    age = (_MON - dt.datetime(1970, 1, 1, tzinfo=ET)).total_seconds() / 3600.0
    assert f.verdict(paused=False, runs=5, age_h=age, ttl_h=24.0, clock="test_session", now=_MON) == "unknown"


def test_registration_is_idempotent_and_refuses_a_different_clock_under_a_known_name(session_clock) -> None:
    assert f.register_clock(session_clock) is session_clock
    with pytest.raises(ValueError, match="different spec"):
        f.register_clock(XnysSessionDays())  # equal name, a different object: refused
    with pytest.raises(TypeError, match="not a Clock"):
        f.register_clock(object())  # type: ignore[arg-type]
    assert f.get_clock("wall") is f.WALL
