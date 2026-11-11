"""ONE PROBE'S SQL ERROR MUST NOT MUTE ITS SIBLINGS (ported from trade-data
tests/test_watchdog_runner_savepoint.py).

The probes share the sweep's session. A probe whose SQL errored left the transaction ABORTED, so the
very next state write raised InFailedSQLTransactionError and the whole sweep persisted nothing and
paged nothing for 16 hours. The runner wraps each probe -- and the investigate hook, the same bug one
function over (36 hours) -- in a SAVEPOINT. This fake session models the aborted-transaction
semantics, so removing either savepoint turns these red."""

from __future__ import annotations

import asyncio
from typing import Any

from edge_platform.watch.runner import WatchRunner
from edge_platform.watch.spec import Probe, Status, WatchRegistry

from .rig import VOCAB, now, spec


class _Aborted(RuntimeError):
    """What asyncpg raises on the next statement after an error."""


class _Savepoint:
    def __init__(self, sess: _Sess) -> None:
        self.sess = sess

    async def __aenter__(self) -> _Savepoint:
        if self.sess.aborted:
            raise _Aborted("current transaction is aborted, commands ignored until end of transaction block")
        self.sess.savepoints += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.sess.aborted = False  # ROLLBACK TO SAVEPOINT: the transaction is live again
        return False


class _Sess:
    def __init__(self) -> None:
        self.aborted = False
        self.savepoints = 0
        self.records: list[str] = []

    def begin_nested(self) -> _Savepoint:
        return _Savepoint(self)

    def fail_statement(self) -> None:
        self.aborted = True
        raise RuntimeError("operator is not unique: date + unknown")

    def statement(self) -> None:
        if self.aborted:
            raise _Aborted("current transaction is aborted, commands ignored until end of transaction block")


class _StatementStore:
    """Every record() is a statement, so a poisoned transaction raises where the SQL store would."""

    def __init__(self, s: _Sess) -> None:
        self.s = s

    async def claim(self, ids):
        return set()

    async def reap_unregistered(self, *, keep):
        return []

    async def load(self, ids):
        return {}

    async def record(self, wid, *, status, transitioned, paged, now, error=None, detail=None, metrics=None):
        self.s.statement()
        self.s.records.append(f"{wid}:{status}")

    async def record_subjects(self, wid, subjects):
        self.s.statement()


def _runner(**kw: Any) -> tuple[WatchRunner, WatchRegistry]:
    async def page(s, probe):
        return True

    reg = WatchRegistry(VOCAB)
    return WatchRunner(reg, store=_StatementStore, session_day=lambda n: True, page=page, **kw), reg


def test_a_raising_probe_leaves_the_session_writable_for_the_next_record() -> None:
    sess = _Sess()
    runner, reg = _runner()

    async def poisoned(session):
        session.fail_statement()

    async def healthy(session):
        session.statement()
        return Probe(Status.DOWN, "sibling is really down")

    reg.register(spec("a-poisoned", poisoned, severity="p3"))
    reg.register(spec("b-sibling", healthy))
    res = asyncio.run(runner.run(sess, now=now()))
    by_id = {r["id"]: r for r in res["results"]}
    assert by_id["a-poisoned"]["status"] == "unknown" and by_id["b-sibling"]["status"] == "down"
    assert res["paged"] == ["b-sibling"], "the sibling's page must still go out"
    assert sess.records == ["a-poisoned:unknown", "b-sibling:down"]
    assert sess.savepoints == 2


def test_a_raising_investigate_hook_leaves_the_session_writable() -> None:
    """TWO specs, and the second is the whole test: the poisoning spec has already recorded its own
    verdict when the hook breaks the transaction; the victim is the NEXT spec's record()."""
    sess = _Sess()

    async def investigate(session, wid, probe, at, **kw):
        if probe.status is Status.DOWN:
            session.fail_statement()  # the uncast-bind 42883, inside the hook
        return None, []

    degraded: list[str] = []
    runner, reg = _runner(investigate=investigate, on_degraded=degraded.append)

    async def down(session):
        session.statement()
        return Probe(Status.DOWN, "really down")

    async def after(session):
        return Probe(Status.OK, "fine")

    reg.register(spec("c-down", down))
    reg.register(spec("d-after", after, severity="p3"))
    res = asyncio.run(runner.run(sess, now=now()))
    assert sess.records == ["c-down:down", "d-after:ok"], f"the hook poisoned the next verdict: {sess.records}"
    assert res["paged"] == ["c-down"] and degraded == ["watchdog_investigate_failed"]


def test_a_session_without_savepoints_still_runs() -> None:
    class _Plain:
        def __init__(self, s):
            pass

        async def claim(self, ids):
            return set()

        async def reap_unregistered(self, *, keep):
            return []

        async def load(self, ids):
            return {}

        async def record(self, *a, **k):
            return None

        async def record_subjects(self, *a, **k):
            return None

    reg = WatchRegistry(VOCAB)

    async def page(s, p):
        return True

    runner = WatchRunner(reg, store=_Plain, session_day=lambda n: True, page=page)

    async def ok(session):
        return Probe(Status.OK, "fine")

    reg.register(spec("c-ok", ok, severity="p3"))
    assert asyncio.run(runner.run(object(), now=now()))["results"][0]["status"] == "ok"
