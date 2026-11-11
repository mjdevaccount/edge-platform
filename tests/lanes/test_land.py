"""land_and_promote and nolock_postgres (extra `[dlt]`).

nolock_postgres is ported from trade-data tests/test_pg_destination.py: the 2026-07-30 lesson was an
@classmethod override that imports fine and explodes only when a pipeline instantiates the
destination, so the test calls the real path. land_and_promote runs against a fake pipeline that
records the transaction, so ZERO-IS-NOT-SUCCESS and the same-transaction heartbeat are seen firing
both ways."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

pytest.importorskip("dlt")

from edge_platform.lanes import land as land_mod
from edge_platform.lanes.destination import nolock_postgres
from edge_platform.lanes.land import EmptyLanding, Heartbeat, PromoteMismatch, land_and_promote

# --- destination (ported) ------------------------------------------------------------------------


def test_capabilities_resolve_and_truncate_is_off() -> None:
    caps = nolock_postgres()._raw_capabilities()
    assert caps.supports_truncate_command is False
    assert caps.preferred_loader_file_format == "insert_values"  # still a real postgres capability set


def test_factory_accepts_env_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in (("USERNAME", "u"), ("PASSWORD", "p"), ("HOST", "h"), ("PORT", "5432"), ("DATABASE", "d")):
        monkeypatch.setenv(f"DESTINATION__POSTGRES__CREDENTIALS__{k}", v)
    dest = nolock_postgres()
    assert dest.config_params["credentials"]["username"] == "u"
    assert dest.config_params["destination_name"] == "postgres"


def test_no_env_credentials_leaves_dlt_to_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DESTINATION__POSTGRES__CREDENTIALS__USERNAME", raising=False)
    assert nolock_postgres().config_params.get("credentials") is None  # nothing wired: dlt resolves


# --- land_and_promote --------------------------------------------------------------------------


class _Client:
    def __init__(self, log: list, rowcount: int, fail_sql: str | None) -> None:
        self.log, self.rowcount, self.fail_sql = log, rowcount, fail_sql

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @contextmanager
    def begin_transaction(self):
        self.log.append("BEGIN")
        try:
            yield
        except BaseException:
            self.log.append("ROLLBACK")
            raise
        self.log.append("COMMIT")

    def execute_sql(self, sql: str, **params) -> None:
        if self.fail_sql and self.fail_sql in sql:
            raise RuntimeError("heartbeat table missing")
        self.log.append(("sql", sql.strip().split()[0], params))

    @contextmanager
    def execute_query(self, sql: str, **params):
        self.log.append(("promote", params))
        yield SimpleNamespace(native_cursor=SimpleNamespace(rowcount=self.rowcount))


class _Pipeline:
    def __init__(self, landed: int, rowcount: int, fail_sql: str | None = None, run_raises: bool = False) -> None:
        self.pipeline_name, self.dataset_name = "p", "raw_t"
        self.landed, self.log = landed, []
        self.rowcount, self.fail_sql, self.run_raises = rowcount, fail_sql, run_raises
        self.last_trace = SimpleNamespace(last_normalize_info=SimpleNamespace(row_counts={"t": landed}))

    def run(self, _resource, **kw):
        self.run_kw = kw
        if self.run_raises:
            raise RuntimeError("source API down")
        return SimpleNamespace(raise_on_failed_jobs=lambda: None, loads_ids=["L1"] if self.landed else [])

    def sql_client(self):
        return _Client(self.log, self.rowcount, self.fail_sql)


@pytest.fixture
def pipe(monkeypatch: pytest.MonkeyPatch):
    made: dict = {}

    def factory(**kw):
        made["kw"] = kw
        return made["pipe"]

    monkeypatch.setattr(land_mod.dlt, "pipeline", factory)
    return made


HB = Heartbeat(table="ops.ingest_heartbeat", source="t_src")
ARGS = dict(pipeline_name="p", dataset_name="raw_t", table="t", promote_sql="INSERT ...", pre_sql="DELETE ...")


def test_a_good_load_promotes_and_beats_ok_in_the_same_transaction(pipe) -> None:
    pipe["pipe"] = p = _Pipeline(landed=3, rowcount=3)
    out = land_and_promote(object(), **ARGS, promote_params={"symbol": "SPY"}, heartbeat=HB, pipelines_dir="/tmp/d")
    assert (out.load_id, out.landed, out.promoted) == ("L1", 3, 3)
    assert pipe["kw"]["pipelines_dir"] == "/tmp/d" and pipe["kw"]["destination"].__class__ is nolock_postgres
    begin, pre, promote, beat, commit = p.log
    assert (begin, commit) == ("BEGIN", "COMMIT")
    assert pre[1] == "DELETE" and promote == ("promote", {"symbol": "SPY", "load_id": "L1"})
    assert beat[1] == "INSERT" and beat[2]["hb_ok"] is True and beat[2]["hb_source"] == "t_src"


def test_zero_rows_is_not_success_and_beats_failure(pipe) -> None:
    pipe["pipe"] = p = _Pipeline(landed=0, rowcount=0)
    with pytest.raises(EmptyLanding, match="0 rows"):
        land_and_promote(object(), **ARGS, heartbeat=HB)
    assert p.log == ["BEGIN", ("sql", "INSERT", p.log[1][2]), "COMMIT"] and p.log[1][2]["hb_ok"] is False


def test_landed_but_not_promoted_rolls_back_the_prune(pipe) -> None:
    pipe["pipe"] = p = _Pipeline(landed=3, rowcount=0)
    with pytest.raises(PromoteMismatch, match="3 rows landed"):
        land_and_promote(object(), **ARGS, heartbeat=HB)
    first_txn = p.log[: p.log.index("ROLLBACK") + 1]
    assert first_txn[1][1] == "DELETE" and "COMMIT" not in first_txn  # the prune never committed
    assert p.log[-2][2]["hb_ok"] is False  # then the failure beat, in its own transaction


def test_a_failing_failure_beat_never_masks_the_real_error(pipe) -> None:
    pipe["pipe"] = _Pipeline(landed=3, rowcount=3, fail_sql="ops.ingest_heartbeat", run_raises=True)
    with pytest.raises(RuntimeError, match="source API down"):
        land_and_promote(object(), **ARGS, heartbeat=HB)


def test_no_heartbeat_writes_nothing_but_the_load(pipe) -> None:
    pipe["pipe"] = p = _Pipeline(landed=2, rowcount=2)
    land_and_promote(object(), **{**ARGS, "pre_sql": None})
    assert [e if isinstance(e, str) else e[0] for e in p.log] == ["BEGIN", "promote", "COMMIT"]


@pytest.mark.parametrize("table", ["ingest_heartbeat", "ops.x; DROP TABLE y", "Ops.Beat"])
def test_the_heartbeat_table_is_validated_because_it_is_interpolated(table: str) -> None:
    with pytest.raises(ValueError, match=r"schema.table"):
        Heartbeat(table=table, source="x")


def test_the_heartbeat_sql_names_the_injected_table() -> None:
    sql = Heartbeat(table="mine.beats", source="x").sql()
    assert "INSERT INTO mine.beats" in sql and "mine.beats.last_ok_at" in sql and "ops." not in sql


# --- v0.11: the dlt schema contract is explicit -----------------------------------------------------


def test_the_explicit_contract_is_always_passed_to_dlt(pipe) -> None:
    """dlt's own default for a pydantic `columns=` resource is discard_value: a new vendor field
    vanishes. The contract is passed on EVERY run, freeze unless the call site loosens it."""
    pipe["pipe"] = p = _Pipeline(landed=3, rowcount=3)
    land_and_promote(object(), **ARGS)
    assert p.run_kw == {"schema_contract": {"tables": "evolve", "columns": "freeze", "data_type": "freeze"}}
    pipe["pipe"] = p = _Pipeline(landed=3, rowcount=3)
    land_and_promote(object(), **ARGS, schema_contract="evolve")  # an explicit, reviewable loosening
    assert p.run_kw == {"schema_contract": "evolve"}
    for bad in (None, {}):
        with pytest.raises(ValueError, match="explicit schema_contract"):
            land_and_promote(object(), **ARGS, schema_contract=bad)  # type: ignore[arg-type]
    assert land_mod.DEFAULT_CONTRACT["columns"] == "freeze"
