"""SourceGate: the switch reads, fails open LOUDLY, rolls back the caller's session, and input_absent
returns its own reason for each branch (ported from trade-data tests/test_source_gate.py)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from edge_platform.lanes.gate import SourceGate
from edge_platform.lanes.spec import Home, LaneRegistry, LaneSpec, LaneVocab

MIRROR = "quant.content_source_registry.enabled = false"


class _Session:
    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


class _World:
    def __init__(self) -> None:
        self.disabled: list[str] = []
        self.raise_disabled = False
        self.fresh: tuple = (10, "2026-09-13 06:00:00", 3.0)
        self.raise_probe = False
        self.degraded: list[str] = []

    async def read_disabled(self, _s):
        if self.raise_disabled:
            raise RuntimeError("UndefinedTableError: quant.content_source_registry")
        return list(self.disabled)

    async def read_freshness(self, _s, key: str, col: str):
        # the contract: the lane's freshness KEY and its ts column (a stub ignoring its arguments
        # would let a column/table swap pass)
        assert (key, col) == ("domain.terminal_alerts", "received_at"), (key, col)
        if self.raise_probe:
            raise RuntimeError("UndefinedColumn")
        return self.fresh


@pytest.fixture
def world() -> _World:
    return _World()


@pytest.fixture
def gate(world: _World) -> SourceGate:
    reg = LaneRegistry(LaneVocab(homes={"trade": Home()}, default_home="trade"))
    reg.register(
        LaneSpec(
            id="walter", kind="content", transport="gmail_label", entrypoint="sources.walter",
            domain_tables=("domain.terminal_alerts",), freshness=("domain.terminal_alerts", "received_at"),
            cadence_h=100.0, schedules=("flow:walter",), note="the wire",
        )
    )  # fmt: skip
    return SourceGate(
        reg, read_disabled=world.read_disabled, read_freshness=world.read_freshness, mirror=MIRROR,
        on_degraded=world.degraded.append,
    )  # fmt: skip


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

    g = SourceGate(LaneRegistry(LaneVocab(homes={"m": Home()}, default_home="m")), read_disabled=world.read_disabled,
                   read_freshness=world.read_freshness, mirror=MIRROR, on_degraded=boom)  # fmt: skip
    world.raise_disabled = True
    assert _run(g.disabled_sources(_Session())) == frozenset()


def test_disabled_reason_is_one_wording(gate) -> None:
    assert gate.disabled_reason("walter", frozenset({"walter"})) == f"source walter disabled ({MIRROR})"
    assert gate.disabled_reason("walter", frozenset()) is None


def test_input_absent_has_a_reason_for_each_branch(gate, world) -> None:
    s = _Session()
    assert _run(gate.input_absent(s, "walter")) is None  # present on a normal day
    world.disabled = ["walter"]
    assert "source walter disabled" in (_run(gate.input_absent(s, "walter")) or "")
    world.disabled = []
    world.fresh = (0, None, None)
    assert "no readable rows" in (_run(gate.input_absent(s, "walter")) or "")
    world.fresh = (10, "2026-09-09 06:00:00", 101.0)
    assert "> cadence 100h" in (_run(gate.input_absent(s, "walter")) or "")
    world.fresh = (10, "2026-09-13 06:00:00", 99.0)
    assert _run(gate.input_absent(s, "walter")) is None
    world.raise_probe = True
    assert "unreadable" in (_run(gate.input_absent(s, "walter")) or "")
    assert s.rollbacks == 1


def test_an_unknown_or_unprobed_lane_is_not_absent(gate) -> None:
    assert _run(gate.input_absent(_Session(), "never_declared")) is None


def test_mirror_is_required(world) -> None:
    with pytest.raises(ValueError, match="mirror"):
        SourceGate(LaneRegistry(LaneVocab(homes={"m": Home()}, default_home="m")), read_disabled=world.read_disabled,
                   read_freshness=world.read_freshness, mirror=" ")  # fmt: skip
