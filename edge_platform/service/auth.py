"""The shared-secret guard on state-changing requests (extra `[service]`).

Lifted from trade-quant `service/main.py` (itself copied from trade-data's; M6 PL2). Opt-in: an
EMPTY key leaves every route open, so callers keep working until the key is rolled out to them.
Once set, POST / PUT / PATCH / DELETE require the header (`X-API-Key` by default); reads (GET /
HEAD / OPTIONS) are never gated. The KEY and the env var it comes from are the consumer's:

    install_api_key_guard(app, lambda: get_settings().api_key.get_secret_value())

ORDER IS LOAD-BEARING. Starlette runs the LAST-added middleware OUTERMOST. Install this AFTER any
middleware that records requests (edge_platform.service.timeline's AdminRunTimeline), so a rejected
request never reaches it: an unauthenticated POST to /admin/run/x used to file a `failed` job row
named x on a shared timeline -- telemetry must not be a write primitive for unauthenticated callers,
and a refused request did not run.

The comparison is constant-time (`hmac.compare_digest`), and the 401 body is the ONE error body
(`envelope.api_error`: `detail` plus `error_code` / `retryable`).
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable
from typing import Any

from .envelope import api_error

MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DETAIL = "invalid or missing API key"


class ApiKeyGuard:
    """Pure ASGI: refuses a mutating HTTP request whose header is not the key. `key` is resolved per
    request, so a rotated key (or a test's settings reset) is seen without a restart."""

    def __init__(
        self,
        app: Any,
        *,
        key: Callable[[], str],
        header: str = "x-api-key",
        methods: frozenset[str] = MUTATING,
    ) -> None:
        self.app = app
        self._key = key
        self._header = header.lower().encode("latin-1")
        self._methods = methods

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("method") in self._methods:
            want = self._key() or ""
            if want:
                got = next((v for k, v in scope.get("headers", ()) if k == self._header), b"")
                if not hmac.compare_digest(got, want.encode("utf-8")):
                    body = json.dumps(api_error(401, DETAIL)).encode("utf-8")
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 401,
                            "headers": [
                                (b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode("latin-1")),
                            ],
                        }
                    )
                    await send({"type": "http.response.body", "body": body})
                    return
        await self.app(scope, receive, send)


def install_api_key_guard(
    app: Any, key: Callable[[], str], *, header: str = "x-api-key", methods: frozenset[str] = MUTATING
) -> None:
    """Add the guard as the OUTERMOST middleware so far (see the module docstring on order)."""
    app.add_middleware(ApiKeyGuard, key=key, header=header, methods=methods)
