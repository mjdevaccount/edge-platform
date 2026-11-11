"""PgWindowBudget hardening (v0.11.0 review of PR #18): a vendor reading only speaks for a window that
is still open, a reset is clamped to a horizon, and a held row lock cannot outlast `max_wait`. Every
case is must-fire BOTH ways, on a REAL Postgres (`tests/pg.py`)."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import types

import pytest
from sqlalchemy import text

from edge_platform.budget import RESET_HORIZON_PERIODS, BudgetExhausted, BudgetUnavailable, PgWindowBudget, Window, ddl

from .pg import scratch_schema


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


# --- finding 3: a held row lock cannot outlast max_wait ------------------------------------------


@pytest.mark.parametrize("release_after_s", [None, 0.2])
def test_a_held_row_lock_is_bounded_by_max_wait(release_after_s: float | None) -> None:
    """Another connection holds the row FOR UPDATE. v0.11-rc blocked `acquire(max_wait=0.5)` for as
    long as the holder liked; now the transaction's lock_timeout is the remaining max_wait and the
    attempt fails closed (`BudgetUnavailable`). A lock released inside max_wait still grants."""

    async def go():
        async with scratch_schema() as (maker, schema):
            t = await _table(maker, schema)
            b = PgWindowBudget(maker, t, "v:prod", Window(5, 60), max_wait=0.5)
            await b.acquire()
            async with maker() as holder:
                await holder.execute(text(f"SELECT 1 FROM {t} WHERE key='v:prod' FOR UPDATE"))

                async def release() -> None:
                    if release_after_s is not None:
                        await asyncio.sleep(release_after_s)
                        await holder.rollback()

                rel = asyncio.create_task(release())
                t0 = time.monotonic()
                try:
                    out = await asyncio.wait_for(b.acquire(), 5)
                except BudgetUnavailable as exc:
                    out = exc
                elapsed = time.monotonic() - t0
                await rel
                flushed = None
                if release_after_s is None:  # a flush behind the same held lock: bounded, counted
                    b.observe(_q(used=4, available=1))
                    flushed = await asyncio.wait_for(b.flush_observed(force=True), 10)
                await holder.rollback()
            return out, elapsed, flushed, b.stats

    out, elapsed, flushed, stats = asyncio.run(go())
    if release_after_s is None:
        assert isinstance(out, BudgetUnavailable) and elapsed < 2.0 and stats.unavailable == 1
        assert flushed is False and stats.flush_failed == 1
    else:
        assert out == 0.0 and stats.grants == 2 and stats.unavailable == 0
