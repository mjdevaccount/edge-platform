"""The strict plugin loader for apps: pluggy as the transport, `Registry` as the store (plugin-registry.md,
M6 PL11). Extra `[plugins]` (pluggy>=1.7).

Code enters a registry by NAME, from the app itself (built-ins) or from another installed distribution
(entry points), and a plugin that fails to load STOPS THE PROCESS -- a plugin that silently did not
load is indistinguishable from one nobody installed.

    pm = pluggy.PluginManager("my_app")
    loaded = load(pm, group="my_app", hookspecs=Spec, builtins=[("core", core_plugin)],
                  pin=os.environ.get("MY_APP_PLUGINS"))
    collect(pm, "register_kinds", KINDS, loaded=loaded)     # KINDS: Registry[Kind]; frozen after
    log/expose `loaded` (R8)

THE CONTRACT (R1-R10; `edge_platform.testing.plugin_conformance` checks every implementation):
  R1  hookspecs are added BEFORE any plugin registers (pluggy validates a hookimpl's arguments only
      against a spec that already exists) -- `load` refuses a PluginManager that already holds plugins.
  R2  built-ins register by `pm.register(obj, name=)`; entry points are for OTHER distributions, so
      an app run from a checkout needs no dist metadata.
  R3  entry points load in this module's own loop, SORTED by name (`pm.load_setuptools_entrypoints`
      walks distributions unsorted, names no plugin in its exception, and skips a registered name).
  R4  every failure raises `PluginLoadError(group, plugin, stage)`: import, register (incl. a hookimpl
      that does not match the spec, a name collision, a non-conforming registered object), hook (the
      plugin's `register_*` raised), duplicate (two plugins, one key), pin, pending.
  R5  a duplicate key raises and names BOTH plugins.
  R6  pin: None = built-ins + every installed plugin; "" = built-ins only; "a,b" = built-ins + exactly
      those, a listed name that is not installed raises.
  R7  `pm.check_pending()` after loading: a misspelled hookimpl otherwise loads and contributes nothing.
  R8  `load` returns `Loaded` (names in load order + origin) and logs `plugins_loaded` at INFO.
  R9  test suites pin the set (`pin=""` or an injected `entry_points`), never the developer's site-packages.
  R10 no order derives from a set or install order: built-ins in the order given, then entry points
      by name; `collect` walks `Loaded.plugins`, never `pm.get_plugins()` (a set).

LIBRARY CHECKED: pluggy 1.7 is the transport (discovery-free here, hook validation, the
`register_*(register)` idiom simonw/llm uses). stevedore 5.9 was not chosen (logs and continues on a
failed load; reads entry points only); raw `importlib.metadata.entry_points` is what the R3 loop uses
underneath. The store is `edge_platform.registry.Registry` (a duplicate raises; `freeze()`).

TELEMETRY: a failure is a raise at boot -- the process exits non-zero and the supervisor's restart
count is the signal; there is no degraded mode. What loads silently WRONG is a plugin that is simply
absent (not installed, or not pinned): `Loaded.series()` renders `plugins_loaded{group,plugin,origin}`
= 1 for the consumer's exporter, and the consumer's watchdog compares it with the pin in its deploy
config.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points as _installed
from types import MappingProxyType
from typing import Any, Literal, TypeVar

import pluggy

from edge_platform.registry import Registry

log = logging.getLogger("edge_platform.plugins")

Stage = Literal["import", "register", "hook", "duplicate", "pin", "pending"]
STAGES: tuple[str, ...] = ("import", "register", "hook", "duplicate", "pin", "pending")
BUILTIN = "builtin"

S = TypeVar("S")


class PluginLoadError(ImportError):
    """A plugin did not load. Names the group, the plugin and the stage it failed at."""

    def __init__(self, group: str, plugin: str, stage: Stage, detail: str) -> None:
        if stage not in STAGES:
            raise ValueError(f"unknown plugin load stage {stage!r}")
        super().__init__(f"plugin {plugin!r} in group {group!r} failed at {stage}: {detail}", name=plugin)
        self.group, self.plugin, self.stage = group, plugin, stage


@dataclass(frozen=True, slots=True)
class Loaded:
    """What this process is running: plugin names in load order and where each came from."""

    group: str
    plugins: tuple[str, ...]
    origin: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def series(self) -> list[tuple[str, dict[str, str], float]]:
        """`plugins_loaded{group,plugin,origin} = 1` per loaded plugin, in load order."""
        return [
            ("plugins_loaded", {"group": self.group, "plugin": p, "origin": self.origin[p]}, 1.0) for p in self.plugins
        ]


def _origin(ep: Any) -> str:
    dist = getattr(ep, "dist", None)
    name, version = getattr(dist, "name", None), getattr(dist, "version", None)
    return f"{name}=={version}" if name and version else f"entry-point {getattr(ep, 'value', '?')}"


def _register(pm: pluggy.PluginManager, group: str, name: str, plugin: object) -> None:
    if pm.get_plugin(name) is not None:
        raise PluginLoadError(group, name, "duplicate", f"a plugin named {name!r} is already registered")
    try:
        pm.register(plugin, name=name)
    except (ValueError, pluggy.PluginValidationError) as exc:
        raise PluginLoadError(group, name, "register", str(exc)) from exc


def _selected(group: str, eps: Iterable[Any], pin: str | None, builtin_names: set[str]) -> list[Any]:
    available: dict[str, Any] = {}
    for ep in eps:
        if ep.name in available:
            raise PluginLoadError(group, ep.name, "duplicate", "two installed entry points share the name")
        available[ep.name] = ep
    if pin is None:
        return [available[n] for n in sorted(available)]
    wanted = {p.strip() for p in pin.split(",") if p.strip()}
    absent = sorted(wanted - set(available) - builtin_names)
    if absent:
        raise PluginLoadError(
            group, ",".join(absent), "pin", f"pinned but not installed (installed: {sorted(available)})"
        )
    return [available[n] for n in sorted(wanted & set(available))]


def load(
    pm: pluggy.PluginManager,
    *,
    group: str,
    hookspecs: object,
    builtins: Sequence[tuple[str, object]] = (),
    pin: str | None = None,
    entry_points: Callable[[], Iterable[Any]] | None = None,
) -> Loaded:
    """Add `hookspecs`, register the built-ins (in the order given), then the selected entry points of
    `group` (sorted by name), then `check_pending`. Any failure raises `PluginLoadError`."""
    if not isinstance(group, str) or not group.strip():
        raise ValueError("plugins.load needs a group")
    if pm.get_plugins():
        raise ValueError("plugins.load needs a fresh PluginManager: hookspecs go in BEFORE any plugin (R1)")
    pm.add_hookspecs(hookspecs)
    names: list[str] = []
    origin: dict[str, str] = {}
    for name, plugin in builtins:
        _register(pm, group, name, plugin)
        names.append(name)
        origin[name] = BUILTIN
    discover = entry_points if entry_points is not None else (lambda: _installed(group=group))
    for ep in _selected(group, discover(), pin, set(names)):
        try:
            plugin = ep.load()
        except Exception as exc:
            raise PluginLoadError(group, ep.name, "import", f"{ep.value}: {exc!r}") from exc
        _register(pm, group, ep.name, plugin)
        names.append(ep.name)
        origin[ep.name] = _origin(ep)
    try:
        pm.check_pending()
    except pluggy.PluginValidationError as exc:
        plugin_name = pm.get_name(exc.plugin) or "?"
        raise PluginLoadError(group, plugin_name, "pending", str(exc)) from exc
    loaded = Loaded(group, tuple(names), MappingProxyType(origin))
    log.info("plugins_loaded group=%s names=%s origin=%s", group, ",".join(names), dict(origin))
    return loaded


def collect(
    pm: pluggy.PluginManager,
    hook: str,
    store: Registry[S],
    *,
    loaded: Loaded,
    kind: type | tuple[type, ...] | None = None,
    freeze: bool = True,
) -> None:
    """Call each plugin's `<hook>(register=...)` ONCE, in `loaded.plugins` order, adding what it
    registers to `store`; then freeze the store (unless `freeze=False`, for a store fed by two hooks
    -- freeze it after the last). A registered object that is not a `kind` (or has no key) raises
    stage `register`; a raising hook, stage `hook`; a duplicate key, stage `duplicate` naming both."""
    caller = getattr(pm.hook, hook, None)
    if caller is None:
        raise ValueError(f"no hook {hook!r} -- was it in the hookspecs passed to load()?")
    impls = {i.plugin_name: i for i in caller.get_hookimpls()}
    owner: dict[str, str] = {}
    for name in loaded.plugins:
        impl = impls.get(name)
        if impl is None:
            continue

        def register(thing: object, _plugin: str = name) -> None:
            if kind is not None and not isinstance(thing, kind):
                raise PluginLoadError(loaded.group, _plugin, "register", f"registered {thing!r}, not a {kind}")
            try:
                k = store.key_of(thing)  # type: ignore[arg-type]
            except Exception as exc:
                raise PluginLoadError(loaded.group, _plugin, "register", f"registered {thing!r}: no key") from exc
            if k in owner or k in store:
                first = owner.get(k, "an earlier registration")
                raise PluginLoadError(
                    loaded.group, _plugin, "duplicate", f"key {k!r} registered by both {first!r} and {_plugin!r}"
                )
            store.add(thing)  # type: ignore[arg-type]
            owner[k] = _plugin

        try:
            impl.function(register=register)
        except PluginLoadError:
            raise
        except Exception as exc:
            raise PluginLoadError(loaded.group, name, "hook", f"{hook} raised {exc!r}") from exc
    if freeze:
        store.freeze()
