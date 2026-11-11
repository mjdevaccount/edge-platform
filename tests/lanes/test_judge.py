"""`judge(spec, obs, now=)` -- the one entry point (lanes.md PL9, acceptance tests 1-3).

The session-clock cases run on `clocks.TestRthClock` (XNYS regular sessions from exchange_calendars,
registered under a test name) -- the shape a consumer registers at its composition root."""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from edge_platform.lanes import freshness as f
from edge_platform.lanes.freshness import Observation, judge
from edge_platform.lanes.spec import LaneSpec

from .clocks import et, rth

LONG_AGO = et(2026, 1, 5, 12)


def _lane(**kw) -> LaneSpec:
    d = dict(
        id="board", kind="data", transport="api", entrypoint="sources.board", domain_tables=("quant.board",),
        freshness=("quant.board", "ts"), cadence_h=1 / 30, due_h=0.25, clock="t_xnys_rth",
        schedules=("flow:board",), note="2-minute session lane", work_unit="board",
    )  # fmt: skip
    d.update(kw)
    return LaneSpec(**d)


def _obs(out: dt.datetime | None, run: dt.datetime | None = "same", *, runs: int = 50, **kw) -> Observation:  # type: ignore[assignment]
    return Observation(newest_output=out, last_run=out if run == "same" else run, last_status=kw.pop("st", "ok"),
                       runs=runs, registered_at=kw.pop("reg", LONG_AGO), **kw)  # fmt: skip


FRI_1558 = et(2026, 11, 6, 15, 58)
SUN_0300 = et(2026, 11, 8, 3)
MON_0950 = et(2026, 11, 9, 9, 50)
MON_0940 = et(2026, 11, 9, 9, 40)

# (name, spec overrides, observation, now, state)
_CASES = (
    ("gated lane, 03:00 ET Sunday, output Friday 15:58", {}, _obs(FRI_1558), SUN_0300, "ok"),
    ("same lane Monday 09:50, no run since Friday", {}, _obs(FRI_1558), MON_0950, "STOPPED"),
    ("Monday 09:40: 12 session-minutes, still inside 15", {}, _obs(FRI_1558), MON_0940, "ok"),
    ("runs every 2 minutes, output frozen since Friday", {}, _obs(FRI_1558, MON_0950 - dt.timedelta(minutes=1)),
     MON_0950, "STALE"),
    ("declined on its last run", {}, _obs(FRI_1558, MON_0950, st="abstained"), MON_0950, "abstaining"),
    ("never ran, registered last week", {}, _obs(None, None, runs=0, reg=et(2026, 11, 2, 9, 30)), MON_0950,
     "NEVER_RAN"),
    ("never ran, registered over the weekend (0 session hours)", {}, _obs(None, None, runs=0, reg=SUN_0300),
     MON_0940, "idle"),
    ("running, output unreadable", {}, _obs(None, MON_0950), MON_0950, "unknown"),
    ("paused", {}, _obs(FRI_1558, paused=True), MON_0950, "paused"),
    ("held by the dial", {}, _obs(FRI_1558, held="richness 1 (template)", held_since=SUN_0300), MON_0950, "held"),
    ("held 10 days", {}, _obs(FRI_1558, held="richness 0 (off)", held_since=et(2026, 10, 30, 9)), MON_0950,
     "HELD_LONG"),
    ("no freshness column", dict(freshness=None, work_unit=None, heartbeat="board"), _obs(FRI_1558), MON_0950,
     "unknown"),
    ("retired", dict(retired="2026-11-01 gone", schedules=("manual",), freshness=None, work_unit=None),
     _obs(None, None, runs=0), MON_0950, "idle"),
    ("wall lane, nightly, read next afternoon", dict(cadence_h=24.0, due_h=None, clock="wall"),
     _obs(et(2026, 11, 8, 2)), et(2026, 11, 8, 20), "ok"),
    ("wall lane, nightly, two editions missed", dict(cadence_h=24.0, due_h=None, clock="wall"),
     _obs(et(2026, 11, 5, 2)), et(2026, 11, 8, 20), "STOPPED"),
    ("event lane with no limit cannot be judged", dict(cadence_h=None, due_h=None, clock="wall", work_unit=None,
     heartbeat="board"), _obs(FRI_1558), MON_0950, "unknown"),
)  # fmt: skip


@pytest.mark.parametrize(("name", "kw", "obs", "now", "want"), _CASES, ids=[c[0] for c in _CASES])
def test_the_judge_table_covers_every_state(name, kw, obs, now, want) -> None:
    rth()
    j = judge(_lane(**kw), obs, now=now)
    assert j.state == want, j
    assert j.why and j.clock == (kw.get("clock") or "t_xnys_rth")


def test_every_state_is_reached_by_the_table() -> None:
    assert {c[-1] for c in _CASES} == set(f.STATES)


def test_ages_are_reported_in_the_lane_s_own_clock() -> None:
    rth()
    j = judge(_lane(), _obs(FRI_1558), now=MON_0950)
    assert j.limit_h == 0.25 and j.age_h == pytest.approx(22 / 60) and j.silence_h == pytest.approx(22 / 60)
    assert "t_xnys_rth" in j.why


@pytest.mark.parametrize(("limits_ago", "want"), [(2.0, "NEVER_RAN"), (0.5, "idle")])
def test_never_ran_must_fire_both_ways(limits_ago: float, want: str) -> None:
    """Acceptance 2: registered two limits ago with zero runs is a finding; half a limit ago, idle.
    (v0.9 read `idle` forever: a guard that could not fire for a lane nobody wired.)"""
    spec = _lane(cadence_h=6.0, due_h=12.0, clock="wall")
    now = et(2026, 11, 9, 12)
    reg = now - dt.timedelta(hours=12.0 * limits_ago)
    j = judge(spec, _obs(None, None, runs=0, reg=reg), now=now)
    assert j.state == want and f.is_finding(j.state) is (want == "NEVER_RAN")
    manual = dataclasses.replace(spec, schedules=("manual",), work_unit=None)
    assert judge(manual, _obs(None, None, runs=0, reg=reg), now=now).state == "idle"  # nothing schedules it


def test_a_clock_that_cannot_answer_is_unknown_never_the_wall_fallback() -> None:
    """Acceptance 3 / invariant 2: hours() -> None makes the lane `unknown`. On the wall clock the
    same reading would be `ok` (2 wall-minutes old) -- proving no fallback happened."""
    clock = rth()
    now = FRI_1558 + dt.timedelta(minutes=2)
    assert judge(_lane(), _obs(FRI_1558), now=now).state == "ok"
    clock.answer = False
    try:
        j = judge(_lane(), _obs(FRI_1558), now=now)
    finally:
        clock.answer = True
    assert j.state == "unknown" and "cannot count" in j.why and j.age_h is None


def test_bad_data_is_unknown_and_an_unregistered_clock_raises_lookup() -> None:
    rth()
    naive = dt.datetime(2026, 11, 6, 15, 58)  # no tzinfo: subtracting it from an aware now raises
    j = judge(_lane(), _obs(naive), now=MON_0950)
    assert j.state == "unknown" and "unreadable" in j.why
    with pytest.raises(LookupError, match="not registered"):
        judge(_lane(clock="t_never_registered"), _obs(FRI_1558), now=MON_0950)
