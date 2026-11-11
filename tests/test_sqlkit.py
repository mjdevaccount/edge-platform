"""edge_platform.sqlkit: the table-name gate (both directions), jsonb text, the savepoint re-export."""

from __future__ import annotations

import datetime as dt
import decimal
import json
import uuid

import pytest

from edge_platform import sqlkit


@pytest.mark.parametrize("name", ["ops.watchdog_state", "a.b", "_x.y_1"])
def test_a_lower_case_schema_table_passes(name) -> None:
    assert sqlkit.qualified(name) == name


@pytest.mark.parametrize(
    "name", ["watchdog_state", "Ops.t", "ops.t; drop table x", "ops.t--", "a.b.c", "", None, 'ops."t"']
)
def test_anything_else_is_refused_naming_the_knob(name) -> None:
    with pytest.raises(ValueError, match="state table"):
        sqlkit.qualified(name, "state table")


def test_json_text_never_raises_and_never_writes_nan() -> None:
    class Odd:
        def __str__(self) -> str:
            return "odd"

    out = json.loads(sqlkit.json_text({
        "nan": float("nan"), "inf": float("inf"), "ok": 1.5, "at": dt.datetime(2026, 1, 2, 3, 4, tzinfo=dt.UTC),
        "d": dt.date(2026, 1, 2), "dec": decimal.Decimal("1.25"), "u": uuid.UUID(int=1), "s": {"b", "a"},
        "t": (1, 2), "odd": Odd(), "nested": [{"x": float("nan")}],
    }))  # fmt: skip
    assert out["nan"] is None and out["inf"] is None and out["ok"] == 1.5
    assert out["at"] == "2026-01-02T03:04:00+00:00" and out["d"] == "2026-01-02"
    assert out["dec"] == "1.25" and out["u"] == str(uuid.UUID(int=1))
    assert out["s"] == ["a", "b"] and out["t"] == [1, 2] and out["odd"] == "odd"
    assert out["nested"] == [{"x": None}]
    assert sqlkit.json_text(None) is None


def test_the_service_db_savepoint_is_the_one_copy() -> None:
    from edge_platform.service import db

    assert db.savepoint is sqlkit.savepoint
