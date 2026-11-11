"""Land one batch through dlt into a raw dataset, then promote it set-based (extra `[dlt]`).

Lifted from trade-data `sources/dlt_land.py` (M6 PL4). The fetch is a dlt resource; dlt owns
typing/loading into a raw landing table (through `nolock_postgres`, DELETE-not-TRUNCATE), and ONE
set-based statement promotes exactly that load's rows into the target -- same key, same ON CONFLICT.

Three decisions, each paid for by an incident:

* The promote runs on dlt's OWN sql_client, i.e. as the identity that created the raw schema. dlt
  creates raw schemas at run time, so no migration can grant on them in advance; promoting as the
  app role failed twice on exactly that ("permission denied for schema raw_gmail").
* The promote reads `WHERE _dlt_load_id = %(load_id)s`, never the whole raw table, so a concurrent
  run of the same source cannot make this run promote someone else's rows.
* ZERO IS NOT SUCCESS. A resource that yields nothing makes dlt report a clean, empty load --
  indistinguishable from a good run in any status column. `EmptyLanding` is raised when the
  resource yielded no rows, `PromoteMismatch` when rows landed but none reached the target. The
  raise fails the scheduler's job, whose error handler pages; the target's newest timestamp is the
  landing spot an OUTSIDE watcher reads.

THE SCHEMA CONTRACT IS EXPLICIT (v0.11): `schema_contract` defaults to `DEFAULT_CONTRACT` --
tables evolve, columns and data types FREEZE -- and is always passed to `pipeline.run`. dlt's own
default for a resource whose `columns=` is a pydantic model is `discard_value`, which silently drops a
field the vendor added; under freeze a new field fails the run (`DataValidationError` inside
`PipelineStepFailed`), the lane fails, nothing is promoted, and the scheduler's error handler pages.
A new table's first run still creates its columns (dlt's rule for a new table), so a lane's first run
belongs on a fixture in CI. Loosening it is an explicit argument at the call site, never a default.

HEARTBEAT (opt-in, the TABLE INJECTED): `heartbeat=Heartbeat(table="ops.ingest_heartbeat",
source="calendar_events")` upserts one row per source into a table shaped
`(source PK, last_run_at, last_ok_at, ok, detail jsonb, updated_at)`. The ok beat commits in the
SAME transaction as the promote, so "beat ok" can only mean "rows reached the target"; any failure
beats ok=false, best-effort, then re-raises. The library names no table.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import dlt
import structlog

from edge_platform.sqlkit import qualified

from .destination import nolock_postgres

log = structlog.get_logger("edge_platform.lanes.land")

#: tables evolve, columns + data types freeze: a new vendor field FAILS the run instead of vanishing.
DEFAULT_CONTRACT: Mapping[str, str] = MappingProxyType({"tables": "evolve", "columns": "freeze", "data_type": "freeze"})


@dataclass(frozen=True)
class Heartbeat:
    """Where a lane beats. `table` is schema-qualified and validated: it is interpolated into SQL."""

    table: str
    source: str

    def __post_init__(self) -> None:
        qualified(self.table, "heartbeat table")
        if not self.source.strip():
            raise ValueError("heartbeat source must be non-empty")

    def sql(self) -> str:
        t = self.table
        return f"""
INSERT INTO {t} (source, last_run_at, last_ok_at, ok, detail, updated_at)
VALUES (%(hb_source)s, now(), CASE WHEN %(hb_ok)s THEN now() END, %(hb_ok)s, %(hb_detail)s::jsonb, now())
ON CONFLICT (source) DO UPDATE SET
    last_run_at = now(),
    last_ok_at  = CASE WHEN EXCLUDED.ok THEN now() ELSE {t}.last_ok_at END,
    ok          = EXCLUDED.ok,
    detail      = EXCLUDED.detail,
    updated_at  = now()
"""

    def params(self, ok: bool, detail: dict[str, Any]) -> dict[str, Any]:
        return {"hb_source": self.source, "hb_ok": ok, "hb_detail": json.dumps(detail)}


class EmptyLanding(RuntimeError):
    """The resource yielded zero rows -- a silent-success shape, raised instead of returned."""


class PromoteMismatch(RuntimeError):
    """Rows landed in raw but the promote wrote none -- the load id did not match its rows."""


@dataclass(frozen=True)
class Landed:
    load_id: str
    landed: int  # rows dlt normalized into the raw table this run
    promoted: int  # rows the promote statement touched in the target


def land_and_promote(
    resource: Any,
    *,
    pipeline_name: str,
    dataset_name: str,
    table: str,
    promote_sql: str,
    promote_params: dict[str, Any] | None = None,
    pre_sql: str | None = None,
    heartbeat: Heartbeat | None = None,
    pipelines_dir: str | None = None,
    destination: Any = None,
    schema_contract: Mapping[str, str] | str = DEFAULT_CONTRACT,
) -> Landed:
    """Run `resource` through dlt into `<dataset_name>.<table>`, then promote it.

    `promote_sql` is ONE statement in psycopg2 pyformat binds; `%(load_id)s` is bound to this run's
    load id. `pre_sql` (optional) runs first in the SAME transaction -- e.g. a window prune -- so the
    target never shows the pruned-but-not-refilled state. `destination` defaults to
    `nolock_postgres()`; `pipelines_dir` to dlt's own default. `schema_contract` is ALWAYS passed
    to dlt (default `DEFAULT_CONTRACT`); None is refused -- "no contract" is dlt's silent discard.
    """
    if schema_contract is None or (isinstance(schema_contract, Mapping) and not schema_contract):
        raise ValueError("land_and_promote needs an explicit schema_contract (default DEFAULT_CONTRACT)")
    contract = schema_contract if isinstance(schema_contract, str) else dict(schema_contract)
    pipeline = dlt.pipeline(
        pipeline_name=pipeline_name,
        destination=destination if destination is not None else nolock_postgres(),
        dataset_name=dataset_name,
        pipelines_dir=pipelines_dir,
    )
    try:
        return _land(
            pipeline, resource, table=table, promote_sql=promote_sql,
            promote_params=promote_params, pre_sql=pre_sql, heartbeat=heartbeat, schema_contract=contract,
        )  # fmt: skip
    except Exception as exc:
        if heartbeat is not None:
            beat_failure(pipeline, heartbeat, exc)
        raise


def beat_failure(pipeline: Any, heartbeat: Heartbeat, exc: BaseException) -> None:
    """ok=false, best-effort: never masks the real error. A missing beat reads as stale downstream,
    which is the correct failure direction (unknown is not fresh)."""
    detail = {"error": type(exc).__name__, "message": str(exc)[:300]}
    try:
        with pipeline.sql_client() as client, client.begin_transaction():
            client.execute_sql(heartbeat.sql(), **heartbeat.params(False, detail))
    except Exception:
        log.warning("dlt_land_heartbeat_failed", source=heartbeat.source, exc_info=True)


def _land(
    pipeline: Any,
    resource: Any,
    *,
    table: str,
    promote_sql: str,
    promote_params: dict[str, Any] | None,
    pre_sql: str | None,
    heartbeat: Heartbeat | None,
    schema_contract: Mapping[str, str] | str,
) -> Landed:
    pipeline_name, dataset_name = pipeline.pipeline_name, pipeline.dataset_name
    info = pipeline.run(resource, schema_contract=schema_contract)
    info.raise_on_failed_jobs()
    trace = pipeline.last_trace
    counts = trace.last_normalize_info.row_counts if trace and trace.last_normalize_info else {}
    landed = int(counts.get(table, 0))
    if landed == 0 or not info.loads_ids:
        raise EmptyLanding(f"{pipeline_name}: resource yielded 0 rows into {dataset_name}.{table}")
    load_id = info.loads_ids[-1]
    params = {**(promote_params or {}), "load_id": load_id}
    # binds go as **kwargs: dlt hands a positional dict to psycopg2 as a 1-tuple
    with pipeline.sql_client() as client, client.begin_transaction():
        if pre_sql:
            client.execute_sql(pre_sql, **params)
        with client.execute_query(promote_sql, **params) as cur:
            promoted = int(cur.native_cursor.rowcount)
        if promoted <= 0:  # raised INSIDE the txn: the pre_sql prune rolls back with it
            raise PromoteMismatch(f"{pipeline_name}: {landed} rows landed (load {load_id}) but 0 promoted")
        if heartbeat is not None:
            detail = {"load_id": load_id, "landed": landed, "promoted": promoted}
            client.execute_sql(heartbeat.sql(), **heartbeat.params(True, detail))
    return Landed(load_id=load_id, landed=landed, promoted=promoted)
