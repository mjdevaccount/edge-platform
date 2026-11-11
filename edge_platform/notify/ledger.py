"""SqlLedger -- the notify pipeline's durable ledger: dedup claims, budget windows, deliveries.

Extra `[sql]`. Lifted from trade-data `service/platform/notify.py` (SqlLedger) and
`service/repositories/notify_repo.py` (M6 PL6). THE TABLES ARE THE CONSUMER'S: `NotifyTables(claims,
budget, deliveries)` names them (each validated -- they are interpolated) and `ddl(tables)` returns
the shapes this ledger expects, for a consumer's migration or a test. Postgres only.

`admit` runs claim + spend in ONE transaction and rolls it back on a refusal, so a notice refused by
its budget leaves no claim behind (the key can pass once the window turns). Each call opens its OWN
short session from the injected session maker: the caller's transaction never carries a ledger write,
so its rollback cannot erase one.

`outcome_series(rows)` shapes `NotifyStore.outcomes_24h()` into the (labels, value) series an outside
watcher alerts on (trade-data renders them as `edge_notify_outcomes_24h{scope,outcome}`): a total
per outcome under `scope=_all` ALWAYS present, every scope seen in 7 days filled with zeros, and
BLIND IS NOT ZERO -- an unreadable ledger is None for every total (renders NaN, never "nothing
failed").
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from edge_platform.sqlkit import qualified

from .policy import TIERS
from .types import RECORDED, Notice, Outcome, window_key

ALL_SCOPES = "_all"

log = logging.getLogger("edge_platform.notify.ledger")


@dataclass(frozen=True)
class NotifyTables:
    claims: str
    budget: str
    deliveries: str

    def __post_init__(self) -> None:
        for what in ("claims", "budget", "deliveries"):
            qualified(getattr(self, what), f"notify {what} table")


def ddl(tables: NotifyTables) -> list[str]:
    """CREATE ... IF NOT EXISTS statements for the three ledger tables (and the deliveries indexes)."""
    c, b, d = tables.claims, tables.budget, tables.deliveries
    d_name = d.split(".", 1)[1]
    tiers = ", ".join(f"'{t}'" for t in TIERS)
    outcomes = ", ".join(f"'{o}'" for o in RECORDED)
    return [
        f"""CREATE TABLE IF NOT EXISTS {c} (
    scope text NOT NULL, key text NOT NULL,
    first_at timestamptz NOT NULL DEFAULT now(), last_at timestamptz NOT NULL DEFAULT now(),
    n_claimed integer NOT NULL DEFAULT 1 CHECK (n_claimed >= 1),
    n_suppressed integer NOT NULL DEFAULT 0 CHECK (n_suppressed >= 0),
    PRIMARY KEY (scope, key))""",
        f"""CREATE TABLE IF NOT EXISTS {b} (
    scope text NOT NULL, window_key text NOT NULL,
    cap integer NOT NULL CHECK (cap >= 0),
    spent integer NOT NULL DEFAULT 0 CHECK (spent >= 0),
    refused integer NOT NULL DEFAULT 0 CHECK (refused >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, window_key))""",
        f"""CREATE TABLE IF NOT EXISTS {d} (
    id bigserial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(),
    scope text NOT NULL, key text NOT NULL,
    tier text NOT NULL CHECK (tier IN ({tiers})),
    outcome text NOT NULL CHECK (outcome IN ({outcomes})),
    delivered boolean NOT NULL, title text NOT NULL, detail text)""",
        f"CREATE INDEX IF NOT EXISTS {d_name}_at_idx ON {d} (at)",
        f"CREATE INDEX IF NOT EXISTS {d_name}_scope_at_idx ON {d} (scope, at)",
    ]


class NotifyStore:
    """Every statement the ledger runs, one plain literal each with named binds."""

    def __init__(self, session: Any, tables: NotifyTables) -> None:
        self._s = session
        c, b, d = tables.claims, tables.budget, tables.deliveries
        # Claim (scope, key). A first claim inserts. A repeat passes only for a WINDOWED policy whose
        # last passing claim is older than the window; window_s NULL = once-ever (never re-passes).
        self._claim = f"""
            INSERT INTO {c} (scope, key) VALUES (CAST(:scope AS text), CAST(:key AS text))
            ON CONFLICT (scope, key) DO UPDATE
               SET last_at = now(), n_claimed = {c}.n_claimed + 1
             WHERE CAST(:window_s AS float8) IS NOT NULL
               AND {c}.last_at <= now() - make_interval(secs => CAST(:window_s AS float8))
            RETURNING n_claimed"""
        self._suppress = f"""
            UPDATE {c} SET n_suppressed = n_suppressed + 1
             WHERE scope = CAST(:scope AS text) AND key = CAST(:key AS text)"""
        # Spend one unit of the scope's window. No row back = the window is spent (or the cap is 0).
        self._spend = f"""
            INSERT INTO {b} (scope, window_key, cap, spent)
            SELECT CAST(:scope AS text), CAST(:window_key AS text), CAST(:cap AS integer), 1
             WHERE CAST(:cap AS integer) > 0
            ON CONFLICT (scope, window_key) DO UPDATE
               SET spent = {b}.spent + 1, cap = EXCLUDED.cap, updated_at = now()
             WHERE {b}.spent < EXCLUDED.cap
            RETURNING spent"""
        self._refuse = f"""
            INSERT INTO {b} (scope, window_key, cap, refused)
            VALUES (CAST(:scope AS text), CAST(:window_key AS text), CAST(:cap AS integer), 1)
            ON CONFLICT (scope, window_key) DO UPDATE
               SET refused = {b}.refused + 1, updated_at = now()"""
        self._record = f"""
            INSERT INTO {d} (scope, key, tier, outcome, delivered, title, detail)
            VALUES (CAST(:scope AS text), CAST(:key AS text), CAST(:tier AS text), CAST(:outcome AS text),
                    CAST(:delivered AS boolean), CAST(:title AS text), CAST(:detail AS text))"""
        # Per (scope, outcome): rows in the last 24h, for every pair seen in the last 7 days -- a scope
        # that went quiet still renders 0 for a week instead of vanishing from the scrape.
        self._outcomes_24h = f"""
            SELECT scope, outcome, count(*) FILTER (WHERE at >= now() - interval '24 hours') AS n
              FROM {d}
             WHERE at >= now() - interval '7 days'
             GROUP BY scope, outcome"""

    async def claim(self, *, scope: str, key: str, window_s: float | None) -> bool:
        """True = this notice holds the claim; False = a duplicate under the policy."""
        res = await self._s.execute(text(self._claim), {"scope": scope, "key": key, "window_s": window_s})
        return res.first() is not None

    async def suppress(self, *, scope: str, key: str) -> None:
        await self._s.execute(text(self._suppress), {"scope": scope, "key": key})

    async def spend(self, *, scope: str, window_key: str, cap: int) -> bool:
        """True = admitted inside the window's cap; False = the window is spent."""
        res = await self._s.execute(text(self._spend), {"scope": scope, "window_key": window_key, "cap": int(cap)})
        return res.first() is not None

    async def refuse(self, *, scope: str, window_key: str, cap: int) -> None:
        await self._s.execute(text(self._refuse), {"scope": scope, "window_key": window_key, "cap": int(cap)})

    async def record(
        self, *, scope: str, key: str, tier: str, outcome: str, delivered: bool, title: str, detail: str | None
    ) -> None:
        await self._s.execute(text(self._record), {
            "scope": scope, "key": key, "tier": tier, "outcome": outcome, "delivered": bool(delivered),
            "title": title, "detail": detail,
        })  # fmt: skip

    async def outcomes_24h(self) -> list[dict[str, Any]]:
        res = await self._s.execute(text(self._outcomes_24h))
        return [dict(r._mapping) for r in res]


class SqlLedger:
    """The production Ledger. `sessions()` returns an async-context session (an `async_sessionmaker`
    is exactly that); it is called once per admit/record."""

    def __init__(self, sessions: Callable[[], AbstractAsyncContextManager[Any]], tables: NotifyTables) -> None:
        self._sessions = sessions
        self.tables = tables

    async def admit(self, notice: Notice, now: dt.datetime) -> tuple[Outcome | None, str | None]:
        ch = notice.channel
        if not ch.dedup.claims and ch.budget is None:
            return None, None
        async with self._sessions() as s:
            store = NotifyStore(s, self.tables)
            if ch.dedup.claims and not await store.claim(scope=ch.scope, key=notice.key, window_s=ch.dedup.window_s):
                await s.rollback()
                await store.suppress(scope=ch.scope, key=notice.key)
                await s.commit()
                return Outcome.DEDUPED, None
            if ch.budget is not None:
                wk = window_key(ch.budget, now)
                if not await store.spend(scope=ch.scope, window_key=wk, cap=ch.budget.cap):
                    await s.rollback()  # the claim goes with it: refused is not "sent"
                    await store.refuse(scope=ch.scope, window_key=wk, cap=ch.budget.cap)
                    await s.commit()
                    return Outcome.BUDGET_REFUSED, wk
            await s.commit()
        return None, None

    async def record(self, notice: Notice, *, tier: str, outcome: Outcome, delivered: bool, detail: str | None) -> None:
        async with self._sessions() as s:
            await NotifyStore(s, self.tables).record(
                scope=notice.channel.scope, key=notice.key, tier=tier, outcome=outcome.value,
                delivered=delivered, title=notice.title[:500], detail=detail,
            )  # fmt: skip
            await s.commit()

    async def outcomes_24h(self) -> list[dict[str, Any]] | None:
        """The rows `outcome_series` shapes; None when the ledger cannot be read (blind, not zero)."""
        try:
            async with self._sessions() as s:
                return await NotifyStore(s, self.tables).outcomes_24h()
        except Exception as exc:
            log.warning("notify_ledger_unreadable err=%s", f"{type(exc).__name__}: {exc}"[:200])
            return None


def outcome_series(
    rows: list[dict[str, Any]] | None, *, all_scope: str = ALL_SCOPES
) -> list[tuple[dict[str, str], float | None]]:
    """Rows of (scope, outcome, n) -> [(labels, value)]. None = the ledger could not be read."""
    if rows is None:
        return [({"scope": all_scope, "outcome": o}, None) for o in RECORDED]
    counts: dict[tuple[str, str], int] = {}
    for r in rows:
        counts[(str(r["scope"]), str(r["outcome"]))] = int(r["n"] or 0)
    scopes = sorted({s for s, _ in counts})
    out: list[tuple[dict[str, str], float | None]] = [
        ({"scope": all_scope, "outcome": oc}, float(sum(n for (_, o), n in counts.items() if o == oc)))
        for oc in RECORDED
    ]
    out += [({"scope": s, "outcome": oc}, float(counts.get((s, oc), 0))) for s in scopes for oc in RECORDED]
    return out
