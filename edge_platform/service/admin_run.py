"""The `/admin/run/*` contract as DATA: one declaration per kind, queryable and reviewable in one diff.

Lifted from trade-data `service/platform/admin_run.py` (M6 PL3). The bug it replaced: seventy-six
admin-run routes, forty-two docstrings promising idempotency, nothing checking any of them -- so
"which jobs are safe to retry" was answerable only by reading seventy-six files. Here a kind is
declared once:

    RUNS = AdminRuns()                      # one table per service, beside ADMIN_RUN_PREFIX
    RUNS.declare("enrich_nightly", idempotent=True, expect_s=300,
                 note="ON CONFLICT annotate; verified: a second run grades zero rows")
    RUNS.spec_for("/admin/run/enrich_nightly/extra").kind   # -> "enrich_nightly"

`expect_s` is a REPORTING threshold, never a kill switch: measured on the box, real jobs ran to 28
minutes and several inline routes sit in the 30-160s band, so a ceiling picked from intuition is an
outage on exactly the heavy jobs that matter. A service measures against it and reports overruns;
the per-kind enforcement flip comes from the measured ledger.

A TABLE PER INSTANCE, not a module global: the library holds no process state, so two services (or
two tests) can never see each other's kinds. A duplicate declaration RAISES (edge_platform.registry,
strict) -- two files claiming one endpoint means one of them is wrong about what runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from operator import attrgetter

from edge_platform.registry import Registry

from .timeline import ADMIN_RUN_PREFIX

#: Default expectation for an undeclared kind. Deliberately generous: a noisy default trains the
#: operator to ignore the signal -- the alert fatigue a watchdog plane exists to undo.
DEFAULT_EXPECT_S = 600


def kind_of(path: str, prefix: str = ADMIN_RUN_PREFIX) -> str | None:
    """The kind a request path names: the FIRST segment after the prefix (`/admin/run/fit/meta` ->
    `fit`), None for a path outside it. One rule, so a deploy fence and a declaration lookup can
    never disagree about which endpoint a request is (trade-data's market_hours.fenced_admin_run
    used the same split by hand)."""
    if not path.startswith(prefix):
        return None
    return path[len(prefix) :].split("/", 1)[0]


@dataclass(frozen=True)
class AdminRunSpec:
    kind: str  # the /admin/run/<kind> segment
    idempotent: bool  # is a second identical run a no-op / same-result?
    expect_s: int = DEFAULT_EXPECT_S  # above this a run is REPORTED, not killed
    note: str = ""  # WHY it is idempotent (or why not) -- the reviewable part
    learn_kind: str | None = None  # a consumer's learning-ledger kind, when it stamps one
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.kind or "/" in self.kind:
            raise ValueError(f"admin-run kind {self.kind!r} must be one non-empty path segment (the first)")
        if self.expect_s <= 0:
            raise ValueError(f"admin-run kind {self.kind!r}: expect_s must be positive, got {self.expect_s}")
        if self.idempotent and not self.note:
            raise ValueError(
                f"admin-run kind {self.kind!r}: an idempotency CLAIM needs its reason -- say what "
                "makes a second run safe (the bug this replaces: docstrings claiming it, nothing checking)."
            )


class AdminRuns:
    """One service's declaration table for the routes under `prefix`."""

    def __init__(self, prefix: str = ADMIN_RUN_PREFIX) -> None:
        if not prefix.startswith("/") or not prefix.endswith("/"):
            raise ValueError(f"admin-run prefix {prefix!r} must start and end with '/'")
        self.prefix = prefix
        self._specs: Registry[AdminRunSpec] = Registry("admin-run kind", key=attrgetter("kind"), verb="declared")

    def declare(
        self,
        kind: str,
        *,
        idempotent: bool,
        expect_s: int = DEFAULT_EXPECT_S,
        note: str = "",
        learn_kind: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> AdminRunSpec:
        """Register one kind (a duplicate raises)."""
        spec = AdminRunSpec(
            kind=kind, idempotent=idempotent, expect_s=expect_s, note=note, learn_kind=learn_kind, tags=tags
        )
        return self._specs.add(spec)

    def spec_for(self, path: str) -> AdminRunSpec | None:
        """The declaration for a request path (`kind_of` rule), None when outside or undeclared."""
        kind = kind_of(path, self.prefix)
        return None if kind is None else self._specs.get(kind)

    def undeclared(self, paths: list[str] | tuple[str, ...]) -> list[str]:
        """The route paths under the prefix with no declaration -- the conformance check a service's
        test runs over `app.routes`, so a new route cannot ship undeclared."""
        out = []
        for p in paths:
            kind = kind_of(p, self.prefix)
            if kind is not None and kind not in self._specs:
                out.append(p)
        return out

    def registry(self) -> dict[str, AdminRunSpec]:
        """A snapshot copy (what a `GET /ops/admin_runs` route serves)."""
        return dict(self._specs)

    def __iter__(self) -> Iterator[AdminRunSpec]:
        return iter(list(self._specs.values()))

    def __len__(self) -> int:
        return len(self._specs)
