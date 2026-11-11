"""Value types of the notify pipeline (`edge_platform.notify.pipeline`) -- zero I/O, stdlib.

Lifted from trade-data `service/domain/notify.py` (M6 PL6). A push is a `Notice` on a declared
`Channel`. The channel owns the POLICY (scope name, dedup, budget, which topic); the notice carries
the CONTENT (key, title, body, tier, actions). The pipeline answers with a `Delivery` whose
`delivered` bool is exactly what a plain "did it push" call returned for the same outcome, so a call
site that branched on that bool branches on `delivery.delivered` unchanged.

Dedup, three shapes, all keyed (scope, key):
  Dedup.caller()    the call site owns dedup (a seal, a transition diff); nothing is claimed.
  Dedup.once()      a key pushes at most once, ever.
  Dedup.window(s)   a key pushes at most once per `s` seconds (measured from the last claim that
                    PASSED -- a suppressed duplicate does not extend the window).

Budget: at most `cap` admitted notices per scope per window. A window is a pure function of the
clock (`window_key`), so the ledger row it counts in is derivable, never stored state to drift. A
weekly window is counted in the zone THE CONSUMER names (`Budget(cap, "iso_week", tz=...)`, v0.11):
the library holds no zone of its own.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from zoneinfo import ZoneInfo

from edge_platform.fence.sessions import ClockBlind

from .policy import TIERS

BUDGET_WINDOWS = ("utc_day", "iso_week")
LEDGER_DOWN_MODES = ("open", "local")


class Outcome(StrEnum):
    """What happened to one notice. Every value but `deduped` is a delivery-ledger row (the
    deliveries table's CHECK, `ledger.ddl`); `deduped` is a counter on the claims table."""

    SENT = "sent"  # 2xx from the push service
    SPOOLED_NIGHT = "spooled_night"  # deferred to the morning flush (the policy's night window)
    DIGEST = "digest"  # appended to the digest spool
    HELD_RESERVE = "held_reserve"  # the shared quota is inside its reserve -> a digest line
    DISABLED = "disabled"  # no topic configured
    SPOOLED_RETRY = "spooled_retry"  # network down / 5xx -> spooled for a re-send
    FAILED = "failed"  # transport error / non-2xx, and it could not be spooled
    BUDGET_REFUSED = "budget_refused"  # the scope's budget window was spent
    DEDUPED = "deduped"  # (scope, key) already claimed inside its dedup policy


#: Outcomes that are rows in the deliveries table (everything but a dedup hit).
RECORDED: tuple[str, ...] = tuple(o.value for o in Outcome if o is not Outcome.DEDUPED)


def delivered_for(outcome: Outcome, *, market: bool) -> bool:
    """The "did it push" bool for an outcome. A reserve hold on a MARKET push is not delivered (the
    caller must leave its row undelivered); an ordinary hold is a successful spool."""
    if outcome is Outcome.HELD_RESERVE:
        return not market
    return outcome in (Outcome.SENT, Outcome.SPOOLED_NIGHT, Outcome.SPOOLED_RETRY, Outcome.DIGEST)


@dataclass(frozen=True)
class Dedup:
    mode: str  # "caller" | "once" | "window"
    window_s: float | None = None

    @classmethod
    def caller(cls) -> Dedup:
        return cls("caller")

    @classmethod
    def once(cls) -> Dedup:
        return cls("once")

    @classmethod
    def window(cls, seconds: float) -> Dedup:
        if seconds <= 0:
            raise ValueError("a dedup window must be positive")
        return cls("window", float(seconds))

    @property
    def claims(self) -> bool:
        return self.mode != "caller"


@dataclass(frozen=True)
class Budget:
    """`per="utc_day"`, or `per="iso_week"` with `tz` an IANA zone name THE CONSUMER chooses (the
    week a Sunday-night notice counts in depends on it). An iso_week without a zone is refused."""

    cap: int
    per: str = "utc_day"  # "utc_day" | "iso_week"
    tz: str | None = None  # required for "iso_week"; refused for "utc_day" (it would be ignored)

    def __post_init__(self) -> None:
        if self.cap < 0:
            raise ValueError("a budget cap cannot be negative")
        if self.per not in BUDGET_WINDOWS:
            raise ValueError(f"unknown budget window {self.per!r} (known: {BUDGET_WINDOWS})")
        if self.per == "iso_week" and not (self.tz or "").strip():
            raise ValueError("an iso_week budget needs tz= (an IANA zone name) -- the library holds no zone")
        if self.per == "utc_day" and self.tz is not None:
            raise ValueError("a utc_day budget takes no tz (it counts the UTC day)")


def window_key(budget: Budget, now: dt.datetime) -> str:
    """The budget row a notice at `now` counts against. `iso_week` is the ISO week in `budget.tz`. A
    zone this host cannot resolve raises ClockBlind (the pipeline reports `notify_clock_blind`)
    rather than counting a wrong week."""
    if budget.per == "utc_day":
        return f"utc-day:{now.astimezone(dt.UTC).date().isoformat()}"
    try:
        zone = ZoneInfo(str(budget.tz))
    except Exception as exc:
        raise ClockBlind(f"this host cannot resolve the budget zone {budget.tz!r} ({type(exc).__name__})") from exc
    y, w, _ = now.astimezone(zone).isocalendar()
    return f"iso-week:{budget.tz}:{y}-W{w:02d}"


@dataclass(frozen=True)
class Channel:
    """A declared push surface. `scope` names the dedup namespace and the budget; one module
    declares one channel per distinct policy, at import time."""

    scope: str
    dedup: Dedup = field(default_factory=Dedup.caller)
    budget: Budget | None = None
    market: bool = False  # the transport's market topic instead of its ops topic
    # ledger unreachable: "open" pushes undeduplicated (a lost page is worse than a noisy one);
    # "local" enforces THIS channel's dedup + budget in process memory (`notify.local.LocalLedger`)
    # -- for a scope an unauthenticated storm feeds, where open would spend the shared quota.
    on_ledger_down: str = "open"

    def __post_init__(self) -> None:
        if not self.scope or not self.scope.strip():
            raise ValueError("a channel needs a scope")
        if self.on_ledger_down not in LEDGER_DOWN_MODES:
            raise ValueError(f"unknown on_ledger_down {self.on_ledger_down!r}")


@dataclass(frozen=True)
class Notice:
    channel: Channel
    key: str
    title: str
    body: str
    severity: str = "default"
    tier: str | None = None  # None -> the Notifier's tier_for(severity, None)
    tags: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.tier is not None and self.tier not in TIERS:
            raise ValueError(f"unknown tier {self.tier!r}")


@dataclass(frozen=True)
class Delivery:
    scope: str
    key: str
    outcome: Outcome
    delivered: bool
    detail: str | None = None
    recorded: bool = True  # False = the ledger could not be written (a fail-open push)
