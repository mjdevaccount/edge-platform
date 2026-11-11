"""notify() NEVER RAISES -- the inputs a pre-merge review reproduced escaping it (v0.10.0 review).

Each case is a port a consumer could plausibly hand the Notifier: a transport speaking bare strings,
a clock that breaks, a local fallback that breaks, a class where a factory was meant. The contract
is the same for all of them: a Delivery comes back, and the failure is NAMED through on_degraded."""

from __future__ import annotations

import asyncio
from typing import Any

from edge_platform.fence.sessions import ClockBlind
from edge_platform.notify.pipeline import Notifier
from edge_platform.notify.types import Budget, Channel, Dedup, Notice, Outcome

from . import ledger_contract as contract
from .fakes import MemoryLedger, RecordingTransport

NOW = contract.NOW
run = asyncio.run


def _n(ch: Channel, key: str = "k") -> Notice:
    return Notice(ch, key=key, title="T", body="B")


class _StringTransport(RecordingTransport):
    async def publish(self, **kw: Any) -> tuple[Any, str | None]:
        self.sent.append(kw)
        return self.outcome.value, self.detail  # a bare str, not the enum


def test_a_bare_string_failed_outcome_still_counts_as_failed() -> None:
    degraded: list[str] = []
    led = MemoryLedger(fail_record=True)
    n = Notifier(transport=_StringTransport(outcome=Outcome.FAILED, detail="boom"), ledger=led,
                 on_degraded=degraded.append)  # fmt: skip
    d = run(n.notify(_n(Channel("s")), now=NOW))
    assert d.outcome is Outcome.FAILED and d.delivered is False and d.recorded is False
    assert "notify_failed:s" in degraded and "notify_ledger_unavailable" in degraded


def test_a_bare_string_sent_outcome_is_coerced_and_recorded() -> None:
    led = MemoryLedger()
    d = run(Notifier(transport=_StringTransport(), ledger=led).notify(_n(Channel("s")), now=NOW))
    assert d.outcome is Outcome.SENT and d.delivered and d.recorded and led.rows[0]["outcome"] == "sent"


def test_an_unknown_transport_outcome_is_a_failure() -> None:
    class Weird(RecordingTransport):
        async def publish(self, **kw: Any) -> tuple[Any, str | None]:
            return "teleported", None

    degraded: list[str] = []
    d = run(
        Notifier(transport=Weird(), ledger=MemoryLedger(), on_degraded=degraded.append).notify(
            _n(Channel("s")), now=NOW
        )
    )
    assert d.outcome is Outcome.FAILED and "teleported" in (d.detail or "") and degraded == ["notify_failed:s"]


def test_a_raising_local_fallback_fails_open() -> None:
    class BrokenLocal:
        async def admit(self, notice: Notice, now: Any) -> Any:
            raise ClockBlind("no tzdata")

    degraded: list[str] = []
    tx = RecordingTransport()
    n = Notifier(transport=tx, ledger=MemoryLedger(fail_admit=True), local=BrokenLocal(),  # type: ignore[arg-type]
                 on_degraded=degraded.append)  # fmt: skip
    ch = Channel("loc", dedup=Dedup.once(), budget=Budget(1, "et_iso_week"), on_ledger_down="local")
    d = run(n.notify(_n(ch), now=NOW))
    assert d.outcome is Outcome.SENT and len(tx.sent) == 1 and d.recorded is False
    assert degraded == ["notify_ledger_unavailable", "notify_local_ledger_failed"]


def test_a_blind_clock_in_the_ledger_is_named_for_its_cause() -> None:
    class BlindLedger(MemoryLedger):
        async def admit(self, notice: Notice, now: Any) -> Any:
            raise ClockBlind("no tzdata")

    degraded: list[str] = []
    d = run(Notifier(transport=RecordingTransport(), ledger=BlindLedger(), on_degraded=degraded.append)
            .notify(_n(Channel("s", budget=Budget(1, "et_iso_week"))), now=NOW))  # fmt: skip
    assert d.outcome is Outcome.SENT and degraded == ["notify_clock_blind"]


def test_a_raising_clock_is_a_failed_delivery() -> None:
    def broken() -> Any:
        raise RuntimeError("clock")

    degraded: list[str] = []
    n = Notifier(transport=RecordingTransport(), ledger=MemoryLedger(), on_degraded=degraded.append, now=broken)
    d = run(n.notify(_n(Channel("s"))))
    assert d.outcome is Outcome.FAILED and degraded == ["notify_failed:s"]
    assert run(n.first_sight(Channel("fs", dedup=Dedup.once()), ["a"])) is None


def test_first_sight_with_a_ledger_factory_that_raises_is_none() -> None:
    def factory() -> Any:
        raise ConnectionError("pool gone")

    degraded: list[str] = []
    n = Notifier(transport=RecordingTransport(), ledger=factory, on_degraded=degraded.append)
    assert run(n.first_sight(Channel("fs", dedup=Dedup.once()), ["a"], now=NOW)) is None
    assert degraded == ["notify_ledger_unavailable"]


def test_a_class_is_taken_as_a_factory_not_an_instance() -> None:
    n = Notifier(transport=RecordingTransport, ledger=MemoryLedger)  # type: ignore[arg-type]
    d = run(n.notify(_n(Channel("s")), now=NOW))
    assert d.outcome is Outcome.SENT and d.recorded is True
