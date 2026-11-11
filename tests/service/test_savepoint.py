"""db.savepoint (ported from trade-data service/db.py's contract) and db.statement_cache_args.

The savepoint is proven against a fake that behaves like Postgres -- an aborted transaction refuses
every later statement, SAVEPOINT included -- and against a real SQLite session for the no-op
fallback and the rollback-to-mark, so the guard is seen firing both ways."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from edge_platform.service.db import savepoint, statement_cache_args


class _Aborted(Exception):
    pass


class _PgLikeSession:
    """Postgres's rule: after a failed statement outside a savepoint, everything fails."""

    def __init__(self) -> None:
        self.aborted = False
        self.ran: list[str] = []

    def begin_nested(self):
        session = self

        class _Nested:
            async def __aenter__(self):
                if session.aborted:
                    raise _Aborted("current transaction is aborted")
                return self

            async def __aexit__(self, exc_type, *_):
                if exc_type is not None:
                    session.aborted = False  # ROLLBACK TO SAVEPOINT revives the transaction
                return False

        return _Nested()

    async def execute(self, stmt: str) -> None:
        if self.aborted:
            raise _Aborted("current transaction is aborted")
        if stmt == "bad":
            self.aborted = True
            raise RuntimeError("syntax error")
        self.ran.append(stmt)


def _fan_out(session, stmts: list[str], *, guarded: bool) -> list[str]:
    async def go() -> list[str]:
        failed = []
        for s in stmts:
            try:
                if guarded:
                    async with savepoint(session):
                        await session.execute(s)
                else:
                    await session.execute(s)
            except Exception:
                failed.append(s)
        return failed

    return asyncio.run(go())


def test_a_guarded_fan_out_keeps_the_members_after_a_failure() -> None:
    s = _PgLikeSession()
    assert _fan_out(s, ["a", "bad", "c"], guarded=True) == ["bad"]
    assert s.ran == ["a", "c"]


def test_must_fire_without_it_one_failure_poisons_every_later_member() -> None:
    s = _PgLikeSession()
    assert _fan_out(s, ["a", "bad", "c"], guarded=False) == ["bad", "c"]


def test_a_session_without_begin_nested_gets_a_noop() -> None:
    class _Sentinel:
        async def execute(self, stmt: str) -> None:
            self.last = stmt

    s = _Sentinel()
    assert _fan_out(s, ["x"], guarded=True) == [] and s.last == "x"


def test_a_real_session_rolls_back_to_the_mark(tmp_path) -> None:
    async def go() -> list[int]:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sp.db'}")
        async with async_sessionmaker(engine)() as session:
            await session.execute(text("CREATE TABLE t (n INTEGER)"))
            await session.execute(text("INSERT INTO t VALUES (1)"))
            with pytest.raises(RuntimeError):
                async with savepoint(session):
                    await session.execute(text("INSERT INTO t VALUES (2)"))
                    raise RuntimeError("member failed")
            await session.execute(text("INSERT INTO t VALUES (3)"))
            rows = [r[0] for r in (await session.execute(text("SELECT n FROM t ORDER BY n"))).all()]
        await engine.dispose()
        return rows

    assert asyncio.run(go()) == [1, 3]


def test_statement_cache_args_reads_the_consumers_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_STMT_CACHE", raising=False)
    assert statement_cache_args("DEMO_STMT_CACHE") == {"statement_cache_size": 0, "prepared_statement_cache_size": 0}
    monkeypatch.setenv("DEMO_STMT_CACHE", "100")
    assert statement_cache_args("DEMO_STMT_CACHE")["prepared_statement_cache_size"] == 100
    monkeypatch.setenv("DEMO_STMT_CACHE", "lots")
    with pytest.raises(ValueError):
        statement_cache_args("DEMO_STMT_CACHE")
    monkeypatch.setenv("DEMO_STMT_CACHE", "-1")
    with pytest.raises(ValueError, match="negative"):
        statement_cache_args("DEMO_STMT_CACHE")
