"""WatchdogStateStore against a REAL Postgres (ported from trade-data
tests/integration/test_watchdog_state_repo.py).

WHY: the runner's unit tests fake this store, so its SQL had never executed when trade-data shipped it
-- and it failed on the FIRST production call (`AmbiguousParameterError` on a bare :now inside a CASE
arm). This runs the statements, over a table whose name the test chooses."""

from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import text

from edge_platform.watch.runner import WatchRunner
from edge_platform.watch.spec import Probe, Status, WatchRegistry
from edge_platform.watch.state import WatchdogStateStore, ddl

from ..pg import scratch_schema
from .rig import VOCAB, spec


async def _setup(maker, schema: str) -> str:
    table = f"{schema}.wd_state"
    async with maker() as s:
        await s.execute(text(ddl(table)))
        await s.commit()
    return table


def test_record_and_load_roundtrip_including_the_page_clock() -> None:
    async def main() -> None:
        async with scratch_schema() as (maker, schema):
            table = await _setup(maker, schema)
            async with maker() as session:
                repo = WatchdogStateStore(session, table)
                t0 = dt.datetime(2026, 8, 16, 13, 30, tzinfo=dt.UTC)
                assert await repo.load(["wd-a"]) == {} and await repo.load([]) == {}

                await repo.record("wd-a", status="ok", transitioned=True, paged=False, now=t0)
                await session.commit()
                row = (await repo.load(["wd-a"]))["wd-a"]
                assert (row["last_status"], row["last_transition_at"], row["last_page_at"]) == ("ok", t0, None)

                t1 = t0 + dt.timedelta(minutes=5)
                await repo.record("wd-a", status="down", transitioned=True, paged=True, now=t1)
                await session.commit()
                row = (await repo.load(["wd-a"]))["wd-a"]
                assert (row["last_status"], row["last_transition_at"], row["last_page_at"]) == ("down", t1, t1)

                # STILL down, no page: neither clock may move -- the property transition-only dedup rests on
                t2 = t1 + dt.timedelta(minutes=5)
                await repo.record("wd-a", status="down", transitioned=False, paged=False, now=t2)
                await session.commit()
                row = (await repo.load(["wd-a"]))["wd-a"]
                assert row["last_transition_at"] == t1, "a non-transition moved the transition clock"
                assert row["last_page_at"] == t1, "a tick with no page moved the page clock"
                assert row["updated_at"] == t2

    asyncio.run(main())


def test_load_filters_reap_removes_and_subjects_do_not_outlive_the_verdict() -> None:
    async def main() -> None:
        async with scratch_schema() as (maker, schema):
            table = await _setup(maker, schema)
            now = dt.datetime(2026, 8, 16, 14, 0, tzinfo=dt.UTC)
            async with maker() as session:
                repo = WatchdogStateStore(session, table)
                await repo.record("wd-x", status="ok", transitioned=True, paged=False, now=now)
                await repo.record("wd-y", status="warn", transitioned=True, paged=False, now=now)
                await repo.record_subjects("wd-y", ["loop-a", "loop-b"])
                await session.commit()
                assert set(await repo.load(["wd-x"])) == {"wd-x"}
                assert (await repo.load(["wd-y"]))["wd-y"]["last_subjects"] == ["loop-a", "loop-b"]
                await repo.record("wd-y", status="warn", transitioned=False, paged=False, now=now)
                assert (await repo.load(["wd-y"]))["wd-y"]["last_subjects"] == ["loop-a", "loop-b"]
                await repo.record("wd-y", status="ok", transitioned=True, paged=False, now=now)
                assert (await repo.load(["wd-y"]))["wd-y"]["last_subjects"] is None
                await repo.record_subjects("wd-x", [])
                assert (await repo.load(["wd-x"]))["wd-x"]["last_subjects"] is None
                assert await repo.reap_unregistered(keep=["wd-x"]) == ["wd-y"]
                assert set(await repo.load(["wd-x", "wd-y"])) == {"wd-x"}
                await session.commit()

    asyncio.run(main())


def test_claim_reports_the_rows_another_transaction_holds() -> None:
    """TWO real transactions, because the property is a lock: claim() must hand the second sweep the
    held row as CONTENDED, at once, without waiting on it."""

    async def main() -> None:
        async with scratch_schema() as (maker, schema):
            table = await _setup(maker, schema)
            now = dt.datetime(2026, 10, 20, 14, 10, tzinfo=dt.UTC)
            async with maker() as seed:
                for wid in ("wd-held", "wd-free"):
                    await WatchdogStateStore(seed, table).record(wid, status="ok", transitioned=True, paged=False,
                                                                 now=now)  # fmt: skip
                await seed.commit()
            async with maker() as sweep, maker() as card:
                assert await WatchdogStateStore(sweep, table).claim(["wd-held"]) == set()
                await card.execute(text("SET LOCAL lock_timeout = '1s'"))
                contended = await WatchdogStateStore(card, table).claim(["wd-held", "wd-free", "wd-new"])
                assert contended == {"wd-held"}  # held -> contended; free -> claimed; new -> not contended
                await sweep.rollback()
                await card.rollback()
            async with maker() as again:
                assert await WatchdogStateStore(again, table).claim(["wd-held", "wd-free"]) == set()
                await again.rollback()

    asyncio.run(main())


def test_the_whole_verdict_roundtrips_and_a_recovery_overwrites_it() -> None:
    async def main() -> None:
        async with scratch_schema() as (maker, schema):
            table = await _setup(maker, schema)
            t0 = dt.datetime(2026, 11, 6, 15, 10, tzinfo=dt.UTC)
            async with maker() as session:
                repo = WatchdogStateStore(session, table)
                await repo.record("wd-v", status="down", transitioned=True, paged=True, now=t0,
                                  detail="3 units stuck > 15m", metrics={"stuck": 3, "probe_ms": 41, "nan": float("nan")})  # fmt: skip
                await session.commit()
                row = (await repo.load(["wd-v"]))["wd-v"]
                assert row["last_detail"] == "3 units stuck > 15m"
                assert row["last_metrics"] == {"stuck": 3, "probe_ms": 41, "nan": None}
                await repo.record("wd-v", status="ok", transitioned=True, paged=False, now=t0 + dt.timedelta(hours=1),
                                  detail="clear", metrics={"probe_ms": 9}, error=None)  # fmt: skip
                await session.commit()
                row = (await repo.load(["wd-v"]))["wd-v"]
                assert (row["last_detail"], row["last_metrics"]) == ("clear", {"probe_ms": 9})
                await repo.record("wd-v", status="ok", transitioned=False, paged=False, now=t0 + dt.timedelta(hours=2))
                await session.commit()
                row = (await repo.load(["wd-v"]))["wd-v"]
                assert row["last_detail"] is None and row["last_metrics"] is None

    asyncio.run(main())


def test_a_real_sweep_survives_a_poisoned_probe_on_postgres() -> None:
    """The savepoint, on the database whose abort semantics it exists for (ported from trade-data
    tests/integration/test_watchdog_runner_savepoint_live.py): a probe whose SQL errors must leave the
    next spec's state write -- and the stored read -- working."""

    async def main() -> None:
        async with scratch_schema() as (maker, schema):
            table = await _setup(maker, schema)
            reg = WatchRegistry(VOCAB)

            async def poisoned(session):
                await session.execute(text("SELECT CAST('x' AS date) + 'unknown'"))

            async def healthy(session):
                n = (await session.execute(text("SELECT 1"))).scalar_one()
                return Probe(Status.DOWN, "really down", metrics={"n": n})

            reg.register(spec("a-poisoned", poisoned))
            reg.register(spec("b-sibling", healthy))
            pages: list[str] = []

            async def page(s, p):
                pages.append(s.id)
                return True

            runner = WatchRunner(reg, store=WatchdogStateStore.bind(table), session_day=lambda n: True, page=page)
            at = dt.datetime(2026, 9, 13, 13, 30, tzinfo=dt.UTC)
            async with maker() as session:
                res = await runner.run(session, now=at)
                await session.commit()
            assert [r["status"] for r in res["results"]] == ["unknown", "down"] and pages == ["b-sibling"]
            async with maker() as session:
                stored = await runner.read_stored(session, now=at + dt.timedelta(minutes=5))
            by_id = {r["id"]: r for r in stored["results"]}
            assert by_id["a-poisoned"]["status"] == "unknown" and "Error" in by_id["a-poisoned"]["error"]
            assert by_id["b-sibling"]["status"] == "down" and by_id["b-sibling"]["metrics"]["n"] == 1
            assert by_id["b-sibling"]["paged"] is True and stored["stale"] == []

    asyncio.run(main())
