"""The freshness verdict: is a producer WIRED, judged by OUTPUT AGE. Pure, stdlib, clock injected.

Lifted from trade-data `service/usecases/producer_freshness.py` (M6 PL4): the verdict rule only --
collecting the ages (SQL) and paging on them (the watchdog plane) stay with the consumer.

WHY OUTPUT AGE: run counts lie (78 `ok` runs, nothing fetched for 13 days -- NOT-DUE IS
INDISTINGUISHABLE FROM DID-IT) and box calls lie. THE LIMIT: a lane's declared `due_h` (in its own
clock's hours); without one, ttl x 3 OR ttl + one day, whichever is SMALLER, never under a 36h floor
(a daily lane missed two editions under a flat 3x) -- a WALL-clock rule, which is why a lane on any
other clock must declare `due_h` (`LaneSpec` refuses it otherwise). A ttl-less producer uses its
DECLARED max silence; one declaring nothing is `unknown` -- a finding.

THE CLOSED STATE LIST (`STATES`): ok · STALE (runs succeed, output frozen: the write path) · STOPPED
(no finished run within the limit: the scheduler) · NEVER_RAN (scheduled, registered longer than one
limit ago, zero runs -- v0.9 called this `idle` forever, a guard that could not fire for a lane
nobody wired) · idle (not yet due) · paused · held (an operator dial) · HELD_LONG (a dial nobody
revisited) · abstaining (declined with a reason) · unknown (cannot measure: NEVER ok).
`FINDING_STATES` are the ones that page.

THE CLOCK IS A PROTOCOL. Ages are counted on the lane's own clock: a 2-minute session lane is
`cadence_h=1/30, due_h=0.25, clock="<session clock>"`, overnight adds zero hours, so it is `ok` at
03:00 and STOPPED fifteen session-minutes after the open if it has not run. No exchange calendar
lives here: a consumer registers its clock (`register_clock(...)`) and names it per lane; an unknown
name raises `UnknownClock`, and a clock that cannot count a span (`hours() -> None`) makes the lane
`unknown` -- never a fall back to wall hours (that fallback IS the false STOPPED).

`judge(spec, obs, now=)` is the ONLY entry point a consumer calls: it takes datetimes (an
`Observation`), resolves the lane's clock, applies the limit and returns a `Judged` with its `why`.
`verdict(...)` stays as the pure core it calls.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from edge_platform.registry import Registry

if TYPE_CHECKING:
    from .spec import LaneSpec

SLACK = 3.0  # multiple of the cadence before STALE (short cadences)
GRACE_H = 24.0  # one day past the cadence before STALE (long cadences)
FLOOR_H = 36.0  # never call something stale sooner than this, whatever its ttl
#: A dial hold is an operator decision until the dial is this old; then it is a decision nobody
#: revisited (a beat sat at richness 1 for 50 days while the verdict read OK).
HELD_MAX_H = 7 * 24.0
STATES: tuple[str, ...] = (
    "ok", "STALE", "STOPPED", "NEVER_RAN", "idle", "paused", "held", "HELD_LONG", "abstaining", "unknown",
)  # fmt: skip
FINDING_STATES: tuple[str, ...] = ("STALE", "STOPPED", "NEVER_RAN", "unknown", "HELD_LONG")
#: A hold reason starting with this is the operator's dial (a budget block resets daily: not a hold).
HOLD_PREFIX = "richness"


@runtime_checkable
class Clock(Protocol):
    """Counts the hours of THIS clock elapsed in [start, end]. A session clock counts only session
    time. None = the clock cannot say (outside its known range, calendar unreadable) -> the verdict
    says `unknown`, never a crashed probe and never a wall-hours guess."""

    name: str

    def hours(self, start: dt.datetime, end: dt.datetime) -> float | None: ...


class UnknownClock(LookupError, ValueError):
    """A clock name nobody registered. A LookupError (the spec's contract) and a ValueError (v0.9's)."""


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
        raise UnknownClock(f"silence clock {name!r} is not registered (known: {', '.join(_CLOCKS)})")
    return clock


def stale_limit_h(ttl_h: float | None, max_silence_h: float | None = None) -> float | None:
    """WALL hours of output age past which a producer is judged, when it declares no `due_h`. A
    declared cadence wins; a ttl-less producer is judged by its declared max silence; neither ->
    None (`verdict` reads `unknown`)."""
    if ttl_h:
        return max(FLOOR_H, min(ttl_h * SLACK, ttl_h + GRACE_H))
    return float(max_silence_h) if max_silence_h else None


def limit_h(ttl_h: float | None, *, due_h: float | None = None, max_silence_h: float | None = None) -> float | None:
    """The limit rule: `due_h` (in the lane's clock hours) when declared, else `stale_limit_h`."""
    return float(due_h) if due_h else stale_limit_h(ttl_h, max_silence_h)


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
    due_h: float | None = None,
    registered_h: float | None = None,
    scheduled: bool = True,
) -> str:
    """The state machine, pure. STOPPED vs STALE sends the reader to the right place (scheduler vs
    write path); a run count cannot make that split, the last run's age can. Ages arrive as WALL
    hours and are judged on `clock`; `due_h` is in `clock` hours. `registered_h` (wall hours since the
    lane was first seen) and `scheduled` (it has something that fires it) decide NEVER_RAN vs idle."""
    if paused:
        return "paused"
    pre_hold_h = None if held_since_h is None else on_clock(age_h, clock, now, until_h=held_since_h)
    raw = (age_h, last_run_h, registered_h)
    age_c, run_c, reg_c = (on_clock(h, clock, now) for h in raw)
    if any(r is not None and c is None for r, c in zip(raw, (age_c, run_c, reg_c), strict=True)):
        return "unknown"  # the clock cannot count a span we HAVE: never ok, never a wall guess
    limit = limit_h(ttl_h, due_h=due_h, max_silence_h=max_silence_h)
    if held and held.startswith(HOLD_PREFIX):  # the dial, an operator decision -- for HELD_MAX_H
        return _held(runs, pre_hold_h, limit, run_c, last_status, held_since_h, reg_c, scheduled)
    if age_c is None:
        return _never_ran(reg_c, limit, scheduled) if runs == 0 else "unknown"  # running + unmeasurable
    if limit is None:
        return "unknown"  # nothing declared: no blanket default
    return "ok" if age_c <= limit else _old_output(runs, run_c, last_status, limit, reg_c, scheduled)


def _never_ran(registered_h: float | None, limit: float | None, scheduled: bool) -> str:
    """Zero runs: NEVER_RAN once a scheduled lane has been registered longer than one limit, idle
    before that (or when nothing schedules it, or the registration age is unread)."""
    if scheduled and registered_h is not None and limit is not None and registered_h > limit:
        return "NEVER_RAN"
    return "idle"


def _held(
    runs: int,
    pre_hold_h: float | None,
    limit: float | None,
    last_run_h: float | None,
    last_status: str | None,
    held_since_h: float | None,
    registered_h: float | None,
    scheduled: bool,
) -> str:
    """A dial hold, judged on how long the DIAL has been set (wall hours: an operator decision ages on
    the calendar), never on output age. Output already past its limit when the dial was set means the
    hold covers an earlier failure: that diagnosis wins. Dial age unreadable -> held."""
    if pre_hold_h is not None and limit is not None and pre_hold_h > limit:
        before = _old_output(runs, last_run_h, last_status, limit, registered_h, scheduled)
        if is_finding(before) and before != "NEVER_RAN":
            return before
    return "HELD_LONG" if held_since_h is not None and held_since_h > HELD_MAX_H else "held"


def _old_output(
    runs: int, last_run_h: float | None, last_status: str | None, limit: float, registered_h: float | None,
    scheduled: bool,
) -> str:  # fmt: skip
    """Output past its limit: quit firing = STOPPED; no runs and no last run = NEVER_RAN / idle by
    registration age; declined with a reason = abstaining; else the real STALE."""
    if last_run_h is not None and last_run_h > limit:
        return "STOPPED"
    if runs == 0:
        return _never_ran(registered_h, limit, scheduled)
    return "abstaining" if last_status == "abstained" else "STALE"


# -- judge: the one entry point ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Observation:
    """What the consumer READ for one lane, in datetimes (never pre-computed hours)."""

    newest_output: dt.datetime | None  # max(freshness column); None = no readable rows
    last_run: dt.datetime | None  # run ledger: newest FINISHED run (a heartbeat row for daemons)
    last_status: str | None
    runs: int  # runs in the ledger's window
    registered_at: dt.datetime | None  # first time the consumer's mirror saw this lane
    paused: bool = False
    held_since: dt.datetime | None = None
    held: str | None = None


@dataclass(frozen=True, slots=True)
class Judged:
    state: str
    clock: str
    limit_h: float | None  # in `clock` hours
    age_h: float | None  # newest output's age, `clock` hours
    silence_h: float | None  # since the last finished run, `clock` hours
    why: str


def _hours_ago(t: dt.datetime | None, now: dt.datetime) -> float | None:
    return None if t is None else (now - t).total_seconds() / 3600.0


def _fmt(h: float | None) -> str:
    return "unread" if h is None else f"{h:.2f}h"


def judge(spec: LaneSpec, obs: Observation, *, now: dt.datetime) -> Judged:
    """Judge one lane. Never raises on DATA (a bad observation is `unknown` with its why); raises
    `UnknownClock` (a LookupError) only for a spec whose clock is not registered, which
    `LaneRegistry.register` already refuses."""
    clock = get_clock(spec.clock)
    lim = limit_h(spec.cadence_h, due_h=spec.due_h)

    def out(state: str, why: str, age: float | None = None, silence: float | None = None) -> Judged:
        return Judged(state, clock.name, lim, age, silence, why)

    if obs.paused:
        return out("paused", "switched off by the operator")
    if spec.retired:
        return out("idle", f"retired {spec.retired}")
    if spec.freshness is None:
        return out("unknown", "declares no freshness column: output age cannot be measured")
    try:
        state = verdict(
            paused=False, runs=obs.runs, age_h=_hours_ago(obs.newest_output, now), ttl_h=spec.cadence_h,
            last_run_h=_hours_ago(obs.last_run, now), last_status=obs.last_status, held=obs.held,
            held_since_h=_hours_ago(obs.held_since, now), clock=clock, now=now, due_h=spec.due_h,
            registered_h=_hours_ago(obs.registered_at, now), scheduled=spec.schedules != ("manual",),
        )  # fmt: skip
        age = on_clock(_hours_ago(obs.newest_output, now), clock, now)
        silence = on_clock(_hours_ago(obs.last_run, now), clock, now)
    except UnknownClock:
        raise
    except Exception as exc:  # a naive datetime, a negative span, a broken clock: unknown, never a crash
        return out("unknown", f"observation unreadable: {type(exc).__name__}: {exc}"[:300])
    return out(state, _why(state, obs, clock.name, lim, age, silence), age, silence)


def _why(state: str, obs: Observation, clock: str, lim: float | None, age: float | None, sil: float | None) -> str:
    at = f"on clock {clock!r}, limit {_fmt(lim)}"
    if state == "unknown":
        if lim is None:
            return "declares no limit (cadence_h / due_h): nothing to judge the output age against"
        if obs.newest_output is None:
            return f"runs ({obs.runs}) but its output is unreadable"
        return f"clock {clock!r} cannot count the span (calendar out of range or unreadable)"
    return {
        "ok": f"newest output {_fmt(age)} old {at}",
        "STALE": f"runs succeed (last {_fmt(sil)} ago) but newest output is {_fmt(age)} old {at}",
        "STOPPED": f"no finished run for {_fmt(sil)} {at} -- the scheduler",
        "NEVER_RAN": f"scheduled, registered at {obs.registered_at}, zero runs {at}",
        "idle": f"zero runs, not yet due {at}",
        "abstaining": f"declined on its last run ({obs.last_status}), output {_fmt(age)} old {at}",
        "held": f"held by the operator: {obs.held}",
        "HELD_LONG": f"held since {obs.held_since} and never revisited: {obs.held}",
    }.get(state, state)
