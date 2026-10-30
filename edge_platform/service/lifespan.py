"""The FastAPI lifespan skeleton every EDGE service shares: boot invariants, startup, guarded teardown.

ORDER (each step is optional except the logs and `dispose`):

    assert_single_worker(reason)  -> import each warm_imports module -> boot_checks
    -> log "startup" -> startup hooks -> start loop tasks
    -> yield
    -> log "shutdown" -> cancel + gather the loop tasks -> shutdown hooks -> dispose()

TEARDOWN IS GUARDED. A shutdown hook that raises is logged as `shutdown_hook_failed` and the next hook
and `dispose()` still run. Unguarded, one raising hook (a log write to a stream already closed during
shutdown) skipped `dispose()` and made Starlette report `lifespan.shutdown.failed`; trade-quant guarded
its one hook by hand and trade-data guarded none. Observation must not break the thing it observes.

Loop tasks are cancelled BEFORE the hooks and `dispose()` on purpose: a task's cancellation path may do a
final write (trade-data's key-usage flush) and needs a live engine for it.

The returned lifespan is an `@asynccontextmanager` function, the form Starlette supports (it warns on a
bare async generator).
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import structlog

Hook = Callable[[], "Awaitable[Any] | Any"]
TaskFactory = Callable[[], Coroutine[Any, Any, Any]]


def assert_single_worker(reason: str) -> None:
    """Refuse to boot under more than one worker (`WEB_CONCURRENCY` / `UVICORN_WORKERS`).

    For a service that keeps state a second worker cannot see. `reason` names that state; it is in the
    error, so the refusal says what would break. A plain function on purpose, never a context manager:
    a decorator would stop the body running at a bare call site with nothing to flag it.
    """
    w = (os.environ.get("WEB_CONCURRENCY") or os.environ.get("UVICORN_WORKERS") or "1").strip()
    if w not in ("", "1"):
        raise RuntimeError(
            f"single-worker invariant violated: WEB_CONCURRENCY/UVICORN_WORKERS={w!r} but {reason} "
            "-- run exactly 1 worker."
        )


async def _call(hook: Hook) -> None:
    result = hook()
    if inspect.isawaitable(result):
        await result


def _name(hook: object) -> str:
    return getattr(hook, "__qualname__", None) or getattr(hook, "__name__", None) or repr(hook)


def build_lifespan(
    *,
    version: str,
    port: Callable[[], int],
    dispose: Callable[[], Awaitable[None]],
    single_worker: str | None = None,
    warm_imports: Sequence[str] = (),
    boot_checks: Sequence[Hook] = (),
    startup: Sequence[Hook] = (),
    tasks: Sequence[TaskFactory] = (),
    shutdown: Sequence[Hook] = (),
    logger: Any = None,
) -> Callable[[Any], AbstractAsyncContextManager[None]]:
    """Compose a service's lifespan. See the module docstring for the order.

    `single_worker` is the reason string for `assert_single_worker` (None = no invariant).
    `warm_imports` are module names imported ONCE on the main thread before any worker thread can race
    a lazy import. `boot_checks` run before the `startup` log, so a failing check fails the boot with no
    misleading "startup" line; `startup` hooks run after it. Hooks may be sync or async.
    """
    log = logger if logger is not None else structlog.get_logger("service")

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        if single_worker is not None:
            assert_single_worker(single_worker)
        for mod in warm_imports:
            importlib.import_module(mod)
        for check in boot_checks:
            await _call(check)
        log.info("startup", version=version, port=port())
        for hook in startup:
            await _call(hook)
        running = [asyncio.create_task(factory()) for factory in tasks]
        try:
            yield
        finally:
            log.info("shutdown")
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            for hook in shutdown:
                try:
                    await _call(hook)
                except Exception as exc:
                    log.warning("shutdown_hook_failed", hook=_name(hook), error=f"{type(exc).__name__}: {exc}")
            await dispose()

    return lifespan
