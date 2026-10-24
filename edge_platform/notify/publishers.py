"""The declared sender registry (ntfy-publishers.json): WHO may spend the home IP's ntfy.sh quota.

A separate file from the policy on purpose: the policy's bytes are sha256-pinned by every
consumer's cases file, and a new sender must not move that pin. Stdlib only, like policy.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .policy import NtfyPolicy, packaged

REGISTRY_PATH = packaged("ntfy-publishers.json")
ROUTES = frozenset({"library", "direct", "am-webhook", "gatus", "ci"})
MODES = frozenset({"gated", "counted", "exempt", "uncounted"})
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_PUBLISHER = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class Sender:
    name: str
    repo: str
    where: tuple[str, ...]
    route: str
    mode: str
    publishers: frozenset[str]
    why: str
    owed: str = ""
    counted_by: str = ""


def _text(entry: dict, key: str, who: str) -> str:
    v = entry.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"sender {who}: `{key}` is missing or empty")
    return v


def parse(raw: dict) -> tuple[Sender, ...]:
    """The registry, validated. Raises ValueError naming the first thing wrong."""
    if raw.get("schema") != 1:
        raise ValueError(f"schema {raw.get('schema')!r}, this reader knows 1")
    out, seen_names, seen_pubs = [], set(), set()
    for e in raw["senders"]:
        name = e.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError(f"sender name {name!r} is not [a-z0-9-]")
        if name in seen_names:
            raise ValueError(f"sender {name}: declared twice")
        seen_names.add(name)
        route, mode = e.get("route"), e.get("mode")
        if route not in ROUTES:
            raise ValueError(f"sender {name}: route {route!r} not in {sorted(ROUTES)}")
        if mode not in MODES:
            raise ValueError(f"sender {name}: mode {mode!r} not in {sorted(MODES)}")
        where = e.get("where")
        if not isinstance(where, list) or not where or not all(isinstance(w, str) and w for w in where):
            raise ValueError(f"sender {name}: `where` must be a non-empty list of paths")
        pubs = e.get("publishers", [])
        if not isinstance(pubs, list) or not all(isinstance(p, str) and _PUBLISHER.fullmatch(p) for p in pubs):
            raise ValueError(f"sender {name}: `publishers` must be a list of safe ledger names")
        dup = seen_pubs & set(pubs)
        if dup:
            raise ValueError(f"sender {name}: publisher(s) {sorted(dup)} already declared by another sender")
        seen_pubs |= set(pubs)
        owed = e.get("owed", "")
        if mode == "uncounted":
            owed = _text(e, "owed", name)  # a known hole is allowed only with what closes it
        elif owed:
            raise ValueError(f"sender {name}: `owed` on a {mode} sender -- the gap is closed, drop it")
        counted_by = _text(e, "counted_by", name) if mode == "counted" else ""
        out.append(
            Sender(
                name=name,
                repo=_text(e, "repo", name),
                where=tuple(where),
                route=route,
                mode=mode,
                publishers=frozenset(pubs),
                why=_text(e, "why", name),
                owed=owed,
                counted_by=counted_by,
            )
        )
    return tuple(out)


def load(path: Path = REGISTRY_PATH) -> tuple[Sender, ...]:
    return parse(json.loads(path.read_text(encoding="utf-8")))


def policy_findings(senders: tuple[Sender, ...], policy: NtfyPolicy) -> list[str]:
    """An exemption in the policy names a publisher; that publisher must be a declared sender's."""
    declared = set().union(*(s.publishers for s in senders))
    return [
        f"policy exempts publisher {p!r} from {what}, but no sender in the registry declares it"
        for what, names in (("the cap", policy.cap_exempt), ("the reserve", policy.reserve_exempt))
        for p in sorted(names - declared)
    ]


def uncounted(senders: tuple[Sender, ...]) -> list[Sender]:
    return [s for s in senders if s.mode == "uncounted"]
