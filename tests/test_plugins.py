"""edge_platform.plugins carries the R1-R10 conformance suite, plus the loader's own surface:
`Loaded` origin + series, the boot log, R1's fresh-manager refusal, and `Registry.freeze()`."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pluggy
import pytest

from edge_platform.plugins import STAGES, Loaded, PluginLoadError, collect, load
from edge_platform.registry import Registry
from edge_platform.testing.plugin_conformance import FakeEntryPoint, PluginConformance

GROUP = "t_widgets"
hookspec = pluggy.HookspecMarker(GROUP)
hookimpl = pluggy.HookimplMarker(GROUP)


@dataclass(frozen=True)
class Widget:
    id: str


class Spec:
    @hookspec
    def register_widgets(self, register) -> None:  # type: ignore[no-untyped-def]
        """Call register(widget) per widget."""


class TestEdgePlatformLoader(PluginConformance):
    error = PluginLoadError
    hookimpl = hookimpl
    hook = "register_widgets"

    def thing(self, key: str) -> Widget:
        return Widget(key)

    def run(self, *, builtins, entry_points, pin):
        pm = pluggy.PluginManager(GROUP)
        loaded = load(pm, group=GROUP, hookspecs=Spec, builtins=builtins, pin=pin, entry_points=entry_points)
        store: Registry[Widget] = Registry("widget")
        collect(pm, "register_widgets", store, loaded=loaded, kind=Widget)
        return loaded.plugins, store, store.add


def _plugin(*keys: str) -> object:
    return TestEdgePlatformLoader().plugin(*keys)


def test_loaded_names_where_each_plugin_came_from_and_logs_it(caplog) -> None:
    eps = [FakeEntryPoint("extra", _plugin("e"), dist_name="acme-widgets", version="1.2.3")]
    with caplog.at_level(logging.INFO, logger="edge_platform.plugins"):
        loaded = load(pluggy.PluginManager(GROUP), group=GROUP, hookspecs=Spec, builtins=[("core", _plugin("c"))],
                      entry_points=lambda: eps)  # fmt: skip
    assert loaded == Loaded(GROUP, ("core", "extra"), loaded.origin)
    assert dict(loaded.origin) == {"core": "builtin", "extra": "acme-widgets==1.2.3"}
    assert loaded.series() == [
        ("plugins_loaded", {"group": GROUP, "plugin": "core", "origin": "builtin"}, 1.0),
        ("plugins_loaded", {"group": GROUP, "plugin": "extra", "origin": "acme-widgets==1.2.3"}, 1.0),
    ]
    assert "plugins_loaded group=t_widgets names=core,extra" in caplog.text


def test_load_refuses_a_manager_that_already_holds_plugins() -> None:
    pm = pluggy.PluginManager(GROUP)
    pm.register(_plugin("early"), name="early")
    with pytest.raises(ValueError, match="fresh PluginManager"):
        load(pm, group=GROUP, hookspecs=Spec, entry_points=lambda: ())


def test_an_installed_plugin_may_not_shadow_a_builtin() -> None:
    eps = [FakeEntryPoint("core", _plugin("x"))]
    with pytest.raises(PluginLoadError) as ei:
        load(pluggy.PluginManager(GROUP), group=GROUP, hookspecs=Spec, builtins=[("core", _plugin("c"))],
             entry_points=lambda: eps)  # fmt: skip
    assert ei.value.stage == "duplicate" and ei.value.plugin == "core"


def test_a_pin_may_name_a_builtin() -> None:
    loaded = load(pluggy.PluginManager(GROUP), group=GROUP, hookspecs=Spec, builtins=[("core", _plugin("c"))],
                  pin="core", entry_points=lambda: [FakeEntryPoint("a", _plugin("a"))])  # fmt: skip
    assert loaded.plugins == ("core",)


def test_collect_needs_a_known_hook_and_stage_is_closed() -> None:
    pm = pluggy.PluginManager(GROUP)
    loaded = load(pm, group=GROUP, hookspecs=Spec, entry_points=lambda: ())
    with pytest.raises(ValueError, match="no hook"):
        collect(pm, "register_gadgets", Registry("widget"), loaded=loaded)
    with pytest.raises(ValueError, match="unknown plugin load stage"):
        PluginLoadError(GROUP, "p", "exploded", "x")  # type: ignore[arg-type]
    assert set(STAGES) == {"import", "register", "hook", "duplicate", "pin", "pending"}


def test_collect_can_leave_the_store_open_for_a_second_hook() -> None:
    pm = pluggy.PluginManager(GROUP)
    loaded = load(pm, group=GROUP, hookspecs=Spec, builtins=[("core", _plugin("c"))], entry_points=lambda: ())
    store: Registry[Widget] = Registry("widget")
    collect(pm, "register_widgets", store, loaded=loaded, freeze=False)
    assert not store.frozen
    store.add(Widget("more"))
    store.freeze()
    with pytest.raises(RuntimeError, match="frozen"):
        store["late"] = Widget("late")


def test_freeze_is_one_way_and_empty_is_writable() -> None:
    reg: Registry[Widget] = Registry("widget")
    reg.add(Widget("a"))
    reg.freeze()
    with pytest.raises(RuntimeError, match="widget registry is frozen: 'b' arrived after load"):
        reg.add(Widget("b"))
    with pytest.raises(RuntimeError, match="frozen"):
        reg.add(Widget("a"))  # even an identical re-add: frozen means no writes
    assert reg.frozen and list(reg) == ["a"] and reg.key_of(Widget("z")) == "z"
    fresh = reg.empty()
    assert not fresh.frozen
    fresh.add(Widget("b"))
