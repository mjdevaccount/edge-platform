"""outcome_series: totals always present, per-scope filled with zeros, blind is None (ported from
trade-data tests/observability/test_notify_gauge.py)."""

from __future__ import annotations

from edge_platform.notify.ledger import outcome_series
from edge_platform.notify.types import RECORDED


def _by(series):
    return {(labels["scope"], labels["outcome"]): v for labels, v in series}


def test_blind_is_none_for_every_total() -> None:
    assert _by(outcome_series(None)) == {("_all", o): None for o in RECORDED}


def test_an_empty_ledger_renders_zero_totals() -> None:
    assert _by(outcome_series([])) == {("_all", o): 0.0 for o in RECORDED}


def test_scopes_are_filled_and_totalled() -> None:
    got = _by(outcome_series([
        {"scope": "errors", "outcome": "sent", "n": 3},
        {"scope": "errors", "outcome": "budget_refused", "n": 2},
        {"scope": "watchdog", "outcome": "failed", "n": 1},
        {"scope": "quiet", "outcome": "sent", "n": 0},
    ]))  # fmt: skip
    assert got[("_all", "sent")] == 3.0 and got[("_all", "failed")] == 1.0
    assert got[("errors", "failed")] == 0.0
    assert got[("quiet", "sent")] == 0.0  # quiet for 24h, seen in 7d: 0, not absent
    assert len(got) == len(RECORDED) * 4


def test_the_total_label_is_the_consumer_s() -> None:
    assert {labels["scope"] for labels, _ in outcome_series([], all_scope="*")} == {"*"}
