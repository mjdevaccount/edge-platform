"""notify_pager: a watchdog page on the REAL notify pipeline with the in-memory doubles (ported from
trade-data tests/test_watchdog_deliver_notify.py). The bool is `Delivery.delivered`, so a failed
page leaves last_page_at NULL and the next sweep retries."""

from __future__ import annotations

import asyncio

import pytest

from edge_platform.notify.pipeline import Notifier
from edge_platform.notify.types import Budget, Channel, Dedup, Outcome
from edge_platform.watch.pager import notify_pager
from edge_platform.watch.spec import Probe, Status, WatchRegistry

from ..notify.fakes import MemoryLedger, RecordingTransport
from .rig import VOCAB, Rig, now, returning, spec

run = asyncio.run
CH = Channel("watchdog")


def _ports():
    reasons: list[str] = []
    tx, led = RecordingTransport(), MemoryLedger()
    return tx, led, reasons, Notifier(transport=tx, ledger=led, on_degraded=reasons.append)


def _spec(severity: str = "p2"):
    return WatchRegistry(VOCAB).register(spec("t-down", returning(Status.DOWN, "it broke"), severity=severity))


@pytest.mark.parametrize(("outcome", "expected"), [
    ("sent", True), ("spooled_night", True), ("digest", True), ("held_reserve", True), ("failed", False),
])  # fmt: skip
def test_the_bool_is_the_delivery_s_bool(outcome, expected) -> None:
    tx, led, _, n = _ports()
    tx.outcome = Outcome(outcome)
    assert run(notify_pager(n, CH)(_spec(), Probe(Status.DOWN, "it broke"))) is expected
    assert [r["outcome"] for r in led.rows] == [outcome]


def test_a_failed_page_degrades_and_is_a_ledger_row() -> None:
    tx, led, reasons, n = _ports()
    tx.outcome = Outcome.FAILED
    assert run(notify_pager(n, CH)(_spec(), Probe(Status.DOWN, "x"))) is False
    assert "notify_failed:watchdog" in reasons
    assert led.rows[0]["scope"] == "watchdog" and led.rows[0]["key"] == "t-down"


@pytest.mark.parametrize("tier", ["p2", "p3"])
def test_the_page_carries_the_spec_s_tier_title_severity_and_detail(tier) -> None:
    tx, _, _, n = _ports()
    s = _spec(tier)
    run(notify_pager(n, CH)(s, Probe(Status.DOWN, "the detail")))
    sent = tx.sent[0]
    assert (sent["title"], sent["body"], sent["tier"]) == (s.title, "the detail", tier)
    assert sent["severity"] == s.ntfy_severity == {"p2": "act_now", "p3": "heads_up"}[tier]
    assert sent["market"] is False and sent["tags"] == ["rotating_light"]


def test_no_dedup_in_the_ledger_the_state_machine_owns_it() -> None:
    tx, _, _, n = _ports()
    page = notify_pager(n, CH)
    for _ in range(3):
        run(page(_spec(), Probe(Status.DOWN, "x")))
    assert len(tx.sent) == 3


@pytest.mark.parametrize("ch", [Channel("w", dedup=Dedup.once()), Channel("w", budget=Budget(5))])
def test_a_deduping_or_budgeted_channel_is_refused(ch) -> None:
    with pytest.raises(ValueError, match="state machine owns dedup"):
        notify_pager(_ports()[3], ch)


def test_an_unconfigured_transport_is_not_a_page() -> None:
    tx, led, _, n = _ports()
    tx.is_disabled = True
    assert run(notify_pager(n, CH)(_spec(), Probe(Status.DOWN, "x"))) is False
    assert tx.sent == [] and led.rows[0]["outcome"] == "disabled"


def test_through_the_runner_a_notice_that_cannot_be_built_never_raises() -> None:
    """A spec whose page cannot be built (here: a tier the notify types refuse) reaches the runner as
    an exception, which it reports as watchdog_page_failed instead of dying."""
    tx, _, _, n = _ports()
    rig = Rig(page=notify_pager(n, CH))
    s = spec("t-down", returning(Status.DOWN))
    object.__setattr__(s, "severity", "p9")  # past the spec's own check, as a corrupt spec would be
    rig.register(s)
    res = run(rig.runner.run(object(), now=now()))
    assert res["paged"] == [] and rig.degraded == ["watchdog_page_failed"] and tx.sent == []


def test_end_to_end_a_down_watchdog_pages_once_through_the_pipeline() -> None:
    tx, led, _, n = _ports()
    rig = Rig(page=notify_pager(n, CH))
    rig.register(spec("t-down", returning(Status.DOWN, "it broke")))
    run(rig.runner.run(object(), now=now(9, 30)))
    run(rig.runner.run(object(), now=now(9, 40)))
    assert len(tx.sent) == 1 and led.rows[0]["outcome"] == "sent"
    assert rig.store.rows["t-down"]["last_page_at"] == now(9, 30)
