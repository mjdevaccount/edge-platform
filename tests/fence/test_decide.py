"""The decision, on lib-clock's fixture. Section 6 of lib-clock.test.sh with the union OFF:
holidays and half-day afternoons are deploy windows (operator call 1, approved 2026-10-24)."""

from __future__ import annotations

import datetime as dt
import json
from importlib import resources
from pathlib import Path

import pytest

from edge_platform.fence import ClockBlind, Loaded, PolicyError, Sessions, decide, load_policy
from edge_platform.fence import sessions as sess

from .test_sessions import FX, _now, et, z

M = dt.timedelta(minutes=1)


@pytest.fixture
def loaded() -> Loaded:
    d = json.loads(FX.read_text(encoding="utf-8"))
    return Loaded(Sessions(d, now=_now(d)), "cache", [])


BLIND = Loaded(None, "superset", ["cache: no file"])


def test_ordinary_session_refuses_until_the_fence_lifts(loaded: Loaded) -> None:
    d = decide("service", at=et("2025-11-26T10:00"), loaded=loaded)
    assert not d.allowed and d.reason == "inside a session window"
    assert d.next_open_window == "2025-11-26T21:35:00Z"  # 16:35 EST
    assert d.source == "cache" and d.horizon == "2027-03-31"


def test_evening_allows_until_the_next_pre_open(loaded: Loaded) -> None:
    d = decide("service", at=et("2025-11-26T16:40"), loaded=loaded)
    assert d.allowed and d.until == "2025-11-28T14:15:00Z"  # skips Thanksgiving: Fri 09:15 EST


def test_holiday_and_half_day_afternoon_are_open(loaded: Loaded) -> None:
    assert decide("service", at=et("2025-11-27T10:00"), loaded=loaded).allowed
    d = decide("service", at=et("2025-11-28T14:00"), loaded=loaded)
    assert d.allowed and d.until == "2025-12-01T14:15:00Z"
    assert not decide("service", at=et("2025-11-28T13:30"), loaded=loaded).allowed


def test_need_longer_than_the_gap_defers_past_the_next_window(loaded: Loaded) -> None:
    d = decide("service", at=et("2025-12-01T09:00"), need=30 * M, loaded=loaded)
    assert not d.allowed and d.reason.startswith("less than 30m")
    assert d.next_open_window == "2025-12-01T21:35:00Z"
    assert decide("service", at=et("2025-12-01T09:00"), need=15 * M, loaded=loaded).allowed


def test_blind_uses_the_superset(loaded: Loaded) -> None:
    d = decide("service", at=et("2025-11-27T10:00"), loaded=BLIND)  # Thanksgiving, but blind
    assert not d.allowed and d.source == "superset"
    assert d.next_open_window == "2025-11-27T21:35:00Z"
    d = decide("service", at=et("2025-11-29T12:00"), loaded=BLIND)
    assert d.allowed and d.until == "2025-12-01T14:15:00Z" and d.blind == ["cache: no file"]


def test_outside_the_table_falls_to_the_superset(loaded: Loaded) -> None:
    d = decide("service", at=et("2027-06-02T10:00"), loaded=loaded)  # a Wednesday past the horizon
    assert not d.allowed and d.source == "superset"
    assert any("outside the table" in b for b in d.blind)


def test_override_allows_and_says_so(loaded: Loaded) -> None:
    d = decide("service", at=et("2025-11-26T10:00"), loaded=loaded, override="the service is down")
    assert d.allowed and d.source == "override" and d.override == "the service is down"


def test_blackout(tmp_path: Path, loaded: Loaded) -> None:
    raw = json.loads(resources.files(sess.__package__).joinpath("fence-policy.json").read_text(encoding="utf-8"))
    raw["blackouts"] = [{"start_et": "17:20", "end_et": "19:00", "why": "nightly chain"}]
    p = tmp_path / "p.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    pol = load_policy(p)
    d = decide("service", at=et("2025-11-26T17:30"), loaded=loaded, policy=pol)
    assert not d.allowed and d.reason == "inside a blackout" and d.next_open_window == "2025-11-27T00:00:00Z"
    d = decide("service", at=et("2025-11-26T16:40"), loaded=loaded, policy=pol)
    assert d.allowed and d.until == "2025-11-26T22:20:00Z"


def test_every_tier_in_the_spec_is_declared() -> None:
    pol = load_policy()
    assert set(pol.tiers) == {
        "service",
        "quant",
        "ui",
        "grounder",
        "infra",
        "windmill",
        "adhoc",
        "observer",
        "pve",
        "llmbox",
    }
    assert all(t.before == 15 * M and t.after == 35 * M for t in pol.tiers.values())


def test_ui_and_quant_are_open_in_the_session(loaded: Loaded) -> None:
    """Operator decision 2026-10-21 (edge-host lib-deploy.sh deploy_tier_rth_open): ui and quant ship
    inside the session; every other tier is fenced, and an unknown tier is an error, never open."""
    pol = load_policy()
    assert {k for k, t in pol.tiers.items() if not t.fenced} == {"ui", "quant"}
    for tier in ("ui", "quant"):
        d = decide(tier, at=et("2025-11-26T10:00"), loaded=loaded)
        assert d.allowed and d.reason == "tier is not fenced"
    for tier in ("grounder", "adhoc"):
        assert not decide(tier, at=et("2025-11-26T10:00"), loaded=loaded).allowed


def test_unknown_tier_and_bad_policy_are_errors(tmp_path: Path, loaded: Loaded) -> None:
    with pytest.raises(PolicyError, match="unknown tier"):
        decide("nope", at=et("2025-11-26T10:00"), loaded=loaded)
    p = tmp_path / "bad.json"
    p.write_text('{"schema": 1}', encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(p)


def test_no_tzdata_cannot_decide(monkeypatch: pytest.MonkeyPatch, loaded: Loaded) -> None:
    from edge_platform.fence import fence as f

    monkeypatch.setattr(f, "tz_ok", lambda: False)
    with pytest.raises(ClockBlind, match="tzdata"):
        decide("service", at=z("2025-11-29T17:00:00Z"), loaded=loaded)


def test_window_edges_are_half_open(loaded: Loaded) -> None:
    """[open - 15m, close + 35m): 09:15:00 ET is inside, 16:35:00 ET is outside (reviewer 409 item 3)."""
    s = dt.timedelta(seconds=1)
    d = decide("service", at=et("2025-11-26T09:15:00"), loaded=loaded)
    assert not d.allowed and d.reason == "inside a session window"
    d = decide("service", at=et("2025-11-26T09:15:00") - s, loaded=loaded)
    assert d.allowed and d.until == "2025-11-26T14:15:00Z"
    d = decide("service", at=et("2025-11-26T16:35:00"), loaded=loaded)
    assert d.allowed and d.until == "2025-11-28T14:15:00Z"
    d = decide("service", at=et("2025-11-26T16:35:00") - s, loaded=loaded)
    assert not d.allowed and d.next_open_window == "2025-11-26T21:35:00Z"


def test_need_exactly_equal_to_the_gap_is_allowed(loaded: Loaded) -> None:
    """08:30 -> 09:15 is 45m: need 45m fits exactly, 46m does not."""
    d = decide("service", at=et("2025-11-26T08:30:00"), need=45 * M, loaded=loaded)
    assert d.allowed and d.until == "2025-11-26T14:15:00Z"
    d = decide("service", at=et("2025-11-26T08:30:00"), need=46 * M, loaded=loaded)
    assert not d.allowed and d.reason == "less than 46m before the next window"
    assert d.next_open_window == "2025-11-26T21:35:00Z"
