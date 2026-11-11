"""PgWindowBudget -- ONE fixed-window rate budget per vendor account, shared by every process through a
Postgres row (rate-budget.md, M6 PL10). Extra `[sql]`. Postgres only.

A vendor's quota belongs to the ACCOUNT, so every process that spends it draws from one counter: one
row per account key (`<vendor>:<account>`), every process using the same key and the same `Window`.

LIBRARY CHECKED: pyrate-limiter 4.5.1 `PostgresBucket`, NOT adopted for the shared bucket -- it is sync
only (a blocking call plus a second driver pool on an async path), runs `CREATE TABLE` from its
constructor (DDL outside the migration ledger), falls back to LOCAL wall time when the DB clock
fails (two processes disagree and both spend), turns lock contention into "over limit", and is a
sliding log where many vendors count a FIXED window from the first request. pyrate-limiter's `InMemoryBucket` stays
the per-process default in the consumer's HTTP port; this module is only the shared store.

THE TABLE IS THE CONSUMER'S: the name is passed in (validated -- it is interpolated) and `ddl(table)`
returns the shape, which the consumer ships as a numbered migration. Nothing here runs DDL.

INVARIANTS
- The DB clock is the only clock: every decision reads `clock_timestamp()` ONCE per statement. No
  local-time fallback.
- One decision statement per attempt (row lock only), in a transaction whose `lock_timeout` /
  `statement_timeout` are set LOCAL to the caller's remaining `max_wait` -- a row lock held by
  someone else past it is `BudgetUnavailable`, never an unbounded block. Waiting holds no
  connection: the sleep is outside any transaction (with a little jitter so waiters do not wake in
  lockstep), and a refused attempt re-asks after the window the DB names.
- FAILS CLOSED: a store error raises `BudgetUnavailable` -- never a silent grant. There is no default
  fallback; a lane that must not stop on a Postgres blip wraps this in an explicit fallback at its
  composition root and reports every fallback use.
- The vendor's counter wins: `observe()` keeps the latest vendor reading, `flush_observed()` writes
  `used = GREATEST(used, vendor_used)` and the vendor's reset time, which becomes the window end. A
  reading with `available == 0` (the vendor refused) pins `used = capacity` until that reset.
- ...but only for a window that is still open: a reading whose reset is already past on the DB clock
  merges NOTHING (`stats.flush_stale`), and a reset is clamped to `RESET_HORIZON_PERIODS` of the
  row's period (`observe` also drops one past it on the local clock, counted `observe_invalid`).
- capacity / period disagreeing between processes: last writer wins on the row, a WARNING names both
  values, and `stats.config_conflict` counts it.

TELEMETRY (what fails silently, where it lands): `stats` holds the per-process counters --
wait_seconds / wait_events (a starved lane), exhausted / unavailable (a lane refused, or the store
down), observe_invalid (malformed vendor headers ignored), flush_failed (the vendor reading did not
land), flush_stale (a reading that arrived after its window closed), config_conflict -- and `stats.series()` renders them as `budget_*_total{key}` for the
consumer's textfile / exporter. The ROW is the outside view: `updated_at` stale while the lane should
be spending means nothing is drawing (a guard nothing invokes); vendor used above ours means an
unmetered caller exists. Watching the row is the consumer's watchdog.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, fields
from typing import Any

from sqlalchemy import text

from edge_platform.sqlkit import qualified

log = logging.getLogger("edge_platform.budget")

#: a refused insert race (two processes creating the row at once) re-asks at once, this many times
_CONFLICT_RETRIES = 5
#: A vendor reset is believed at most this many of the row's own periods ahead (the flush clamps to
#: it on the DB clock; `observe` drops a reset past it). 2 = one full vendor window plus one of slack
#: for a vendor window that started before ours. A header saying 2099 would otherwise pin the bucket
#: forever. A vendor window genuinely longer than this means `Window.period_s` is wrong -- model the
#: vendor's period.
RESET_HORIZON_PERIODS = 2
#: `observe` checks the horizon on the LOCAL clock (the DB clamp is the authority); this much skew is
#: tolerated before a reset is called implausible.
_OBSERVE_SKEW_S = 300.0
#: Earliest plausible reset: anything before is a unit/parse error (seconds read as ms -> 1970).
_EPOCH_FLOOR = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)
#: Postgres `lock_timeout = 0` means NO timeout, so a remaining wait of ~0 still gets this floor.
_MIN_LOCK_MS = 50
#: `statement_timeout` = the lock budget plus this (the statement itself, once it holds the lock).
_STMT_SLACK_MS = 1000
#: A flush has no caller-side max_wait; it waits this long for the row lock, then counts a failure.
_FLUSH_LOCK_S = 2.0
_TIMEOUTS = text("SELECT set_config('lock_timeout', :lock, true), set_config('statement_timeout', :stmt, true)")


def ddl(table: str) -> str:
    """CREATE TABLE IF NOT EXISTS for the consumer's migration (idempotent)."""
    t = qualified(table, "budget table")
    return f"""CREATE TABLE IF NOT EXISTS {t} (
    key text PRIMARY KEY,
    window_start timestamptz NOT NULL,
    used integer NOT NULL CHECK (used >= 0),
    capacity integer NOT NULL CHECK (capacity > 0),
    period_s integer NOT NULL CHECK (period_s > 0),
    vendor_resets_at timestamptz NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
)"""


@dataclass(frozen=True, slots=True)
class Window:
    """`capacity` units per `period_s` -- what the CONSUMER allows itself (below the vendor's limit by
    a stated headroom)."""

    capacity: int
    period_s: int

    def __post_init__(self) -> None:
        for what in ("capacity", "period_s"):
            v = getattr(self, what)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ValueError(f"Window.{what} must be a positive int, got {v!r}")


class BudgetExhausted(RuntimeError):
    """Not granted within `max_wait`."""

    def __init__(self, key: str, waited_s: float, next_in_s: float) -> None:
        super().__init__(
            f"budget {key}: not granted within max_wait (waited {waited_s:.2f}s, next in {next_in_s:.2f}s)"
        )
        self.key, self.waited_s, self.next_in_s = key, waited_s, next_in_s


class BudgetUnavailable(RuntimeError):
    """The store could not be reached or did not answer -- fail closed, no grant."""

    def __init__(self, key: str, why: str) -> None:
        super().__init__(f"budget {key} unavailable: {why}")
        self.key = key


@dataclass(slots=True)
class BudgetStats:
    """Per-process counters. Monotonic; rendered by `series()`."""

    wait_seconds: float = 0.0
    wait_events: int = 0
    grants: int = 0
    exhausted: int = 0
    unavailable: int = 0
    observe_invalid: int = 0
    flush_failed: int = 0
    flush_stale: int = 0
    config_conflict: int = 0

    def series(self, key: str) -> list[tuple[str, dict[str, str], float]]:
        """(metric, labels, value) -- `budget_<field>_total{key}` for every counter, zeros included."""
        return [(f"budget_{f.name}_total", {"key": key}, float(getattr(self, f.name))) for f in fields(self)]


@dataclass(frozen=True, slots=True)
class _Reading:
    used: int | None
    resets_at: dt.datetime | None
    refused: bool


# One statement, one clock reading. `cur` locks the row (READ COMMITTED re-reads the newest version
# after a concurrent holder commits); `g` decides from that version; `upd` writes ABSOLUTE values from
# `g`, never from its own (possibly stale) snapshot of the row. No row yet -> `ins` creates it granted;
# a concurrent create makes `ins` return nothing and the caller re-asks.
_ACQUIRE = """
WITH n AS (SELECT clock_timestamp() AS t),
cur AS (SELECT b.* FROM {t} b WHERE b.key = :key FOR UPDATE),
r AS (
  SELECT c.key, n.t, c.capacity AS prior_cap, c.period_s AS prior_period,
         n.t >= coalesce(c.vendor_resets_at, c.window_start + make_interval(secs => c.period_s)) AS rolled,
         c.window_start, c.used, c.period_s, c.vendor_resets_at
  FROM cur c CROSS JOIN n
),
g AS (
  SELECT key, t, prior_cap, prior_period,
         CASE WHEN rolled THEN t ELSE window_start END AS ws,
         CASE WHEN rolled THEN 0 ELSE used END AS base,
         CASE WHEN rolled THEN NULL ELSE vendor_resets_at END AS vra,
         CASE WHEN rolled THEN :period ELSE period_s END AS per
  FROM r
),
d AS (SELECT g.*, g.base + :w <= :cap AS granted FROM g),
upd AS (
  UPDATE {t} b SET window_start = d.ws,
                   used = CASE WHEN d.granted THEN d.base + :w ELSE d.base END,
                   vendor_resets_at = d.vra, capacity = :cap, period_s = :period, updated_at = d.t
  FROM d WHERE b.key = d.key
  RETURNING d.granted,
            extract(epoch FROM coalesce(d.vra, d.ws + make_interval(secs => d.per)) - d.t)::float8 AS wait_s,
            d.prior_cap, d.prior_period
),
ins AS (
  INSERT INTO {t} (key, window_start, used, capacity, period_s, updated_at)
  SELECT :key, n.t, :w, :cap, :period, n.t FROM n WHERE NOT EXISTS (SELECT 1 FROM cur)
  ON CONFLICT (key) DO NOTHING
  RETURNING true AS granted, 0.0::float8 AS wait_s, NULL::int AS prior_cap, NULL::int AS prior_period
)
SELECT granted, wait_s, prior_cap, prior_period FROM upd
UNION ALL SELECT granted, wait_s, prior_cap, prior_period FROM ins
"""

# One statement, one clock reading. A reading whose reset is ALREADY PAST (a late / retried flush, a
# response from the previous vendor window landing after the row rolled, a seconds-vs-ms parse
# landing in 1970) describes a window that is over: it merges NOTHING -- no `used`, no refusal pin,
# no reset -- or the next acquire would roll the window on it and zero `used` mid-window. A reset
# further out than `RESET_HORIZON_PERIODS` of the row's own period is clamped to that horizon, so a
# bad header (year 2099) cannot pin the bucket forever. `stale` reports the dropped case.
_FLUSH = """
WITH n AS (SELECT clock_timestamp() AS t, CAST(:resets AS timestamptz) AS r)
UPDATE {t} b
   SET used = CASE WHEN n.r <= n.t THEN b.used
                   ELSE GREATEST(b.used, coalesce(CAST(:vu AS integer), 0),
                                 CASE WHEN CAST(:refused AS boolean) THEN b.capacity ELSE 0 END) END,
       vendor_resets_at = CASE WHEN n.r IS NULL OR n.r <= n.t THEN b.vendor_resets_at
                               ELSE LEAST(n.r, n.t + make_interval(secs => :horizon * b.period_s)) END,
       updated_at = n.t
  FROM n
 WHERE b.key = :key
RETURNING coalesce(n.r <= n.t, false) AS stale
"""


class PgWindowBudget:
    """The shared fixed-window budget for one account key. `acquire(weight)` returns seconds waited."""

    def __init__(
        self,
        sessions: Callable[[], AbstractAsyncContextManager[Any]],
        table: str,
        key: str,
        window: Window,
        *,
        max_wait: float,
        on_wait: Callable[[str, float], None] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        min_flush_interval_s: float = 1.0,
        jitter_s: float = 0.05,
    ) -> None:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("PgWindowBudget needs a non-empty account key")
        if not isinstance(window, Window):
            raise TypeError("window must be a Window")
        if max_wait < 0:
            raise ValueError(f"max_wait must be >= 0, got {max_wait!r}")
        self._sessions, self._key, self._window = sessions, key, window
        self._acquire_sql = text(_ACQUIRE.format(t=qualified(table, "budget table")))
        self._flush_sql = text(_FLUSH.format(t=qualified(table, "budget table")))
        self._max_wait, self._on_wait, self._sleep = float(max_wait), on_wait, sleep
        self._min_flush, self._last_flush = float(min_flush_interval_s), float("-inf")
        self._jitter = max(float(jitter_s), 0.0)
        self._pending: _Reading | None = None
        self._conflicts_seen: set[tuple[int, int]] = set()
        self.stats = BudgetStats()

    @property
    def key(self) -> str:
        return self._key

    async def acquire(self, weight: int = 1) -> float:
        if isinstance(weight, bool) or not isinstance(weight, int) or weight <= 0:
            raise ValueError(f"weight must be a positive int, got {weight!r}")
        if weight > self._window.capacity:
            raise ValueError(f"weight {weight} can never fit capacity {self._window.capacity} ({self._key})")
        waited, conflicts = 0.0, 0
        while True:
            row = await self._attempt(weight, self._max_wait - waited)
            if row is None:  # lost the create race: the row exists now, ask again
                conflicts += 1
                if conflicts > _CONFLICT_RETRIES:
                    self.stats.unavailable += 1
                    raise BudgetUnavailable(self._key, f"row create raced {conflicts} times")
                continue
            granted, wait_s, prior_cap, prior_period = row
            self._check_config(prior_cap, prior_period)
            if granted:
                self.stats.grants += 1
                return waited
            wait_s = max(float(wait_s or 0.0), 0.001)
            if waited + wait_s > self._max_wait:
                self.stats.exhausted += 1
                raise BudgetExhausted(self._key, waited, wait_s)
            self.stats.wait_events += 1
            self.stats.wait_seconds += wait_s
            if self._on_wait is not None:
                try:
                    self._on_wait(self._key, wait_s)
                except Exception:  # a broken telemetry hook must not become the failure
                    log.warning("budget_on_wait_hook_failed key=%s", self._key, exc_info=True)
            # Every waiter is told the same window end: a little jitter (inside max_wait) spreads the
            # wake so N processes do not queue on the row lock in the same millisecond.
            spread = min(self._jitter, self._max_wait - waited - wait_s)
            nap = wait_s + (random.uniform(0.0, spread) if spread > 0 else 0.0)
            await self._sleep(nap)
            waited += nap

    @staticmethod
    async def _bound(s: Any, budget_s: float) -> None:
        """Bound THIS transaction's lock wait by `budget_s` (set_config(..., true) = SET LOCAL:
        transaction-scoped, so safe behind pgbouncer transaction pooling). A row lock held past it
        raises -- mapped to BudgetUnavailable by the caller -- instead of blocking a `max_wait=0.5`
        caller for as long as the holder likes."""
        lock_ms = max(int(budget_s * 1000), _MIN_LOCK_MS)
        await s.execute(_TIMEOUTS, {"lock": f"{lock_ms}ms", "stmt": f"{lock_ms + _STMT_SLACK_MS}ms"})

    async def _attempt(self, weight: int, remaining_s: float) -> tuple[bool, float, int | None, int | None] | None:
        params = {"key": self._key, "w": weight, "cap": self._window.capacity, "period": self._window.period_s}
        try:
            async with self._sessions() as s:
                await self._bound(s, remaining_s)
                rows = (await s.execute(self._acquire_sql, params)).all()
                await s.commit()
        except Exception as exc:
            self.stats.unavailable += 1
            log.warning("budget_unavailable key=%s err=%s", self._key, type(exc).__name__)
            raise BudgetUnavailable(self._key, f"{type(exc).__name__}: {exc}") from exc
        if not rows:
            return None
        g, w, pc, pp = rows[0]
        return bool(g), float(w), pc, pp

    def _check_config(self, prior_cap: int | None, prior_period: int | None) -> None:
        if prior_cap is None:
            return
        mine = (self._window.capacity, self._window.period_s)
        if (prior_cap, prior_period) != mine:
            self.stats.config_conflict += 1
            if (prior_cap, prior_period) not in self._conflicts_seen:
                self._conflicts_seen.add((prior_cap, prior_period))
                log.warning(
                    "budget_config_conflict key=%s row=capacity %s/period %s mine=capacity %s/period %s -- last writer wins",
                    self._key, prior_cap, prior_period, *mine,
                )  # fmt: skip

    def observe(self, quota: Any) -> None:
        """Keep the latest vendor reading (duck-typed: `.used`, `.available`, `.resets_at`). Malformed
        -> ignored and counted. Never raises."""
        try:
            used, avail, resets = (getattr(quota, a, None) for a in ("used", "available", "resets_at"))
            for v in (used, avail):
                if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
                    raise ValueError(v)
            if resets is not None and (not isinstance(resets, dt.datetime) or resets.tzinfo is None):
                raise ValueError(resets)
            if used is None and avail is None:
                raise ValueError("no counter")
            refused = avail == 0
            implausible = resets is not None and not self._plausible_reset(resets)
            if refused and (resets is None or implausible):
                raise ValueError("a refusal with no believable reset time would pin the bucket forever")
        except Exception:
            self.stats.observe_invalid += 1
            return
        if implausible:  # keep the counter, drop the reset (the flush would clamp it anyway): counted
            self.stats.observe_invalid += 1
            resets = None
        prev = self._pending
        # A refusal is not erased by a later reading FROM THE SAME VENDOR WINDOW (no reset, or one no
        # later than the refusal's). A reading with a LATER reset is the next window: the refusal is
        # over, and carrying it would pin the fresh window under the new reset.
        if prev is not None and prev.refused and not refused and (resets is None or resets <= prev.resets_at):
            refused, resets = True, prev.resets_at
        self._pending = _Reading(used, resets, refused)

    def _plausible_reset(self, resets: dt.datetime) -> bool:
        """A sanity check on the LOCAL clock; the flush's clamp on the DB clock is the authority."""
        horizon = RESET_HORIZON_PERIODS * self._window.period_s + _OBSERVE_SKEW_S
        return _EPOCH_FLOOR <= resets <= dt.datetime.now(dt.UTC) + dt.timedelta(seconds=horizon)

    async def flush_observed(self, *, force: bool = False) -> bool:
        """Write the pending vendor reading to the row (at most once per `min_flush_interval_s`, a
        refusal always). Never raises: a failure is counted, logged, and the reading kept for the next
        flush. True when a reading landed."""
        r = self._pending
        if r is None:
            return False
        now = time.monotonic()
        if not (force or r.refused) and now - self._last_flush < self._min_flush:
            return False
        params = {"key": self._key, "vu": r.used, "refused": r.refused, "resets": r.resets_at,
                  "horizon": RESET_HORIZON_PERIODS}  # fmt: skip
        try:
            async with self._sessions() as s:
                await self._bound(s, _FLUSH_LOCK_S)
                stale = (await s.execute(self._flush_sql, params)).scalar()
                await s.commit()
        except Exception as exc:
            self.stats.flush_failed += 1
            log.warning("budget_flush_failed key=%s err=%s", self._key, type(exc).__name__)
            return False
        if self._pending is r:
            self._pending = None
        self._last_flush = now
        if stale:  # the reading's window was already over when it landed: it merged nothing
            self.stats.flush_stale += 1
            log.info("budget_flush_stale key=%s resets_at=%s", self._key, r.resets_at)
        return True
