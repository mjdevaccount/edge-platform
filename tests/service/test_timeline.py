"""AdminRunTimeline: records /admin/run/* only, hands the writer the status, lets exceptions through;
remote_timeline says once whether it is live."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from edge_platform.service.timeline import AdminRunTimeline, remote_timeline, unit_name


class _Rec:
    def __init__(self):
        self.units: list[dict] = []

    @asynccontextmanager
    async def recorder(self, name, path, method):
        unit = {"name": name, "path": path, "method": method, "status": "open"}
        self.units.append(unit)
        try:
            yield unit
        except BaseException as exc:
            unit["error"] = type(exc).__name__
            raise
        finally:
            unit["closed"] = True

    @staticmethod
    def on_status(unit, code):
        unit["status"] = code


def _app(rec):
    app = FastAPI()

    @app.post("/admin/run/ok_job")
    def ok():
        return {"ok": True}

    @app.post("/admin/run/conflict")
    def conflict():
        raise HTTPException(409)

    @app.post("/admin/run/boom")
    def boom():
        raise RuntimeError("bug")

    @app.get("/healthz")
    def health():
        return {"ok": True}

    app.add_middleware(AdminRunTimeline, recorder=rec.recorder, on_status=rec.on_status)
    return app


def test_unit_name():
    assert unit_name("/admin/run/pull_ohlcv") == "pull_ohlcv"
    assert unit_name("/admin/run/pull_ohlcv/") == "pull_ohlcv"
    assert unit_name("/admin/run/") == "(root)"


def test_records_admin_runs_with_status():
    rec = _Rec()
    c = TestClient(_app(rec))
    assert c.post("/admin/run/ok_job").status_code == 200
    assert c.post("/admin/run/conflict").status_code == 409
    assert [(u["name"], u["status"], u["method"], u.get("closed")) for u in rec.units] == [
        ("ok_job", 200, "POST", True),
        ("conflict", 409, "POST", True),
    ]


def test_other_paths_record_nothing():
    rec = _Rec()
    assert TestClient(_app(rec)).get("/healthz").status_code == 200
    assert rec.units == []


def test_exception_reaches_the_recorder_and_propagates():
    rec = _Rec()
    c = TestClient(_app(rec), raise_server_exceptions=True)
    with pytest.raises(RuntimeError):
        c.post("/admin/run/boom")
    assert rec.units[0]["error"] == "RuntimeError" and rec.units[0]["closed"]


class _Client:
    def __init__(self):
        self.rec = _Rec()

    def arecorded(self, name):
        return self.rec.recorder(name, "", "")


def test_remote_timeline_disabled_is_loud():
    with capture_logs() as logs:
        rt = remote_timeline(_Client(), missing=["TRADE_DATA_API_KEY"], target="http://x")
    assert rt.enabled is False
    msg = [e for e in logs if e["log_level"] == "warning"]
    assert msg and "work timeline DISABLED" in msg[0]["event"]
    assert "TRADE_DATA_API_KEY" in msg[0]["event"]


def test_remote_timeline_enabled_records_via_mark_http():
    client = _Client()
    with capture_logs() as logs:
        rt = remote_timeline(client, missing=[], target="http://td")
    assert rt.enabled is True
    assert not [e for e in logs if e["log_level"] == "warning"]
    marks = []

    class _Unit(dict):
        def mark_http(self, code):
            marks.append(code)

    @asynccontextmanager
    async def arecorded(name):
        yield _Unit(name=name)

    client.arecorded = arecorded
    app = FastAPI()

    @app.post("/admin/run/x")
    def x():
        return {}

    rt.install(app)
    TestClient(app).post("/admin/run/x")
    assert marks == [200]
