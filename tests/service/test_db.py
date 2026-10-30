"""Database: lazy, one engine per build, kwargs reach the engine, on_engine fires once, dispose resets."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from edge_platform.service.db import APP_NAME_MAX, Database, application_name


def test_application_name(monkeypatch):
    monkeypatch.delenv("PGAPPNAME", raising=False)
    assert application_name("svc") == "svc"
    monkeypatch.setenv("PGAPPNAME", "   ")
    assert application_name("svc") == "svc"  # whitespace-only is not a name
    monkeypatch.setenv("PGAPPNAME", " trade-quant-service ")
    assert application_name("svc") == "trade-quant-service"
    monkeypatch.setenv("PGAPPNAME", "x" * 100)
    assert len(application_name("svc")) == APP_NAME_MAX


def test_lazy_build_round_trip_and_dispose():
    seen = []
    db = Database(lambda: "sqlite+aiosqlite://", engine_kwargs=lambda: {"echo": False}, on_engine=seen.append)
    assert not db.built and seen == []

    async def go():
        maker = db.session_maker()
        assert db.session_maker() is maker and db.engine() is seen[0]
        async with maker() as s:
            assert (await s.execute(text("SELECT 1"))).scalar() == 1
        await db.dispose()
        assert not db.built and db.state() == (None, None)
        await db.dispose()  # twice is safe
        db.engine()
        assert len(seen) == 2  # a fresh build after dispose
        await db.dispose()

    asyncio.run(go())


def test_pool_pre_ping_default_and_override():
    db = Database(lambda: "sqlite+aiosqlite://")
    assert db.engine().pool._pre_ping is True
    asyncio.run(db.dispose())
    db = Database(lambda: "sqlite+aiosqlite://", engine_kwargs=lambda: {"pool_pre_ping": False})
    assert db.engine().pool._pre_ping is False
    asyncio.run(db.dispose())


def test_failed_hook_keeps_no_engine():
    calls = []

    def hook(engine):
        calls.append(engine)
        if len(calls) == 1:
            raise RuntimeError("tracer down")

    db = Database(lambda: "sqlite+aiosqlite://", on_engine=hook)
    try:
        db.engine()
    except RuntimeError:
        pass
    assert not db.built
    db.engine()
    assert db.built and len(calls) == 2
    asyncio.run(db.dispose())


def test_adopt_and_forget():
    a = Database(lambda: "sqlite+aiosqlite://")
    b = Database(lambda: "sqlite+aiosqlite://")
    b.adopt(*(a.engine(), a.session_maker()))
    assert b.engine() is a.engine()
    b.forget()
    assert not b.built and a.built
    asyncio.run(a.dispose())
