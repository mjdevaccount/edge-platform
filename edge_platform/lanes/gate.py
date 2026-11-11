"""SourceGate -- is a lane switched OFF, or is its input absent? The one read every gate shares.

Lifted from trade-data `service/usecases/source_gate.py` (M6 PL4). The two reads are INJECTED (the
library owns no table): `read_disabled(session)` returns the ids an operator switched off, and
`read_freshness(session, freshness_key, ts_col)` returns `(rows, newest, age_h)` for a lane's
output. `mirror` names the switch table in every reason, so a daemon log line, a worklist's blocked
marker and a decision card all say the same thing.

BEST-EFFORT BY CONSTRUCTION. An unreadable switch table (a restore, an unapplied migration) reads as
NOTHING disabled -- a control table must never black out the pipeline -- but never silently: a
warning line always, `on_degraded(reason)` when the caller supplies one (trade-data marks its
current work unit degraded), and the failed read is rolled back so the caller's session stays usable.

THAT ROLLBACK IS THE CALLER'S CONSTRAINT, AND IT IS TRANSITIVE: read the switch BEFORE writing in the
same transaction, in every frame above a read of this gate. trade-data's 2026-09-19 audit found every
direct caller correct and the defect four frames up (a scan wrote its exits, then built a decision
whose clearance read the switch; the rollback discarded the exits).

`input_absent` never raises: off, no readable rows, older than the lane's own cadence, or the probe
itself failing (unknown is not fresh) each return their own reason.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from .spec import LaneRegistry

#: stdlib logging, so the gate imports with no extra (structlog renders it where installed)
log = logging.getLogger("edge_platform.lanes.gate")

ReadDisabled = Callable[[Any], Awaitable[Iterable[str]]]
ReadFreshness = Callable[[Any, str, str], Awaitable[tuple[int, Any, float | None]]]


class SourceGate:
    def __init__(
        self,
        lanes: LaneRegistry,
        *,
        read_disabled: ReadDisabled,
        read_freshness: ReadFreshness,
        mirror: str,
        on_degraded: Callable[[str], None] | None = None,
    ) -> None:
        if not mirror.strip():
            raise ValueError("mirror must name the switch table (it is quoted in every reason)")
        self.lanes = lanes
        self._read_disabled = read_disabled
        self._read_freshness = read_freshness
        self.mirror = mirror
        self._on_degraded = on_degraded

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
            if spec is None or spec.freshness is None or spec.cadence_h is None or spec.freshness_key is None:
                return None
            rows, _newest, age_h = await self._read_freshness(session, spec.freshness_key, spec.freshness[1])
            if not rows or age_h is None:
                return f"{spec.freshness_key} has no readable rows"
            if age_h > spec.cadence_h:
                return (
                    f"{spec.freshness_key} newest write {age_h:.0f}h ago > cadence {spec.cadence_h:g}h "
                    "-- the feed has stopped"
                )
            return None
        except Exception as exc:
            with contextlib.suppress(Exception):
                await session.rollback()
            return f"{source_id} input freshness unreadable: {type(exc).__name__}"
