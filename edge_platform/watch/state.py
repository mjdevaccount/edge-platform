"""WatchdogStateStore: the only SQL that touches the watchdog state table (extra `[sql]`).

Lifted from trade-data `service/repositories/watchdog_state_repo.py` (M6 PL5). The durable page-dedup
memory behind `WatchRunner`; it replaced two /tmp JSON files whose deploy-amnesia re-paged every
standing failure after every container recreate. The row holds the whole verdict (detail + metrics),
so a READ serves it without probing.

THE TABLE IS THE CONSUMER'S: its schema-qualified name is passed in (validated -- it is interpolated)
and `ddl(table)` returns the shape this store expects, for a consumer's migration or a test. Postgres
only (`FOR UPDATE SKIP LOCKED`, `text[]`, `jsonb`); the tests run it against a real Postgres.

EVERY bind is CAST explicitly. The first version bound :now bare in three positions, one inside a
CASE arm (no column context to infer a type from), and asyncpg refused the statement on EVERY call
(`AmbiguousParameterError: inconsistent types deduced for parameter $3`) -- which every unit test
missed, because the store was faked in all of them.

Left with the consumer: queries that join the state to its OWN tables (trade-data's
`uncovered_failures` reads its job queue) -- subclass this store and add them.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from sqlalchemy import text

from edge_platform.sqlkit import json_text, qualified


def ddl(table: str) -> str:
    """CREATE TABLE IF NOT EXISTS for the state table this store reads and writes."""
    t = qualified(table, "watchdog state table")
    return f"""
CREATE TABLE IF NOT EXISTS {t} (
    watchdog_id        text        PRIMARY KEY,
    last_status        text        NOT NULL,
    last_transition_at timestamptz NOT NULL DEFAULT now(),
    last_page_at       timestamptz,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    last_error         text,
    last_subjects      text[],
    last_detail        text,
    last_metrics       jsonb
)"""


class WatchdogStateStore:
    def __init__(self, session: Any, table: str, *, encode_json: Callable[[Any], str | None] = json_text) -> None:
        self._s = session
        self.table = qualified(table, "watchdog state table")
        self._json = encode_json

    @classmethod
    def bind(cls, table: str, **kw: Any) -> Callable[[Any], WatchdogStateStore]:
        """session -> store, the factory `WatchRunner(store=...)` takes. Validates `table` now."""
        qualified(table, "watchdog state table")
        return lambda session: cls(session, table, **kw)

    async def load(self, watchdog_ids: list[str]) -> dict[str, dict[str, Any]]:
        """State rows keyed by watchdog_id; a never-evaluated watchdog is simply absent."""
        if not watchdog_ids:
            return {}
        res = await self._s.execute(
            text(f"""
                SELECT watchdog_id, last_status, last_transition_at, last_page_at, updated_at,
                       last_error, last_subjects, last_detail, last_metrics
                  FROM {self.table}
                 WHERE watchdog_id = ANY(CAST(:ids AS text[]))
            """),
            {"ids": list(watchdog_ids)},
        )
        return {r["watchdog_id"]: dict(r) for r in (dict(row) for row in res.mappings())}

    async def claim(self, watchdog_ids: list[str]) -> set[str]:
        """Row-lock these rows until the caller commits; return the ids ANOTHER sweep holds.

        TWO SWEEPS, ONE ROW: a full sweep and a dedicated subset cron evaluated the same specs, and
        the second one's upsert waited out lock_timeout (a 500). SKIP LOCKED makes the overlap a
        decision instead of a wait: a contended row is being evaluated right now by its holder. A
        row that does not exist yet is not contended."""
        if not watchdog_ids:
            return set()
        params = {"ids": list(watchdog_ids)}
        locked = await self._s.execute(
            text(f"""
                SELECT watchdog_id FROM {self.table}
                 WHERE watchdog_id = ANY(CAST(:ids AS text[]))
                   FOR UPDATE SKIP LOCKED
            """),
            params,
        )
        existing = await self._s.execute(
            text(f"SELECT watchdog_id FROM {self.table} WHERE watchdog_id = ANY(CAST(:ids AS text[]))"), params
        )
        return {str(r[0]) for r in existing.fetchall()} - {str(r[0]) for r in locked.fetchall()}

    async def reap_unregistered(self, *, keep: list[str]) -> list[str]:
        """Delete rows whose watchdog is no longer registered; returns the ids removed (a retired
        spec's `down` row otherwise leads every state query for good)."""
        res = await self._s.execute(
            text(f"""
                DELETE FROM {self.table}
                 WHERE NOT (watchdog_id = ANY(CAST(:keep AS text[])))
                RETURNING watchdog_id
            """),
            {"keep": list(keep)},
        )
        return [str(r[0]) for r in res.fetchall()]

    async def record(
        self,
        watchdog_id: str,
        *,
        status: str,
        transitioned: bool,
        paged: bool,
        now: dt.datetime,
        error: str | None = None,
        detail: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        """Record one evaluation. `last_transition_at` moves only on a transition; `last_page_at`
        only when a page was actually pushed. error/detail/metrics are OVERWRITTEN every time, NULL
        included: a recovered probe must clear the message, and the stored verdict is THIS
        evaluation's, whole. Subjects do not outlive the verdict: a row leaving warn/down forgets
        them, so the next episode's subjects all read as new."""
        if not isinstance(now, dt.datetime):
            raise TypeError(f"record(now=) requires a datetime, got {type(now).__name__}")
        t = self.table
        await self._s.execute(
            text(f"""
                INSERT INTO {t}
                       (watchdog_id, last_status, last_transition_at, last_page_at, updated_at,
                        last_error, last_detail, last_metrics)
                VALUES (CAST(:id AS text), CAST(:st AS text), CAST(:now AS timestamptz),
                        CASE WHEN CAST(:paged AS boolean) THEN CAST(:now AS timestamptz) END,
                        CAST(:now AS timestamptz),
                        CAST(:err AS text), CAST(:detail AS text), CAST(:metrics AS jsonb))
                ON CONFLICT (watchdog_id) DO UPDATE
                   SET last_status        = EXCLUDED.last_status,
                       last_transition_at = CASE WHEN CAST(:trans AS boolean)
                                                 THEN EXCLUDED.last_transition_at
                                                 ELSE {t}.last_transition_at END,
                       last_page_at       = CASE WHEN CAST(:paged AS boolean)
                                                 THEN EXCLUDED.last_page_at
                                                 ELSE {t}.last_page_at END,
                       updated_at         = EXCLUDED.updated_at,
                       last_error         = EXCLUDED.last_error,
                       last_detail        = EXCLUDED.last_detail,
                       last_metrics       = EXCLUDED.last_metrics,
                       last_subjects      = CASE WHEN CAST(:st AS text) IN ('warn', 'down')
                                                 THEN {t}.last_subjects END
            """),
            {
                "id": watchdog_id,
                "st": status,
                "trans": bool(transitioned),
                "paged": bool(paged),
                "now": now,
                "err": (error or None),
                "detail": detail,
                "metrics": self._json(metrics),
            },
        )

    async def record_subjects(self, watchdog_id: str, subjects: list[str]) -> None:
        """Store the failing subjects the investigate hook has dealt with. Runs after record() in the
        same sweep, so the row exists. An empty list is stored as NULL: "none" and "never declared"
        both mean every next subject is new."""
        await self._s.execute(
            text(
                f"UPDATE {self.table} SET last_subjects = CAST(:subjects AS text[]) WHERE watchdog_id = CAST(:id AS text)"
            ),
            {"id": watchdog_id, "subjects": list(subjects) or None},
        )
