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

WHERE THE KEY MAY ARRIVE (v0.12.0, for the llm box's ops API, which took `Authorization: Bearer` OR
`X-Ops-Token`): `headers=(...)` names every raw header that may carry it, and `bearer=True` also
reads `Authorization: Bearer <key>` (scheme case-insensitive, RFC 7235). Every candidate is
compared, so the time taken does not say which header matched. `exempt_paths` leaves a path open
-- the entry itself and everything under it at a `/` boundary (`"/mcp"` covers `/mcp` and
`/mcp/x`, never `/mcpx`) -- for a sub-app that carries its own auth or a route that is open on
purpose. An exemption is a decision: name it at the call site with its reason.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable, Iterable
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
        headers: Iterable[str] = (),
        bearer: bool = False,
        exempt_paths: Iterable[str] = (),
        methods: frozenset[str] = MUTATING,
    ) -> None:
        self.app = app
        self._key = key
        names = tuple(headers) or (header,)
        self._headers = tuple(h.lower().encode("latin-1") for h in names)
        self._bearer = bearer
        self._exempt = tuple(p.rstrip("/") or "/" for p in exempt_paths)
        self._methods = methods

    def _exempted(self, path: str) -> bool:
        return any(path == p or path.startswith(p if p == "/" else p + "/") for p in self._exempt)

    def _candidates(self, raw: Iterable[tuple[bytes, bytes]]) -> list[bytes]:
        """The first value of each named header, then the Bearer credential, absent ones as b''."""
        got = dict.fromkeys(self._headers, b"")
        auth = b""
        for k, v in raw:
            if k in got and not got[k]:
                got[k] = v.strip()
            elif self._bearer and k == b"authorization" and not auth:
                auth = v
        out = list(got.values())
        if self._bearer:
            scheme, _, cred = auth.strip().partition(b" ")
            out.append(cred.strip() if scheme.lower() == b"bearer" else b"")
        return out

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") in self._methods
            and not (self._exempt and self._exempted(scope.get("path", "")))
        ):
            want = self._key() or ""
            if want:
                w = want.encode("utf-8")
                # a list, not a generator: every candidate is compared, whichever one matches
                hits = [hmac.compare_digest(c, w) for c in self._candidates(scope.get("headers", ()))]
                if not any(hits):
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
    app: Any,
    key: Callable[[], str],
    *,
    header: str = "x-api-key",
    headers: Iterable[str] = (),
    bearer: bool = False,
    exempt_paths: Iterable[str] = (),
    methods: frozenset[str] = MUTATING,
) -> None:
    """Add the guard as the OUTERMOST middleware so far (see the module docstring on order)."""
    app.add_middleware(
        ApiKeyGuard,
        key=key,
        header=header,
        headers=tuple(headers),
        bearer=bearer,
        exempt_paths=tuple(exempt_paths),
        methods=methods,
    )
