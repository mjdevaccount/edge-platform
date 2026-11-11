"""LaneSpec structure is refused at declaration; estate names are refused at registration against the
vocabulary passed in (ported from trade-data tests/test_content_sources.py, the core cases)."""

from __future__ import annotations

import dataclasses

import pytest

from edge_platform.lanes.spec import Home, LaneRegistry, LaneSpec, LaneVocab

VOCAB = LaneVocab(
    homes={
        "trade": Home(daemons=frozenset({"live-projector"}), module_roots=("service", "sources")),
        "pg-batch": Home(
            daemons=frozenset({"signal-ingest"}),
            module_roots=("infra",),
            tables=frozenset({"research.events"}),
            probes=False,
        ),
    },
    default_home="trade",
    timers=frozenset({"corpus-nightly"}),
    decision_inputs=frozenset({"news_pressure"}),
    surfaces=frozenset({"churn"}),
)


def _base(**kw) -> LaneSpec:
    d = dict(
        id="t_src", kind="content", transport="api", entrypoint="sources.t_src",
        domain_tables=("quant.t_tbl",), freshness=("quant.t_tbl", "ts"), cadence_h=1.0,
        schedules=("flow:t_flow",), note="t", heartbeat="t_src",
    )  # fmt: skip
    d.update(kw)
    return LaneSpec(**d)


@pytest.mark.parametrize(
    "bad",
    [
        dict(id="Bad-Id"), dict(entrypoint="not a module"), dict(entrypoint="single"),
        dict(domain_tables=()), dict(domain_tables=("nodot",)), dict(raw_dataset="gmail"),
        dict(freshness=("quant.other", "ts")), dict(freshness=("quant.t_tbl", "no col")), dict(cadence_h=0),
        dict(schedules=()), dict(schedules=("cron:whatever",)), dict(schedules=("producer:notakey",)),
        dict(schedules=("queue:email_vision",)),  # queue: alone names what fills, not the drainer's clock
        dict(producers=("x",)), dict(queues=("Bad Q",)), dict(note="   "),
        dict(freshness_where="source = 'x'; DROP TABLE t"), dict(freshness_where="source = 'x' -- c"),
        dict(freshness_where="source = 'x' /* c */"), dict(freshness=None, freshness_where="source = 'x'"),
        dict(work_unit="pull_x", cadence_h=None), dict(work_unit="Bad Unit"),
        dict(retired="yesterday"),  # not '<date> <why>', and still scheduled
        dict(retired="2026-06-23 gone"),  # dated, but something still runs it
    ],
)  # fmt: skip
def test_a_malformed_spec_is_refused_at_declaration(bad: dict) -> None:
    with pytest.raises(ValueError, match="source"):
        _base(**bad)


def test_the_good_shapes_construct() -> None:
    assert _base(
        retired="2026-06-23 gone", schedules=("manual",), freshness=None, cadence_h=None, heartbeat=None
    ).retired
    assert _base(schedules=("queue:email_vision", "manual")).schedules
    s = _base(freshness_where="source = 'tear_feed'", work_unit="pull_t")
    assert s.freshness_key == "quant.t_tbl WHERE source = 'tear_feed'"


@pytest.mark.parametrize(
    "bad",
    [
        dict(kind="stuff"), dict(transport="carrier_pigeon"), dict(home="nowhere"),
        dict(entrypoint="scripts.t_src"),  # not under the home's module roots
        dict(schedules=("daemon:no-such-service",)), dict(schedules=("timer:no-such-timer",)),
        dict(decision_inputs=("vibes",)), dict(surfaces=("nowhere",)),
        # pg-batch: unreachable by the probe, and only its known tables
        dict(home="pg-batch", entrypoint="infra.x", domain_tables=("research.events",), freshness=("research.events", "ts")),
        dict(home="pg-batch", entrypoint="infra.x", domain_tables=("quant.t_tbl",), freshness=None, cadence_h=None),
        dict(schedules=("daemon:signal-ingest",)),  # a daemon of ANOTHER home
    ],
)  # fmt: skip
def test_an_unknown_estate_name_is_refused_at_registration(bad: dict) -> None:
    spec = _base(**bad)  # structurally fine
    with pytest.raises(ValueError, match="source 't_src'"):
        LaneRegistry(VOCAB).register(spec)


def test_known_estate_names_register_both_directions() -> None:
    reg = LaneRegistry(VOCAB)
    reg.register(_base(schedules=("daemon:live-projector", "timer:corpus-nightly"), decision_inputs=("news_pressure",)))
    pg = _base(
        id="research", home="pg-batch", entrypoint="infra.research", domain_tables=("research.events",),
        freshness=None, cadence_h=None, schedules=("daemon:signal-ingest",),
    )  # fmt: skip
    assert reg.register(pg).home == "pg-batch" and len(reg) == 2


def test_an_empty_vocabulary_refuses_every_reference_to_it() -> None:
    bare = LaneVocab(homes={"main": Home()}, default_home="main")
    reg = LaneRegistry(bare)
    for kw in (dict(schedules=("daemon:x",)), dict(schedules=("timer:x",)), dict(decision_inputs=("x",))):
        with pytest.raises(ValueError):
            reg.register(_base(**kw))
    assert reg.register(_base()).id == "t_src"  # a lane citing nothing estate-specific needs nothing


def test_default_home_must_be_a_home() -> None:
    with pytest.raises(ValueError, match="default_home"):
        LaneVocab(homes={"a": Home()}, default_home="b")


def test_register_is_idempotent_on_identical_and_refuses_a_different_spec() -> None:
    reg = LaneRegistry(VOCAB)
    a = _base()
    assert reg.register(a) is a
    assert reg.register(_base()) is a
    with pytest.raises(ValueError, match="different spec"):
        reg.register(_base(note="different"))


def test_blast_radius_and_table_queries() -> None:
    reg = LaneRegistry(VOCAB)
    reg.register(_base(producers=("narrative.research",), decision_inputs=("news_pressure",), work_unit="pull_t"))
    reg.register(_base(id="other", domain_tables=("quant.t_tbl", "quant.u"), freshness=None, cadence_h=None))
    b = reg.blast_radius("t_src")
    assert b["tables"] == ["quant.t_tbl"] and b["producers"] == ["narrative.research"]
    assert b["on_money_path"] is True and b["home"] == "trade"
    assert reg.sources_of_table("quant.t_tbl") == ("other", "t_src")
    assert reg.declared_tables() == frozenset({"quant.t_tbl", "quant.u"})
    assert [s.id for s in reg.judged_lanes()] == ["t_src"]
    with pytest.raises(KeyError):
        reg.blast_radius("nope")


def test_a_consumer_extends_the_core_by_subclassing() -> None:
    @dataclasses.dataclass(frozen=True)
    class SourceSpec(LaneSpec):
        data_class: str | None = None

        def __post_init__(self) -> None:
            super().__post_init__()
            if self.data_class is None:
                raise ValueError(f"source {self.id!r}: data_class is required")

    with pytest.raises(ValueError, match="data_class"):
        SourceSpec(**dataclasses.asdict(_base()))
    with pytest.raises(ValueError, match="snake_case"):  # the core's checks still run
        SourceSpec(**{**dataclasses.asdict(_base()), "id": "Bad"}, data_class="owned")
    assert LaneRegistry(VOCAB).register(SourceSpec(**dataclasses.asdict(_base()), data_class="owned"))


# --- v0.11 (PL9): due_h, clock, repo, the run-ledger rule ----------------------------------------


class _SessionClock:
    name = "t_session"

    def hours(self, start, end):  # never called here: registration only
        return 0.0


def test_market_gated_is_gone() -> None:
    """The v0.9 boolean said a clock existed, not which, and nothing consumed it: passing it is a
    TypeError now, never a silently ignored field."""
    with pytest.raises(TypeError, match="market_gated"):
        _base(market_gated=True)


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        (dict(due_h=0.0), "due_h must be positive"),
        (dict(cadence_h=1.0, due_h=1.0), "due_h 1 <= cadence_h 1"),  # acceptance 4: due_h <= cadence_h
        (dict(cadence_h=1.0, due_h=0.5), "<= cadence_h"),
        (dict(clock="t_session", cadence_h=1 / 30), "needs due_h"),  # acceptance 4: non-wall without due_h
        (dict(clock="Bad Clock"), "clock"),
        (dict(repo="Not A Repo"), "repo"),
        (dict(work_unit="pull_t", cadence_h=None, due_h=None), "work_unit"),
    ],
)  # fmt: skip
def test_the_due_and_clock_shapes_are_refused(kw: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _base(**kw)


def test_the_due_and_clock_shapes_that_construct() -> None:
    s = _base(cadence_h=1 / 30, due_h=0.25, clock="t_session", repo="other-repo")
    assert (s.due_h, s.clock, s.repo, s.limited, s.judged) == (0.25, "t_session", "other-repo", True, True)
    assert _base(cadence_h=None, due_h=6.0).limited  # an event lane may declare only a due
    assert _base(work_unit="pull_t", cadence_h=None, due_h=6.0).work_unit == "pull_t"
    assert not _base(cadence_h=None).judged and not _base(schedules=("manual",)).judged


def test_an_unregistered_clock_is_refused_at_register_and_a_registered_one_passes() -> None:
    from edge_platform.lanes.freshness import register_clock

    spec = _base(cadence_h=1 / 30, due_h=0.25, clock="t_nobody_registered")
    with pytest.raises(ValueError, match="not registered"):
        LaneRegistry(VOCAB).register(spec)
    register_clock(_SessionClock())
    ok = _base(cadence_h=1 / 30, due_h=0.25, clock="t_session")
    assert LaneRegistry(VOCAB).register(ok) is ok


def test_a_judged_lane_without_a_run_ledger_is_refused_both_ways() -> None:
    """Invariant 4: STOPPED needs a run ledger. Refused: freshness + limit + a schedule, no work_unit,
    no heartbeat. Passes: either ledger, transport='computed', a manual lane, an event lane."""
    with pytest.raises(ValueError, match="run ledger"):
        LaneRegistry(VOCAB).register(_base(heartbeat=None))
    for good in (
        _base(heartbeat=None, work_unit="pull_t"), _base(), _base(heartbeat=None, transport="computed"),
        _base(heartbeat=None, schedules=("manual",)), _base(heartbeat=None, cadence_h=None),
    ):  # fmt: skip
        assert LaneRegistry(VOCAB).register(good) is good
