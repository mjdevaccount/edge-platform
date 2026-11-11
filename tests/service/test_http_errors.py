"""edge_platform.service.http_errors: every error answers ONE JSON shape, ADDITIVELY -- `detail` is
exactly what FastAPI would have sent, plus `error_code` + `retryable` (ported from trade-data
tests/test_http_errors.py; its JobEnqueueFailed case is a consumer refusal registered with json_error)."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from edge_platform.service.http_errors import ERROR_RESPONSES, install_error_handlers, json_error


class _EnqueueFailed(RuntimeError):
    """A consumer's own refusal type (trade-data's JobEnqueueFailed)."""


def _app() -> FastAPI:
    app = FastAPI(responses=ERROR_RESPONSES)
    install_error_handlers(app)

    @app.exception_handler(_EnqueueFailed)
    async def _refused(_request: Request, exc: _EnqueueFailed):
        return json_error(
            503,
            str(exc),
            error_code="enqueue_failed",
            status="enqueue_failed",
            retry="the job was NOT queued -- nothing is running, so this is safe to retry",
        )

    @app.get("/missing")
    async def missing() -> dict:
        raise HTTPException(404, detail="no such thing", headers={"X-Why": "gone"})

    @app.get("/dict_detail")
    async def dict_detail() -> dict:
        raise HTTPException(409, detail={"reason": "busy", "holder": "job 7"})

    @app.get("/no_body")
    async def no_body() -> dict:
        raise HTTPException(304)

    @app.get("/typed")
    async def typed(n: int) -> dict:
        return {"n": n}

    @app.get("/enqueue")
    async def enqueue() -> dict:
        raise _EnqueueFailed("could not queue deep_history_replay: connection refused")

    @app.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("postgresql://user:secret@host/db exploded")

    return app


def _client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


def test_http_exception_keeps_detail_and_headers() -> None:
    r = _client().get("/missing")
    assert r.status_code == 404 and r.headers["X-Why"] == "gone"
    assert r.json() == {"detail": "no such thing", "error_code": "not_found", "retryable": False}


def test_dict_detail_passes_through_untouched() -> None:
    assert _client().get("/dict_detail").json()["detail"] == {"reason": "busy", "holder": "job 7"}


def test_bodyless_status_stays_bodyless() -> None:
    r = _client().get("/no_body")
    assert r.status_code == 304 and r.content == b""


def test_unknown_route_gets_the_shape_too() -> None:
    r = _client().get("/nope")
    assert r.status_code == 404 and r.json()["error_code"] == "not_found"


def test_validation_error_keeps_fastapis_detail_list() -> None:
    r = _client().get("/typed", params={"n": "x"})
    body = r.json()
    assert r.status_code == 422 and body["error_code"] == "validation_error"
    assert body["retryable"] is False and body["detail"][0]["loc"] == ["query", "n"]


def test_a_consumer_refusal_keeps_its_legacy_keys() -> None:
    body = _client().get("/enqueue").json()
    assert body["status"] == "enqueue_failed" and "NOT queued" in body["retry"]
    assert body["error_code"] == "enqueue_failed" and body["retryable"] is True
    assert "connection refused" in body["detail"]


def test_unhandled_exception_is_json_and_leaks_nothing() -> None:
    r = _client().get("/boom")
    assert r.status_code == 500
    assert r.json() == {"detail": "Internal Server Error", "error_code": "internal", "retryable": None}
    assert "secret" not in r.text


def test_handlers_are_installed_and_openapi_declares_the_body() -> None:
    app = _app()
    for exc in (StarletteHTTPException, Exception):
        assert exc in app.exception_handlers
    op = app.openapi()["paths"]["/missing"]["get"]["responses"]
    assert op["default"]["content"]["application/json"]["schema"]["$ref"].endswith("/ApiError")
