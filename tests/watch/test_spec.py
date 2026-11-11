"""The watchdog contract: the alert-budget law, the consumer's vocabulary (both directions), the
strict registry and the decorator (ported from trade-data tests/test_watchdog_runner.py's registry
half)."""

from __future__ import annotations

import pytest

from edge_platform.watch.spec import Probe, Status, WatchdogSpec, WatchRegistry, WatchVocab

from .rig import VOCAB, returning, spec


def test_duplicate_registration_raises() -> None:
    reg = WatchRegistry(VOCAB)
    reg.register(spec("t-1", returning(Status.OK)))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(spec("t-1", returning(Status.OK)))


def test_p1_severity_is_unrepresentable() -> None:
    # the alert-budget law: a scheduled check must never be able to page p1
    with pytest.raises(ValueError, match="alert-budget"):
        spec("t-1", returning(Status.OK), severity="p1")


def test_push_severity_comes_from_the_vocab_and_an_explicit_one_wins() -> None:
    reg = WatchRegistry(VOCAB)
    assert reg.register(spec("t-2", None, severity="p2")).ntfy_severity == "act_now"
    assert reg.register(spec("t-3", None, severity="p3")).ntfy_severity == "heads_up"
    assert reg.register(spec("t-4", None, ntfy_severity="high")).ntfy_severity == "high"
    bare = WatchRegistry(WatchVocab(owners=frozenset({"service"})))
    assert bare.register(spec("t-5", None)).ntfy_severity == ""  # no map: the pager decides


def test_title_defaults_to_the_id() -> None:
    assert spec("queue-stalled", None).title == "QUEUE STALLED"


def test_an_unknown_owner_is_refused_and_a_known_one_passes() -> None:
    reg = WatchRegistry(VOCAB)
    orphan = WatchdogSpec(id="t-orphan", description="d", owner="nobody", severity="p2", market_gated=False,
                          probe=None)  # fmt: skip
    with pytest.raises(ValueError, match="not a known service"):
        reg.register(orphan)
    assert reg.register(spec("t-owned", None)).owner == "service"


def test_an_empty_owner_list_refuses_everything() -> None:
    with pytest.raises(ValueError, match="not a known service"):
        WatchRegistry(WatchVocab(owners=frozenset())).register(spec("t-1", None))


def test_watches_refs_are_shaped_and_their_kinds_are_the_vocab_s() -> None:
    with pytest.raises(ValueError, match="a tuple, not a bare string"):
        spec("t-1", None, watches="daemon:worker")
    with pytest.raises(ValueError, match="<kind>:<name>"):
        spec("t-1", None, watches=("daemon:",))
    reg = WatchRegistry(VOCAB)
    with pytest.raises(ValueError, match="kind not in"):
        reg.register(spec("t-1", None, watches=("route:/x",)))
    assert reg.register(spec("t-2", None, watches=("daemon:worker",))).watches == ("daemon:worker",)


def test_the_decorator_registers_and_returns_the_probe() -> None:
    reg = WatchRegistry(VOCAB)

    @reg.watchdog(id="t-dec", description="d", owner="worker", severity="p3", market_gated=True, observe_only=True)
    async def probe(session):
        return Probe(Status.OK, "fine")

    s = reg.get("t-dec")
    assert s is not None and s.probe is probe and s.observe_only and s.market_gated
    assert "t-dec" in reg and len(reg) == 1 and list(reg.specs()) == ["t-dec"]


def test_a_malformed_decorated_spec_raises_at_import() -> None:
    reg = WatchRegistry(VOCAB)
    with pytest.raises(ValueError, match="alert-budget"):

        @reg.watchdog(id="t-bad", description="d", owner="service", severity="p1", market_gated=False)
        async def probe(session): ...


def test_empty_is_a_fresh_registry_with_the_same_vocab() -> None:
    reg = WatchRegistry(VOCAB)
    reg.register(spec("t-1", None))
    fresh = reg.empty()
    assert len(fresh) == 0 and fresh.vocab is reg.vocab
    fresh.register(spec("t-1", None))  # its own table: no duplicate


def test_specs_is_a_copy() -> None:
    reg = WatchRegistry(VOCAB)
    reg.register(spec("t-1", None))
    reg.specs().clear()
    assert len(reg) == 1
