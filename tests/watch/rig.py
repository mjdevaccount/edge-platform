"""The watchdog runner's test rig: an in-memory state store with the SQL store's semantics, a page
recorder and a runner over a fresh registry (ported from trade-data tests/test_watchdog_runner.py)."""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from edge_platform.watch.runner import WatchRunner
from edge_platform.watch.spec import Probe, Status, WatchdogSpec, WatchRegistry, WatchVocab

ET = ZoneInfo("America/New_York")
VOCAB = WatchVocab(
    owners=frozenset({"service", "worker"}),
    watch_kinds=("producer", "daemon"),
    push_severity={"p2": "act_now", "p3": "heads_up"},
)


def now(h: int = 9, m: int = 30) -> dt.datetime:
    return dt.datetime(2026, 8, 11, h, m, tzinfo=ET)


def spec(sid: str, probe: Any, *, severity: str = "p2", market_gated: bool = False, **kw: Any) -> WatchdogSpec:
    return WatchdogSpec(id=sid, description="d" * 25, owner="service", severity=severity,
                        market_gated=market_gated, probe=probe, **kw)  # fmt: skip


def returning(status: Status, detail: str = "x", calls: list[str] | None = None, **metrics: Any):
    async def probe(session: Any) -> Probe:
        if calls is not None:
            calls.append(detail)
        return Probe(status, detail, metrics=metrics)

    return probe


class Store:
    """The state table in memory. record() mirrors the SQL: error/detail/metrics overwritten every
    time (None included), transition and page clocks move only when told, subjects cleared off a
    non-failing status -- so the double cannot pass a test the SQL would fail."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.contended: set[str] = set()
        self.claim_calls: list[list[str]] = []
        self.on_claim: Any = None

    def __call__(self, session: Any) -> Store:  # the runner's store factory
        return self

    async def load(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self.rows.items() if k in ids}

    async def claim(self, ids: list[str]) -> set[str]:
        self.claim_calls.append(list(ids))
        if self.on_claim:
            self.on_claim()  # e.g. a holder's commit landing as our lock is taken
        return self.contended & set(ids)

    async def reap_unregistered(self, *, keep: list[str]) -> list[str]:
        gone = [k for k in self.rows if k not in keep]
        for k in gone:
            del self.rows[k]
        return gone

    async def record(self, wid, *, status, transitioned, paged, now, error=None, detail=None, metrics=None):
        row = self.rows.setdefault(wid, {"last_status": None, "last_transition_at": now, "last_page_at": None})
        row["last_status"] = status
        row["last_error"] = error
        row["last_detail"], row["last_metrics"] = detail, metrics
        row["updated_at"] = now
        if status not in ("warn", "down"):
            row["last_subjects"] = None
        if transitioned:
            row["last_transition_at"] = now
        if paged:
            row["last_page_at"] = now

    async def record_subjects(self, wid: str, subjects: list[str]) -> None:
        self.rows[wid]["last_subjects"] = list(subjects) or None


class Rig:
    def __init__(self, **runner_kw: Any) -> None:
        self.store = Store()
        self.pages: list[str] = []
        self.degraded: list[str] = []
        self.session_day = True
        self.push_ok = True
        self.registry = WatchRegistry(VOCAB)

        async def page(s: WatchdogSpec, probe: Probe) -> bool:
            if self.push_ok:
                self.pages.append(s.id)
            return self.push_ok

        kw: dict[str, Any] = {
            "store": self.store, "session_day": lambda _now: self.session_day, "page": page,
            "now": now, "on_degraded": self.degraded.append,
        }  # fmt: skip
        kw.update(runner_kw)
        self.runner = WatchRunner(self.registry, **kw)

    def register(self, s: WatchdogSpec) -> WatchdogSpec:
        return self.registry.register(s)
