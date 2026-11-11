"""PgWindowBudget hardening (v0.11.0 review of PR #18): a vendor reading only speaks for a window that
is still open, a reset is clamped to a horizon, and the row lock follows the LOCK RULE (contention
re-asks and ends granted or exhausted; only a lock held past max_wait + LOCK_FLOOR_S is unavailable).
Every case is must-fire BOTH ways, on a REAL Postgres (`tests/pg.py`)."""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import time
import types

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from edge_platform.budget import (
    LOCK_FLOOR_S,
    MAX_OVERRUN_S,
    RESET_HORIZON_PERIODS,
    BudgetExhausted,
    BudgetUnavailable,
    PgWindowBudget,
    Window,
    ddl,
)

from .pg import dsn, scratch_schema


def _q(**kw) -> types.SimpleNamespace:
    return types.SimpleNamespace(**{"used": None, "available": None, "resets_at": None, **kw})


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _table(maker, schema: str) -> str:
    t = f"{schema}.rate_budget"
    async with maker() as s:
        await s.execute(text(ddl(t)))
        await s.commit()
    return t


async def _grants(b: PgWindowBudget, n: int) -> int:
    got = 0
    for _ in range(n):
        try:
            await b.acquire()
        except BudgetExhausted:
            break
        got += 1
    return got


async def _row(maker, table: str):
    async with maker() as s:
        q = f"SELECT used, vendor_resets_at, clock_timestamp() AS now FROM {table} WHERE key='v:prod'"
        return (await s.execute(text(q))).one()


# --- finding 1: a reading whose reset is already past merges nothing ------------------------------


@pytest.mark.parametrize("past", [True, False])
def test_a_reading_from_a_window_already_over_cannot_reopen_this_one(past: bool) -> None:
    """Repro: Window(100, 60), 60 grants, then a late reading (reset 5s AGO) flushed -- v0.11-rc rolled
    the window on it and granted 100 more (160 vs cap 100). Now it merges nothing and is counted
    `flush_stale`. The other direction: a reading for the OPEN vendor window still wins."""

    async def go():
        async with scratch_schema() as (maker, schema):
            t = await _table(maker, schema)
            b = PgWindowBudget(maker, t, "v:prod", Window(100, 60), max_wait=0)
            first = await _grants(b, 60)
            resets = _now() + (dt.timedelta(seconds=-5) if past else dt.timedelta(seconds=30))
            b.observe(_q(used=70 if not past else 10, available=50, resets_at=resets))
            assert await b.flush_observed(force=True) is True
            row = await _row(maker, t)
            return first, await _grants(b, 200), row, b.stats

    first, second, row, stats = asyncio.run(go())
    assert first == 60
    if past:
        assert second == 40 and first + second == 100  # never past the cap
        assert row.used == 60 and row.vendor_resets_at is None and stats.flush_stale == 1
    else:
        assert row.used == 70 and row.vendor_resets_at is not None and stats.flush_stale == 0
        assert second == 30  # the vendor's (higher) counter still wins in the open window


def test_a_failed_refusal_flush_does_not_pin_the_next_window() -> None:
    """A refusal whose flush failed is still pending when the vendor's next window starts: it lands
    stale (merges nothing), and a later reading with a LATER reset replaces it instead of inheriting
    the refusal under the new reset."""

    async def go():
        async with scratch_schema() as (maker, schema):
            t = await _table(maker, schema)
            b = PgWindowBudget(maker, t, "v:prod", Window(5, 60), max_wait=0)
            await b.acquire()
            b.observe(_q(used=120, available=0, resets_at=_now() + dt.timedelta(milliseconds=300)))
            await asyncio.sleep(0.5)  # the flush "failed" until after the vendor reset
            assert await b.flush_observed() is True
            return b.stats.flush_stale, await _grants(b, 10)

    stale, granted = asyncio.run(go())
    assert stale == 1 and granted == 4  # 1 + 4 = capacity: not pinned at capacity until 60s from now


@pytest.mark.parametrize(("later", "carried"), [(True, False), (False, True), (None, True)])
def test_a_refusal_is_carried_only_inside_its_own_vendor_window(later: bool | None, carried: bool) -> None:
    b = PgWindowBudget(lambda: None, "s.t", "v:prod", Window(5, 60), max_wait=1)  # type: ignore[arg-type]
    r1 = _now() + dt.timedelta(seconds=10)
    b.observe(_q(used=9, available=0, resets_at=r1))
    nxt = None if later is None else r1 + dt.timedelta(seconds=60 if later else -1)
    b.observe(_q(used=1, available=4, resets_at=nxt))
    assert b._pending is not None and b._pending.refused is carried
    assert b._pending.resets_at == (r1 if carried else nxt)


# --- finding 2: a reset is clamped to a horizon ----------------------------------------------------


@pytest.mark.parametrize("ahead_s", [200, 1])
def test_a_far_reset_is_clamped_to_the_horizon_on_the_db_clock(ahead_s: int) -> None:
    """Window(5, 1): a refusal saying "reset in 200s" (inside observe's local-clock skew allowance, so
    it reaches the DB) is clamped to RESET_HORIZON_PERIODS x 1s; v0.11-rc pinned the bucket the full
    200s. A reset INSIDE the horizon is kept as the vendor said."""

    async def go():
        async with scratch_schema() as (maker, schema):
            t = await _table(maker, schema)
            b = PgWindowBudget(maker, t, "v:prod", Window(5, 1), max_wait=RESET_HORIZON_PERIODS + 1)
            await b.acquire()
            said = _now() + dt.timedelta(seconds=ahead_s)
            b.observe(_q(used=5, available=0, resets_at=said))
            assert b._pending is not None
            assert await b.flush_observed() is True
            row = await _row(maker, t)
            waited = await b.acquire()  # granted once the (clamped) reset passes
            return said, row, waited

    said, row, waited = asyncio.run(go())
    ahead = (row.vendor_resets_at - row.now).total_seconds()
    if ahead_s > RESET_HORIZON_PERIODS:
        assert ahead <= RESET_HORIZON_PERIODS + 0.05 and row.vendor_resets_at < said
    else:
        assert abs((row.vendor_resets_at - said).total_seconds()) < 0.01  # not clamped
    assert waited <= RESET_HORIZON_PERIODS + 0.2


def test_observe_drops_an_implausible_reset_and_keeps_a_plausible_one() -> None:
    b = PgWindowBudget(lambda: None, "s.t", "v:prod", Window(5, 60), max_wait=1)  # type: ignore[arg-type]
    far, epoch = dt.datetime(2099, 1, 1, tzinfo=dt.UTC), dt.datetime(1970, 1, 21, tzinfo=dt.UTC)
    for bad in (far, epoch):
        b.observe(_q(used=5, available=0, resets_at=bad))  # a refusal needs a believable reset: dropped
    assert b._pending is None and b.stats.observe_invalid == 2
    b.observe(_q(used=3, available=2, resets_at=far))  # the counter is kept, the reset is not
    assert b._pending is not None and (b._pending.used, b._pending.resets_at) == (3, None)
    assert b.stats.observe_invalid == 3
    ok = _now() + dt.timedelta(seconds=30)
    b.observe(_q(used=4, available=1, resets_at=ok))
    assert b._pending.resets_at == ok and b.stats.observe_invalid == 3


# --- finding 3 + round 2: the LOCK RULE -----------------------------------------------------------


def test_contention_is_never_unavailable() -> None:
    """Round-2 repro: 60 concurrent acquires from 3 engines, max_wait=0, capacity never reached. At
    0cb8c98 (lock_timeout = remaining max_wait, 50ms floor) 31 of 60 were BudgetUnavailable -- the
    paging counter fired on a queue. Now every call grants or is exhausted; none is unavailable."""

    async def go():
        async with scratch_schema() as (maker, schema):
            t = await _table(maker, schema)
            engines = [create_async_engine(dsn(), pool_size=20, max_overflow=0) for _ in range(3)]
            try:
                bs = [PgWindowBudget(async_sessionmaker(e), t, "v:p", Window(1000, 60), max_wait=0.0) for e in engines]
                await bs[0].acquire()
                out: collections.Counter[str] = collections.Counter()

                async def one(b: PgWindowBudget) -> None:
                    try:
                        await b.acquire()
                        out["granted"] += 1
                    except BudgetUnavailable:
                        out["unavailable"] += 1
                    except BudgetExhausted:
                        out["exhausted"] += 1

                await asyncio.gather(*(one(bs[i % 3]) for i in range(60)))
                return out, sum(b.stats.unavailable for b in bs)
            finally:
                for e in engines:
                    await e.dispose()

    out, unavailable = asyncio.run(go())
    assert out["unavailable"] == 0 and unavailable == 0, out
    assert out["granted"] + out["exhausted"] == 60, out


async def _with_held_lock(*, max_wait: float, hold_s: float | None, capacity: int = 5):
    """Hold the row FOR UPDATE on a second connection for `hold_s` (None = never released while the
    acquire runs), then acquire. -> (result or exception, elapsed, stats, flushed)."""
    async with scratch_schema() as (maker, schema):
        t = await _table(maker, schema)
        b = PgWindowBudget(maker, t, "v:prod", Window(capacity, 60), max_wait=max_wait)
        await b.acquire()
        async with maker() as holder:
            await holder.execute(text(f"SELECT 1 FROM {t} WHERE key='v:prod' FOR UPDATE"))

            async def release() -> None:
                if hold_s is not None:
                    await asyncio.sleep(hold_s)
                    await holder.rollback()

            rel = asyncio.create_task(release())
            t0 = time.monotonic()
            try:
                out: object = await asyncio.wait_for(b.acquire(), max_wait + LOCK_FLOOR_S + 5)
            except (BudgetUnavailable, BudgetExhausted) as exc:
                out = exc
            elapsed = time.monotonic() - t0
            await rel
            flushed = None
            if hold_s is None:  # a flush behind the same stuck lock: bounded, counted
                b.observe(_q(used=4, available=1))
                flushed = await asyncio.wait_for(b.flush_observed(force=True), 10)
            await holder.rollback()
        return out, elapsed, b.stats, flushed


def test_a_stuck_lock_ends_unavailable_only_past_max_wait_plus_the_floor() -> None:
    """A lock nobody releases: v0.11-rc blocked forever. Now: one attempt inside max_wait (times
    out, contended), then the LOCK_FLOOR_S attempt (times out) -> BudgetUnavailable, at max_wait +
    LOCK_FLOOR_S and not before. The flush behind it is bounded and counted."""
    out, elapsed, stats, flushed = asyncio.run(_with_held_lock(max_wait=0.5, hold_s=None))
    assert isinstance(out, BudgetUnavailable) and "hung transaction" in str(out)
    assert 0.5 + LOCK_FLOOR_S - 0.1 <= elapsed < 0.5 + LOCK_FLOOR_S + 1.5
    assert stats.unavailable == 1 and stats.lock_contended == 2
    assert flushed is False and stats.flush_failed == 1


@pytest.mark.parametrize(
    ("hold_s", "capacity", "expect"),
    [
        (0.2, 5, "granted"),  # released inside max_wait: no contention timeout at all
        (1.0, 5, "granted"),  # held past max_wait but inside the floor: contention, still granted
        (1.0, 1, "exhausted"),  # ...and when the budget is spent, exhausted -- never unavailable
    ],
)
def test_a_lock_released_inside_the_floor_is_contention_not_unavailability(
    hold_s: float, capacity: int, expect: str
) -> None:
    out, _elapsed, stats, _ = asyncio.run(_with_held_lock(max_wait=0.5, hold_s=hold_s, capacity=capacity))
    assert stats.unavailable == 0
    if expect == "granted":
        assert out == 0.0 and stats.grants == 2
    else:
        assert isinstance(out, BudgetExhausted) and out.next_in_s > 50 and stats.exhausted == 1
    assert stats.lock_contended == (0 if hold_s < 0.5 else 1)


class _PgErr(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _Wrapped(Exception):
    def __init__(self, orig: BaseException) -> None:
        super().__init__("wrapped")
        self.orig = orig


@pytest.mark.parametrize(
    ("exc", "took_s", "kind"),
    [
        (_Wrapped(_PgErr("55P03")), 0.06, "lock"),  # lock_not_available: only our bound sets it
        (_Wrapped(_PgErr("57014")), 3.0, "slow"),  # our statement_timeout fired (budget 2s + 1s slack)
        (_Wrapped(_PgErr("57014")), 0.1, None),  # canceled early: an operator's pg_cancel_backend
        (_Wrapped(_PgErr("08006")), 3.0, None),  # connection failure: unavailable
        (RuntimeError("no sqlstate"), 3.0, None),
    ],
)
def test_only_our_own_bound_counts_as_contention(exc: BaseException, took_s: float, kind: str | None) -> None:
    assert PgWindowBudget._bound_fired(exc, 2.0, took_s) == kind


# --- round 3: lock time spends max_wait; a slow store is not a hung lock; the true bound -----------


def test_lock_wait_spends_max_wait_so_a_late_refusal_never_sleeps() -> None:
    """A held lock eats most of max_wait (1.2s of 1.5s), then the row refuses with ~0.75s to the
    window end. Lock time counts: 1.2 + 0.75 > 1.5 -> BudgetExhausted, no sleep. With the lock time
    ignored (`elapsed = waited`), 0 + 0.75 <= 1.5 and it slept past max_wait."""

    async def go():
        async with scratch_schema() as (maker, schema):
            t = await _table(maker, schema)
            naps: list[float] = []

            async def sleep(s: float) -> None:
                naps.append(s)
                await asyncio.sleep(s)

            b = PgWindowBudget(maker, t, "v:prod", Window(1, 2), max_wait=1.5, sleep=sleep)
            await b.acquire()  # the window is full until ~2s from now
            async with maker() as holder:
                await holder.execute(text(f"SELECT 1 FROM {t} WHERE key='v:prod' FOR UPDATE"))

                async def release() -> None:
                    await asyncio.sleep(1.2)
                    await holder.rollback()

                rel = asyncio.create_task(release())
                try:
                    out: object = await b.acquire()
                except BudgetExhausted as exc:
                    out = exc
                await rel
            return out, naps, b.stats

    out, naps, stats = asyncio.run(go())
    assert isinstance(out, BudgetExhausted) and naps == [] and stats.unavailable == 0
    assert 0 < out.next_in_s < 1.0 and out.waited_s == 0.0


def test_a_slow_store_is_named_slow_not_a_hung_lock() -> None:
    """Round-3 repro: a BEFORE UPDATE trigger sleeps 30s, so our statement_timeout (57014) fires while
    the statement HOLDS the row. That is a slow store: `store_slow`, not `lock_contended`, and the
    reason says statement_timeout -- never "hung transaction". It ends inside max_wait + MAX_OVERRUN_S."""

    async def go():
        async with scratch_schema() as (maker, schema):
            t = await _table(maker, schema)
            async with maker() as s:
                await s.execute(
                    text(
                        f"CREATE FUNCTION {schema}.slow() RETURNS trigger LANGUAGE plpgsql AS "
                        "$$ BEGIN PERFORM pg_sleep(30); RETURN NEW; END $$"
                    )
                )
                await s.commit()
            b = PgWindowBudget(maker, t, "v:prod", Window(100, 60), max_wait=1.0)
            await b.acquire()
            async with maker() as s:
                await s.execute(
                    text(f"CREATE TRIGGER slow BEFORE UPDATE ON {t} FOR EACH ROW EXECUTE FUNCTION {schema}.slow()")
                )
                await s.commit()
            t0 = time.monotonic()
            try:
                out: object = await asyncio.wait_for(b.acquire(), 1.0 + MAX_OVERRUN_S + 5)
            except BudgetUnavailable as exc:
                out = exc
            return out, time.monotonic() - t0, b.stats

    out, elapsed, stats = asyncio.run(go())
    assert isinstance(out, BudgetUnavailable) and "store slow (statement_timeout)" in str(out)
    assert "hung transaction" not in str(out)
    assert stats.store_slow == 2 and stats.lock_contended == 0 and stats.unavailable == 1
    assert elapsed <= 1.0 + MAX_OVERRUN_S + 0.5  # the documented true bound
    assert MAX_OVERRUN_S == pytest.approx(LOCK_FLOOR_S + 2.0 + 0.05)
