"""One declare-and-check registry for every "specs declared at import, keyed by id" table.

Lifted from trade-data `service/platform/registry.py` (M6 PL2, 2026-11-11), where five modules had
each hand-rolled the same dozen lines -- a module dict, a `register()` that raises on a duplicate
id, a `get()` and a sorted enumeration. Five copies of a duplicate guard is five places it can
drift, and silent replace is exactly the bug each of them documented against (two planes each
believing they own a check while neither's version runs). This is the one copy; each consumer keeps
its spec type, its registrar's signature and its enumeration ORDER, and delegates the guard here.

Two duplicate policies, because the consumers disagree on purpose:

  strict         ANY second declaration of an id raises (watchdogs, admin runs, job kinds).
  identical_ok   an EQUAL spec under a known id is a module re-import and returns the one already
                 held; a DIFFERENT spec raises (loops, content sources, LLM roles).

Library check: `catalogue` (explosion) is a string-namespace registry for FUNCTIONS with
entry-point discovery; its `register` overwrites silently and has no identical-vs-different notion,
so it would need the guard written around it anyway. Stdlib suffices, and this module needs no
extra: `edge_platform.registry` imports nothing outside the standard library.

The registry is a `MutableMapping[str, S]` so the usual test discipline keeps working -- `set(reg)`,
`dict(reg)`, `reg.pop(id, None)`, `reg.clear()/update()` -- and a test that wants a clean table
swaps in `reg.empty()` rather than a bare `{}` (a bare dict has no guard, which is the thing under
test).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, MutableMapping
from operator import attrgetter
from typing import Generic, TypeVar

S = TypeVar("S")


class Registry(MutableMapping[str, S], Generic[S]):
    """Insertion-ordered id -> spec table with the duplicate guard on `add()`.

    `label` names the thing in the error ("watchdog id", "loop", ...); `key` extracts the id from a
    spec (default `spec.id`); `verb` is the past participle in the strict message ("already
    registered" / "already declared") -- the error TEXT is part of each consumer's contract (tests
    match on it), so it is a parameter, not a rewrite.
    """

    __slots__ = ("_frozen", "_identical_ok", "_items", "_key", "_label", "_verb")

    def __init__(
        self,
        label: str,
        key: Callable[[S], str] = attrgetter("id"),
        *,
        identical_ok: bool = False,
        verb: str = "registered",
    ) -> None:
        self._items: dict[str, S] = {}
        self._key = key
        self._label = label
        self._identical_ok = identical_ok
        self._verb = verb
        self._frozen = False

    # -- the guard ---------------------------------------------------------------------------
    def add(self, spec: S) -> S:
        """Declare `spec`. Strict: a known id raises. identical_ok: an equal spec under a known id
        returns the held one; an unequal one raises. Returns the spec now held.

        identical_ok keeps the FIRST object. Equal frozen specs are interchangeable by the
        definition of equality, so the choice is unobservable unless a spec field is excluded from
        `__eq__` -- and one that were would be a spec design bug, not a reason to keep the newer
        copy."""
        k = self._key(spec)
        if self._frozen:
            raise RuntimeError(f"{self._label} registry is frozen: {k!r} arrived after load")
        have = self._items.get(k)
        if have is not None:
            if self._identical_ok and have == spec:
                return have
            if self._identical_ok:
                raise ValueError(f"{self._label} {k!r} registered twice with a different spec")
            raise ValueError(f"{self._label} {k!r} already {self._verb}")
        self._items[k] = spec
        return spec

    def key_of(self, spec: S) -> str:
        """The id this registry files `spec` under."""
        return self._key(spec)

    def freeze(self) -> None:
        """Refuse every later `add` (and item write): the composition root has finished loading. One
        way -- there is no thaw; a test that needs a writable table swaps in `empty()`. Removal stays
        raw (see below), so teardown still works."""
        self._frozen = True

    @property
    def frozen(self) -> bool:
        return self._frozen

    def empty(self) -> Registry[S]:
        """A fresh table with the same policy, NOT frozen -- what a test swaps in for isolation."""
        return Registry(self._label, self._key, identical_ok=self._identical_ok, verb=self._verb)

    # -- MutableMapping. A raw item write is GUARDED too (`update` and `setdefault` land here; `|=`
    # does not exist on the ABC and is a TypeError): the only legitimate writers are test teardowns
    # that plant a NEW id or refill an emptied table, and both pass the guard -- so a production
    # `_REGISTRY[k] = spec` cannot silently replace a spec either. `del`/`pop`/`clear` stay raw BY
    # DESIGN (teardown needs them), so pop-then-set is the one shape that swaps a spec without an
    # error.
    def __getitem__(self, k: str) -> S:
        return self._items[k]

    def __setitem__(self, k: str, spec: S) -> None:
        if k != self._key(spec):
            raise ValueError(f"{self._label}: key {k!r} is not the spec's own id {self._key(spec)!r}")
        self.add(spec)

    def __delitem__(self, k: str) -> None:
        del self._items[k]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"Registry({self._label!r}, n={len(self._items)})"
