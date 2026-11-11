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
        schedules=("flow:t_flow",), note="t",
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
    assert _base(retired="2026-06-23 gone", schedules=("manual",), freshness=None, cadence_h=None).retired
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
