"""build_lifespan: the boot invariant refuses AND allows; teardown is guarded; tasks die before dispose."""

from __future__ import annotations

import asyncio
import inspect

import pytest
from structlog.testing import capture_logs

from edge_platform.service.lifespan import assert_single_worker, build_lifespan


def _run(lifespan, events=None):
    async def go():
        async with lifespan(None):
            await asyncio.sleep(0)  # let loop tasks reach their first await
            if events is not None:
                events.append("serving")

    asyncio.run(go())


def test_single_worker_refuses_two(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(RuntimeError, match=r"single-worker invariant violated.*the pending store"):
        assert_single_worker("the pending store is in-process")


@pytest.mark.parametrize("value", ["1", "", " 1 "])
def test_single_worker_allows_one(monkeypatch, value):
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setenv("UVICORN_WORKERS", value)
    assert_single_worker("x")  # no raise


def test_single_worker_is_a_plain_function():
    # a decorator would stop the body running at a bare call site (trade-data's 2026-08-04 note)
    assert not inspect.isasyncgenfunction(assert_single_worker)
    assert not hasattr(assert_single_worker, "__wrapped__")


def test_boot_refuses_before_startup_log(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "3")

    async def dispose():
        pass

    ls = build_lifespan(version="1", port=lambda: 1, dispose=dispose, single_worker="state")
    with capture_logs() as logs, pytest.raises(RuntimeError):
        _run(ls)
    assert not [e for e in logs if e["event"] == "startup"]


def test_order_and_guarded_teardown(monkeypatch):
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    events: list[str] = []

    async def task():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append("task-cancelled")
            raise

    def bad_hook():
        events.append("bad-hook")
        raise ValueError("stream closed")

    async def good_hook():
        events.append("good-hook")

    async def dispose():
        events.append("dispose")

    ls = build_lifespan(
        version="9.9",
        port=lambda: 8091,
        dispose=dispose,
        warm_imports=("json",),
        boot_checks=(lambda: events.append("check"),),
        startup=(lambda: events.append("startup-hook"),),
        tasks=(task,),
        shutdown=(bad_hook, good_hook),
    )
    with capture_logs() as logs:
        _run(ls, events)
    assert events == ["check", "startup-hook", "serving", "task-cancelled", "bad-hook", "good-hook", "dispose"]
    start = next(e for e in logs if e["event"] == "startup")
    assert start["version"] == "9.9" and start["port"] == 8091
    failed = [e for e in logs if e["event"] == "shutdown_hook_failed"]
    assert len(failed) == 1 and "bad_hook" in failed[0]["hook"] and "stream closed" in failed[0]["error"]
    assert any(e["event"] == "shutdown" for e in logs)


def test_clean_teardown_logs_no_failure():
    async def dispose():
        pass

    ls = build_lifespan(version="1", port=lambda: 1, dispose=dispose, shutdown=(lambda: None,))
    with capture_logs() as logs:
        _run(ls)
    assert not [e for e in logs if e["event"] == "shutdown_hook_failed"]


def test_is_starlette_supported_form():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    hit: list[str] = []

    async def dispose():
        hit.append("dispose")

    app = FastAPI(lifespan=build_lifespan(version="1", port=lambda: 1, dispose=dispose))
    with TestClient(app):
        pass
    assert hit == ["dispose"]
