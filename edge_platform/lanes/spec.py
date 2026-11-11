"""LaneSpec -- an ingest lane (content source) as DECLARATION, and the registry that checks it.

Lifted from trade-data `service/platform/content_sources.py` (M6 PL4): the SourceSpec CORE. One
place a lane is declared -- identity, transport, the tables it writes, the column that proves it
moved, what schedules it -- so "what dies if this source goes" is a query, not a grep, and every
reader of a lane's freshness (the API, the source gate, the connectivity watchdog) derives from ONE
declaration instead of re-typing it.

STRUCTURE vs VOCABULARY. A spec checks its own shape on construction (snake_case ids, schema.table
names, a freshness column on one of its own tables, a predicate alphabet). Whether a NAME it cites
exists -- a compose daemon, a box timer, a decision gate, a UI surface, a home database -- is the
consumer's estate, so the name lists are passed in once as a `LaneVocab` and checked when a spec is
registered. An empty list refuses every reference to it: an unknown name is a finding, never a pass.
A consumer extends the core by subclassing (trade-data adds its rights tier, sighting slugs, rag
sources) and its subclass's `__post_init__` calls `super().__post_init__()`.

No DB, no clock, no I/O. Stdlib only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from edge_platform.registry import Registry

KINDS: tuple[str, ...] = ("content", "data")
#: How the bytes arrive. `computed` = derived from a library or another table with no external call;
#: `web` = a budget-capped web search; `queue` = fed by a queue another source fills.
TRANSPORTS: tuple[str, ...] = ("gmail_label", "gmail_query", "dlt", "scrape", "api", "computed", "web", "queue")
#: flow:<name> (a scheduler flow) · producer:<feature.action> (an LLM work market) · daemon:<service>
#: (a long-running container) · timer:<name> (a host timer) · queue:<name> (drained from a queue by a
#: consumer another schedule drives -- so queue: ALONE is refused) · manual.
SCHEDULE_PREFIXES: tuple[str, ...] = ("flow:", "producer:", "daemon:", "timer:", "queue:")

_ID_RX = re.compile(r"^[a-z][a-z0-9_]*$")
_TABLE_RX = re.compile(r"^[a-z_]+\.[a-z_0-9]+$")
_COL_RX = re.compile(r"^[a-z_][a-z_0-9]*$")
_KEY_RX = re.compile(r"^[a-z_]+\.[a-z_]+$")  # feature.action
_QUEUE_RX = re.compile(r"^[a-z_]+$")
_MODULE_RX = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+$")
_RAW_RX = re.compile(r"^raw_[a-z_]+$")
_RETIRED_RX = re.compile(r"^\d{4}-\d{2}-\d{2} \S.*$")  # "<date> <PR or reason>"
#: a freshness_where predicate is interpolated raw into a probe; it is a literal from the registrant
#: module, never input, and this is the whole alphabet it may use -- no statement separator, no
#: comment opener, nothing that reaches past one predicate.
_WHERE_RX = re.compile(r"^[A-Za-z_0-9 .'=<>!%()]+$")


@dataclass(frozen=True)
class LaneSpec:
    """One lane. Every field is a CLAIM a consumer's comparer can check against code or migrations.

    entrypoint       dotted module of the ingest
    domain_tables    schema.table it WRITES (others may share the table; `sources_of_table` lists lanes)
    freshness        (table, ts_col) the output-freshness probe reads -- the honest heartbeat: the only
                     test of a producer is whether its OUTPUT moved
    freshness_where  a predicate (a literal, never input) scoping the probe to THIS lane's rows of a
                     shared table -- without it a shared table's freshness is some other writer's
    cadence_h        how often a healthy lane moves that column, sized so a Monday holiday does not trip
                     it; None = event-driven, NEVER judged stale (a permanent warn carries nothing)
    raw_dataset      the dlt raw schema it lands in (raw_<name>), or None for a direct writer
    producers        work-market keys (feature.action) whose worklist comes from this lane
    queues           queues it FILLS
    schedules        what fires it (SCHEDULE_PREFIXES vocabulary, or "manual")
    decision_inputs  live decision gates it feeds (`LaneVocab.decision_inputs`): on the money path, its
                     absence must land as UNKNOWN there, never as a quiet day
    surfaces         UI surfaces that render it (`LaneVocab.surfaces`)
    heartbeat        the heartbeat-table source name it beats, or None
    work_unit        the run-ledger unit name its runs record under; declaring it puts the lane on the
                     connectivity verdict (RUN vs OUTPUT, cadence_h as its max silence). Only where the
                     freshness column moves on EVERY healthy run, or it pages every quiet week
    market_gated     the runner ticks only while the market is open, so weekend silence is health
    home             the database its tables live in (`LaneVocab.homes`; None = the vocab's default)
    retired          "<date> <why>" when the writer was DELETED and its tables are frozen
    watched_by       outside watchers no field implies ("alert:<rule>", "sweep:<check>")
    """

    id: str
    kind: str
    transport: str
    entrypoint: str
    domain_tables: tuple[str, ...]
    freshness: tuple[str, str] | None
    cadence_h: float | None
    schedules: tuple[str, ...]
    note: str
    raw_dataset: str | None = None
    producers: tuple[str, ...] = ()
    queues: tuple[str, ...] = ()
    decision_inputs: tuple[str, ...] = ()
    surfaces: tuple[str, ...] = ()
    heartbeat: str | None = None
    freshness_where: str | None = None
    enabled_default: bool = field(default=True)
    work_unit: str | None = None
    market_gated: bool = False
    home: str | None = None
    retired: str | None = None
    watched_by: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self._validate_identity()
        self._validate_tables()
        self._validate_schedules()
        self._validate_links()
        self._validate_retired()

    def _validate_identity(self) -> None:
        if not _ID_RX.match(self.id):
            raise ValueError(f"source id {self.id!r} must be snake_case")
        if not _MODULE_RX.match(self.entrypoint):
            raise ValueError(f"source {self.id!r}: entrypoint {self.entrypoint!r} must be a dotted module path")
        if not self.note.strip():
            raise ValueError(f"source {self.id!r}: a note is required -- say what it is in one sentence")

    def _validate_tables(self) -> None:
        if not self.domain_tables:
            raise ValueError(f"source {self.id!r}: a source with no domain_tables writes nothing -- what is it?")
        for t in self.domain_tables:
            if not _TABLE_RX.match(t):
                raise ValueError(f"source {self.id!r}: domain table {t!r} must be schema.table")
        if self.raw_dataset is not None and not _RAW_RX.match(self.raw_dataset):
            raise ValueError(f"source {self.id!r}: raw_dataset {self.raw_dataset!r} must be raw_<name>")
        if self.freshness is not None:
            t, c = self.freshness
            if not _TABLE_RX.match(t) or not _COL_RX.match(c):
                raise ValueError(
                    f"source {self.id!r}: freshness must be (schema.table, ts_col), got {self.freshness!r}"
                )
            if t not in self.domain_tables:
                raise ValueError(f"source {self.id!r}: freshness table {t!r} is not one of its domain_tables")
        if self.cadence_h is not None and self.cadence_h <= 0:
            raise ValueError(f"source {self.id!r}: cadence_h must be positive")
        if self.work_unit is not None and (
            not _ID_RX.match(self.work_unit) or self.freshness is None or self.cadence_h is None
        ):
            raise ValueError(
                f"source {self.id!r}: work_unit {self.work_unit!r} must be a snake_case run name on a source "
                "with a freshness probe AND a cadence_h -- a run nothing can judge against an output age is not watched"
            )
        if self.freshness_where is not None:
            w = self.freshness_where
            if self.freshness is None or not w.strip() or not _WHERE_RX.match(w) or "--" in w or "/*" in w:
                raise ValueError(
                    f"source {self.id!r}: freshness_where must be one predicate over the freshness table in the "
                    f"alphabet {_WHERE_RX.pattern!r} (no semicolon, no comments); got {w!r}"
                )

    def _validate_schedules(self) -> None:
        if not self.schedules:
            raise ValueError(f"source {self.id!r}: declare what fires it, or 'manual'")
        for s in self.schedules:
            if s != "manual" and not s.startswith(SCHEDULE_PREFIXES):
                raise ValueError(
                    f"source {self.id!r}: schedule {s!r} must start with one of {SCHEDULE_PREFIXES} or be 'manual'"
                )
            if s.startswith("producer:") and not _KEY_RX.match(s[len("producer:") :]):
                raise ValueError(f"source {self.id!r}: schedule {s!r} must name feature.action")
        if all(s.startswith("queue:") for s in self.schedules):
            raise ValueError(
                f"source {self.id!r}: a queue: schedule names what FILLS the work, not what drains it -- add the "
                "clock that runs the drainer (flow:/producer:/daemon:/timer:) or 'manual' if nothing does"
            )

    def _validate_links(self) -> None:
        for p in self.producers:
            if not _KEY_RX.match(p):
                raise ValueError(f"source {self.id!r}: producer {p!r} must be feature.action")
        for q in self.queues:
            if not _QUEUE_RX.match(q):
                raise ValueError(f"source {self.id!r}: queue {q!r} is not a queue name")

    def _validate_retired(self) -> None:
        if self.retired is not None and (
            not _RETIRED_RX.match(self.retired)
            or self.schedules != ("manual",)
            or self.freshness is not None
            or self.work_unit
            or self.heartbeat
        ):
            raise ValueError(
                f"source {self.id!r}: retired must be '<YYYY-MM-DD> <why>' on a source nothing runs: "
                "schedules=('manual',), no freshness, work_unit or heartbeat"
            )

    @property
    def freshness_key(self) -> str | None:
        """`schema.table` or `schema.table WHERE <predicate>` -- the probe's row key."""
        if self.freshness is None:
            return None
        t = self.freshness[0]
        return f"{t} WHERE {self.freshness_where}" if self.freshness_where else t

    @property
    def on_money_path(self) -> bool:
        return bool(self.decision_inputs)

    def flow_names(self) -> tuple[str, ...]:
        return tuple(s[len("flow:") :] for s in self.schedules if s.startswith("flow:"))

    def producer_schedules(self) -> tuple[str, ...]:
        return tuple(s[len("producer:") :] for s in self.schedules if s.startswith("producer:"))


@dataclass(frozen=True)
class Home:
    """A database a lane's tables can live in, and what is reachable about it.

    daemons       the long-running services there that may appear as `daemon:` schedules
    module_roots  allowed first segments of an entrypoint (empty = any dotted module)
    tables        None = tables are checked elsewhere (the consumer's migrations); a set = the ONLY
                  tables a lane in this home may declare (a home the consumer's repo cannot read)
    probes        can the consumer's freshness probe reach this database? False = a lane here must
                  declare no freshness probe (it would read the wrong database and say "missing")
    """

    daemons: frozenset[str] = frozenset()
    module_roots: tuple[str, ...] = ()
    tables: frozenset[str] | None = None
    probes: bool = True


@dataclass(frozen=True)
class LaneVocab:
    """The consumer's estate names, passed in once. Each list is enforced: empty refuses all."""

    homes: Mapping[str, Home]
    default_home: str
    timers: frozenset[str] = frozenset()
    decision_inputs: frozenset[str] = frozenset()
    surfaces: frozenset[str] = frozenset()
    kinds: tuple[str, ...] = KINDS
    transports: tuple[str, ...] = TRANSPORTS

    def __post_init__(self) -> None:
        if self.default_home not in self.homes:
            raise ValueError(f"default_home {self.default_home!r} is not one of the homes {sorted(self.homes)}")

    def check(self, spec: LaneSpec) -> None:
        """Raise ValueError naming the first estate name `spec` cites that this vocabulary does not hold."""
        sid = spec.id
        if spec.kind not in self.kinds:
            raise ValueError(f"source {sid!r}: kind {spec.kind!r} not in {self.kinds}")
        if spec.transport not in self.transports:
            raise ValueError(f"source {sid!r}: transport {spec.transport!r} not in {self.transports}")
        home_name = spec.home or self.default_home
        home = self.homes.get(home_name)
        if home is None:
            raise ValueError(f"source {sid!r}: home {home_name!r} not in {sorted(self.homes)}")
        if home.module_roots and spec.entrypoint.split(".", 1)[0] not in home.module_roots:
            raise ValueError(
                f"source {sid!r}: entrypoint {spec.entrypoint!r} must start with one of {home.module_roots}"
            )
        if not home.probes and spec.freshness is not None:
            raise ValueError(f"source {sid!r}: home {home_name!r} is unreachable by the probe -- declare no freshness")
        if home.tables is not None and (bad := [t for t in spec.domain_tables if t not in home.tables]):
            raise ValueError(f"source {sid!r}: tables {bad} are not known in home {home_name!r}")
        for s in spec.schedules:
            if s.startswith("daemon:") and s[len("daemon:") :] not in home.daemons:
                raise ValueError(f"source {sid!r}: schedule {s!r} names no known {home_name} daemon")
            if s.startswith("timer:") and s[len("timer:") :] not in self.timers:
                raise ValueError(f"source {sid!r}: schedule {s!r} names no known timer")
        for d in spec.decision_inputs:
            if d not in self.decision_inputs:
                raise ValueError(f"source {sid!r}: decision input {d!r} not in {sorted(self.decision_inputs)}")
        for s in spec.surfaces:
            if s not in self.surfaces:
                raise ValueError(f"source {sid!r}: surface {s!r} is not a known UI surface")


class LaneRegistry:
    """The declared lanes of ONE consumer, checked against its vocabulary on registration.

    Idempotent on an identical spec (module re-imports); a DIFFERENT spec under a known id raises --
    two declarations of one source is the drift this registry exists to end."""

    def __init__(self, vocab: LaneVocab) -> None:
        self.vocab = vocab
        self._specs: Registry[LaneSpec] = Registry("content source", identical_ok=True)

    def register(self, spec: LaneSpec) -> LaneSpec:
        self.vocab.check(spec)
        return self._specs.add(spec)

    def registered(self) -> list[LaneSpec]:
        return sorted(self._specs.values(), key=lambda s: s.id)

    def get(self, source_id: str) -> LaneSpec | None:
        return self._specs.get(source_id)

    def sources_of_table(self, table: str) -> tuple[str, ...]:
        """Which lanes write a table (usually one; a shared table is partitioned by freshness_where)."""
        return tuple(sorted(s.id for s in self._specs.values() if table in s.domain_tables))

    def declared_tables(self) -> frozenset[str]:
        """Every table some lane declares it writes -- what a coverage test holds every INSERT target
        against (a lane writing a table nothing declares is unwatched)."""
        return frozenset(t for s in self._specs.values() for t in s.domain_tables)

    def judged_lanes(self) -> list[LaneSpec]:
        """Lanes that declare a work_unit: the rows a connectivity verdict derives from here."""
        return [s for s in self.registered() if s.work_unit]

    def blast_radius(self, source_id: str) -> dict[str, object]:
        """PURE. What goes dark if this lane is excluded -- a QUERY, not a grep. KeyError if unknown."""
        s = self._specs[source_id]
        return {
            "source": s.id, "kind": s.kind, "on_money_path": s.on_money_path,
            "tables": list(s.domain_tables), "producers": list(s.producers), "queues": list(s.queues),
            "decision_inputs": list(s.decision_inputs), "surfaces": list(s.surfaces),
            "schedules": list(s.schedules), "home": s.home or self.vocab.default_home, "retired": s.retired,
        }  # fmt: skip

    def __len__(self) -> int:
        return len(self._specs)
