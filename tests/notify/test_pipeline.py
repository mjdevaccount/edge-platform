"""Notifier: the one push path -- dedup, budget, publish, record, never raise (ported from trade-data
tests/platform/test_notify.py; the ledger laws run here on the in-memory double and in
test_ledger_pg.py on SqlLedger)."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from edge_platform.fence.sessions import ClockBlind
from edge_platform.notify import types as nt
from edge_platform.notify.local import LocalLedger
from edge_platform.notify.pipeline import Notifier, default_tier_for
from edge_platform.notify.types import Budget, Channel, Dedup, Notice, Outcome

from . import ledger_contract as contract
from .fakes import MemoryLedger, RecordingTransport

NOW = contract.NOW
run = asyncio.run

#: trade-data's legacy severity map, as a CONSUMER would pass it -- the library defaults to p3.
_LEGACY = {"heads_up": "p2", "act_now": "p2", "high": "p1"}


def _tier_for(severity: str, tier: str | None) -> str:
    return tier or _LEGACY.get(severity, "p3")


def _notifier(tx=None, led=None, degraded=None, **kw) -> Notifier:
    return Notifier(transport=tx or RecordingTransport(), ledger=led or MemoryLedger(), tier_for=_tier_for,
                    on_degraded=(degraded.append if degraded is not None else None), **kw)  # fmt: skip


def _n(ch: Channel, key: str = "k", **kw) -> Notice:
    return Notice(ch, key=key, title="T", body="B", **kw)


@pytest.mark.parametrize("case", contract.CASES, ids=[c.__name__ for c in contract.CASES])
def test_memory_ledger_obeys_the_contract(case) -> None:
    led = MemoryLedger()

    async def age(scope: str, key: str, seconds: float) -> None:
        led.claims[(scope, key)] -= dt.timedelta(seconds=seconds)

    run(case(led, age))


def test_a_sent_notice_is_recorded_and_delivered() -> None:
    led, tx = MemoryLedger(), RecordingTransport()
    d = run(_notifier(tx, led).notify(_n(Channel("s"), severity="heads_up", tags=("x",), actions=("a",)), now=NOW))
    assert (d.outcome, d.delivered, d.recorded) == (Outcome.SENT, True, True)
    assert tx.sent == [{"title": "T", "body": "B", "severity": "heads_up", "tags": ["x"], "tier": None,
                        "market": False, "actions": ["a"]}]  # fmt: skip
    assert led.rows == [{"scope": "s", "key": "k", "tier": "p2", "outcome": "sent", "delivered": True,
                         "title": "T", "detail": None}]  # fmt: skip


def test_disabled_claims_nothing_and_records_it() -> None:
    led = MemoryLedger()
    ch = Channel("s", dedup=Dedup.once(), budget=Budget(1))
    d = run(_notifier(RecordingTransport(is_disabled=True), led).notify(_n(ch), now=NOW))
    assert (d.outcome, d.delivered) == (Outcome.DISABLED, False)
    assert led.claims == {} and led.spent == {}
    assert [r["outcome"] for r in led.rows] == ["disabled"]


def test_a_duplicate_is_not_published_and_not_a_row() -> None:
    led, tx = MemoryLedger(), RecordingTransport()
    ch = Channel("s", dedup=Dedup.once())
    n = _notifier(tx, led)
    run(n.notify(_n(ch), now=NOW))
    d = run(n.notify(_n(ch), now=NOW))
    assert (d.outcome, d.delivered) == (Outcome.DEDUPED, False)
    assert len(tx.sent) == 1 and len(led.rows) == 1 and led.suppressed == {("s", "k"): 1}


def test_a_budget_refusal_is_recorded_degraded_and_unpublished() -> None:
    led, tx, degraded = MemoryLedger(), RecordingTransport(), []
    d = run(_notifier(tx, led, degraded).notify(_n(Channel("s", budget=Budget(0))), now=NOW))
    assert (d.outcome, d.delivered, d.detail) == (Outcome.BUDGET_REFUSED, False, "utc-day:2026-10-23")
    assert tx.sent == [] and led.rows[0]["outcome"] == "budget_refused"
    assert degraded == ["notify_budget_refused:s"]


def test_a_failed_publish_is_recorded_and_degraded() -> None:
    led, degraded = MemoryLedger(), []
    tx = RecordingTransport(outcome=Outcome.FAILED, detail="HTTP 429")
    d = run(_notifier(tx, led, degraded).notify(_n(Channel("s")), now=NOW))
    assert (d.outcome, d.delivered, d.detail) == (Outcome.FAILED, False, "HTTP 429")
    assert led.rows[0]["detail"] == "HTTP 429" and degraded == ["notify_failed:s"]


def test_a_raising_transport_is_a_failed_delivery_not_an_exception() -> None:
    led = MemoryLedger()
    d = run(_notifier(RecordingTransport(raises=True), led).notify(_n(Channel("s")), now=NOW))
    assert d.outcome is Outcome.FAILED and "RuntimeError" in (d.detail or "")
    assert led.rows[0]["outcome"] == "failed"


def test_a_transport_factory_that_raises_is_a_failed_delivery_not_an_exception() -> None:
    def factory():
        raise RuntimeError("settings unreadable")

    degraded: list[str] = []
    n = Notifier(transport=factory, ledger=MemoryLedger(), on_degraded=degraded.append)
    d = run(n.notify(_n(Channel("s")), now=NOW))
    assert (d.outcome, d.delivered, d.recorded) == (Outcome.FAILED, False, False)
    assert degraded == ["notify_failed:s"]


@pytest.mark.parametrize(("market", "delivered"), [(True, False), (False, True)])
def test_a_reserve_hold_keeps_the_legacy_bool(market, delivered) -> None:
    tx = RecordingTransport(outcome=Outcome.HELD_RESERVE)
    d = run(_notifier(tx).notify(_n(Channel("s", market=market)), now=NOW))
    assert d.delivered is delivered and tx.sent[0]["market"] is market


def test_a_dead_ledger_fails_open() -> None:
    tx, led, degraded = RecordingTransport(), MemoryLedger(fail_admit=True), []
    d = run(_notifier(tx, led, degraded).notify(_n(Channel("s", dedup=Dedup.once())), now=NOW))
    assert (d.outcome, d.delivered, d.recorded) == (Outcome.SENT, True, False)
    assert len(tx.sent) == 1 and led.rows == [] and degraded == ["notify_ledger_unavailable"]


def test_a_failed_record_still_returns_the_delivery() -> None:
    degraded: list[str] = []
    d = run(_notifier(led=MemoryLedger(fail_record=True), degraded=degraded).notify(_n(Channel("s")), now=NOW))
    assert (d.outcome, d.delivered, d.recorded) == (Outcome.SENT, True, False)
    assert degraded == ["notify_ledger_unavailable"]


def test_tier_is_resolved_for_the_record_by_the_consumer_s_map() -> None:
    led = MemoryLedger()
    n = _notifier(led=led)
    run(n.notify(_n(Channel("s"), severity="fyi"), now=NOW))
    run(n.notify(_n(Channel("s"), key="k2", severity="fyi", tier="p1"), now=NOW))
    run(n.notify(_n(Channel("s"), key="k3", severity="high"), now=NOW))
    assert [r["tier"] for r in led.rows] == ["p3", "p1", "p1"]


def test_the_library_default_tier_is_the_explicit_one_and_a_missing_one_is_refused() -> None:
    """v0.11 fix-now: an unknown/missing tier was silently p3. Must-fire both ways: the explicit tier
    records; no tier -> ValueError at the map, which notify() turns into a FAILED, degraded delivery
    (never raises) -- and nothing is published under a guessed tier."""
    assert default_tier_for("high", "p2") == "p2"
    with pytest.raises(ValueError, match="no tier"):
        default_tier_for("high", None)
    with pytest.raises(ValueError, match="unknown tier"):
        default_tier_for("high", "p9")
    led, tx, degraded = MemoryLedger(), RecordingTransport(), []
    n = Notifier(transport=tx, ledger=led, on_degraded=degraded.append)
    d = run(n.notify(_n(Channel("s"), severity="high"), now=NOW))
    assert (d.outcome, d.delivered, d.recorded) == (Outcome.FAILED, False, False) and "no tier" in (d.detail or "")
    assert tx.sent == [] and degraded == ["notify_failed:s"]
    run(n.notify(_n(Channel("s"), key="k2", tier="p2"), now=NOW))
    assert [r["tier"] for r in led.rows] == ["p2"] and len(tx.sent) == 1


def test_a_consumer_map_returning_an_unknown_tier_fails_the_push() -> None:
    tx, degraded = RecordingTransport(), []
    n = Notifier(transport=tx, ledger=MemoryLedger(), tier_for=lambda s, t: "P2", on_degraded=degraded.append)
    d = run(n.notify(_n(Channel("s")), now=NOW))
    assert d.outcome is Outcome.FAILED and "unknown tier 'P2'" in (d.detail or "") and tx.sent == []


def test_a_local_channel_keeps_its_policy_when_the_ledger_dies() -> None:
    tx, led, degraded = RecordingTransport(), MemoryLedger(fail_admit=True), []
    n = _notifier(tx, led, degraded, local=LocalLedger())
    ch = Channel("s", dedup=Dedup.once(), budget=Budget(1), on_ledger_down="local")
    got = [run(n.notify(_n(ch, key=k), now=NOW)) for k in ("k", "k", "k2")]
    assert [(d.outcome, d.recorded) for d in got] == [
        (Outcome.SENT, False), (Outcome.DEDUPED, False), (Outcome.BUDGET_REFUSED, False)]  # fmt: skip
    assert len(tx.sent) == 1 and led.rows == [] and "notify_budget_refused:s" in degraded


def test_the_local_fallback_is_per_notifier() -> None:
    ch = Channel("s", dedup=Dedup.once(), on_ledger_down="local")
    a, b = _notifier(led=MemoryLedger(fail_admit=True)), _notifier(led=MemoryLedger(fail_admit=True))
    run(a.notify(_n(ch), now=NOW))
    assert run(b.notify(_n(ch), now=NOW)).outcome is Outcome.SENT


def test_a_raising_on_degraded_hook_never_escapes() -> None:
    def boom(reason):
        raise RuntimeError("telemetry down")

    tx = RecordingTransport(outcome=Outcome.FAILED)
    n = Notifier(transport=tx, ledger=MemoryLedger(), on_degraded=boom)
    assert run(n.notify(_n(Channel("s"), tier="p3"), now=NOW)).outcome is Outcome.FAILED
    assert len(tx.sent) == 1  # it reached the transport: the FAILED is the transport's, not the tier map's


def test_the_local_ledger_is_bounded() -> None:
    led = LocalLedger(max_keys=8)
    ch = Channel("s", dedup=Dedup.window(60))
    for i in range(20):
        run(led.admit(_n(ch, key=str(i)), NOW + dt.timedelta(seconds=i)))
    assert len(led.claims) <= 8 and ("s", "19") in led.claims


@pytest.mark.parametrize("off", [True, False])
def test_disabled_asks_the_transport_built_per_call(off) -> None:
    built: list[int] = []

    def factory():
        built.append(1)
        return RecordingTransport(is_disabled=off)

    n = Notifier(transport=factory, ledger=MemoryLedger())
    assert n.disabled() is off and n.disabled() is off and len(built) == 2


def test_first_sight_on_a_dead_ledger_is_none_and_degraded() -> None:
    degraded: list[str] = []
    n = _notifier(led=MemoryLedger(fail_admit=True), degraded=degraded)
    assert run(n.first_sight(Channel("s", dedup=Dedup.once()), ["a"], now=NOW)) is None
    assert degraded == ["notify_ledger_unavailable"]


def test_first_sight_refuses_a_budget_or_caller_channel() -> None:
    n = _notifier()
    for ch in (Channel("s"), Channel("s", dedup=Dedup.once(), budget=Budget(1))):
        with pytest.raises(ValueError, match="claims-only"):
            run(n.first_sight(ch, ["a"]))


# -- the pure half -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("outcome", "market", "want"), [
    (Outcome.SENT, False, True), (Outcome.SPOOLED_NIGHT, False, True), (Outcome.SPOOLED_RETRY, False, True),
    (Outcome.DIGEST, True, True), (Outcome.HELD_RESERVE, False, True), (Outcome.HELD_RESERVE, True, False),
    (Outcome.DISABLED, False, False), (Outcome.FAILED, False, False), (Outcome.BUDGET_REFUSED, False, False),
    (Outcome.DEDUPED, False, False),
])  # fmt: skip
def test_delivered_for_is_the_legacy_push_bool(outcome, market, want) -> None:
    assert nt.delivered_for(outcome, market=market) is want


def test_recorded_is_every_outcome_but_deduped() -> None:
    assert set(nt.RECORDED) == {o.value for o in Outcome} - {"deduped"}


def test_policy_values_are_validated() -> None:
    with pytest.raises(ValueError, match="positive"):
        Dedup.window(0)
    with pytest.raises(ValueError, match="negative"):
        Budget(-1)
    with pytest.raises(ValueError, match="unknown budget window"):
        Budget(1, "fortnight")
    with pytest.raises(ValueError, match="needs a scope"):
        Channel(" ")
    with pytest.raises(ValueError, match="unknown tier"):
        Notice(Channel("s"), key="k", title="t", body="b", tier="p9")
    with pytest.raises(ValueError, match="on_ledger_down"):
        Channel("s", on_ledger_down="closed")


def test_the_week_zone_is_the_consumer_s() -> None:
    """v0.11 fix-now: `window_key` hard-coded US Eastern. The zone is now the Budget's, so the same
    instant counts in different weeks for different consumers, and the library names none."""
    at = dt.datetime(2026, 10, 26, 3, 0, tzinfo=dt.UTC)  # Sunday night in New York, Monday in UTC
    assert nt.window_key(Budget(1, "iso_week", tz="America/New_York"), at).endswith("2026-W43")
    assert nt.window_key(Budget(1, "iso_week", tz="UTC"), at).endswith("2026-W44")
    with pytest.raises(ValueError, match="needs tz"):
        Budget(1, "iso_week")
    with pytest.raises(ValueError, match="takes no tz"):
        Budget(1, "utc_day", tz="UTC")
    with pytest.raises(ValueError, match="unknown budget window"):
        Budget(1, "et_iso_week")  # the v0.10 name, gone with its hard-coded zone
    with pytest.raises(ClockBlind, match="cannot resolve"):
        nt.window_key(Budget(1, "iso_week", tz="Mars/Olympus_Mons"), at)


def test_window_keys() -> None:
    assert nt.window_key(Budget(1), dt.datetime(2026, 10, 23, 23, 59, tzinfo=dt.UTC)) == "utc-day:2026-10-23"
    assert (
        nt.window_key(Budget(1, "iso_week", tz="America/New_York"), dt.datetime(2026, 10, 26, 3, 59, tzinfo=dt.UTC))
        == "iso-week:America/New_York:2026-W43"
    )
    assert (
        nt.window_key(Budget(1, "iso_week", tz="America/New_York"), dt.datetime(2026, 10, 26, 4, 0, tzinfo=dt.UTC))
        == "iso-week:America/New_York:2026-W44"
    )
