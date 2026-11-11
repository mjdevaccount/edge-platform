"""Liveness and readiness: `/healthz` never touches the database, `/readyz` must (extra `[service]`).

Lifted from trade-quant `service/main.py` (CONTRACTS-PLAN E2; M6 PL2). One incident each:
- a `/healthz` that touched the DB let autoheal restart-loop a service 7x in 9 minutes while the
  real problem was a lock a restart cannot clear. So LIVENESS answers with the DB gone;
- an old `/healthz` returned HTTP 200 with `ok:false` on a dead DB -- `curl -fsS` and urlopen both
  read that as healthy. So READINESS speaks in STATUS CODES (503), not body prose;
- a blue/green deploy probed `/readyz` on a service that had none, and died on a 404 every time.

    add_health_routes(app, version=__version__, session_maker=lambda: get_session_maker())

`session_maker` is resolved per request (a test, or a re-built engine, is seen). The readiness
probe opens a session and runs `SELECT 1`; ANY exception -- the maker itself exploding included --
is a 503 naming the exception type. The message after the type is cut to `max_error_chars`, and a
DSN-shaped `scheme://user:password@` in it is masked: a connect error can carry its URL.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Response
from sqlalchemy import text

_CREDENTIALS = re.compile(r"(\w+://[^:/@\s]*):[^@\s]*@")


def describe_error(exc: BaseException, max_chars: int = 300) -> str:
    """`Type: message`, credentials in any URL masked, cut to `max_chars`."""
    return _CREDENTIALS.sub(r"\1:***@", f"{type(exc).__name__}: {exc}")[:max_chars]


def add_health_routes(
    app: FastAPI,
    *,
    version: str,
    session_maker: Callable[[], Callable[[], Any]],
    healthz: str = "/healthz",
    readyz: str = "/readyz",
    max_error_chars: int = 300,
) -> None:
    """Register GET `healthz` (liveness, no I/O) and GET `readyz` (a DB round-trip; 503 on failure)."""

    @app.get(healthz)
    async def _healthz() -> dict[str, object]:
        """LIVENESS: is this process able to serve a request. Never touches the database."""
        return {"ok": True, "version": version, "checks": "liveness"}

    @app.get(readyz)
    async def _readyz(response: Response) -> dict[str, object]:
        """READINESS: liveness plus a real DB round-trip. 503 when the DB is unreachable."""
        err: str | None = None
        try:
            async with session_maker()() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            err = describe_error(exc, max_error_chars)
            response.status_code = 503
        return {"ok": err is None, "version": version, "db_error": err}
