"""The freshness verdict: is a producer WIRED, judged by OUTPUT AGE. Pure, stdlib, clock injected.

Lifted from trade-data `service/usecases/producer_freshness.py` (M6 PL4): the verdict rule only --
collecting the ages (SQL) and paging on them (the watchdog plane) stay with the consumer.

WHY OUTPUT AGE: run counts lie (78 `ok` runs, nothing fetched for 13 days -- NOT-DUE IS
INDISTINGUISHABLE FROM DID-IT) and box calls lie. THE LIMIT: ttl x 3 OR ttl + one day, whichever is
SMALLER, never under a 36h floor (a daily lane missed two editions under a flat 3x). A ttl-less
producer uses its DECLARED max silence; one declaring nothing is `unknown` -- a finding.

THE CLOSED STATE LIST (`STATES`): ok · STALE (write path) · STOPPED (scheduler) · idle · paused ·
held (an operator dial) · HELD_LONG (a dial nobody revisited) · abstaining (declined with a reason) ·
unknown (cannot measure: NEVER ok). `FINDING_STATES` are the ones that page.

THE CLOCK IS A PROTOCOL. Silence is wall hours unless a producer is judged on another clock -- a
producer that writes only on exchange session days reads STOPPED every weekend on the wall clock.
No exchange calendar lives here: a consumer registers its clock (`register_clock(XnysClock())`) and
names it per producer; an unknown name raises, never falls back to wall hours (that fallback IS the
false STOPPED).
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable

from edge_platform.registry import Registry

SLACK = 3.0  # multiple of the cadence before STALE (short cadences)
GRACE_H = 24.0  # one day past the cadence before STALE (long cadences)
FLOOR_H = 36.0  # never call something stale sooner than this, whatever its ttl
#: A dial hold is an operator decision until the dial is this old; then it is a decision nobody
#: revisited (a beat sat at richness 1 for 50 days while the verdict read OK).
HELD_MAX_H = 7 * 24.0
STATES: tuple[str, ...] = ("ok", "STALE", "STOPPED", "idle", "paused", "held", "HELD_LONG", "abstaining", "unknown")
FINDING_STATES: tuple[str, ...] = ("STALE", "STOPPED", "unknown", "HELD_LONG")
#: A hold reason starting with this is the operator's dial (a budget block resets daily: not a hold).
HOLD_PREFIX = "richness"


@runtime_checkable
class Clock(Protocol):
    """Counts the hours between two instants that matter to a producer. None = uncountable (outside
    the clock's known range) -> the verdict says `unknown`, never a crashed probe."""

    name: str

    def hours(self, start: dt.datetime, end: dt.datetime) -> float | None: ...


class WallClock:
    name = "wall"

    def hours(self, start: dt.datetime, end: dt.datetime) -> float | None:
        return (end - start).total_seconds() / 3600.0


WALL = WallClock()
_CLOCKS: Registry[Clock] = Registry("silence clock", key=lambda c: c.name, identical_ok=True)
_CLOCKS.add(WALL)


def register_clock(clock: Clock) -> Clock:
    """Make `clock` nameable (`clock.name`). Re-registering the same object is a no-op; a different
    clock under a known name raises."""
    if not isinstance(clock, Clock):
        raise TypeError(f"{clock!r} is not a Clock (needs .name and .hours(start, end))")
    return _CLOCKS.add(clock)


def get_clock(name: str) -> Clock:
    clock = _CLOCKS.get(name)
    if clock is None:
        raise ValueError(f"silence clock {name!r} is not registered (known: {', '.join(_CLOCKS)})")
    return clock


def stale_limit_h(ttl_h: float | None, max_silence_h: float | None = None) -> float | None:
    """Hours of output age past which a producer is judged. A declared cadence wins; a ttl-less
    producer is judged by its declared max silence; neither -> None (`verdict` reads `unknown`)."""
    if ttl_h:
        return max(FLOOR_H, min(ttl_h * SLACK, ttl_h + GRACE_H))
    return float(max_silence_h) if max_silence_h else None


def ttl_hours(raw: str | None) -> float | None:
    """A declared cadence ('18h', '7d', '90m') in hours; None when absent or unparseable."""
    if not raw:
        return None
    txt = str(raw).strip().lower()
    try:
        if txt.endswith("h"):
            return float(txt[:-1])
        if txt.endswith("d"):
            return float(txt[:-1]) * 24.0
        if txt.endswith("m"):
            return float(txt[:-1]) / 60.0
    except ValueError:
        return None
    return None


def is_finding(state: str) -> bool:
    if state not in STATES:
        raise ValueError(f"{state!r} is not a freshness state {STATES}")
    return state in FINDING_STATES


def on_clock(
    hours_ago: float | None, clock: Clock | str, now: dt.datetime | None, until_h: float = 0.0
) -> float | None:
    """Hours between `hours_ago` and `until_h` (both WALL hours before `now`) counted on `clock`. The
    wall clock is plain subtraction; any other clock needs `now` -- a silent fall back to wall hours
    is the false STOPPED this exists to end."""
    if hours_ago is None:
        return None
    c = get_clock(clock) if isinstance(clock, str) else clock
    if c is WALL or c.name == "wall":
        return hours_ago - until_h
    if now is None:
        raise ValueError(f"silence clock {c.name!r} needs `now`")
    return c.hours(now - dt.timedelta(hours=hours_ago), now - dt.timedelta(hours=until_h))


def verdict(
    *,
    paused: bool,
    runs: int,
    age_h: float | None,
    ttl_h: float | None,
    last_run_h: float | None = None,
    last_status: str | None = None,
    held: str | None = None,
    max_silence_h: float | None = None,
    held_since_h: float | None = None,
    clock: Clock | str = WALL,
    now: dt.datetime | None = None,
) -> str:
    """The state machine, pure. STOPPED vs STALE sends the reader to the right place (scheduler vs
    write path); a run count cannot make that split, the last run's age can. Ages arrive as WALL
    hours and are judged on `clock`."""
    if paused:
        return "paused"
    pre_hold_h = None if held_since_h is None else on_clock(age_h, clock, now, until_h=held_since_h)
    age_h, last_run_h = on_clock(age_h, clock, now), on_clock(last_run_h, clock, now)
    if held and held.startswith(HOLD_PREFIX):  # the dial, an operator decision -- for HELD_MAX_H
        return _held(runs, pre_hold_h, ttl_h, last_run_h, last_status, max_silence_h, held_since_h)
    if age_h is None:
        return "idle" if runs == 0 else "unknown"  # running and unmeasurable is the dangerous case
    limit = stale_limit_h(ttl_h, max_silence_h)
    if limit is None:
        return "unknown"  # nothing declared: no blanket default
    return "ok" if age_h <= limit else _old_output(runs, last_run_h, last_status, limit)


def _held(
    runs: int,
    pre_hold_h: float | None,
    ttl_h: float | None,
    last_run_h: float | None,
    last_status: str | None,
    max_silence_h: float | None,
    held_since_h: float | None,
) -> str:
    """A dial hold, judged on how long the DIAL has been set, never on output age. Output already past
    its limit when the dial was set means the hold covers an earlier failure: that diagnosis wins.
    Dial age unreadable -> held."""
    limit = stale_limit_h(ttl_h, max_silence_h)
    if pre_hold_h is not None and limit is not None and pre_hold_h > limit:
        before = _old_output(runs, last_run_h, last_status, limit)
        if is_finding(before):
            return before
    return "HELD_LONG" if held_since_h is not None and held_since_h > HELD_MAX_H else "held"


def _old_output(runs: int, last_run_h: float | None, last_status: str | None, limit: float) -> str:
    """Output past its limit: never ran = idle; quit firing = STOPPED; declined with a reason =
    abstaining; else the real STALE."""
    if runs == 0:
        return "idle"
    if last_run_h is not None and last_run_h > limit:
        return "STOPPED"
    return "abstaining" if last_status == "abstained" else "STALE"
