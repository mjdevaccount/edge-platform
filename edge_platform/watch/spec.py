"""The watchdog contract: absence detectors as registered specs. Pure declaration, stdlib.

Lifted from trade-data `service/platform/watchdog.py` (M6 PL5). The audit there found sixteen
hand-rolled absence detectors, each restating the same skeleton (threshold -> deadline -> page) with
its own market gate, its own dedup (2/16 had any) and its own status vocabulary. The bug this
contract exists against: **a guard that exists somewhere it cannot fire** -- a watchdog whose dedup
memory was erased by every deploy, or whose bespoke wall-clock gate judged a holiday a session, is
indistinguishable from one that works until the day it pages wrong, or does not page at all.

    Status         closed vocabulary: OK / WARN / DOWN / INSUFFICIENT / UNKNOWN. INSUFFICIENT is
                   "not enough evidence to judge" and never pages; UNKNOWN is "the probe failed".
    Probe          one evaluation: (status, detail, metrics, subjects).
    WatchdogSpec   the declaration: id, description, owner, severity (p2|p3 ONLY -- a scheduled
                   check can never page p1, the alert-budget law), market_gated, the probe.
    WatchVocab     THE CONSUMER'S names, passed in once: the owners a page can be traced to, the
                   row kinds a `watches=` ref may name, the push severity each tier defaults to.
                   Empty owners refuse every spec (a page nobody owns is a page nobody acts on).
    WatchRegistry  one consumer's specs, checked against its vocab; a duplicate id raises.

What moved to inputs: trade-data's `KNOWN_OWNERS` (its service-cards names), `WATCHABLE_KINDS` (its
watched-surface row kinds) and the p2->"act_now" / p3->"heads_up" push-severity map (its ntfy
client's vocabulary). The probe runtime is `edge_platform.watch.runner`; the durable dedup memory is
`edge_platform.watch.state`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from edge_platform.registry import Registry

#: The alert-budget law: a scheduled check may page p2 or p3, never p1.
ALLOWED_SEVERITIES: tuple[str, ...] = ("p2", "p3")


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    DOWN = "down"
    INSUFFICIENT = "insufficient"  # not enough evidence to judge -- never pages
    UNKNOWN = "unknown"  # the probe itself failed


#: Only DOWN pages. WARN surfaces in the result but stays out of the phone budget; INSUFFICIENT
#: ranks with OK; UNKNOWN is surfaced loudly but a broken probe must not page as if the thing it
#: watches were down.
PAGEABLE = frozenset({Status.DOWN})
#: The statuses whose stored subjects stand (the state store clears them on any other).
FAILING: tuple[Status, ...] = (Status.WARN, Status.DOWN)

# OBSERVE-ONLY (spec.observe_only=True). The audit's complaint was not that too few things paged --
# it was that nobody could SEE the detector population, so nobody could budget it. An observe-only
# spec is evaluated, recorded and listed like any other, and the runner never pages it. Promoting
# one to a pager is a one-word diff with a reviewable blast radius.


@dataclass(frozen=True)
class Probe:
    """One evaluation's verdict. `detail` is the operator-facing sentence (page body material);
    `metrics` the evidence a page or a listing can cite.

    `subjects` is for an AGGREGATE watchdog (one spec judging many things): failing subject id ->
    that subject's own sentence, so each subject ENTERING the failing set can open its own
    investigation (the runner's `investigate` hook). Left empty, a transition is one event for the
    whole watchdog."""

    status: Status
    detail: str
    metrics: dict[str, Any] = field(default_factory=dict)
    subjects: dict[str, str] = field(default_factory=dict)


ProbeFn = Callable[..., Awaitable[Probe]]


@dataclass(frozen=True)
class WatchdogSpec:
    id: str  # unique; the state table's key
    description: str  # what absence this detects, one operator sentence
    owner: str  # the service a page traces to (checked against WatchVocab.owners)
    severity: str  # p2 | p3 -- NEVER p1 from a scheduled check
    market_gated: bool  # True -> the runner skips it when the market gate says no session
    probe: ProbeFn | None  # async probe(session) -> Probe
    title: str = ""  # page title; defaults to the id upper-cased
    ntfy_severity: str = ""  # push severity; "" -> WatchVocab.push_severity[severity] at register
    tags: tuple[str, ...] = ("rotating_light",)  # ntfy tags on the page
    observe_only: bool = False  # registered for VISIBILITY, never pages
    watches: tuple[str, ...] = ()  # "<kind>:<name>" refs it sees fail (kinds: WatchVocab.watch_kinds)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("a watchdog needs a non-empty id")
        if self.severity not in ALLOWED_SEVERITIES:
            raise ValueError(
                f"watchdog {self.id!r}: severity {self.severity!r} not allowed -- a scheduled "
                f"check may only page {ALLOWED_SEVERITIES} (the alert-budget law)"
            )
        if isinstance(self.watches, str) or any(
            not isinstance(w, str) or not w.partition(":")[0] or not w.partition(":")[2] for w in self.watches
        ):
            raise ValueError(
                f"watchdog {self.id!r}: watches={self.watches!r} -- each ref is '<kind>:<name>' (a tuple, not a bare string)"
            )
        if not self.title:
            object.__setattr__(self, "title", self.id.replace("-", " ").upper())


@dataclass(frozen=True)
class WatchVocab:
    """The consumer's names. `owners` is enforced (empty refuses all); `watch_kinds` is enforced
    for any spec that declares `watches` (empty refuses every ref); `push_severity` fills a spec's
    blank `ntfy_severity` from its tier (a tier missing from it leaves the field blank: the pager
    decides)."""

    owners: frozenset[str]
    watch_kinds: tuple[str, ...] = ()
    push_severity: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "owners", frozenset(self.owners))
        object.__setattr__(self, "watch_kinds", tuple(self.watch_kinds))
        object.__setattr__(self, "push_severity", MappingProxyType(dict(self.push_severity)))

    def check(self, spec: WatchdogSpec) -> None:
        if spec.owner not in self.owners:
            raise ValueError(
                f"watchdog {spec.id!r}: owner {spec.owner!r} is not a known service -- a page nobody "
                f"owns is a page nobody acts on (known: {sorted(self.owners)})"
            )
        bad = [w for w in spec.watches if w.partition(":")[0] not in self.watch_kinds]
        if bad:
            raise ValueError(f"watchdog {spec.id!r}: watches {bad} -- kind not in {self.watch_kinds}")


class WatchRegistry:
    """One consumer's watchdogs. STRICT: a second declaration of an id raises at import -- silent
    replace is how two planes each believe they own a check while neither's version runs."""

    def __init__(self, vocab: WatchVocab) -> None:
        self.vocab = vocab
        self._specs: Registry[WatchdogSpec] = Registry("watchdog id")

    def register(self, spec: WatchdogSpec) -> WatchdogSpec:
        self.vocab.check(spec)
        if not spec.ntfy_severity and spec.severity in self.vocab.push_severity:
            spec = dataclasses.replace(spec, ntfy_severity=self.vocab.push_severity[spec.severity])
        return self._specs.add(spec)

    def watchdog(
        self,
        *,
        id: str,
        description: str,
        owner: str,
        severity: str,
        market_gated: bool,
        watches: tuple[str, ...] = (),
        **opts: Any,
    ) -> Callable[[ProbeFn], ProbeFn]:
        """THE way to declare a watchdog: decorate its probe.

            @watchdogs.watchdog(id="queue-stalled", description="...", owner="worker",
                                severity="p3", market_gated=False, watches=("daemon:worker",))
            async def probe(session) -> Probe: ...

        The spec is built here, so a malformed one raises at import. Keep `id` and `watches`
        LITERALS: tools that never import the module can then read both from source. `opts` = the
        remaining WatchdogSpec fields (title, ntfy_severity, tags, observe_only)."""

        def _wrap(fn: ProbeFn) -> ProbeFn:
            self.register(
                WatchdogSpec(
                    id=id,
                    description=description,
                    owner=owner,
                    severity=severity,
                    market_gated=market_gated,
                    probe=fn,
                    watches=watches,
                    **opts,
                )
            )
            return fn

        return _wrap

    def specs(self) -> dict[str, WatchdogSpec]:
        """A copy, in registration order -- the registry mutates only through register()."""
        return dict(self._specs)

    def get(self, watchdog_id: str) -> WatchdogSpec | None:
        return self._specs.get(watchdog_id)

    def empty(self) -> WatchRegistry:
        """A fresh registry with the same vocabulary -- what a test swaps in for isolation."""
        return WatchRegistry(self.vocab)

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, watchdog_id: object) -> bool:
        return watchdog_id in self._specs
