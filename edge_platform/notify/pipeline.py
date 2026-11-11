"""Notifier.notify(Notice) -> Delivery -- the ONE path a phone push takes. Stdlib.

Lifted from trade-data `service/platform/notify.py` (M6 PL6). WHY: thirteen call sites pushed through
one shared client and deduplicated / rate-limited about nine different ways (a claim row, a
last-page timestamp, an id list, a UNIQUE seal, an in-memory per-IP limiter, a debounce machine,
nothing). One reported "sent" on a failed push for weeks. The transport was shared; the POLICY and
the RECORD were not, so nobody could say which scope spent the quota or which pages never left.

For one Notice on its declared Channel, in order:
  1. a disabled transport (no topic)  -> outcome `disabled`; nothing claimed or spent.
  2. admit: dedup claim + budget spend in ONE ledger transaction. A duplicate returns `deduped` (a
     counter, no row). A spent budget rolls the claim back and returns `budget_refused` -- the key
     is free to pass when the window turns.
  3. publish through the Transport: sent | spooled_night | digest | held_reserve | spooled_retry |
     failed. A RAISING transport is recorded as `failed`, never propagated.
  4. record the outcome in the delivery ledger (append-only), whatever it was.
notify() NEVER RAISES.

FAILURE TELEMETRY -- every signal survives as the injected `on_degraded(reason)`:
  `notify_failed:<scope>`          a publish that did not go out
  `notify_budget_refused:<scope>`  a scope that hit its declared cap
  `notify_ledger_unavailable`      the ledger unreachable (admit or record). FAIL OPEN: the push
                                   goes out undeduplicated and `Delivery.recorded` is False; a
                                   channel declaring on_ledger_down="local" keeps its own dedup +
                                   budget in this Notifier's LocalLedger instead.
The delivery rows themselves are the outside watcher's input (`ledger.outcome_series`).

WHAT IS INJECTED: the Transport (trade-data: its ntfy client), the Ledger (`ledger.SqlLedger` over the
consumer's own tables), `tier_for(severity, tier)` (the consumer's legacy severity -> tier map) and
`on_degraded`. The library names no table, no topic and no publisher.

Library check (integrate before write): apprise and the ntfy Python clients are TRANSPORTS. None
carries a persisted dedup ledger or a per-scope budget, which is the part the call sites each
hand-rolled; that part is this module.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Protocol

from .local import LocalLedger
from .types import Channel, Delivery, Notice, Outcome, delivered_for

log = logging.getLogger("edge_platform.notify.pipeline")


class Transport(Protocol):
    def disabled(self) -> bool: ...

    async def publish(
        self,
        *,
        title: str,
        body: str,
        severity: str,
        tags: list[str] | None = None,
        tier: str | None = None,
        market: bool = False,
        actions: list[str] | None = None,
    ) -> tuple[Outcome, str | None]: ...


class Ledger(Protocol):
    async def admit(self, notice: Notice, now: dt.datetime) -> tuple[Outcome | None, str | None]:
        """(None, None) = admitted; (DEDUPED, None) or (BUDGET_REFUSED, window_key) = refused."""
        ...

    async def record(
        self, notice: Notice, *, tier: str, outcome: Outcome, delivered: bool, detail: str | None
    ) -> None: ...


def default_tier_for(severity: str, tier: str | None) -> str:
    """The explicit tier, else p3. A consumer with a legacy severity vocabulary passes its own map."""
    return tier or "p3"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Notifier:
    """One consumer's push path.

    `transport` is a Transport, or a zero-argument factory returning one (resolved per call, so a
    consumer's settings are read when a push happens, never at import). `ledger` is the durable
    Ledger. Both can be overridden per call (tests, a one-off)."""

    def __init__(
        self,
        *,
        transport: Transport | Callable[[], Transport],
        ledger: Ledger | Callable[[], Ledger],
        tier_for: Callable[[str, str | None], str] = default_tier_for,
        on_degraded: Callable[[str], None] | None = None,
        local: LocalLedger | None = None,
        now: Callable[[], dt.datetime] = _utcnow,
    ) -> None:
        self._transport = transport
        self._ledger = ledger
        self.tier_for = tier_for
        self._on_degraded = on_degraded
        #: the outage fallback for on_ledger_down="local" channels -- one per Notifier, keyed
        #: (scope, key) inside, so channels never share a claim or a budget.
        self.local = local if local is not None else LocalLedger()
        self._now = now

    def _resolve_transport(self, override: Transport | None) -> Transport:
        if override is not None:
            return override
        t = self._transport
        return t if hasattr(t, "publish") else t()  # type: ignore[operator,return-value]

    def _resolve_ledger(self, override: Ledger | None) -> Ledger:
        if override is not None:
            return override
        led = self._ledger
        return led if hasattr(led, "admit") else led()  # type: ignore[operator,return-value]

    def _degraded(self, reason: str) -> None:
        if self._on_degraded is None:
            return
        try:
            self._on_degraded(reason)
        except Exception:
            log.exception("notify on_degraded hook raised (reason=%s)", reason)

    def disabled(self) -> bool:
        """No topic configured. For a caller that spends its OWN state before notify() (a one-shot
        or a budget it consumes before the push): ask first, so a dead topic spends nothing."""
        return self._resolve_transport(None).disabled()

    async def _publish(self, transport: Transport, notice: Notice) -> tuple[Outcome, str | None]:
        try:
            return await transport.publish(
                title=notice.title, body=notice.body, severity=notice.severity, tags=list(notice.tags) or None,
                tier=notice.tier, market=notice.channel.market, actions=list(notice.actions) or None,
            )  # fmt: skip
        except Exception as exc:  # publish() never raises by contract; a broken one still records
            log.exception("notify transport raised scope=%s", notice.channel.scope)
            return Outcome.FAILED, f"{exc.__class__.__name__}: {str(exc)[:160]}"

    async def first_sight(
        self, channel: Channel, keys: list[str], *, ledger: Ledger | None = None, now: dt.datetime | None = None
    ) -> set[str] | None:
        """The keys of `keys` this channel has NEVER claimed, claiming them now (the same admit() a
        notice takes, so the claim survives a restart). For a caller that collapses many events into
        one push: ask which are new BEFORE building it, then push on a Dedup.caller channel.

        None = the ledger is unreachable: the caller keeps its own process memory (fail open) and
        `notify_ledger_unavailable` is raised, the same signal notify() raises."""
        if not channel.dedup.claims or channel.budget is not None:
            raise ValueError("first_sight needs a claims-only channel (Dedup.once/window, no budget)")
        led = self._resolve_ledger(ledger)
        now = now or self._now()
        fresh: set[str] = set()
        try:
            for key in keys:
                refused, _ = await led.admit(Notice(channel, key=key, title=key, body=""), now)
                if refused is None:
                    fresh.add(key)
        except Exception:
            log.exception("notify ledger unavailable -- first_sight scope=%s falls back to the caller", channel.scope)
            self._degraded("notify_ledger_unavailable")
            return None
        return fresh

    async def notify(
        self,
        notice: Notice,
        *,
        transport: Transport | None = None,
        ledger: Ledger | None = None,
        now: dt.datetime | None = None,
    ) -> Delivery:
        """Dedup, budget, publish and record one notice. Never raises."""
        ch = notice.channel
        try:
            tx = self._resolve_transport(transport)
            led = self._resolve_ledger(ledger)
            tier = self.tier_for(notice.severity, notice.tier)
            disabled = tx.disabled()
        except Exception as exc:
            # a transport/ledger that cannot even be BUILT is a page that did not leave
            log.exception("notify could not resolve its ports scope=%s", ch.scope)
            self._degraded(f"notify_failed:{ch.scope}")
            return Delivery(ch.scope, notice.key, Outcome.FAILED, False, f"{type(exc).__name__}: {exc}"[:200], False)
        now = now or self._now()

        detail: str | None = None
        ledger_ok = True
        if disabled:
            outcome = Outcome.DISABLED
        else:
            try:
                refused, detail = await led.admit(notice, now)
            except Exception:
                local = ch.on_ledger_down == "local"
                log.exception(
                    "notify ledger unavailable -- %s scope=%s",
                    "process-local dedup+budget" if local else "pushing undeduplicated", ch.scope,
                )  # fmt: skip
                self._degraded("notify_ledger_unavailable")
                ledger_ok = False
                refused, detail = await self.local.admit(notice, now) if local else (None, None)
            if refused is Outcome.DEDUPED:
                log.debug("notify deduped scope=%s key=%s", ch.scope, notice.key)
                return Delivery(ch.scope, notice.key, Outcome.DEDUPED, False, recorded=ledger_ok)
            if refused is Outcome.BUDGET_REFUSED:
                log.warning("notify budget refused scope=%s window=%s cap=%s", ch.scope, detail,
                            ch.budget.cap if ch.budget else None)  # fmt: skip
                self._degraded(f"notify_budget_refused:{ch.scope}")
                outcome = Outcome.BUDGET_REFUSED
            else:
                outcome, detail = await self._publish(tx, notice)
                if outcome is Outcome.FAILED:
                    self._degraded(f"notify_failed:{ch.scope}")

        delivered = delivered_for(outcome, market=ch.market)
        recorded = False
        if ledger_ok:
            try:
                await led.record(notice, tier=tier, outcome=outcome, delivered=delivered, detail=detail)
                recorded = True
            except Exception:
                log.exception("notify delivery record failed scope=%s outcome=%s", ch.scope, outcome.value)
                self._degraded("notify_ledger_unavailable")
        return Delivery(ch.scope, notice.key, outcome, delivered, detail, recorded)
