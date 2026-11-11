"""SqlLedger on a REAL Postgres obeys the same contract as the in-memory double, and a delivery row
comes back out through outcome_series (ported from trade-data tests/integration/test_notify_ledger.py)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from edge_platform.notify.ledger import NotifyStore, NotifyTables, SqlLedger, ddl, outcome_series
from edge_platform.notify.pipeline import Notifier
from edge_platform.notify.types import Budget, Channel, Dedup, Notice, Outcome

from ..pg import scratch_schema
from . import ledger_contract as contract
from .fakes import RecordingTransport


def _tables(schema: str) -> NotifyTables:
    return NotifyTables(claims=f"{schema}.nclaims", budget=f"{schema}.nbudget", deliveries=f"{schema}.ndeliveries")


async def _setup(maker, schema: str) -> NotifyTables:
    tables = _tables(schema)
    async with maker() as s:
        for stmt in ddl(tables):
            await s.execute(text(stmt))
        await s.commit()
    return tables


@pytest.mark.parametrize("case", contract.CASES, ids=[c.__name__ for c in contract.CASES])
def test_sql_ledger_obeys_the_contract(case) -> None:
    async def main() -> None:
        async with scratch_schema() as (maker, schema):
            tables = await _setup(maker, schema)

            async def age(scope: str, key: str, seconds: float) -> None:
                async with maker() as s:
                    await s.execute(
                        text(f"UPDATE {tables.claims} SET last_at = last_at - make_interval(secs => CAST(:sec AS float8)) "
                             "WHERE scope = :scope AND key = :key"),
                        {"sec": seconds, "scope": scope, "key": key},
                    )  # fmt: skip
                    await s.commit()

            await case(SqlLedger(maker, tables), age)

    asyncio.run(main())


def test_counters_land() -> None:
    async def main() -> None:
        async with scratch_schema() as (maker, schema):
            tables = await _setup(maker, schema)
            ch = Channel("it-counters", dedup=Dedup.once(), budget=Budget(1))
            led = SqlLedger(maker, tables)
            for key in ("a", "a", "b"):
                await led.admit(Notice(ch, key=key, title="t", body="b"), contract.NOW)
            async with maker() as s:
                claims = (
                    await s.execute(text(f"SELECT key, n_claimed, n_suppressed FROM {tables.claims} ORDER BY key"))
                ).all()
                budget = (await s.execute(text(f"SELECT spent, refused, cap FROM {tables.budget}"))).one()
            assert [tuple(r) for r in claims] == [("a", 1, 1)]  # b was refused: no claim left
            assert tuple(budget) == (1, 1, 1)

    asyncio.run(main())


def test_every_recorded_outcome_passes_the_deliveries_check_and_deduped_does_not() -> None:
    """The ddl's CHECK is the domain: each RECORDED outcome writes, the counter-only one cannot."""

    async def main() -> None:
        async with scratch_schema() as (maker, schema):
            tables = await _setup(maker, schema)
            async with maker() as s:
                store = NotifyStore(s, tables)
                for o in Outcome:
                    if o is Outcome.DEDUPED:
                        continue
                    await store.record(scope="chk", key=o.value, tier="p2", outcome=o.value, delivered=False,
                                       title="t", detail=None)  # fmt: skip
                await s.commit()
            with pytest.raises(Exception, match="check"):
                async with maker() as s:
                    await NotifyStore(s, tables).record(scope="chk", key="d", tier="p2", outcome="deduped",
                                                        delivered=False, title="t", detail=None)  # fmt: skip

    asyncio.run(main())


def test_a_delivery_row_reaches_the_series() -> None:
    async def main() -> None:
        async with scratch_schema() as (maker, schema):
            tables = await _setup(maker, schema)
            led = SqlLedger(maker, tables)
            n = Notifier(transport=RecordingTransport(outcome=Outcome.FAILED, detail="HTTP 500"), ledger=led)
            d = await n.notify(Notice(Channel("it-gauge"), key="k", title="t", body="b"))
            assert (d.outcome, d.recorded) == (Outcome.FAILED, True)
            got = {(lb["scope"], lb["outcome"]): v for lb, v in outcome_series(await led.outcomes_24h())}
            assert got[("it-gauge", "failed")] == 1.0 and got[("it-gauge", "sent")] == 0.0
            assert got[("_all", "failed")] == 1.0

    asyncio.run(main())


def test_an_unreadable_ledger_is_blind_not_zero() -> None:
    async def main() -> None:
        async with scratch_schema() as (maker, schema):
            led = SqlLedger(maker, _tables(schema))  # tables never created
            assert await led.outcomes_24h() is None
            assert all(v is None for _, v in outcome_series(await led.outcomes_24h()))

    asyncio.run(main())
