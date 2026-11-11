"""The /admin/run/* declaration contract (ported from trade-data tests/test_admin_run_contract.py).

The bug replaced is docstrings claiming idempotency with nothing checking them, so these tests make a
CLAIM cost something: an idempotency claim without its reason is refused at declaration time, and a
path resolves to exactly one declaration by the one `kind_of` rule."""

from __future__ import annotations

import pytest

from edge_platform.service.admin_run import DEFAULT_EXPECT_S, AdminRuns, AdminRunSpec, kind_of
from edge_platform.service.timeline import ADMIN_RUN_PREFIX


@pytest.fixture
def runs() -> AdminRuns:
    r = AdminRuns()
    r.declare("card_watchdog", idempotent=True, expect_s=30, note="verified: reads only, writes one verdict row")
    r.declare("fit", idempotent=False)
    return r


def test_idempotency_claim_requires_its_reason() -> None:
    with pytest.raises(ValueError, match="needs its reason"):
        AdminRunSpec(kind="x", idempotent=True)
    # only the safe-sounding claim is gated; a NON-idempotent declaration needs no justification
    assert AdminRunSpec(kind="x", idempotent=False).expect_s == DEFAULT_EXPECT_S


@pytest.mark.parametrize("kind", ["fit/meta", ""])
def test_kind_must_be_one_non_empty_segment(kind: str) -> None:
    with pytest.raises(ValueError, match="path segment"):
        AdminRunSpec(kind=kind, idempotent=False)


def test_expect_s_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        AdminRunSpec(kind="x", idempotent=False, expect_s=0)


def test_spec_for_resolves_sub_paths_to_their_head(runs: AdminRuns) -> None:
    assert runs.spec_for("/admin/run/card_watchdog").kind == "card_watchdog"
    assert runs.spec_for("/admin/run/card_watchdog/extra").kind == "card_watchdog"
    assert runs.spec_for("/admin/run/fit/{model}").kind == "fit"
    assert runs.spec_for("/admin/run/never_declared_kind") is None
    assert runs.spec_for("/ops/health") is None


def test_kind_of_is_the_one_rule() -> None:
    assert kind_of("/admin/run/fit/meta") == "fit"
    assert kind_of("/ops/admin/run/check_watchdogs", "/ops/admin/run/") == "check_watchdogs"
    assert kind_of("/admin/runner") is None
    assert ADMIN_RUN_PREFIX == "/admin/run/"


def test_duplicate_declaration_raises(runs: AdminRuns) -> None:
    with pytest.raises(ValueError, match="already declared"):
        runs.declare("card_watchdog", idempotent=False)


def test_tables_are_per_instance() -> None:
    a, b = AdminRuns(), AdminRuns()
    a.declare("x", idempotent=False)
    assert len(a) == 1 and len(b) == 0 and b.spec_for("/admin/run/x") is None


def test_undeclared_names_the_routes_that_need_a_declaration(runs: AdminRuns) -> None:
    paths = ["/admin/run/card_watchdog", "/admin/run/fit/{model}", "/admin/run/new_job", "/ops/health"]
    assert runs.undeclared(paths) == ["/admin/run/new_job"]
    assert runs.undeclared(paths[:2]) == []  # both directions: a fully declared app passes


def test_registry_is_a_copy(runs: AdminRuns) -> None:
    snap = runs.registry()
    snap.pop("fit")
    assert runs.spec_for("/admin/run/fit") is not None
    assert [s.kind for s in runs] == ["card_watchdog", "fit"]


def test_a_custom_prefix_must_be_slash_delimited() -> None:
    with pytest.raises(ValueError, match="start and end"):
        AdminRuns("/ops/admin/run")
    r = AdminRuns("/ops/admin/run/")
    r.declare("check_watchdogs", idempotent=False)
    assert r.spec_for("/ops/admin/run/check_watchdogs").kind == "check_watchdogs"
    assert r.spec_for("/admin/run/check_watchdogs") is None
