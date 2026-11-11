"""A real Postgres for the SQL stores' own tests -- a faked store proves the caller, never the statement.

Set `EDGE_PLATFORM_TEST_PG=postgresql://user:pw@host:port/db`. Unset, a throwaway `postgres:18`
testcontainer is started once per run (Docker needed); no Docker either -> these tests SKIP locally.
CI sets `EDGE_PLATFORM_TEST_PG_REQUIRED=1` beside a postgres service, which turns that skip into a
FAILURE -- a suite that silently skips its only real-database check is a guard that cannot fire.

Each test gets its own throwaway schema, so the consumer-named tables are genuinely parameters.
"""

from __future__ import annotations

import atexit
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

ENV = "EDGE_PLATFORM_TEST_PG"
REQUIRED = "EDGE_PLATFORM_TEST_PG_REQUIRED"


_CONTAINER: dict[str, Any] = {}


def _container_url() -> str | None:
    """One postgres:18 testcontainer for the whole run (stopped at exit); None when Docker is absent."""
    if "url" in _CONTAINER:
        return _CONTAINER["url"]
    url = None
    try:
        from testcontainers.community.postgres import PostgresContainer

        pg = PostgresContainer("postgres:18", driver=None).start()
        atexit.register(pg.stop)
        url = pg.get_connection_url()
    except Exception as exc:  # no Docker / no image: the skip below names it
        _CONTAINER["why"] = f"{type(exc).__name__}: {exc}"
    _CONTAINER["url"] = url
    return url


def dsn() -> str:
    raw = (os.environ.get(ENV) or "").strip()
    if not raw:
        if os.environ.get(REQUIRED) == "1":
            pytest.fail(f"{REQUIRED}=1 but ${ENV} is unset: the real-Postgres tests would not run")
        raw = _container_url() or ""
        if not raw:
            pytest.skip(f"${ENV} unset and no testcontainer ({_CONTAINER.get('why')})")
    return raw.replace("postgresql://", "postgresql+asyncpg://", 1)


@asynccontextmanager
async def scratch_schema() -> AsyncIterator[tuple[Any, str]]:
    """(session maker, schema) -- the schema is dropped, and the engine disposed, on the way out."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(dsn())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    schema = f"t_{uuid.uuid4().hex[:12]}"
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
        yield maker, schema
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await engine.dispose()
