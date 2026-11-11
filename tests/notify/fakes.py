"""In-memory doubles of the notify pipeline's two ports (ported from trade-data tests/_notify_fakes.py).

`MemoryLedger` IS the library's LocalLedger plus two failure switches and a row list, so it has the
SAME dedup / budget semantics as SqlLedger -- `ledger_contract.py` holds both to one suite (unit:
this double; tests/notify/test_ledger_pg.py: SqlLedger on a real Postgres)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from edge_platform.notify.local import LocalLedger
from edge_platform.notify.types import Notice, Outcome


@dataclass
class MemoryLedger(LocalLedger):
    rows: list[dict[str, Any]] = field(default_factory=list)
    fail_admit: bool = False
    fail_record: bool = False

    async def admit(self, notice: Notice, now: dt.datetime) -> tuple[Outcome | None, str | None]:
        if self.fail_admit:
            raise ConnectionError("ledger down")
        return await super().admit(notice, now)

    async def record(self, notice: Notice, *, tier: str, outcome: Outcome, delivered: bool, detail: str | None) -> None:
        if self.fail_record:
            raise ConnectionError("ledger down")
        self.rows.append({"scope": notice.channel.scope, "key": notice.key, "tier": tier, "outcome": outcome.value,
                          "delivered": delivered, "title": notice.title, "detail": detail})  # fmt: skip


@dataclass
class RecordingTransport:
    outcome: Outcome = Outcome.SENT
    detail: str | None = None
    is_disabled: bool = False
    raises: bool = False
    sent: list[dict[str, Any]] = field(default_factory=list)

    def disabled(self) -> bool:
        return self.is_disabled

    async def publish(self, **kw: Any) -> tuple[Outcome, str | None]:
        if self.raises:
            raise RuntimeError("transport broke")
        self.sent.append(kw)
        return self.outcome, self.detail
