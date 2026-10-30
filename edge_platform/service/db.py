"""One lazy async engine + session maker per process, and the Postgres identity it presents.

trade-data and trade-quant each carried this singleton as two module globals with a getter and a
`dispose()`, and they had drifted: one nulled the session maker only when an engine existed, the other
always. `Database` is the one copy. Each service keeps module-level `get_engine` / `get_session_maker` /
`dispose` as one-line delegates, so callers (and tests that patch `service.db.get_session_maker`) do not
change, and passes ITS engine kwargs -- pool sizes, statement-cache settings and the comments that
justify them stay in the service, because they are facts about that service's connection budget.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

#: Postgres truncates application_name at NAMEDATALEN-1 bytes.
APP_NAME_MAX = 63


def application_name(default: str) -> str:
    """What this process calls itself in `pg_stat_activity`: `$PGAPPNAME`, else `default`, capped.

    Services that share one role, database and pgbouncer pool are told apart ONLY by this string, so
    an empty one is the anonymous connection it exists to prevent. asyncpg does not read PGAPPNAME,
    which is why it must reach the engine through `server_settings`. strip() runs BEFORE the fallback:
    a whitespace-only PGAPPNAME is truthy and would otherwise hand Postgres an empty name.
    """
    return ((os.environ.get("PGAPPNAME") or "").strip() or default)[:APP_NAME_MAX]


class Database:
    """The process's engine and session maker, built on first use and torn down by `dispose()`.

    `url` and `engine_kwargs` are callables so nothing reads settings at import time. `pool_pre_ping`
    defaults on; `engine_kwargs` may override it. `on_engine` runs once with each engine created (a
    tracing hook); it must not raise, and if it does the engine is NOT kept, so the next call retries.
    """

    def __init__(
        self,
        url: Callable[[], str],
        *,
        engine_kwargs: Callable[[], Mapping[str, Any]] | None = None,
        on_engine: Callable[[AsyncEngine], None] | None = None,
        expire_on_commit: bool = False,
    ) -> None:
        self._url = url
        self._engine_kwargs = engine_kwargs
        self._on_engine = on_engine
        self._expire_on_commit = expire_on_commit
        self._engine: AsyncEngine | None = None
        self._session_maker: async_sessionmaker[AsyncSession] | None = None

    def engine(self) -> AsyncEngine:
        if self._engine is None:
            kwargs: dict[str, Any] = {"pool_pre_ping": True}
            if self._engine_kwargs is not None:
                kwargs.update(self._engine_kwargs())
            engine = create_async_engine(self._url(), **kwargs)
            if self._on_engine is not None:
                self._on_engine(engine)
            self._engine = engine
        return self._engine

    def session_maker(self) -> async_sessionmaker[AsyncSession]:
        if self._session_maker is None:
            self._session_maker = async_sessionmaker(self.engine(), expire_on_commit=self._expire_on_commit)
        return self._session_maker

    @property
    def built(self) -> bool:
        """True once an engine exists (and until `dispose()` / `forget()`)."""
        return self._engine is not None

    def adopt(self, engine: AsyncEngine | None, session_maker: async_sessionmaker[AsyncSession] | None) -> None:
        """Install an existing engine/session maker (tests that share one across event loops)."""
        self._engine = engine
        self._session_maker = session_maker

    def state(self) -> tuple[AsyncEngine | None, async_sessionmaker[AsyncSession] | None]:
        return self._engine, self._session_maker

    def forget(self) -> None:
        """Drop the references WITHOUT disposing (the pool belongs to a loop that is gone)."""
        self._engine = None
        self._session_maker = None

    async def dispose(self) -> None:
        """Close the pool and forget both, so the next use builds fresh. Safe to call twice."""
        engine = self._engine
        self.forget()
        if engine is not None:
            await engine.dispose()
