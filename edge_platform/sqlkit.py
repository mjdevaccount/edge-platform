"""Three SQL helpers every SQL-touching tenant shares. Stdlib only: importing this needs no extra.

    qualified(name, what)   a consumer-supplied `schema.table`, validated because it is INTERPOLATED
                            into SQL (a bind cannot name a table). Since v0.6.0 the library names no
                            table; every tenant that writes one takes its name from the consumer.
    savepoint(session)      one SAVEPOINT per member of a fan-out over a SHARED session.
    json_text(value)        a python value as the text a `CAST(:x AS jsonb)` bind takes.

`savepoint` moved here from `edge_platform.service.db` (v0.10.0) so the watchdog runner, which is
stdlib, can use the ONE copy; `edge_platform.service.db.savepoint` re-exports it unchanged.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import math
import re
import uuid
from typing import Any

_QUALIFIED = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")


def qualified(name: str, what: str = "table") -> str:
    """`name` when it is a lower-case `schema.table`, else ValueError naming `what`."""
    if not isinstance(name, str) or not _QUALIFIED.match(name):
        raise ValueError(f"{what} {name!r} must be a lower-case schema.table")
    return name


class _NoSavepoint:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


def savepoint(session: Any) -> Any:
    """A SAVEPOINT on a SHARED session (SQLAlchemy `begin_nested`), so one caller's failed SQL rolls
    back to the mark and the callers after it keep a live transaction.

        for probe in probes:
            try:
                async with savepoint(session):
                    await probe(session)
            except Exception: ...

    THE RULE IT ENFORCES: catching a SQL error does NOT un-abort a Postgres transaction. Every later
    statement on that connection then fails with InFailedSQLTransactionError -- including
    SQLAlchemy's own `SAVEPOINT sa_savepoint_N`, which is why the recovery has to be opened BEFORE
    the failure, not after it. Any fan-out over one session needs this around each member.

    A session without `begin_nested` gets a no-op, for test sentinels that stand in for a session
    without being one. That fallback is deliberately more permissive than Postgres, so a containment
    test must hand in a fake that HAS `begin_nested` and refuses it on an aborted transaction, the
    way the database does."""
    nested = getattr(session, "begin_nested", None)
    return nested() if nested is not None else _NoSavepoint()


def _plain(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None  # jsonb rejects NaN/inf
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, set | frozenset):
        return [_plain(v) for v in sorted(value, key=str)]
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def json_text(value: Any) -> str | None:
    """`value` as JSON text for a jsonb bind; None stays SQL NULL. Never raises on a type it does
    not know (`str` of it): an audit write must never be the thing that fails. NaN/inf -> null,
    because jsonb refuses them; datetimes ISO-8601; sets sorted lists."""
    if value is None:
        return None
    return json.dumps(_plain(value), default=str, allow_nan=False)
