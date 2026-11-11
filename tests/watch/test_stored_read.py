"""WatchRunner.read_stored -- the verdict a listing serves without probing (ported from trade-data
tests/test_watchdog_stored_read.py). The real sweep writes into the state double and the stored row
is checked against the row the sweep itself returned, not a hand-built dict."""

from __future__ import annotations

import asyncio
import datetime as dt

from edge_platform.watch.runner import STALE_AFTER_S
from edge_platform.watch.spec import Status

from .rig import ET, Rig, now, returning, spec

run = asyncio.run


def test_the_stored_read_serves_what_the_sweep_recorded_and_probes_nothing() -> None:
    rig = Rig()
    calls: list[str] = []
    rig.register(spec("t-down", returning(Status.DOWN, "3 stuck", calls, n=3)))
    rig.register(spec("t-ok", returning(Status.OK, "all clear", calls)))
    swept = {r["id"]: r for r in run(rig.runner.run(object(), now=now(9, 10)))["results"]}
    assert len(calls) == 2
    out = run(rig.runner.read_stored(object(), now=now(9, 40)))
    assert len(calls) == 2, "a stored read ran a probe"
    got = {r["id"]: r for r in out["results"]}
    for wid in ("t-down", "t-ok"):
        for key in ("status", "detail", "metrics", "severity", "owner", "observe_only"):
            assert got[wid][key] == swept[wid][key], (wid, key)
        assert set(swept[wid]) <= set(got[wid])
    assert got["t-down"]["metrics"]["n"] == 3 and "probe_ms" in got["t-down"]["metrics"]
    assert got["t-down"]["paged"] is True and got["t-down"]["would_page"] is False  # paged 30m ago
    assert out["source"] == "stored" and out["stale"] == []
    assert got["t-ok"]["age_s"] == 30 * 60 and got["t-ok"]["stale"] is False


def test_a_watchdog_no_sweep_recorded_is_unknown_and_stale_never_ok() -> None:
    rig = Rig()
    rig.register(spec("t-new", returning(Status.OK)))
    out = run(rig.runner.read_stored(object(), now=now(9, 40)))
    row = out["results"][0]
    assert row["status"] == "unknown" and row["stale"] is True and row["age_s"] is None
    assert "not yet evaluated" in row["detail"] and out["stale"] == ["t-new"]


def test_a_verdict_older_than_two_sweeps_is_named_stale() -> None:
    rig = Rig()
    rig.register(spec("t-old", returning(Status.OK, "fine")))
    run(rig.runner.run(object(), now=now(9, 10)))
    at = rig.store.rows["t-old"]["updated_at"]
    assert run(rig.runner.read_stored(object(), now=at + dt.timedelta(seconds=STALE_AFTER_S)))["stale"] == []
    late = run(rig.runner.read_stored(object(), now=at + dt.timedelta(seconds=STALE_AFTER_S + 1)))
    assert late["stale"] == ["t-old"] and late["results"][0]["status"] == "ok"


def test_the_staleness_bound_is_the_consumer_s() -> None:
    rig = Rig(stale_after_s=60)
    rig.register(spec("t-old", returning(Status.OK)))
    run(rig.runner.run(object(), now=now(9, 10)))
    out = run(rig.runner.read_stored(object(), now=now(9, 12)))
    assert out["stale"] == ["t-old"] and out["stale_after_s"] == 60


def test_an_off_session_gated_spec_is_the_skip_row_and_not_stale() -> None:
    rig = Rig()
    rig.register(spec("t-gated", returning(Status.OK), market_gated=True))
    rig.session_day = False
    out = run(rig.runner.read_stored(object(), now=now(9, 40)))
    row = out["results"][0]
    assert row["skipped"] is True and row["status"] == "idle" and row["stale"] is False and out["stale"] == []


def test_an_observe_only_down_never_says_it_would_page() -> None:
    rig = Rig()
    rig.register(spec("t-obs", returning(Status.DOWN, "bad"), observe_only=True))
    run(rig.runner.run(object(), now=now(9, 10)))
    rig.store.rows["t-obs"]["last_page_at"] = None
    row = run(rig.runner.read_stored(object(), now=now(9, 40)))["results"][0]
    assert row["status"] == "down" and row["would_page"] is False and row["paged"] is False


def test_a_row_with_no_stored_detail_says_so() -> None:
    rig = Rig()
    rig.register(spec("t-pre", returning(Status.WARN)))
    run(rig.runner.run(object(), now=now(9, 10)))
    rig.store.rows["t-pre"].update(last_detail=None, last_metrics=None)
    row = run(rig.runner.read_stored(object(), now=now(9, 40)))["results"][0]
    assert row["status"] == "warn" and row["metrics"] == {}
    assert "before verdict details were stored" in row["detail"]


def test_a_status_this_build_does_not_know_is_shown_never_paged() -> None:
    rig = Rig()
    rig.register(spec("t-odd", returning(Status.OK)))
    run(rig.runner.run(object(), now=now(9, 10)))
    rig.store.rows["t-odd"]["last_status"] = "purple"
    row = run(rig.runner.read_stored(object(), now=now(9, 20)))["results"][0]
    assert row["status"] == "purple" and row["would_page"] is False


def test_a_gated_verdict_is_not_stale_at_the_start_of_the_next_session_day() -> None:
    # Gated rows get no write off-session, so Monday 00:05 sees Friday's row. Its age counts from
    # Monday's midnight (in the clock's zone): fresh until the sweeps miss, then stale like any row.
    rig = Rig(session_day=lambda n: n.weekday() < 5)
    rig.register(spec("t-gated", returning(Status.OK, "fine"), market_gated=True))
    run(rig.runner.run(object(), now=dt.datetime(2026, 8, 7, 15, 10, tzinfo=ET)))  # Fri
    monday = dt.datetime(2026, 8, 10, 0, 5, tzinfo=ET)
    early = run(rig.runner.read_stored(object(), now=monday))
    assert early["session_day"] is True and early["stale"] == []
    assert early["results"][0]["age_s"] > 2 * 24 * 3600  # the true age is still reported
    late = run(rig.runner.read_stored(object(), now=monday.replace(minute=0) + dt.timedelta(seconds=STALE_AFTER_S + 1)))
    assert late["stale"] == ["t-gated"]


def test_an_injected_day_start_is_used() -> None:
    rig = Rig(session_day=lambda n: True, day_start=lambda n: n - dt.timedelta(minutes=1))
    rig.register(spec("t-gated", returning(Status.OK), market_gated=True))
    run(rig.runner.run(object(), now=dt.datetime(2026, 8, 7, 15, 10, tzinfo=ET)))
    out = run(rig.runner.read_stored(object(), now=dt.datetime(2026, 8, 10, 0, 5, tzinfo=ET)))
    assert out["stale"] == []
