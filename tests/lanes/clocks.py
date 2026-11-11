"""A session clock for the tests, built the way a consumer would build one: exchange_calendars' XNYS
open/close per session (half days included), counting only the hours inside them. The library itself
holds no calendar -- this lives in the tests, registered under a test-only name."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from edge_platform.lanes import freshness as f

ET = ZoneInfo("America/New_York")


class TestRthClock:
    """Hours of [start, end] inside XNYS regular sessions. None outside the calendar's window."""

    __test__ = False  # not a pytest class
    name = "t_xnys_rth"

    def __init__(self) -> None:
        import exchange_calendars as xcals

        self.cal = xcals.get_calendar("XNYS", start="2026-01-01", end="2027-12-31")
        self.answer: bool = True  # flip to False to make the clock "unreadable"

    def hours(self, start: dt.datetime, end: dt.datetime) -> float | None:
        if not self.answer:
            return None
        if end <= start:
            return 0.0
        first, last = start.astimezone(ET).date(), end.astimezone(ET).date()
        if first < self.cal.first_session.date() or last > self.cal.last_session.date():
            return None
        total, day = 0.0, first
        while day <= last:
            if self.cal.is_session(day.isoformat()):
                lo = max(start, self.cal.session_open(day.isoformat()).to_pydatetime())
                hi = min(end, self.cal.session_close(day.isoformat()).to_pydatetime())
                total += max(0.0, (hi - lo).total_seconds() / 3600.0)
            day += dt.timedelta(days=1)
        return total


_CLOCK: TestRthClock | None = None


def rth() -> TestRthClock:
    """The one registered instance (registering a second object under the name would be refused)."""
    global _CLOCK
    if _CLOCK is None:
        _CLOCK = TestRthClock()
        f.register_clock(_CLOCK)
    _CLOCK.answer = True
    return _CLOCK


def et(y: int, mo: int, d: int, h: int, mi: int = 0) -> dt.datetime:
    return dt.datetime(y, mo, d, h, mi, tzinfo=ET)
