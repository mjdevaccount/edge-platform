"""SourceGate: the switch reads, fails open LOUDLY, rolls back the caller's session, and input_absent
returns its own reason for each branch (ported from trade-data tests/test_source_gate.py). v0.11:
input_absent goes through `freshness.judge` -- the gate and the page cannot disagree, and a session
lane is not "absent" overnight (lanes.md acceptance 5)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import pytest

from edge_platform.lanes.freshness import Observation
from edge_platform.lanes.gate import SourceGate
from edge_platform.lanes.spec import Home, LaneRegistry, LaneSpec, LaneVocab

from .clocks import et, rth

MIRROR = "quant.content_source_registry.enabled = false"
NOW = dt.datetime(2026, 9, 13, 9, 0, tzinfo=dt.UTC)


class _Session:
    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


class _World:
    def __init__(self) -> None:
        self.disabled: list[str] = []
        self.raise_disabled = False
        self.newest: dt.datetime | None = NOW - dt.timedelta(hours=3)
        self.raise_probe = False
        self.degraded: list[str] = []
        self.asked: list[str] = []

    async def read_disabled(self, _s):
        if self.raise_disabled:
            raise RuntimeError("UndefinedTableError: quant.content_source_registry")
        return list(self.disabled)

    async def observe(self, _s, spec: LaneSpec) -> Observation:
        self.asked.append(spec.id)  # the contract: the gate asks about THE lane it was given
        if self.raise_probe:
            raise RuntimeError("UndefinedColumn")
        return Observation(newest_output=self.newest, last_run=self.newest, last_status="ok", runs=10,
                           registered_at=NOW - dt.timedelta(days=30))  # fmt: skip


@pytest.fixture
def world() -> _World:
    return _World()


def _walter(**kw) -> LaneSpec:
    d = dict(
        id="walter", kind="content", transport="gmail_label", entrypoint="sources.walter",
        domain_tables=("domain.terminal_alerts",), freshness=("domain.terminal_alerts", "received_at"),
        cadence_h=100.0, schedules=("flow:walter",), note="the wire", heartbeat="walter",
    )  # fmt: skip
    d.update(kw)
    return LaneSpec(**d)


def _gate(world: _World, *specs: LaneSpec, now=lambda: NOW, on_degraded=None) -> SourceGate:
    reg = LaneRegistry(LaneVocab(homes={"trade": Home()}, default_home="trade"))
    for s in specs:
        reg.register(s)
    return SourceGate(
        reg, read_disabled=world.read_disabled, observe=world.observe, mirror=MIRROR,
        on_degraded=on_degraded if on_degraded is not None else world.degraded.append, now=now,
    )  # fmt: skip


@pytest.fixture
def gate(world: _World) -> SourceGate:
    return _gate(world, _walter())


def _run(coro):
    return asyncio.run(coro)


def test_disabled_sources_reads_the_switch_and_fails_open_loudly(gate, world, caplog) -> None:
    world.disabled = ["walter"]
    assert _run(gate.disabled_sources(_Session())) == frozenset({"walter"})
    world.raise_disabled = True
    sess = _Session()
    with caplog.at_level(logging.WARNING, logger="edge_platform.lanes.gate"):
        assert _run(gate.disabled_sources(sess)) == frozenset()  # nothing disabled, never a crash
    assert sess.rollbacks == 1  # and the caller's session is not poisoned
    assert "content_source_mirror_unreadable" in caplog.text
    assert world.degraded and "RuntimeError" in world.degraded[0]


def test_a_raising_degraded_hook_does_not_become_the_failure(world) -> None:
    def boom(_r: str) -> None:
        raise RuntimeError("telemetry down")

    g = _gate(world, on_degraded=boom)
    world.raise_disabled = True
    assert _run(g.disabled_sources(_Session())) == frozenset()


def test_disabled_reason_is_one_wording(gate) -> None:
    assert gate.disabled_reason("walter", frozenset({"walter"})) == f"source walter disabled ({MIRROR})"
    assert gate.disabled_reason("walter", frozenset()) is None


def test_input_absent_has_a_reason_for_each_branch(gate, world) -> None:
    s = _Session()
    assert _run(gate.input_absent(s, "walter")) is None  # present on a normal day
    assert world.asked == ["walter"]
    world.disabled = ["walter"]
    assert "source walter disabled" in (_run(gate.input_absent(s, "walter")) or "")
    world.disabled = []
    world.newest = None
    assert "no readable rows" in (_run(gate.input_absent(s, "walter")) or "")
    # cadence 100h, no due_h -> the gate holds cadence_h (v0.9), NOT the page's min(300, 124) = 124h
    world.newest = NOW - dt.timedelta(hours=125)
    assert "STOPPED" in (_run(gate.input_absent(s, "walter")) or "")  # its last run is as old as its output
    world.newest = NOW - dt.timedelta(hours=101)
    assert "> cadence 100h" in (_run(gate.input_absent(s, "walter")) or "")  # page: ok; gate: absent
    world.newest = NOW - dt.timedelta(hours=99)
    assert _run(gate.input_absent(s, "walter")) is None
    world.raise_probe = True
    assert "unreadable" in (_run(gate.input_absent(s, "walter")) or "")
    assert s.rollbacks == 1


def test_an_unknown_or_unlimited_lane_is_not_absent(world) -> None:
    g = _gate(world, _walter(id="event_lane", cadence_h=None, heartbeat=None))
    assert _run(g.input_absent(_Session(), "never_declared")) is None
    assert _run(g.input_absent(_Session(), "event_lane")) is None and world.asked == []


def test_mirror_is_required(world) -> None:
    reg = LaneRegistry(LaneVocab(homes={"m": Home()}, default_home="m"))
    with pytest.raises(ValueError, match="mirror"):
        SourceGate(reg, read_disabled=world.read_disabled, observe=world.observe, mirror=" ")


# --- lanes.md acceptance 5: a session lane overnight is not absent -------------------------------


def test_a_session_lane_overnight_is_present_and_the_wall_hours_bug_is_gone(world) -> None:
    rth()
    gated = _walter(id="board", cadence_h=1 / 30, due_h=0.25, clock="t_xnys_rth")
    wall = _walter(id="board_wall", cadence_h=1 / 30, due_h=0.25)  # the same limit counted in WALL hours
    sunday_3am = et(2026, 11, 8, 3)
    world.newest = et(2026, 11, 6, 15, 58)  # Friday, two minutes before the close
    g = _gate(world, gated, wall, now=lambda: sunday_3am)
    assert _run(g.input_absent(_Session(), "board")) is None  # 2 session-minutes old: present
    assert _run(g.input_absent(_Session(), "board_wall")) is not None  # v0.9 wall model: absent every night
    monday = _gate(world, gated, now=lambda: et(2026, 11, 9, 9, 50))  # 20 session-minutes in, no run
    assert "STOPPED" in (_run(monday.input_absent(_Session(), "board")) or "")


# --- review of PR #18: an undeclared limit fails CLOSED at the gate ------------------------------


class _Hourly(_World):
    """An hourly lane that RUNS (last run 5 minutes ago) but whose newest output is old."""

    def __init__(self, output_age_h: float) -> None:
        super().__init__()
        self.output_age_h = output_age_h

    async def observe(self, _s, spec: LaneSpec) -> Observation:
        return Observation(newest_output=NOW - dt.timedelta(hours=self.output_age_h),
                           last_run=NOW - dt.timedelta(minutes=5), last_status="ok", runs=500,
                           registered_at=NOW - dt.timedelta(days=30))  # fmt: skip


@pytest.mark.parametrize(
    ("output_age_h", "due_h", "absent"),
    [
        (20.0, None, True),  # v0.11-rc: present (page limit 36h floor); v0.9 + trade-data main: absent
        (0.5, None, False),  # inside its cadence: present
        (20.0, 24.0, False),  # a DECLARED due_h is the gate's limit too: present, same as the page
        (25.0, 24.0, True),  # ...and past it, absent
    ],
)
def test_an_hourly_lane_with_old_output_is_absent_unless_a_due_h_says_otherwise(
    output_age_h: float, due_h: float | None, absent: bool
) -> None:
    world = _Hourly(output_age_h)
    g = _gate(world, _walter(id="hourly", cadence_h=1.0, due_h=due_h))
    reason = _run(g.input_absent(_Session(), "hourly"))
    assert (reason is not None) is absent, reason
    if absent and due_h is None:
        assert "> cadence 1h" in reason and "no due_h" in reason
