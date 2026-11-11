"""The ONE error body, installed as exception handlers (body model: `envelope.ApiError`).

Lifted from trade-data `service/http_errors.py` (M6 PL2). Before it there was one handler beside
~100 `raise HTTPException` sites, each answering FastAPI's bare `{"detail": ...}`, and an unhandled
exception answered TEXT/PLAIN "Internal Server Error" -- a body an agent or a generated client
cannot parse as JSON at all.

ADDITIVE, never a rename: `detail` keeps FastAPI's key and value exactly, and every body gains
`error_code` + `retryable`. One place, so the next route that raises inherits the shape instead of
having to remember it.

    install_error_handlers(app)                       # HTTPException, validation, unhandled
    @app.exception_handler(MyRefusal)                 # a service's own refusals, same body
    async def _(request, exc):
        return json_error(503, str(exc), error_code="enqueue_failed", status="enqueue_failed")

Checked before writing (integrate-before-write): fastapi-problem / fastapi-problem-details render
RFC 9457 problem+json, which REPLACES `detail` semantics (validation errors move to `errors`) and the
content type -- a breaking change for every existing consumer. FastAPI's own handlers are kept as
the base: the 204/304 no-body rule and the validation encoding are reproduced from them below.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.utils import is_body_allowed_for_status_code
from starlette.exceptions import HTTPException as StarletteHTTPException

from .envelope import ApiError, api_error

#: The OpenAPI half of the one error body: pass as `FastAPI(responses=ERROR_RESPONSES)` (or per
#: router) so every operation declares `default -> ApiError`, and a generated client types its
#: `error` branch from the real body instead of FastAPI's stock `HTTPValidationError` -- which these
#: handlers no longer send (a 422 is ApiError with the validation list under `detail`). FastAPI skips
#: its auto-422 entry when an operation declares `default`, so this one key corrects both.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    "default": {
        "model": ApiError,
        "description": "Error body (edge_platform.service.http_errors): "
        "`detail` as FastAPI sends it, plus `error_code` and `retryable`.",
    },
}


def json_error(
    http_status: int, detail: Any, /, *, headers: Mapping[str, str] | None = None, **extra: Any
) -> JSONResponse:
    """The ONE error body as a response. `extra` carries a route's legacy keys (kept, never renamed;
    positional-only parameters, so a legacy key named `status` or `detail`-adjacent cannot collide)
    and may override `error_code` with a more specific one."""
    return JSONResponse(api_error(http_status, detail, **extra), status_code=http_status, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    """Register the three generic handlers. Starlette routes an `Exception` handler to
    ServerErrorMiddleware, the OUTERMOST layer, so tracing and timeline middleware still see the
    exception first exactly as before, and it is still re-raised after the response (the server log
    keeps it). A service registers its OWN refusal types beside these with `json_error`."""

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> Response:
        headers = getattr(exc, "headers", None)
        if not is_body_allowed_for_status_code(exc.status_code):
            return Response(status_code=exc.status_code, headers=headers)
        return json_error(exc.status_code, exc.detail, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> Response:
        return json_error(422, jsonable_encoder(exc.errors()))

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, _exc: Exception) -> Response:
        # No exception text in the body: it can carry SQL, paths or a credential-bearing URL.
        # The server log has the traceback (ServerErrorMiddleware re-raises after this returns).
        return json_error(500, "Internal Server Error")
