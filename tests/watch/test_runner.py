"""WatchRunner: the dedup policy, deploy survival, probe-failure containment, contention, the market
gate and every injected hook (ported from trade-data tests/test_watchdog_runner.py; the hook tests
are new -- each hook is proven to fire AND to be survivable)."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import inspect
import re
import subprocess
import sys

import pytest

from edge_platform.watch import runner as wr
from edge_platform.watch.runner import WatchRunner, should_page
from edge_platform.watch.spec import Probe, Status

from .rig import Rig, now, returning, spec

run = asyncio.run


# -- should_page: transition-only + re-alert -------------------------------------------------------


def test_first_evidence_of_down_pages() -> None:
    assert should_page(None, Status.DOWN, now()) == (True, True)


def test_transition_into_down_pages() -> None:
    assert should_page({"last_status": "ok", "last_page_at": None}, Status.DOWN, now()) == (True, True)


def test_steady_down_within_realert_stays_quiet() -> None:
    row = {"last_status": "down", "last_page_at": now(9, 0)}
    assert should_page(row, Status.DOWN, now(9, 30)) == (False, False)


def test_steady_down_past_realert_repages() -> None:
    row = {"last_status": "down", "last_page_at": now(3, 0)}
    assert should_page(row, Status.DOWN, now(9, 30)) == (True, False)
    assert should_page(row, Status.DOWN, now(9, 30), realert_h=12.0) == (False, False)


def test_steady_down_with_no_recorded_page_pages() -> None:
    # the burn-slot fix: a failed push leaves last_page_at NULL, so the next tick retries
    assert should_page({"last_status": "down", "last_page_at": None}, Status.DOWN, now()) == (True, False)


def test_recovery_never_pages_but_records_transition() -> None:
    row = {"last_status": "down", "last_page_at": now(9, 0)}
    assert should_page(row, Status.OK, now(9, 30)) == (False, True)


def test_warn_insufficient_unknown_never_page() -> None:
    for status in (Status.WARN, Status.INSUFFICIENT, Status.UNKNOWN):
        assert should_page(None, status, now())[0] is False, status


# -- the sweep ------------------------------------------------------------------------------------


def test_no_duplicate_page_across_a_simulated_restart() -> None:
    rig = Rig()
    rig.register(spec("t-down", returning(Status.DOWN, "it broke")))
    run(rig.runner.run(object(), now=now(9, 30)))
    assert rig.pages == ["t-down"]
    # "deploy": a new runner over the SAME durable store must not re-page the standing failure
    fresh = WatchRunner(rig.registry, store=rig.store, session_day=lambda _n: True, page=rig.runner._page)
    run(fresh.run(object(), now=now(9, 40)))
    assert rig.pages == ["t-down"]
    run(fresh.run(object(), now=now(16, 0)))  # ...but the 6h re-alert still fires
    assert rig.pages == ["t-down", "t-down"]


def test_failed_push_does_not_burn_the_slot() -> None:
    rig = Rig()
    rig.register(spec("t-flaky", returning(Status.DOWN)))
    rig.push_ok = False
    res = run(rig.runner.run(object(), now=now(9, 30)))
    assert res["paged"] == [] and rig.store.rows["t-flaky"]["last_page_at"] is None
    rig.push_ok = True
    run(rig.runner.run(object(), now=now(9, 35)))
    assert rig.pages == ["t-flaky"]  # retried on the very next tick


def test_probe_exception_becomes_unknown_and_does_not_page() -> None:
    rig = Rig()

    async def broken(session):
        raise RuntimeError("repo exploded")

    rig.register(spec("t-broken", broken))
    row = run(rig.runner.run(object(), now=now()))["results"][0]
    assert row["status"] == "unknown" and "repo exploded" in row["detail"]
    assert rig.pages == []


def test_a_spec_with_no_probe_is_unknown_not_a_crash() -> None:
    rig = Rig()
    rig.register(spec("t-none", None))
    row = run(rig.runner.run(object(), now=now()))["results"][0]
    assert row["status"] == "unknown" and "declares no probe" in row["error"]


def test_a_row_another_sweep_holds_is_skipped_not_probed_or_paged() -> None:
    rig = Rig()
    calls: list[str] = []
    rig.register(spec("t-held", returning(Status.DOWN, "it broke", calls)))
    rig.store.rows["t-held"] = {"last_status": "down", "last_transition_at": None, "last_page_at": None}
    rig.store.contended = {"t-held"}
    row = run(rig.runner.run(object(), now=now(10, 10)))["results"][0]
    assert calls == [] and rig.pages == []
    assert row["skipped"] is True and "another sweep" in row["detail"]
    assert row["status"] == "down"  # the STORED verdict, never a fabricated "ok"


def test_run_subset_reprobes_a_contended_spec_read_only() -> None:
    rig = Rig()
    rig.register(spec("t-sub", returning(Status.DOWN, "it broke", verdict="RED")))
    rig.store.rows["t-sub"] = {"last_status": "ok", "last_transition_at": None, "last_page_at": None}
    rig.store.contended = {"t-sub"}
    res = run(rig.runner.run_subset(object(), ["t-sub"], notify=True))
    row = res["results"][0]
    assert res["contended"] == ["t-sub"] and row["contended"] is True
    assert row["status"] == "down" and row["metrics"]["verdict"] == "RED"
    assert rig.pages == [] and rig.store.rows["t-sub"]["last_status"] == "ok"  # the holder's job


def test_claim_runs_before_load_so_a_just_committed_transition_is_not_repaged() -> None:
    rig = Rig()
    rig.register(spec("t-race", returning(Status.DOWN)))
    rig.store.rows["t-race"] = {"last_status": "ok", "last_transition_at": None, "last_page_at": None}

    def holder_commits() -> None:
        rig.store.rows["t-race"].update(last_status="down", last_page_at=now(10, 9))

    rig.store.on_claim = holder_commits
    run(rig.runner.run(object(), now=now(10, 10)))
    assert rig.pages == []
    row = rig.store.rows["t-race"]
    assert row["last_status"] == "down" and row["last_transition_at"] is None
    assert row["last_page_at"] == now(10, 9)


def test_a_read_only_sweep_claims_records_and_reaps_nothing() -> None:
    rig = Rig()
    rig.register(spec("t-read", returning(Status.DOWN)))
    rig.store.rows["retired"] = {"last_status": "down", "last_transition_at": now(9, 0), "last_page_at": None}
    res = run(rig.runner.run(object(), notify=False, now=now(10, 10)))
    assert rig.store.claim_calls == [] and rig.pages == []
    assert res["results"][0]["would_page"] is True and res["results"][0]["paged"] is False
    assert set(rig.store.rows) == {"retired"}, "a read wrote: it must not consume the transition"
    run(rig.runner.run(object(), notify=True, now=now(10, 10)))
    assert rig.store.claim_calls == [["t-read"]] and "retired" not in rig.store.rows


def test_market_gated_spec_skips_off_session() -> None:
    rig = Rig()
    calls: list[str] = []
    rig.register(spec("t-gated", returning(Status.DOWN, "no", calls), market_gated=True))
    rig.session_day = False
    res = run(rig.runner.run(object(), now=now()))
    assert calls == [] and res["results"][0]["skipped"] is True and rig.pages == []
    assert res["session_day"] is False


def test_a_gated_skip_resets_a_stored_down_so_two_truths_cannot_stand() -> None:
    rig = Rig()
    rig.register(spec("t-gated", returning(Status.DOWN), market_gated=True))
    run(rig.runner.run(object(), now=now()))
    assert rig.store.rows["t-gated"]["last_status"] == "down"
    rig.session_day = False
    run(rig.runner.run(object(), notify=False, now=now(10)))
    assert rig.store.rows["t-gated"]["last_status"] == "down", "a read-only skip wrote the state"
    run(rig.runner.run(object(), now=now(10)))
    assert rig.store.rows["t-gated"]["last_status"] == "ok"
    assert rig.store.rows["t-gated"]["last_detail"] == wr.OFF_SESSION


def test_a_raising_market_gate_degrades_loudly_and_still_evaluates() -> None:
    def gate(_now):
        raise RuntimeError("calendar import failed")

    rig = Rig(session_day=gate)
    calls: list[str] = []
    rig.register(spec("t-gated", returning(Status.DOWN, "x", calls), market_gated=True))
    res = run(rig.runner.run(object(), now=now()))
    assert calls == ["x"] and res["session_day"] is True
    assert rig.degraded == ["watchdog_market_gate_failed"]


def test_observe_only_spec_is_evaluated_recorded_but_never_paged() -> None:
    rig = Rig()
    rig.register(spec("t-observed", returning(Status.DOWN, "very broken"), severity="p3", observe_only=True))
    row = run(rig.runner.run(object(), now=now()))["results"][0]
    assert row["status"] == "down" and row["would_page"] is False and row["observe_only"] is True
    assert rig.pages == []
    assert rig.store.rows["t-observed"]["last_status"] == "down"
    assert rig.store.rows["t-observed"]["last_page_at"] is None


def test_a_skipped_row_carries_the_same_keys_as_an_evaluated_one() -> None:
    rig = Rig()
    rig.register(spec("t-skipme", returning(Status.OK), market_gated=True))
    rig.session_day = False
    skipped = run(rig.runner.run(object(), now=now()))["results"][0]
    rig.session_day = True
    evaluated = run(rig.runner.run(object(), now=now()))["results"][0]
    assert set(evaluated) - set(skipped) == set()
    assert skipped["skipped"] is True and skipped["would_page"] is False and skipped["error"] is None


def test_a_full_sweep_reaps_the_state_row_of_a_retired_watchdog() -> None:
    rig = Rig()
    rig.store.rows["retired-one"] = {"last_status": "down", "last_transition_at": now(9, 0), "last_page_at": None}
    rig.register(spec("t-live", returning(Status.OK), severity="p3"))
    run(rig.runner.run(object(), only_ids=["t-live"], now=now(9, 30)))
    assert "retired-one" in rig.store.rows, "a partial sweep owns nothing but its ids"
    run(rig.runner.run(object(), now=now(9, 40)))
    assert "retired-one" not in rig.store.rows and "t-live" in rig.store.rows


def test_an_empty_registry_reaps_nothing() -> None:
    rig = Rig()
    rig.store.rows["kept"] = {"last_status": "ok", "last_transition_at": now(9, 0), "last_page_at": None}
    run(rig.runner.run(object(), now=now()))
    assert "kept" in rig.store.rows, "an import failure is not a retirement of everything"


class TestACrashedProbeSaysWhy:
    def test_the_reason_is_persisted_and_reported(self) -> None:
        rig = Rig()

        async def broken(session):
            raise RuntimeError("repo exploded")

        rig.register(spec("t-why", broken))
        res = run(rig.runner.run(object(), now=now()))
        assert res["results"][0]["error"] == "RuntimeError: repo exploded"
        assert rig.store.rows["t-why"]["last_error"] == "RuntimeError: repo exploded"

    def test_recovery_clears_the_message(self) -> None:
        rig = Rig()
        boom = {"on": True}

        async def flaky(session):
            if boom["on"]:
                raise RuntimeError("blip")
            return Probe(Status.OK, "fine")

        rig.register(spec("t-flaky", flaky))
        run(rig.runner.run(object(), now=now(9, 30)))
        assert rig.store.rows["t-flaky"]["last_error"] == "RuntimeError: blip"
        boom["on"] = False
        res = run(rig.runner.run(object(), now=now(9, 35)))
        assert rig.store.rows["t-flaky"]["last_error"] is None and res["results"][0]["error"] is None

    def test_one_crashed_probe_does_not_brand_the_next(self) -> None:
        rig = Rig()

        async def broken(session):
            raise RuntimeError("only mine")

        rig.register(spec("t-a-broken", broken))
        rig.register(spec("t-b-fine", returning(Status.OK)))
        run(rig.runner.run(object(), now=now()))
        assert rig.store.rows["t-a-broken"]["last_error"] == "RuntimeError: only mine"
        assert rig.store.rows["t-b-fine"]["last_error"] is None


def test_probe_ms_is_measured_into_the_metrics() -> None:
    rig = Rig()
    rig.register(spec("t-ms", returning(Status.OK, n=3)))
    row = run(rig.runner.run(object(), now=now()))["results"][0]
    assert row["metrics"]["n"] == 3 and isinstance(row["metrics"]["probe_ms"], int)
    assert rig.store.rows["t-ms"]["last_metrics"]["probe_ms"] == row["metrics"]["probe_ms"]


# -- the hooks: each one fires, and none can take the sweep down ----------------------------------


def test_a_raising_pager_degrades_and_the_sweep_goes_on() -> None:
    async def page(s, probe):
        raise UnicodeEncodeError("latin-1", "x", 0, 1, "bad title")

    rig = Rig(page=page)
    rig.register(spec("t-a", returning(Status.DOWN)))
    rig.register(spec("t-b", returning(Status.OK)))
    res = run(rig.runner.run(object(), now=now()))
    assert res["paged"] == [] and [r["status"] for r in res["results"]] == ["down", "ok"]
    assert rig.degraded == ["watchdog_page_failed"]
    assert rig.store.rows["t-a"]["last_page_at"] is None  # not burned: the next sweep retries


def test_a_raising_on_degraded_hook_is_swallowed() -> None:
    def boom(reason):
        raise RuntimeError("telemetry down")

    async def page(s, probe):
        raise RuntimeError("pager down")

    rig = Rig(page=page, on_degraded=boom)
    rig.register(spec("t-a", returning(Status.DOWN)))
    assert run(rig.runner.run(object(), now=now()))["results"][0]["paged"] is False


def test_the_sweep_scope_wraps_every_sweep() -> None:
    entered: list[str] = []

    @contextlib.asynccontextmanager
    async def scope():
        entered.append("in")
        yield
        entered.append("out")

    rig = Rig(sweep_scope=scope)
    rig.register(spec("t-a", returning(Status.OK)))
    run(rig.runner.run(object(), now=now()))
    assert entered == ["in", "out"]


def test_load_registrants_runs_before_every_sweep_and_read() -> None:
    """The registry is populated by the runner itself, never by whoever remembered an import."""
    rig = Rig()

    def load() -> None:
        if "t-late" not in rig.registry:
            rig.register(spec("t-late", returning(Status.OK)))

    rig.runner._load = load
    assert [r["id"] for r in run(rig.runner.run(object(), now=now()))["results"]] == ["t-late"]
    fresh = Rig()
    fresh.runner._load = lambda: fresh.register(spec("t-read", returning(Status.OK)))
    assert [r["id"] for r in run(fresh.runner.read_stored(object(), now=now()))["results"]] == ["t-read"]


def test_investigate_is_called_with_the_transition_and_its_subjects_are_stored() -> None:
    seen: list[tuple] = []

    async def investigate(session, wid, probe, at, *, prior_subjects, notify, transitioned):
        seen.append((wid, probe.status, prior_subjects, notify, transitioned))
        return "enqueued: job 1", sorted(probe.subjects)

    rig = Rig(investigate=investigate)

    async def agg(session):
        return Probe(Status.WARN, "2 loops stalled", subjects={"loop-b": "b", "loop-a": "a"})

    rig.register(spec("t-agg", agg))
    row = run(rig.runner.run(object(), now=now()))["results"][0]
    assert row["investigation"] == "enqueued: job 1"
    assert seen == [("t-agg", Status.WARN, [], True, True)]
    assert rig.store.rows["t-agg"]["last_subjects"] == ["loop-a", "loop-b"]
    run(rig.runner.run(object(), now=now(9, 40)))
    assert seen[-1][2] == ["loop-a", "loop-b"] and seen[-1][4] is False  # prior handed back


def test_a_raising_investigate_hook_is_named_degraded_and_survived() -> None:
    async def investigate(session, wid, probe, at, **kw):
        raise RuntimeError("jobs table gone")

    rig = Rig(investigate=investigate)
    rig.register(spec("t-a", returning(Status.DOWN)))
    rig.register(spec("t-b", returning(Status.OK)))
    res = run(rig.runner.run(object(), now=now()))
    assert res["results"][0]["investigation"] == "failed: RuntimeError: jobs table gone"
    assert res["paged"] == ["t-a"] and rig.store.rows["t-b"]["last_status"] == "ok"
    assert rig.degraded == ["watchdog_investigate_failed", "watchdog_investigate_failed"]


def test_without_an_investigate_hook_no_subjects_are_written() -> None:
    rig = Rig()

    async def agg(session):
        return Probe(Status.DOWN, "x", subjects={"s": "s"})

    rig.register(spec("t-agg", agg))
    row = run(rig.runner.run(object(), now=now()))["results"][0]
    assert row["investigation"] is None and rig.store.rows["t-agg"].get("last_subjects") is None


# -- guards on the guard ----------------------------------------------------------------------------


def test_every_store_method_the_runner_calls_exists_on_the_sql_store_and_the_protocol() -> None:
    """The runner calling a method the REAL store does not have passes every faked test and dies on
    the box. Reads the runner's source for `repo.<name>(` and holds each against both."""
    from edge_platform.watch.state import WatchdogStateStore

    called = set(re.findall(r"\brepo\.([a-z_][a-z0-9_]*)\s*\(", inspect.getsource(wr)))
    assert called, "the runner stopped talking to the state store -- this guard is now blind"
    assert not sorted(n for n in called if not hasattr(WatchdogStateStore, n))
    assert not sorted(n for n in called if not hasattr(wr.StateStore, n))
    assert {"record_subjects"} <= {n for n in dir(wr.StateStore) if not n.startswith("_")}


def test_the_runner_and_spec_import_with_nothing_optional() -> None:
    """The plane is stdlib: a consumer without sqlalchemy can declare and run watchdogs."""
    code = (
        "import sys; sys.modules['sqlalchemy'] = None;"
        "import edge_platform.watch.runner, edge_platform.watch.spec, edge_platform.watch.pager;"
        "print('ok')"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-400:]


def test_a_non_datetime_now_is_refused_by_the_sql_store() -> None:
    from edge_platform.watch.state import WatchdogStateStore

    store = WatchdogStateStore(object(), "ops.watchdog_state")
    with pytest.raises(TypeError, match="requires a datetime"):
        run(store.record("x", status="ok", transitioned=True, paged=False, now="2026-01-01"))  # type: ignore[arg-type]


def test_the_sql_store_refuses_an_unqualified_table() -> None:
    from edge_platform.watch.state import WatchdogStateStore, ddl

    for bad in ("watchdog_state", "ops.state; drop"):
        with pytest.raises(ValueError, match=r"schema.table"):
            WatchdogStateStore.bind(bad)
        with pytest.raises(ValueError, match=r"schema.table"):
            ddl(bad)


def test_now_defaults_to_the_injected_clock() -> None:
    rig = Rig(now=lambda: dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.UTC))
    assert run(rig.runner.run(object()))["now"] == "2026-08-11T12:00:00+00:00"
