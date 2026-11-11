"""LocalLedger -- the notify ledger's laws held in process memory, for when the SQL ledger is down.

Lifted from trade-data `service/platform/notify_local.py` (M6 PL6). The pipeline fails OPEN when its
ledger is unreachable: a lost page is worse than a noisy one. For a scope fed by an UNAUTHENTICATED
endpoint that is wrong -- an undeduplicated storm spends the push service's per-IP quota every other
sender shares (four days of a dark paging plane, once). A channel declaring
`on_ledger_down="local"` gets its own Dedup + Budget enforced by THIS object during the outage, so
the fallback cannot drift from the declared policy.

Same semantics as `SqlLedger` -- once / window / budget, the claim rolled back on a refusal -- held to
one contract suite (tests/notify/ledger_contract.py). What it cannot do is survive a restart.

Bounded: an unauthenticated feed can invent keys, so past `max_keys` the oldest claims are evicted (a
`once` key can then repeat -- in fallback mode only, and still under the budget).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .types import Notice, Outcome, window_key

__all__ = ["LocalLedger"]


@dataclass
class LocalLedger:
    claims: dict[tuple[str, str], dt.datetime] = field(default_factory=dict)
    suppressed: dict[tuple[str, str], int] = field(default_factory=dict)
    spent: dict[tuple[str, str], int] = field(default_factory=dict)
    refused: dict[tuple[str, str], int] = field(default_factory=dict)
    max_keys: int = 4096

    async def admit(self, notice: Notice, now: dt.datetime) -> tuple[Outcome | None, str | None]:
        ch = notice.channel
        ck = (ch.scope, notice.key)
        claimed_prev: dt.datetime | bool | None = False  # False = no claim taken
        if ch.dedup.claims:
            prev = self.claims.get(ck)
            if prev is not None and (ch.dedup.window_s is None or (now - prev).total_seconds() < ch.dedup.window_s):
                self.suppressed[ck] = self.suppressed.get(ck, 0) + 1
                return Outcome.DEDUPED, None
            claimed_prev = prev
            self.claims[ck] = now
        if ch.budget is not None:
            wk = window_key(ch.budget, now)
            bk = (ch.scope, wk)
            if self.spent.get(bk, 0) >= ch.budget.cap:
                if claimed_prev is not False:  # roll the claim back
                    if claimed_prev is None:
                        self.claims.pop(ck, None)
                    else:
                        self.claims[ck] = claimed_prev
                self.refused[bk] = self.refused.get(bk, 0) + 1
                return Outcome.BUDGET_REFUSED, wk
            self.spent[bk] = self.spent.get(bk, 0) + 1
        self._prune()
        return None, None

    async def record(self, notice: Notice, *, tier: str, outcome: Outcome, delivered: bool, detail: str | None) -> None:
        """Nothing to keep: a fallback delivery is reported as Delivery.recorded=False."""

    def _prune(self) -> None:
        half = self.max_keys // 2
        if len(self.claims) > self.max_keys:  # keep the newest half of the claims
            self.claims = dict(sorted(self.claims.items(), key=lambda kv: kv[1])[-half:])
        if len(self.suppressed) > self.max_keys:
            self.suppressed = {k: n for k, n in self.suppressed.items() if k in self.claims}
        for d in (self.spent, self.refused):
            if len(d) > self.max_keys:  # budget windows only move forward: keep the newest
                kept = dict(list(d.items())[-half:])
                d.clear()
                d.update(kept)
