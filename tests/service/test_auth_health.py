"""The API-key guard and the liveness/readiness pair (ported from trade-quant tests/test_api_key_guard.py
and tests/test_healthz_readyz.py).

The guard uses a nonexistent /admin path so nothing touches a DB: a 404 proves the guard let the
request through; a 401 proves it blocked it. The DB-down case makes the session maker itself
explode -- harsher than a failed query, which is the point: liveness must survive even that."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from edge_platform.service.auth import install_api_key_guard
from edge_platform.service.health import add_health_routes, describe_error
from edge_platform.service.timeline import AdminRunTimeline


def _guarded(key: str, recorded: list[str] | None = None) -> TestClient:
    app = FastAPI()
    if recorded is not None:  # the timeline FIRST, the guard after it -> the guard runs outside

        @asynccontextmanager
        async def recorder(name: str, path: str, method: str):
            recorded.append(name)
            yield None

        app.add_middleware(AdminRunTimeline, recorder=recorder, on_status=lambda unit, status: None)
    install_api_key_guard(app, lambda: key)

    @app.get("/version")
    async def version() -> dict[str, str]:
        return {"version": "x"}

    return TestClient(app)


def test_guard_off_when_the_key_is_empty() -> None:
    assert _guarded("").post("/admin/run/nonexistent").status_code == 404


def test_missing_key_rejected_with_the_one_error_body() -> None:
    r = _guarded("s3cret").post("/admin/run/nonexistent")
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid or missing API key", "error_code": "unauthorized", "retryable": False}


def test_wrong_key_rejected() -> None:
    assert _guarded("s3cret").post("/admin/run/nonexistent", headers={"X-API-Key": "nope"}).status_code == 401


def test_correct_key_passes() -> None:
    assert _guarded("s3cret").post("/admin/run/nonexistent", headers={"X-API-Key": "s3cret"}).status_code == 404


def test_reads_never_gated() -> None:
    assert _guarded("s3cret").get("/version").status_code == 200


def test_a_rejected_request_records_nothing_on_the_timeline() -> None:
    recorded: list[str] = []
    c = _guarded("s3cret", recorded)
    assert c.post("/admin/run/pull_ohlcv").status_code == 401
    assert recorded == []
    # both directions: the same timeline DOES record an accepted request
    assert c.post("/admin/run/pull_ohlcv", headers={"X-API-Key": "s3cret"}).status_code == 404
    assert recorded == ["pull_ohlcv"]


def _health(maker: Any) -> TestClient:
    app = FastAPI()
    add_health_routes(app, version="1.2.3", session_maker=lambda: maker)
    return TestClient(app)


def _exploding_maker():
    raise RuntimeError("database is gone")


class _Session:
    async def execute(self, *_a, **_k):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_healthz_is_liveness_only_survives_dead_db() -> None:
    r = _health(_exploding_maker).get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True, "version": "1.2.3", "checks": "liveness"}


def test_readyz_returns_503_when_db_unreachable() -> None:
    r = _health(_exploding_maker).get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False and "RuntimeError" in (body["db_error"] or "")


def test_readyz_returns_200_when_db_answers() -> None:
    r = _health(_Session).get("/readyz")
    assert r.status_code == 200 and r.json() == {"ok": True, "version": "1.2.3", "db_error": None}


def test_describe_error_masks_a_dsn_password() -> None:
    msg = describe_error(OSError("connect to postgresql+asyncpg://app_rw:hunter2@db:5432/trade failed"))
    assert "hunter2" not in msg and "app_rw:***@" in msg and msg.startswith("OSError: ")
