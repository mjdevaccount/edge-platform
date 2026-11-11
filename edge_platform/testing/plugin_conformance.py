"""The plugin-registry conformance suite (plugin-registry.md R1-R10, acceptance 1-10) as importable tests.

Every implementation of the contract -- `edge_platform.plugins`, and the public ports libs' own small
loaders -- carries the SAME ten checks by subclassing `PluginConformance` in a test module whose class
name starts with `Test`, and supplying four things:

    class TestMyLoader(PluginConformance):
        error = MyPluginLoadError                       # the implementation's one exception type
        hookimpl = my_hookimpl                          # its pluggy HookimplMarker
        hook = "register_kinds"                         # the one hook the suite drives
        checks_stage = True                             # False if the error carries no `.stage`

        def thing(self, key):                           # a conforming registrable object
            return Kind(id=key)

        def run(self, *, builtins, entry_points, pin):  # load + collect; -> (names, {key: thing}, register)
            ...

`run` returns the plugin names in load order, the collected mapping, and a `register(thing)` callable
that writes to the SAME store after load (the freeze check). `entry_points` is a zero-arg callable
returning `FakeEntryPoint`s -- the suite never reads site-packages (R9).

The suite needs pytest and pluggy, both imported lazily, so importing this module costs nothing.
"""

from __future__ import annotations

import types
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["FakeEntryPoint", "PluginConformance"]


@dataclass
class FakeEntryPoint:
    """The slice of `importlib.metadata.EntryPoint` a loader reads: name, value, dist, load()."""

    name: str
    target: object = None
    raises: BaseException | None = None
    dist_name: str = "fake-dist"
    version: str = "0.0.0"

    @property
    def value(self) -> str:
        return f"fake_{self.name}:plugin"

    @property
    def dist(self) -> Any:
        return types.SimpleNamespace(name=self.dist_name, version=self.version)

    def load(self) -> object:
        if self.raises is not None:
            raise self.raises
        return self.target


Run = tuple[tuple[str, ...], Mapping[str, Any], Callable[[Any], Any]]


class PluginConformance:
    """Mixin: R1-R10. Not collected by itself (no `Test` prefix)."""

    error: type[BaseException]
    hookimpl: Any
    hook: str
    checks_stage: bool = True

    def thing(self, key: str) -> Any:  # pragma: no cover - supplied by the subclass
        raise NotImplementedError

    def run(self, *, builtins: Iterable[tuple[str, object]], entry_points: Callable[[], Iterable[Any]],
            pin: str | None) -> Run:  # fmt: skip  # pragma: no cover - supplied by the subclass
        raise NotImplementedError

    # -- plugin factories --------------------------------------------------------------------------
    def plugin(self, *keys: str, raises: bool = False, extra_arg: bool = False, misspelled: bool = False,
               junk: bool = False) -> object:  # fmt: skip
        """A plugin object with ONE hookimpl: registers `keys` (or raises / takes a stray argument /
        carries a misspelled hook name / registers a non-conforming object)."""
        make = self.thing

        if extra_arg:

            def impl(register, not_in_spec):  # the point is the stray argument
                return None
        else:

            def impl(register):
                if raises:
                    raise RuntimeError("plugin hook exploded")
                if junk:
                    register(object())
                for k in keys:
                    register(make(k))

        name = self.hook + "_tpyo" if misspelled else self.hook
        impl.__name__ = name
        mod = types.ModuleType(f"conformance_plugin_{'_'.join(keys) or 'none'}")  # hashable, like a real plugin
        setattr(mod, name, self.hookimpl(impl))
        return mod

    def _raises(self, stage: str, fn: Callable[[], Any], *names: str) -> BaseException:
        import pytest

        with pytest.raises(self.error) as ei:
            fn()
        err = ei.value
        assert isinstance(err, ImportError), "the load error must subclass ImportError"
        if self.checks_stage:
            assert getattr(err, "stage", None) == stage, f"stage {getattr(err, 'stage', None)!r} != {stage!r}: {err}"
        for n in names:
            assert n in str(err), f"{n!r} not named in: {err}"
        return err

    # -- R1 / acceptance 1 ---------------------------------------------------------------------------
    def test_r1_a_hookimpl_off_the_spec_is_refused_at_register(self) -> None:
        import pluggy

        bad = self.plugin(extra_arg=True)
        err = self._raises("register", lambda: self.run(builtins=[("bad", bad)], entry_points=lambda: (), pin=None),
                           "bad")  # fmt: skip
        chain = [err, err.__cause__, err.__context__]
        assert any(isinstance(e, pluggy.PluginValidationError) for e in chain), "R1: pluggy must have validated it"

    # -- R3 / R10 / acceptance 2 ---------------------------------------------------------------------
    def test_r3_r10_entry_point_order_never_follows_install_order(self) -> None:
        eps = [FakeEntryPoint("zeta", self.plugin("z")), FakeEntryPoint("alpha", self.plugin("a")),
               FakeEntryPoint("mid", self.plugin("m"))]  # fmt: skip
        one, got1, _ = self.run(builtins=[("core", self.plugin("c"))], entry_points=lambda: list(eps), pin=None)
        two, got2, _ = self.run(builtins=[("core", self.plugin("c"))], entry_points=lambda: eps[::-1], pin=None)
        assert one == two == ("core", "alpha", "mid", "zeta")
        assert list(got1) == list(got2)

    # -- R4 / acceptance 3-5 -------------------------------------------------------------------------
    def test_r4_an_import_failure_must_fire_both_ways(self) -> None:
        good = [FakeEntryPoint("fine", self.plugin("f"))]
        broken = [*good, FakeEntryPoint("broken", raises=ModuleNotFoundError("no module named fake_broken"))]
        self._raises("import", lambda: self.run(builtins=(), entry_points=lambda: broken, pin=None), "broken")
        names, got, _ = self.run(builtins=(), entry_points=lambda: good, pin=None)  # the same set without it
        assert names == ("fine",) and set(got) == {"f"}

    def test_r4_a_raising_hook_is_a_load_error(self) -> None:
        eps = [FakeEntryPoint("loud", self.plugin(raises=True))]
        self._raises("hook", lambda: self.run(builtins=(), entry_points=lambda: eps, pin=None), "loud")

    def test_r4_a_non_conforming_object_is_a_load_error(self) -> None:
        eps = [FakeEntryPoint("junky", self.plugin(junk=True))]
        self._raises("register", lambda: self.run(builtins=(), entry_points=lambda: eps, pin=None), "junky")

    # -- R5 / acceptance 6 ---------------------------------------------------------------------------
    def test_r5_a_duplicate_key_names_both_plugins(self) -> None:
        eps = [FakeEntryPoint("first", self.plugin("x")), FakeEntryPoint("second", self.plugin("x"))]
        self._raises("duplicate", lambda: self.run(builtins=(), entry_points=lambda: eps, pin=None), "first", "second")

    # -- R6 / acceptance 7 ---------------------------------------------------------------------------
    def test_r6_the_pin_selects_exactly(self) -> None:
        def eps():
            return [FakeEntryPoint("a", self.plugin("ka")), FakeEntryPoint("b", self.plugin("kb"))]

        core = ("core", self.plugin("kc"))
        assert self.run(builtins=[core], entry_points=eps, pin="")[0] == ("core",)
        assert self.run(builtins=[core], entry_points=eps, pin="b")[0] == ("core", "b")
        assert self.run(builtins=[core], entry_points=eps, pin=None)[0] == ("core", "a", "b")
        self._raises("pin", lambda: self.run(builtins=[core], entry_points=eps, pin="a,ghost"), "ghost")

    # -- R7 / acceptance 8 ---------------------------------------------------------------------------
    def test_r7_a_misspelled_hook_is_refused_at_check_pending(self) -> None:
        eps = [FakeEntryPoint("typo", self.plugin("t", misspelled=True))]
        self._raises("pending", lambda: self.run(builtins=(), entry_points=lambda: eps, pin=None))

    # -- R2 / acceptance 9 ---------------------------------------------------------------------------
    def test_r2_builtins_need_no_dist_metadata(self) -> None:
        names, got, _ = self.run(builtins=[("core", self.plugin("k1", "k2"))], entry_points=lambda: (), pin=None)
        assert names == ("core",) and list(got) == ["k1", "k2"]

    # -- invariant 2 / acceptance 10 -----------------------------------------------------------------
    def test_freeze_a_register_after_load_raises(self) -> None:
        import pytest

        _, got, register = self.run(builtins=[("core", self.plugin("k1"))], entry_points=lambda: (), pin=None)
        with pytest.raises(Exception, match=r"(?i)frozen|after load|immutable"):
            register(self.thing("late"))
        assert "late" not in got
