"""SourceGate -- is a lane switched OFF, or is its input absent? The one read every gate shares.

Lifted from trade-data `service/usecases/source_gate.py` (M6 PL4). The two reads are INJECTED (the
library owns no table): `read_disabled(session)` returns the ids an operator switched off, and
`observe(session, spec)` returns the lane's `freshness.Observation` (the same read the freshness
sweep makes). `mirror` names the switch table in every reason, so a daemon log line, a worklist's
blocked marker and a decision card all say the same thing.

v0.11 (M6 PL9): `input_absent` CALLS `freshness.judge`, so the gate and the page can never disagree.
v0.9 compared a wall-hours age with `cadence_h`, so a session-clock lane read "absent" every night
and every weekend. The limit is now the lane's own (`due_h`, else the wall rule), counted on the
lane's own clock.

BEST-EFFORT BY CONSTRUCTION. An unreadable switch table (a restore, an unapplied migration) reads as
NOTHING disabled -- a control table must never black out the pipeline -- but never silently: a
warning line always, `on_degraded(reason)` when the caller supplies one (trade-data marks its
current work unit degraded), and the failed read is rolled back so the caller's session stays usable.

THAT ROLLBACK IS THE CALLER'S CONSTRAINT, AND IT IS TRANSITIVE: read the switch BEFORE writing in the
same transaction, in every frame above a read of this gate. trade-data's 2026-09-19 audit found every
direct caller correct and the defect four frames up (a scan wrote its exits, then built a decision
whose clearance read the switch; the rollback discarded the exits).

`input_absent` never raises: off, no readable rows, past the lane's own limit on its own clock, or
the observation itself failing (unknown is not fresh) each return their own reason.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from .freshness import Observation, judge
from .spec import LaneRegistry, LaneSpec

#: stdlib logging, so the gate imports with no extra (structlog renders it where installed)
log = logging.getLogger("edge_platform.lanes.gate")

ReadDisabled = Callable[[Any], Awaitable[Iterable[str]]]
Observe = Callable[[Any, LaneSpec], Awaitable[Observation]]


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class SourceGate:
    def __init__(
        self,
        lanes: LaneRegistry,
        *,
        read_disabled: ReadDisabled,
        observe: Observe,
        mirror: str,
        on_degraded: Callable[[str], None] | None = None,
        now: Callable[[], dt.datetime] = _utcnow,
    ) -> None:
        if not mirror.strip():
            raise ValueError("mirror must name the switch table (it is quoted in every reason)")
        self.lanes = lanes
        self._read_disabled = read_disabled
        self._observe = observe
        self.mirror = mirror
        self._on_degraded = on_degraded
        self._now = now

    async def disabled_sources(self, session: Any) -> frozenset[str]:
        """Ids the operator switched off. Empty -- loudly -- when the switch table cannot be read."""
        try:
            return frozenset(await self._read_disabled(session))
        except Exception as exc:
            log.warning("content_source_mirror_unreadable mirror=%s err=%s", self.mirror, str(exc)[:200])
            if self._on_degraded is not None:
                with contextlib.suppress(Exception):  # telemetry must never become the failure
                    self._on_degraded(
                        f"content-source mirror unreadable ({type(exc).__name__}); no source treated as disabled"
                    )
            with contextlib.suppress(Exception):  # best-effort on a session we do not own
                await session.rollback()  # a failed read aborts the transaction; do not poison the caller
            return frozenset()

    def disabled_reason(self, source_id: str, disabled: frozenset[str] | set[str]) -> str | None:
        """PURE. The one wording every gate shows, or None."""
        if source_id in disabled:
            return f"source {source_id} disabled ({self.mirror})"
        return None

    async def blocked_reason(self, session: Any, source_id: str) -> str | None:
        return self.disabled_reason(source_id, await self.disabled_sources(session))

    async def input_absent(self, session: Any, source_id: str) -> str | None:
        """Why a lane has no INPUT right now, or None. Never raises."""
        try:
            off = await self.blocked_reason(session, source_id)
            if off:
                return off
            spec = self.lanes.get(source_id)
            if spec is None or spec.freshness_key is None or not spec.limited:
                return None  # nothing declared to gate on (an event lane): never a guessed limit
            obs = await self._observe(session, spec)
            if obs.newest_output is None:
                return f"{spec.freshness_key} has no readable rows"
            j = judge(spec, obs, now=self._now())
            if j.age_h is not None and j.limit_h is not None and j.age_h <= j.limit_h:
                return None  # inside its own limit, on its own clock (held / abstaining included)
            return f"{spec.freshness_key} {j.state}: {j.why}"
        except Exception as exc:
            with contextlib.suppress(Exception):
                await session.rollback()
            return f"{source_id} input freshness unreadable: {type(exc).__name__}"
