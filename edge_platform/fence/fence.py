"""The decision: "is a change to TIER allowed at T, and until when?"

    d = decide("service", at=now, need=dt.timedelta(minutes=45), loaded=load_sources(...))
    d.allowed / d.until / d.next_open_window / d.source / d.as_json()

A refusal is a DEFERRAL, never a failure: the caller records it and tries the next slot. Exit
codes (cli.py): 0 allow · 1 refuse · 2 could not evaluate, which every caller treats as refuse.

Blind never deadlocks and never ships into a session: with no session table the fence is the
policy's weekday superset, which contains every real window. Without tzdata nothing ET-based can
be computed, so that is exit 2 (the old rth-guard HELD in that case too).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .sessions import ClockBlind, Loaded, Sessions, et, tz_ok

POLICY = "fence-policy.json"


class PolicyError(ValueError):
    """The policy file is unusable: exit 2, never a default."""


@dataclass(frozen=True)
class Tier:
    name: str
    fenced: bool
    before: dt.timedelta
    after: dt.timedelta
    need: dt.timedelta


@dataclass(frozen=True)
class Policy:
    tiers: dict[str, Tier]
    superset_days: frozenset[int]
    superset_start: dt.time
    superset_end: dt.time
    blackouts: tuple[tuple[frozenset[int], dt.time, dt.time, str], ...]
    sha256: str


def _hm(s: str) -> dt.time:
    try:
        h, m = s.split(":")
        return dt.time(int(h), int(m))
    except (ValueError, AttributeError) as e:
        raise PolicyError(f"not an HH:MM time: {s!r}") from e


def load_policy(path: Path | None = None) -> Policy:
    import hashlib

    raw = path.read_bytes() if path else resources.files(__package__).joinpath(POLICY).read_bytes()
    try:
        doc = json.loads(raw)
        if doc.get("schema") != 1:
            raise PolicyError("fence policy: want schema 1")
        dflt = doc["defaults"]
        tiers = {
            n: Tier(
                n,
                bool(t["fenced"]),
                dt.timedelta(minutes=int(t.get("pad_before_min", dflt["pad_before_min"]))),
                dt.timedelta(minutes=int(t.get("pad_after_min", dflt["pad_after_min"]))),
                dt.timedelta(minutes=int(t.get("need_min", dflt["need_min"]))),
            )
            for n, t in doc["tiers"].items()
        }
        sup = doc["superset"]
        blackouts = tuple(
            (frozenset(b.get("weekdays", [1, 2, 3, 4, 5, 6, 7])), _hm(b["start_et"]), _hm(b["end_et"]), str(b["why"]))
            for b in doc["blackouts"]
        )
        return Policy(
            tiers,
            frozenset(sup["weekdays"]),
            _hm(sup["start_et"]),
            _hm(sup["end_et"]),
            blackouts,
            hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        if isinstance(e, PolicyError):
            raise
        raise PolicyError(f"fence policy malformed ({type(e).__name__}: {e})") from e


@dataclass
class Decision:
    allowed: bool
    tier: str
    reason: str
    at: str
    until: str | None  # allowed: when the allowance ends (next window start); None = open-ended
    next_open_window: str | None  # refused: the earliest instant a change could be allowed
    source: str  # db | cache | bundled | superset | override
    horizon: str | None
    policy_sha256: str
    need_min: int
    override: str | None = None
    blind: list[str] = field(default_factory=list)
    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def as_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)


def _z(t: dt.datetime | None) -> str | None:
    return None if t is None else t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- windows: (start, end) pairs as aware UTC instants ------------------------------------------
def _daily(days: frozenset[int], start: dt.time, end: dt.time, around: dt.datetime, span_days: int) -> list:
    """[start, end) ET wall-clock windows on `days` (ISO weekday) from the day before `around`."""
    out = []
    d0 = around.astimezone(et()).date() - dt.timedelta(days=1)
    for i in range(span_days + 2):
        d = d0 + dt.timedelta(days=i)
        if d.isoweekday() in days:
            s = dt.datetime.combine(d, start, et()).astimezone(dt.UTC)
            e = dt.datetime.combine(d, end, et()).astimezone(dt.UTC)
            out.append((s, e if e > s else e + dt.timedelta(days=1)))
    return out


def _session_windows(s: Sessions, tier: Tier) -> list:
    return [(o - tier.before, c + tier.after) for _, o, c in s.rows]


def _first_gap(windows: list, t: dt.datetime, need: dt.timedelta, limit: dt.datetime) -> tuple:
    """(allowed_now, until, next_ok). until = the next window start after t when allowed;
    next_ok = the earliest instant >= t outside every window with `need` free after it."""
    ws = sorted(w for w in windows if w[1] > t - dt.timedelta(days=1))
    cur = t
    for _ in range(len(ws) + 1):
        inside = [e for s, e in ws if s <= cur < e]
        if inside:
            cur = max(inside)
            continue
        nxt = min((s for s, _e in ws if s > cur), default=None)
        if nxt is not None and nxt - cur < need:
            cur = max(e for s, e in ws if s == nxt)
            continue
        if cur > limit:
            raise ClockBlind(f"no allowed window before the table's horizon ({_z(limit)})")
        return (cur == t, nxt if nxt is not None else limit, cur)  # no window left = good to the horizon
    raise ClockBlind("could not find an allowed window")  # pragma: no cover -- loop is bounded


def decide(
    tier: str,
    *,
    at: dt.datetime,
    need: dt.timedelta | None = None,
    loaded: Loaded,
    policy: Policy | None = None,
    override: str | None = None,
) -> Decision:
    """Raises PolicyError (unknown tier, bad policy) and ClockBlind (no tzdata): both are exit 2."""
    pol = policy or load_policy()
    if tier not in pol.tiers:
        raise PolicyError(f"unknown tier {tier!r} (policy has {', '.join(sorted(pol.tiers))})")
    if at.tzinfo is None:
        raise ValueError("the fence needs a tz-aware instant")
    if not tz_ok():
        raise ClockBlind("this host cannot resolve America/New_York (no tzdata): nothing ET-based can be decided")
    t, tr = at.astimezone(dt.UTC), pol.tiers[tier]
    need = tr.need if need is None else need  # the policy min-grant default
    base: dict[str, Any] = {
        "tier": tier,
        "at": _z(t),
        "policy_sha256": pol.sha256,
        "need_min": int(need.total_seconds() // 60),
        "blind": list(loaded.blind),
    }
    if override:
        return Decision(
            True,
            reason=f"override: {override}",
            until=None,
            next_open_window=None,
            source="override",
            horizon=None,
            override=override,
            **base,
        )
    if not tr.fenced:
        return Decision(
            True,
            reason="tier is not fenced",
            until=None,
            next_open_window=None,
            source=loaded.source,
            horizon=None,
            **base,
        )
    span = 21
    blk = [w for days, s, e, _ in pol.blackouts for w in _daily(days, s, e, t, span)]
    s = loaded.sessions
    source, horizon = loaded.source, None
    if s is not None:
        try:
            s.is_trading_day(t)  # validates t against [first, horizon]
            windows = _session_windows(s, tr) + blk
            limit = dt.datetime.combine(s.horizon, dt.time(0), et()).astimezone(dt.UTC)
            horizon = s.horizon.isoformat()
            ok, until, next_ok = _first_gap(windows, t, need, limit)
        except ClockBlind as e:
            base["blind"].append(f"{source}: {e}")
            s, source = None, "superset"
    if s is None:
        windows = _daily(pol.superset_days, pol.superset_start, pol.superset_end, t, span) + blk
        ok, until, next_ok = _first_gap(windows, t, need, t + dt.timedelta(days=span))
    if ok:
        why = "outside every session window" if source != "superset" else "outside the weekday superset (table blind)"
        return Decision(
            True, reason=why, until=_z(until), next_open_window=None, source=source, horizon=horizon, **base
        )
    why = (
        "inside a session window"
        if any(a <= t < b for a, b in windows)
        else f"less than {base['need_min']}m before the next window"
    )
    if any(a <= t < b for a, b in blk):
        why = "inside a blackout"
    return Decision(False, reason=why, until=None, next_open_window=_z(next_ok), source=source, horizon=horizon, **base)
