"""WatchRunner: evaluate registered watchdogs with ONE market gate, ONE dedup, ONE pager. Stdlib.

Lifted from trade-data `service/usecases/watchdog_runner.py` (M6 PL5). For each registered spec:

  1. MARKET GATE: a market_gated spec is skipped when `session_day(now)` says no session. The gate
     is the CONSUMER's callable (an exchange calendar lives with the consumer, never here). A gate
     that RAISES is a plane that cannot fire, so the runner logs it, reports `on_degraded(
     "watchdog_market_gate_failed")` and evaluates as a session day: a gated probe run on a holiday
     is noise, a plane that never runs is silence.
  2. PROBE: the spec's coroutine, inside a SAVEPOINT on the shared session -- one probe's SQL error
     must not abort the transaction its siblings and the state writes share (17 sweeps persisted
     nothing for 16 hours before this). A raising probe becomes UNKNOWN, its exception text is
     PERSISTED (`last_error`), and it never pages as if the watched thing were down.
  3. DEDUP (transition-only + re-alert, DB-backed): page when the status ENTERS the pageable set,
     or every `realert_h` while it stays there. The memory is the state store (`watch.state`), so
     it survives deploys -- the /tmp version re-paged every standing failure after every restart.
     The state records the PUSH, not the intent: a failed push leaves `last_page_at` alone and the
     next sweep retries.
  4. PAGE through the injected `page(spec, probe) -> bool`. It never raises out of the runner: an
     exception is `on_degraded("watchdog_page_failed")` and an unpaged row. `notify=False` evaluates
     but records, claims, pages and investigates nothing -- A READ MUST NOT CONSUME THE TRANSITION
     (an observer once silenced three alarms by advancing their state on a dashboard load).
  5. INVESTIGATE (optional hook): a transition into WARN/DOWN, or each declared subject entering the
     failing set, may open an investigation. The hook runs in its own savepoint and fails OPEN: a
     raising hook is `on_degraded("watchdog_investigate_failed")`, never a dead sweep.
  6. SERVE (`read_stored`): the registry with each STORED verdict -- one SELECT, no probe. A row
     older than `stale_after_s` is flagged `stale`: a stored read otherwise keeps serving the last
     verdicts as current after the sweep itself stops.

TELEMETRY (every signal the trade-data runner emitted survives as an injected hook or a stdlib log):
`sweep_scope()` wraps each sweep (trade-data: its work-timeline unit "watchdog_sweep");
`on_degraded(reason)` gets `watchdog_page_failed` / `watchdog_investigate_failed` /
`watchdog_market_gate_failed`; `metrics["probe_ms"]` per probe; `last_error` persisted; log events
`watchdog_state_reaped`, `watchdog_contended_skip`, `watchdog_verdicts_stale` (warning),
`watchdog probe failed`, `watchdog page failed`.

The caller owns the session/commit boundary.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from edge_platform.sqlkit import savepoint

from .spec import FAILING, PAGEABLE, Probe, Status, WatchdogSpec, WatchRegistry

log = logging.getLogger("edge_platform.watch.runner")

#: Re-page cadence while a watchdog STAYS down (hours): between "48 unthrottled pushes a day" and
#: "paged once at 09:25 and never again while it stayed red for a week".
REALERT_H = 6.0
#: A stored verdict older than this is STALE. The default fits an HOURLY sweep: two missed firings,
#: plus 15 minutes so one missed firing (age exactly 2h while the next sweep runs) is not two.
#: Set it from your own sweep cadence.
STALE_AFTER_S = 2 * 3600 + 15 * 60
#: The off-session skip's detail: ONE string, so the stored row, the live sweep and the stored read
#: cannot word the same verdict three ways.
OFF_SESSION = "skipped — not a trading session"


class StateStore(Protocol):
    """The durable dedup memory (`watch.state.WatchdogStateStore` is the SQL one)."""

    async def load(self, watchdog_ids: list[str]) -> dict[str, dict[str, Any]]: ...

    async def claim(self, watchdog_ids: list[str]) -> set[str]: ...

    async def reap_unregistered(self, *, keep: list[str]) -> list[str]: ...

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
    ) -> None: ...

    async def record_subjects(self, watchdog_id: str, subjects: list[str]) -> None: ...


Pager = Callable[[WatchdogSpec, Probe], Awaitable[bool]]
#: investigate(session, watchdog_id, probe, now, *, prior_subjects, notify, transitioned)
#:   -> (investigation note or None, the failing subjects now dealt with)
Investigate = Callable[..., Awaitable[tuple[Any, list[str]]]]


def should_page(
    state_row: dict[str, Any] | None, status: Status, now: dt.datetime, *, realert_h: float = REALERT_H
) -> tuple[bool, bool]:
    """(page, transitioned) for one evaluation against the stored state.

    Page on the transition INTO the pageable set, or after realert_h while still in it. A
    never-evaluated watchdog (no row) that probes DOWN pages immediately -- first evidence of a
    standing failure is a transition from the operator's view."""
    prev = (state_row or {}).get("last_status")
    transitioned = prev != status.value
    if status not in PAGEABLE:
        return False, transitioned
    if transitioned:
        return True, True
    last_page = (state_row or {}).get("last_page_at")
    if last_page is None:
        return True, False
    elapsed_h = (now - last_page).total_seconds() / 3600.0
    return elapsed_h >= realert_h, False


def _iso(v: Any) -> str | None:
    """A timestamp, or None when never evaluated -- "never run" and "ran at the epoch" must differ."""
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


def _midnight(now: dt.datetime) -> dt.datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _skipped_row(spec: WatchdogSpec, prior: dict[str, Any], status: str, detail: str) -> dict[str, Any]:
    """A spec this sweep did not probe. SAME KEY SET as an evaluated row: a slim payload renders a
    blank cell on the row hardest to read (is it off? is it fine?)."""
    return {
        "id": spec.id, "status": status, "detail": detail, "skipped": True, "metrics": {},
        "severity": spec.severity, "owner": spec.owner, "description": spec.description,
        "observe_only": spec.observe_only, "would_page": False, "paged": False, "error": None,
        "investigation": None, "last_evaluated_at": _iso(prior.get("updated_at")),
        "last_page_at": _iso(prior.get("last_page_at")),
        "last_transition_at": _iso(prior.get("last_transition_at")),
    }  # fmt: skip


class WatchRunner:
    """One consumer's watchdog plane. Every estate fact is an argument:

    registry          the consumer's WatchRegistry
    store             session -> StateStore (e.g. `WatchdogStateStore.bind("ops.watchdog_state")`)
    session_day       now -> is this a trading session day (the market gate)
    page              (spec, probe) -> pushed? (e.g. `watch.pager.notify_pager(notifier, channel)`)
    now               the clock; its zone is the zone a gated row's "day start" is counted in
    load_registrants  imports the modules whose import registers a spec. The runner calls it before
                      every sweep and read: "the registry is whatever someone remembered to import"
                      is not a registry (an autofixer once removed the import as unused).
    on_degraded       reason -> None, for the failures that must not be silent
    investigate       the optional transition hook (see the module docstring)
    sweep_scope       () -> async context manager wrapped around each sweep (a timeline unit)
    day_start         now -> the start of now's session day (default: midnight in now's zone)
    """

    def __init__(
        self,
        registry: WatchRegistry,
        *,
        store: Callable[[Any], StateStore],
        session_day: Callable[[dt.datetime], bool],
        page: Pager,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        load_registrants: Callable[[], None] | None = None,
        on_degraded: Callable[[str], None] | None = None,
        investigate: Investigate | None = None,
        sweep_scope: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
        day_start: Callable[[dt.datetime], dt.datetime] = _midnight,
        realert_h: float = REALERT_H,
        stale_after_s: int = STALE_AFTER_S,
    ) -> None:
        self.registry = registry
        self._store = store
        self._session_day = session_day
        self._page = page
        self._now = now
        self._load = load_registrants
        self._on_degraded = on_degraded
        self._investigate = investigate
        self._scope = sweep_scope
        self._day_start = day_start
        self.realert_h = realert_h
        self.stale_after_s = stale_after_s

    # -- the hooks, each guarded ------------------------------------------------------------------
    def _degraded(self, reason: str) -> None:
        if self._on_degraded is None:
            return
        try:
            self._on_degraded(reason)
        except Exception:
            log.exception("watchdog on_degraded hook raised (reason=%s)", reason)

    def load_registrants(self) -> None:
        if self._load is not None:
            self._load()

    def is_session_day(self, now: dt.datetime) -> bool:
        try:
            return bool(self._session_day(now))
        except Exception:
            log.exception("watchdog market gate raised -- evaluating as a session day")
            self._degraded("watchdog_market_gate_failed")
            return True

    async def _deliver(self, spec: WatchdogSpec, probe: Probe) -> bool:
        """Push one page. Never raises -- a dead pager must not abort the remaining watchdogs."""
        try:
            return bool(await self._page(spec, probe))
        except Exception:
            log.exception("watchdog page failed: %s", spec.id)
            # THE PAGE OF LAST RESORT, failing quietly: record(paged=False) notes nothing went out,
            # but nothing READS that unless the failure is also raised somewhere watched.
            self._degraded("watchdog_page_failed")
            return False

    async def _open_investigation(
        self, session: Any, spec: WatchdogSpec, probe: Probe, now: dt.datetime, *,
        prior_subjects: list[str], notify: bool, transitioned: bool,
    ) -> tuple[Any, list[str]]:  # fmt: skip
        if self._investigate is None:
            return None, prior_subjects
        try:
            async with savepoint(session):
                investigation, subjects = await self._investigate(
                    session, spec.id, probe, now, prior_subjects=prior_subjects, notify=notify,
                    transitioned=transitioned,
                )  # fmt: skip
            return investigation, list(subjects)
        except Exception as exc:
            # A handler that fails open on the wrong layer fails closed: the savepoint keeps the
            # NEXT spec's record() alive, and the sweep goes on.
            log.exception("watchdog investigate hook failed: %s", spec.id)
            self._degraded("watchdog_investigate_failed")
            return f"failed: {type(exc).__name__}: {exc}"[:500], prior_subjects

    # -- the sweep ---------------------------------------------------------------------------------
    async def run(
        self, session: Any, *, notify: bool = True, only_ids: list[str] | None = None, now: dt.datetime | None = None
    ) -> dict[str, Any]:
        """Evaluate the registry (or the `only_ids` subset), dedup against the stored state, page what
        transitioned. Returns the full evaluation."""
        self.load_registrants()
        scope = self._scope() if self._scope is not None else contextlib.nullcontext()
        async with scope:
            return await self._run(session, notify=notify, only_ids=only_ids, now=now)

    async def _run(
        self, session: Any, *, notify: bool, only_ids: list[str] | None, now: dt.datetime | None
    ) -> dict[str, Any]:
        now = now or self._now()
        session_day = self.is_session_day(now)
        specs = [s for s in self.registry.specs().values() if only_ids is None or s.id in only_ids]
        repo = self._store(session)
        # A RECORDING sweep row-locks its rows up front and skips any row another sweep holds (that
        # row is being judged right now). CLAIM BEFORE LOAD: loading first leaves a window where the
        # holder commits a transition between our read and our lock, and we re-page it.
        contended = await repo.claim([s.id for s in specs]) if notify else set()
        stored = await repo.load([s.id for s in specs])
        if notify and only_ids is None and specs:
            # A FULL recording sweep owns the table: a retired watchdog's row goes with its spec. An
            # empty registry is an import failure, not a retirement of everything -- nothing reaped.
            reaped = await repo.reap_unregistered(keep=[s.id for s in specs])
            if reaped:
                log.info("watchdog_state_reaped ids=%s", reaped)

        results: list[dict[str, Any]] = []
        paged: list[str] = []
        for spec in specs:
            prior = stored.get(spec.id) or {}
            if spec.id in contended:
                # The holder records and pages this spec; status is the STORED one -- "ok" would
                # claim a verdict nobody reached.
                log.info("watchdog_contended_skip id=%s", spec.id)
                row = _skipped_row(spec, prior, prior.get("last_status") or Status.UNKNOWN.value,
                                   "skipped — another sweep is evaluating it now")  # fmt: skip
                results.append({**row, "contended": True})
                continue
            if spec.market_gated and not session_day:
                results.append(_skipped_row(spec, prior, Status.OK.value, OFF_SESSION))
                # THE STORED ROW MUST NOT OUTLIVE THE VERDICT: a watchdog that went DOWN and was
                # then gated kept 'down' stored while the listing said ok/skipped -- two truths.
                if notify and (prior.get("last_status") or "ok") != "ok":
                    await repo.record(spec.id, status="ok", transitioned=True, paged=False, now=now,
                                      error=None, detail=OFF_SESSION, metrics={})  # fmt: skip
                continue
            t0 = time.monotonic()
            probe_error: str | None = None
            try:
                async with savepoint(session):
                    if spec.probe is None:
                        raise RuntimeError(f"watchdog {spec.id!r} declares no probe")
                    probe = await spec.probe(session)
            except Exception as exc:
                log.exception("watchdog probe failed: %s", spec.id)
                # PERSISTED, not just logged: UNKNOWN alone is three characters that never say why.
                probe_error = f"{type(exc).__name__}: {exc}"[:500]
                probe = Probe(Status.UNKNOWN, f"probe raised: {exc!r}")
            metrics = {**(probe.metrics or {}), "probe_ms": int((time.monotonic() - t0) * 1000)}
            page, transitioned = should_page(stored.get(spec.id), probe.status, now, realert_h=self.realert_h)
            page = page and not spec.observe_only
            pushed = False
            if page and notify:
                pushed = await self._deliver(spec, probe)
                if pushed:
                    paged.append(spec.id)
            if notify:
                # `error` passed on EVERY call, None included: that clears a stale message on recovery.
                await repo.record(spec.id, status=probe.status.value, transitioned=transitioned, paged=pushed,
                                  now=now, error=probe_error, detail=probe.detail, metrics=metrics)  # fmt: skip
            prior_subjects = list(prior.get("last_subjects") or [])
            investigation, subjects = await self._open_investigation(
                session, spec, probe, now, prior_subjects=prior_subjects, notify=notify, transitioned=transitioned
            )
            # record() already cleared the subjects of a row no longer failing; this write is only
            # the change, so a steady sweep costs no extra statement.
            if notify and subjects != prior_subjects and (subjects or probe.status in FAILING):
                await repo.record_subjects(spec.id, subjects)
            results.append({
                "id": spec.id, "status": probe.status.value, "detail": probe.detail, "metrics": metrics,
                "severity": spec.severity, "owner": spec.owner, "description": spec.description,
                "observe_only": spec.observe_only, "would_page": page, "paged": pushed,
                "investigation": investigation,
                # THIS run's reason, not the stored one (the row was written a line above).
                "error": probe_error,
                "last_evaluated_at": _iso(prior.get("updated_at")),
                "last_page_at": _iso(prior.get("last_page_at")),
                "last_transition_at": _iso(prior.get("last_transition_at")),
            })  # fmt: skip
        return {
            "now": now.isoformat(), "session_day": session_day, "results": results, "paged": paged,
            "contended": sorted(contended),
        }  # fmt: skip

    async def run_subset(self, session: Any, ids: list[str], *, notify: bool = True) -> dict[str, Any]:
        """A dedicated cron's subset run whose rows may be HELD by the full sweep. A contended spec is
        re-probed READ-ONLY (no record, no page, no lock), so the caller gets real metrics -- never
        an empty row read as zero. The holder still records and pages."""
        res = await self.run(session, notify=notify, only_ids=ids)
        if res["contended"]:
            fresh = await self.run(session, notify=False, only_ids=res["contended"])
            by_id = {r["id"]: {**r, "contended": True} for r in fresh["results"]}
            res["results"] = [by_id.get(r["id"], r) for r in res["results"]]
        return res

    # -- the stored read --------------------------------------------------------------------------
    async def read_stored(self, session: Any, *, now: dt.datetime | None = None) -> dict[str, Any]:
        """The registry with each watchdog's STORED verdict. Probes nothing, writes nothing, pages
        nothing: one SELECT. Same row keys as `run`, plus `stale` / `age_s`.

        What it cannot know it says, never fills in: a watchdog no sweep recorded is UNKNOWN "not yet
        evaluated". Off-session skips are exempt from staleness, and a market-gated row's age on a
        session day counts from that day's start at the earliest (its last write is the previous
        session day, so without this every gated row reads stale after each weekend)."""
        self.load_registrants()
        now = now or self._now()
        session_day = self.is_session_day(now)
        specs = list(self.registry.specs().values())
        stored = await self._store(session).load([s.id for s in specs])
        day_start = self._day_start(now)
        results: list[dict[str, Any]] = []
        for spec in specs:
            prior = stored.get(spec.id) or {}
            if spec.market_gated and not session_day:
                row = _skipped_row(spec, prior, Status.OK.value, OFF_SESSION)
                results.append({**row, "stale": False, "age_s": None})
                continue
            status = prior.get("last_status") or Status.UNKNOWN.value
            at = prior.get("updated_at")
            age_s = int((now - at).total_seconds()) if isinstance(at, dt.datetime) else None
            stale_age = age_s
            if spec.market_gated and isinstance(at, dt.datetime):
                stale_age = int((now - max(at, day_start)).total_seconds())
            if not prior:
                detail = "not yet evaluated — no sweep has recorded this watchdog"
            else:
                detail = prior.get("last_detail") or "(recorded before verdict details were stored)"
            try:
                page, _ = should_page(prior or None, Status(status), now, realert_h=self.realert_h)
            except ValueError:  # a status this build no longer knows: show it, never page on it
                page = False
            page_at, trans_at = prior.get("last_page_at"), prior.get("last_transition_at")
            results.append({
                "id": spec.id, "status": status, "detail": detail, "metrics": prior.get("last_metrics") or {},
                "severity": spec.severity, "owner": spec.owner, "description": spec.description,
                "observe_only": spec.observe_only, "skipped": False,
                "would_page": page and not spec.observe_only,
                "paged": bool(page_at and trans_at and page_at >= trans_at),
                "error": prior.get("last_error"), "investigation": None,
                "stale": stale_age is None or stale_age > self.stale_after_s, "age_s": age_s,
                "last_evaluated_at": _iso(at), "last_page_at": _iso(page_at),
                "last_transition_at": _iso(trans_at),
            })  # fmt: skip
        stale = [r["id"] for r in results if r["stale"]]
        if stale:
            log.warning("watchdog_verdicts_stale ids=%s stale_after_s=%s", stale, self.stale_after_s)
        return {
            "now": now.isoformat(), "session_day": session_day, "source": "stored",
            "stale_after_s": self.stale_after_s, "stale": stale, "results": results, "paged": [],
            "contended": [],
        }  # fmt: skip
