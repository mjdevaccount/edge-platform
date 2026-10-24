"""Alertmanager's night mute, checked against the policy's night window.

Alertmanager posts to ntfy.sh by webhook and reads no policy, so its p2 night mute (a
`time_intervals` entry used by a route's `mute_time_intervals`) is a hand copy of policy.night.
am_night_findings() fails when the two disagree, minute by minute -- and, because a window
nothing fires is the estate's signature defect, it also ROUTES synthetic alerts through the tree
the way Alertmanager does and fails unless a p2 alert (and an unlabelled one, which is p2) lands
on a route that mutes with exactly that interval, and a p1 alert lands on one that never mutes.

Routing model (prometheus/alertmanager dispatch/route.go): the root matches everything; children
are tried in order, the first match wins unless it sets `continue: true`; a node no child
matches is itself the landing route. A missing label reads as "" and regexes are anchored.
`mute_time_intervals` / `active_time_intervals` are NOT inherited from a parent, and the root may
not carry them -- so only the landing route's own lists are judged.

Needs PyYAML (the one non-stdlib import in this package, kept out of policy.py so the box's bare
python3 can still read the policy).
"""

from __future__ import annotations

import re

from .policy import NtfyPolicy

_DAY = 24 * 60
_EVERY_DAY_KEYS = ("weekdays", "days_of_month", "months", "years")
_MATCHER = re.compile(
    r'^\s*\{?\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*(?:"((?:[^"\\]|\\.)*)"|([^",{}]*?))\s*\}?\s*$'
)
# The alerts routed for the judgement. Unlabelled = p2 by the estate's tier rule (no tier is never p1).
PROBES = {
    "p2": {"alertname": "EdgeNotifyProbeP2", "tier": "p2"},
    "unlabelled": {"alertname": "EdgeNotifyProbeUnlabelled"},
    "p1": {"alertname": "EdgeNotifyProbeP1", "tier": "p1"},
}


def _minute(hhmm: str, what: str) -> int:
    h, _, m = str(hhmm).partition(":")
    if not (h.isdigit() and m.isdigit()):
        raise ValueError(f"{what}: {hhmm!r} is not HH:MM (quote it: YAML reads a bare 22:00 as a number)")
    v = int(h) * 60 + int(m)
    if not 0 <= v <= _DAY:
        raise ValueError(f"{what}: {hhmm!r} is outside 00:00..24:00")
    return v


def _matchers(route: dict) -> list[tuple[str, str, str]]:
    """(label, op, value) for every matcher on a route: `matchers`, plus legacy `match` / `match_re`."""
    out = []
    for raw in route.get("matchers") or []:
        m = _MATCHER.match(str(raw))
        if not m:
            raise ValueError(f"matcher {raw!r} is not label<op>value -- cannot route it")
        value = m.group(3) if m.group(3) is not None else m.group(4)
        out.append((m.group(1), m.group(2), value.replace('\\"', '"')))
    out += [(k, "=", str(v)) for k, v in (route.get("match") or {}).items()]
    out += [(k, "=~", str(v)) for k, v in (route.get("match_re") or {}).items()]
    return out


def _matches(route: dict, labels: dict[str, str]) -> bool:
    for name, op, value in _matchers(route):
        got = labels.get(name, "")
        hit = got == value if op in ("=", "!=") else re.fullmatch(value, got) is not None
        if hit == (op in ("!=", "!~")):
            return False
    return True


def landing_routes(route: dict, labels: dict[str, str]) -> list[dict]:
    """The route(s) an alert with these labels is delivered by, in Alertmanager's order."""
    out = []
    for child in route.get("routes") or []:
        if _matches(child, labels):
            out += landing_routes(child, labels)
            if not child.get("continue", False):
                break
    return out or [route]


def _all(route: dict):
    yield route
    for child in route.get("routes") or []:
        yield from _all(child)


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


def _window_findings(named: dict, policy: NtfyPolicy, interval: str) -> list[str]:
    found, muted = [], set()
    for n, ti in enumerate(named.get("time_intervals") or []):
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
    return found


def _variants(root: dict) -> list[tuple[str, str]]:
    """(label, value) pairs the tree routes on, beyond tier/alertname: a probe is also routed
    carrying each one, so an unmuted sibling keyed on e.g. severity="warning" cannot hide.
    alertname is left out on purpose -- a route for ONE named alert (the Watchdog deadman) is a
    deliberate per-alert decision, not a tier rule."""
    out = []
    for r in _all(root):
        for name, op, value in _matchers(r):
            if name in ("tier", "alertname") or op not in ("=", "=~"):
                continue
            literals = [value] if op == "=" else value.split("|") if _LITERALS.fullmatch(value) else []
            out += [(name, v) for v in literals if (name, v) not in out]
    return out


_LITERALS = re.compile(r"[A-Za-z0-9_.:-]+(\|[A-Za-z0-9_.:-]+)*")


def _route_findings(root: dict, interval: str) -> list[str]:
    found = []
    for r in _all(root):
        for key in ("mute_time_intervals", "active_time_intervals"):
            if key in r and not isinstance(r[key], list):
                found.append(f"a route's {key} is {r[key]!r}, not a list (Alertmanager refuses the file)")
    if root.get("mute_time_intervals") or root.get("active_time_intervals"):
        found.append("the root route carries time intervals (Alertmanager refuses the file)")
    want = [interval]
    for probe, labels, tag in _probes(root):
        for r in landing_routes(root, labels):
            where = f"a {probe} alert{tag} lands on receiver {r.get('receiver')!r}"
            mutes, active = r.get("mute_time_intervals") or [], r.get("active_time_intervals")
            if probe == "p1":
                if mutes or active:
                    found.append(f"{where}, which is time-gated: p1 must always wake")
                continue
            if mutes != want:
                found.append(f"{where}, which mutes with {mutes!r}, want exactly {want!r}")
            if active:
                found.append(f"{where}, which is only active in {active!r}")
    return list(dict.fromkeys(found))


def _probes(root: dict):
    for probe, labels in PROBES.items():
        yield probe, labels, ""
        for name, value in _variants(root):
            yield probe, {**labels, name: value}, f" with {name}={value!r}"


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
    return _window_findings(named[0], policy, interval) + _route_findings(doc["route"], interval)
