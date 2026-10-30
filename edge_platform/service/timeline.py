"""Every `/admin/run/*` request becomes one unit on the shared work timeline (`ops.work`).

`AdminRunTimeline` is the ASGI middleware; WHERE the unit is written is the service's: trade-data writes
`ops.work` in-process (it owns the table), every other service posts to trade-data through the
`work-timeline` package. Both hand the middleware a RECORDER:

    recorder(name, path, method) -> async context manager yielding a unit
    on_status(unit, status_code)  -> mark the unit from the response status

so the status rule stays the writer's (trade-data: >=500 failed, 4xx ok; work-timeline's `mark_http`:
>=400 failed). An exception from the app propagates through the recorder's context manager, which
records it; the middleware never swallows it.

PURE ASGI, not `@app.middleware("http")`: BaseHTTPMiddleware runs the endpoint in a separate task, so a
ContextVar the recorder sets (the current unit) would not be visible inside the handler.

`remote_timeline()` builds the recorder for a remote writer from a work-timeline `TimelineClient`, and
says ONCE at boot whether it is live: a writer that ships without its URL or key is inert by design
(telemetry must never fail the job) and silent unless this line is logged and `.enabled` is exported.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

import structlog

ADMIN_RUN_PREFIX = "/admin/run/"

Recorder = Callable[[str, str, str], AbstractAsyncContextManager[Any]]
OnStatus = Callable[[Any, "int | None"], None]


def unit_name(path: str, prefix: str = ADMIN_RUN_PREFIX) -> str:
    """`/admin/run/pull_ohlcv` -> `pull_ohlcv`; slashes trimmed; an empty remainder is `(root)`."""
    return path[len(prefix) :].strip("/") or "(root)"


class AdminRunTimeline:
    """ASGI middleware: one recorder unit per HTTP request under `prefix`; everything else passes."""

    def __init__(self, app: Any, *, recorder: Recorder, on_status: OnStatus, prefix: str = ADMIN_RUN_PREFIX) -> None:
        self.app = app
        self.recorder = recorder
        self.on_status = on_status
        self.prefix = prefix

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "") if scope.get("type") == "http" else ""
        if not path.startswith(self.prefix):
            await self.app(scope, receive, send)
            return
        status: dict[str, int | None] = {"code": None}

        async def _send(message: Any) -> None:
            if message.get("type") == "http.response.start":
                status["code"] = int(message["status"])
            await send(message)

        async with self.recorder(unit_name(path, self.prefix), path, scope.get("method", "")) as unit:
            await self.app(scope, receive, _send)
            # Called with None too (no response started): the writer decides what an absent status means.
            self.on_status(unit, status["code"])


class TimelineClientLike(Protocol):
    """The slice of `work_timeline.TimelineClient` this module uses (no dependency on the package)."""

    def arecorded(self, name: str) -> AbstractAsyncContextManager[Any]: ...


class RemoteTimeline:
    """A remote writer's recorder + whether it is live. Build it with `remote_timeline()`."""

    def __init__(self, client: TimelineClientLike, enabled: bool) -> None:
        self.client = client
        self.enabled = enabled

    def recorder(self, name: str, _path: str, _method: str) -> AbstractAsyncContextManager[Any]:
        return self.client.arecorded(name)

    @staticmethod
    def on_status(unit: Any, code: int | None) -> None:
        if code is not None:
            unit.mark_http(code)

    def install(self, app: Any, *, prefix: str = ADMIN_RUN_PREFIX) -> None:
        app.add_middleware(AdminRunTimeline, recorder=self.recorder, on_status=self.on_status, prefix=prefix)


def remote_timeline(
    client: TimelineClientLike, *, missing: Sequence[str], target: str, logger: Any = None
) -> RemoteTimeline:
    """Wrap `client`; log `work timeline DISABLED -- missing ...` when `missing` names any setting,
    else `work timeline enabled -> <target>`. `missing` is the service's own env names, so the line
    says exactly what to set."""
    log = logger if logger is not None else structlog.get_logger("service")
    if missing:
        log.warning(
            "work timeline DISABLED -- missing %s; /admin/run/* jobs will not appear on the shared day view",
            ", ".join(missing),
        )
        return RemoteTimeline(client, enabled=False)
    log.info("work timeline enabled -> %s", target)
    return RemoteTimeline(client, enabled=True)
