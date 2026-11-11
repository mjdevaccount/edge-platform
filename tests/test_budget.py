"""PgWindowBudget (rate-budget.md acceptance 1-6). The store tests run on a REAL Postgres
(`tests/pg.py`: $EDGE_PLATFORM_TEST_PG in CI, a testcontainer locally) -- a faked store would prove
the caller, never the statement that has to be atomic across processes."""

from __future__ import annotations

import asyncio
import datetime as dt
import types

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from edge_platform.budget import BudgetExhausted, BudgetUnavailable, PgWindowBudget, Window, ddl

from .pg import dsn, scratch_schema


class _Sleeps:
    """Records each wait and really sleeps it (the DB clock has to move for the window to turn)."""

    def __init__(self, real: bool = True) -> None:
        self.waits: list[float] = []
        self.real = real

    async def __call__(self, s: float) -> None:
        self.waits.append(s)
        if self.real:
            await asyncio.sleep(s)


def _q(**kw) -> types.SimpleNamespace:
    return types.SimpleNamespace(**{"used": None, "available": None, "resets_at": None, **kw})


async def _with_table(maker, schema: str) -> str:
    table = f"{schema}.rate_budget"
    async with maker() as s:
        await s.execute(text(ddl(table)))
        await s.commit()
    return table


async def _row(maker, table: str, key: str):
    async with maker() as s:
        return (
            await s.execute(text(f"SELECT used, capacity, vendor_resets_at FROM {table} WHERE key=:k"), {"k": key})
        ).one()


# --- pure ---------------------------------------------------------------------------------------


def test_ddl_refuses_an_unqualified_or_injected_table() -> None:
    for bad in ("rate_budget", "x.y; DROP TABLE z", 'x."Y"', "X.y", ""):
        with pytest.raises(ValueError, match=r"schema.table"):
            ddl(bad)
    with pytest.raises(ValueError, match=r"schema.table"):
        PgWindowBudget(lambda: None, "nope", "v:prod", Window(5, 60), max_wait=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [(0, 60), (5, 0), (-1, 60), (True, 60), (5.0, 60)])
def test_a_window_must_be_positive_ints(bad) -> None:
    with pytest.raises(ValueError):
        Window(*bad)


def test_observe_ignores_and_counts_malformed_readings() -> None:
    b = PgWindowBudget(lambda: None, "s.t", "v:prod", Window(5, 60), max_wait=1)  # type: ignore[arg-type]
    for q in (_q(), _q(used=-1), _q(used="9"), _q(used=True), _q(available=0),  # refusal with no reset
              _q(used=1, resets_at=dt.datetime(2026, 1, 1)), object()):  # fmt: skip
        b.observe(q)
    assert b.stats.observe_invalid == 7 and b._pending is None
    b.observe(_q(used=3, available=2))
    assert b._pending is not None and b._pending.used == 3


def test_stats_render_every_counter_zeros_included() -> None:
    b = PgWindowBudget(lambda: None, "s.t", "v:prod", Window(5, 60), max_wait=1)  # type: ignore[arg-type]
    series = {name: v for name, labels, v in b.stats.series(b.key) if labels == {"key": "v:prod"}}
    assert series["budget_exhausted_total"] == 0.0 and series["budget_unavailable_total"] == 0.0
    assert {"budget_wait_seconds_total", "budget_wait_events_total", "budget_flush_failed_total"} <= set(series)


def test_a_weight_that_can_never_fit_is_refused_up_front() -> None:
    b = PgWindowBudget(lambda: None, "s.t", "v:prod", Window(5, 60), max_wait=1)  # type: ignore[arg-type]
    for w in (6, 0, -1, True):
        with pytest.raises(ValueError):
            asyncio.run(b.acquire(w))


# --- acceptance 3: store down -> BudgetUnavailable, no grant -------------------------------------


def test_store_down_fails_closed_with_no_grant() -> None:
    async def go():
        engine = create_async_engine("postgresql+asyncpg://nobody:x@127.0.0.1:1/none", connect_args={"timeout": 2})
        b = PgWindowBudget(async_sessionmaker(engine), "s.rate_budget", "v:prod", Window(5, 60), max_wait=5)
        with pytest.raises(BudgetUnavailable):
            await b.acquire()
        b.observe(_q(used=4))
        assert await b.flush_observed(force=True) is False  # a flush never raises; it counts
        await engine.dispose()
        return b

    b = asyncio.run(go())
    assert b.stats.grants == 0 and b.stats.unavailable == 1 and b.stats.flush_failed == 1
    assert b._pending is not None  # kept for the next flush


def test_a_statement_error_fails_closed_too() -> None:
    async def go():
        async with scratch_schema() as (maker, schema):
            b = PgWindowBudget(maker, f"{schema}.never_migrated", "v:prod", Window(5, 60), max_wait=5)
            with pytest.raises(BudgetUnavailable, match=r"UndefinedTable|ProgrammingError"):
                await b.acquire()
            assert b.stats.grants == 0

    asyncio.run(go())


# --- acceptance 1: two connections race one window -----------------------------------------------


def test_two_connections_share_one_window() -> None:
    async def go():
        async with scratch_schema() as (maker, schema):
            table = await _with_table(maker, schema)
            other = create_async_engine(dsn())  # a SECOND engine: a different pool, a different process's view
            try:
                sleeps = _Sleeps()
                win = Window(capacity=5, period_s=1)
                a = PgWindowBudget(maker, table, "v:prod", win, max_wait=5, sleep=sleeps)
                b = PgWindowBudget(async_sessionmaker(other), table, "v:prod", win, max_wait=5, sleep=sleeps)
                waited = await asyncio.gather(*(x.acquire() for x in (a, b, a, b, a, b, a, b)))
            finally:
                await other.dispose()
            return waited, sleeps, (a.stats, b.stats), await _row(maker, table, "v:prod")

    waited, sleeps, stats, row = asyncio.run(go())
    assert sorted(w == 0.0 for w in waited).count(True) == 5  # exactly five immediate
    late = [w for w in waited if w > 0]
    assert len(late) == 3 and all(0 < w <= 1.05 for w in late)  # three waited for the window to turn
    assert len(sleeps.waits) == 3 and sum(s.grants for s in stats) == 8
    assert row.used == 3  # the second window holds the three late grants


# --- acceptance 2: must-fire both ways ------------------------------------------------------------


@pytest.mark.parametrize(("capacity", "fires"), [(1, True), (2, False)])
def test_exhausted_must_fire_both_ways(capacity: int, fires: bool) -> None:
    async def go():
        async with scratch_schema() as (maker, schema):
            table = await _with_table(maker, schema)
            sleeps = _Sleeps(real=False)
            b = PgWindowBudget(maker, table, "v:prod", Window(capacity, 60), max_wait=0.1, sleep=sleeps)
            assert await b.acquire() == 0.0
            if fires:
                with pytest.raises(BudgetExhausted) as ei:
                    await b.acquire()
                assert ei.value.key == "v:prod" and ei.value.waited_s == 0.0 and ei.value.next_in_s > 50
                assert sleeps.waits == [] and b.stats.exhausted == 1  # no pointless sleep past max_wait
            else:
                assert await b.acquire() == 0.0 and b.stats.exhausted == 0

    asyncio.run(go())


# --- acceptance 4 + 5: the vendor's counter wins --------------------------------------------------


def test_the_vendor_counter_wins_on_flush() -> None:
    async def go():
        async with scratch_schema() as (maker, schema):
            table = await _with_table(maker, schema)
            b = PgWindowBudget(maker, table, "v:prod", Window(100, 60), max_wait=1)
            for _ in range(10):
                await b.acquire()
            b.observe(_q(used=90, available=30))
            assert await b.flush_observed() is True
            assert (await _row(maker, table, "v:prod")).used == 90
            b.observe(_q(used=5, available=115))  # a LOWER vendor reading never un-spends ours
            assert await b.flush_observed(force=True) is True
            return await _row(maker, table, "v:prod")

    assert asyncio.run(go()).used == 90


def test_a_vendor_refusal_pins_the_bucket_until_the_vendor_resets() -> None:
    async def go():
        async with scratch_schema() as (maker, schema):
            table = await _with_table(maker, schema)
            sleeps = _Sleeps()
            b = PgWindowBudget(maker, table, "v:prod", Window(100, 60), max_wait=10, sleep=sleeps)
            await b.acquire()
            resets = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=2)
            b.observe(_q(used=120, available=0, resets_at=resets))
            assert await b.flush_observed() is True  # a refusal flushes at once, never throttled
            row = await _row(maker, table, "v:prod")
            waited = await b.acquire()
            return row, waited, sleeps, await _row(maker, table, "v:prod")

    pinned, waited, sleeps, after = asyncio.run(go())
    assert pinned.used == 120 and pinned.vendor_resets_at is not None
    assert len(sleeps.waits) == 1 and 1.0 < waited <= 2.5  # waited to the VENDOR's reset, not our 60s
    assert after.used == 1 and after.vendor_resets_at is None  # a fresh window, the reading consumed


# --- acceptance 6: idempotent DDL; a capacity disagreement is named -------------------------------


def test_ddl_applies_twice_and_a_config_conflict_is_logged(caplog) -> None:
    async def go():
        async with scratch_schema() as (maker, schema):
            table = await _with_table(maker, schema)
            await _with_table(maker, schema)  # second apply: no error
            a = PgWindowBudget(maker, table, "v:prod", Window(100, 60), max_wait=1)
            b = PgWindowBudget(maker, table, "v:prod", Window(80, 60), max_wait=1)
            await a.acquire()
            await b.acquire()
            await b.acquire()  # warned once per distinct value
            return a.stats, b.stats, await _row(maker, table, "v:prod")

    with caplog.at_level("WARNING", logger="edge_platform.budget"):
        sa, sb, row = asyncio.run(go())
    assert sa.config_conflict == 0 and sb.config_conflict == 1 and row.capacity == 80  # last writer wins
    assert caplog.text.count("budget_config_conflict") == 1 and "capacity 100" in caplog.text
