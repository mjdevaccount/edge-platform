"""Alertmanager's night mute, checked against the policy's night window.

Alertmanager posts to ntfy.sh by webhook and reads no policy, so its p2 night mute (a
`time_intervals` entry used by a route's `mute_time_intervals`) is a hand copy of policy.night.
am_night_findings() fails when the two disagree, minute by minute, and also when the interval
exists but NO route mutes with it -- a window nothing fires is the estate's signature defect.

Needs PyYAML (the one non-stdlib import in this package, kept out of policy.py so the box's bare
python3 can still read the policy).
"""

from __future__ import annotations

from .policy import NtfyPolicy

_DAY = 24 * 60
_EVERY_DAY_KEYS = ("weekdays", "days_of_month", "months", "years")


def _minute(hhmm: str, what: str) -> int:
    h, _, m = str(hhmm).partition(":")
    if not (h.isdigit() and m.isdigit()):
        raise ValueError(f"{what}: {hhmm!r} is not HH:MM")
    v = int(h) * 60 + int(m)
    if not 0 <= v <= _DAY:
        raise ValueError(f"{what}: {hhmm!r} is outside 00:00..24:00")
    return v


def _routes(route: dict):
    yield route
    for child in route.get("routes") or []:
        yield from _routes(child)


def _fmt(minutes: set[int]) -> str:
    """Contiguous HH:MM-HH:MM runs, for a finding a human can read."""
    runs, start, prev = [], None, None
    for m in sorted(minutes):
        if start is None:
            start = prev = m
        elif m == prev + 1:
            prev = m
        else:
            runs.append((start, prev))
            start = prev = m
    if start is not None:
        runs.append((start, prev))
    return ", ".join(f"{a // 60:02d}:{a % 60:02d}-{(b + 1) // 60:02d}:{(b + 1) % 60:02d}" for a, b in runs)


def am_night_findings(am_yaml: str, policy: NtfyPolicy, interval: str = "quiet-night") -> list[str]:
    """Findings (empty = in agreement). Raises ValueError when the file cannot be judged."""
    import yaml

    doc = yaml.safe_load(am_yaml)
    if not isinstance(doc, dict) or not isinstance(doc.get("route"), dict):
        raise ValueError("not an Alertmanager config (no top-level `route`)")
    defs = [*(doc.get("time_intervals") or []), *(doc.get("mute_time_intervals") or [])]
    named = [d for d in defs if isinstance(d, dict) and d.get("name") == interval]
    if len(named) != 1:
        return [f"time interval {interval!r} is defined {len(named)} times, want exactly 1"]
    found, muted = [], set()
    for n, ti in enumerate(named[0].get("time_intervals") or []):
        where = f"{interval}[{n}]"
        for k in _EVERY_DAY_KEYS:
            if ti.get(k):
                found.append(f"{where}: restricted by `{k}`; the policy night applies every day")
        loc = ti.get("location", "UTC")
        if loc != policy.night_tz:
            found.append(f"{where}: location {loc!r}, the policy night is in {policy.night_tz!r}")
        times = ti.get("times")
        if not times:
            muted |= set(range(_DAY))  # no `times` = the whole day, per Alertmanager's docs
        for t in times or []:
            a, b = _minute(t["start_time"], where), _minute(t["end_time"], where)
            muted |= set(range(a, b))  # end_time is exclusive in Alertmanager too
    want = {h * 60 + m for h in policy.night_hours() for m in range(60)}
    if extra := muted - want:
        found.append(f"{interval} mutes {_fmt(extra)} that the policy night does not")
    if missing := want - muted:
        found.append(f"{interval} leaves {_fmt(missing)} loud that the policy night mutes")
    users = [r for r in _routes(doc["route"]) if interval in (r.get("mute_time_intervals") or [])]
    if not users:
        found.append(f"no route lists {interval!r} in mute_time_intervals: the window exists but mutes nothing")
    return found
