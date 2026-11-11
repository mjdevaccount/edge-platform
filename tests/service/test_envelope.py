"""The response envelope (edge_platform.service.envelope; ported from trade-data tests/schemas/test_envelope.py): the honest shapes construct, the dishonest
ones are refused, and the additive legacy envelope changes no byte of an existing payload."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from pydantic import ValidationError

from edge_platform.service.envelope import (
    Fresh,
    Freshness,
    FreshStamped,
    api_error,
    error_code,
    is_stale_or_unknown,
    retryable,
    snapshot_age_s,
    weakest,
)

NOW = dt.datetime(2026, 10, 23, 15, 0, tzinfo=dt.UTC)


def _obs(age_s: float, cap: float = 300.0, prov: str = "quant.t") -> Freshness:
    return Freshness.observe(NOW - dt.timedelta(seconds=age_s), max_age_s=cap, provenance=prov, now=NOW)


def test_observe_fresh_and_stale() -> None:
    f = _obs(30)
    assert (f.age_s, f.stale, f.max_age_s, f.absent_reason) == (30.0, False, 300.0, None)
    assert _obs(301).stale is True


def test_observe_none_is_absent_with_its_reason_never_a_zero_age() -> None:
    f = Freshness.observe(None, max_age_s=300, provenance="quant.t", absent_reason="no grid")
    assert (f.as_of, f.age_s, f.stale, f.absent_reason) == (None, None, True, "no grid")


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"as_of": None, "age_s": None, "stale": True}, "unexplained null"),
        ({"as_of": NOW, "age_s": 1.0, "stale": False, "max_age_s": 60, "absent_reason": "x"}, "absent with data"),
        ({"as_of": NOW, "age_s": None, "stale": True}, "computable age left null"),
        ({"as_of": NOW, "age_s": 5.0, "stale": False}, "fresh with no cadence"),
        ({"as_of": None, "age_s": None, "stale": False, "absent_reason": "x"}, "absent but fresh"),
        ({"as_of": NOW.replace(tzinfo=None), "age_s": 1.0, "stale": True}, "naive timestamp"),
        ({"as_of": NOW, "age_s": -1.0, "stale": True}, "negative age"),
    ],
)
def test_dishonest_blocks_are_refused(kwargs: dict[str, Any], why: str) -> None:
    with pytest.raises(ValidationError):
        Freshness(provenance="quant.t", **kwargs)


def test_weakest_prefers_absent_then_stale_then_oldest() -> None:
    old, new, stale = _obs(200, prov="old"), _obs(10, prov="new"), _obs(400, prov="stale")
    gone = Freshness.absent("off", provenance="gone")
    assert weakest(new, old).provenance == "old"
    assert weakest(new, stale, old).provenance == "stale"
    assert weakest(stale, gone, new).provenance == "gone"
    with pytest.raises(ValueError, match="weakest"):
        weakest()


def test_fresh_generic_null_data_requires_an_absent_reason() -> None:
    assert Fresh[dict](data={"a": 1}, fresh=_obs(1)).data == {"a": 1}
    assert Fresh[dict](data=None, fresh=Freshness.absent("off", provenance="p")).data is None
    with pytest.raises(ValidationError):
        Fresh[dict](data=None, fresh=_obs(1))


def test_block_round_trips_through_the_stamped_model() -> None:
    blk = _obs(42).block()
    assert blk["as_of"].endswith("Z") and blk["age_s"] == 42.0
    assert FreshStamped.model_validate({"fresh": blk}).fresh == Freshness.model_validate(blk)


def test_stamped_route_changes_no_legacy_byte() -> None:
    """The point of FreshStamped: a response_model on an existing route must not re-encode the
    legacy keys. The adopted routes are annotated `-> dict[str, Any]`, which FastAPI already turns
    into an inferred response model (pydantic JSON mode: Decimal -> STRING, UTC -> `Z`), so THAT is
    today's wire shape and the one FreshStamped must reproduce -- not jsonable_encoder's."""
    legacy = {
        "px": Decimal("101.25"),
        "n": Decimal("3"),
        "ts": NOW,
        "day": NOW.date(),
        "rows": [{"k": Decimal("0.5"), "t": NOW}],
        "none": None,
        "s": "x",
    }
    app = FastAPI()

    @app.get("/old")
    async def old() -> dict[str, Any]:
        return legacy

    @app.get("/new", response_model=FreshStamped)
    async def new() -> dict[str, Any]:
        return {**legacy, "fresh": _obs(5).block()}

    c = TestClient(app)
    before, after = c.get("/old").json(), c.get("/new").json()
    fresh = after.pop("fresh")
    assert after == before
    assert before["px"] == "101.25" and before["ts"].endswith("Z")  # today's shape, pinned
    # ...and it is NOT jsonable_encoder's: a route WITHOUT the annotation would differ, which is why
    # the adoption test pins the annotation on every adopted endpoint.
    assert before != json.loads(json.dumps(jsonable_encoder(legacy)))
    assert fresh["stale"] is False and fresh["provenance"] == "quant.t"
    assert "fresh" in FreshStamped.model_json_schema()["required"]


def test_stamped_route_without_fresh_fails_loudly() -> None:
    app = FastAPI()

    @app.get("/x", response_model=FreshStamped)
    async def x() -> dict[str, Any]:
        return {"a": 1}

    with pytest.raises(Exception, match="fresh"):
        TestClient(app).get("/x")


@pytest.mark.parametrize(
    ("status", "code", "retry"),
    [
        (404, "not_found", False),
        (422, "validation_error", False),
        (429, "rate_limited", True),
        (503, "unavailable", True),
        (500, "internal", None),
        (418, "http_418", False),
        (599, "http_599", None),
    ],
)
def test_error_codes(status: int, code: str, retry: bool | None) -> None:
    assert (error_code(status), retryable(status)) == (code, retry)


def test_api_error_keeps_detail_and_legacy_keys() -> None:
    body = api_error(503, "down", error_code="enqueue_failed", status="enqueue_failed")
    assert body == {"detail": "down", "error_code": "enqueue_failed", "retryable": True, "status": "enqueue_failed"}


def test_the_staleness_helpers_fail_closed() -> None:
    assert snapshot_age_s(None, NOW) is None
    assert snapshot_age_s(NOW, NOW - dt.timedelta(seconds=5)) == 0.0  # never negative
    assert snapshot_age_s(NOW - dt.timedelta(seconds=7), NOW) == 7.0
    assert is_stale_or_unknown(None, 60) is True  # unknown is never fresh
    assert (is_stale_or_unknown(60, 60), is_stale_or_unknown(61, 60)) == (False, True)
