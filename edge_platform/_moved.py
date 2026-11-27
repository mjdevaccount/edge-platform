"""Where the general planes went: platform-kit (v0.13.0). Stdlib only.

`edge_platform.<moved>` IS `platform_kit.<moved>` -- the same module object, not a copy and not a
proxy. So a class is one class (`isinstance` and `except` agree across both names), a monkeypatch on
either name lands on the module the code runs in, a private name a consumer reaches for is still
there, and `edge_platform.config.knobs` never loads a second copy beside `platform_kit.config.knobs`.

HOW: `install()` puts one meta-path finder FIRST on `sys.meta_path` (it must run before the path
finder, which would otherwise load a moved package's submodule a second time from platform_kit's
`__path__` under the old name). The finder answers only for the names in `MOVED` and imports
platform_kit only when one is asked for, so the planes that stayed -- fence, env, notify's policy and
CLI -- still run from a bare checkout on PYTHONPATH under `python -S`, where platform_kit is absent.
`python -m edge_platform.config` works: the loader hands runpy the target's code.

Library check (integrate before write): apipkg and lazy_loader proxy or lazy-load a module; neither
aliases one, and a proxy is a second object, so a monkeypatch on it misses the module that runs. The
stdlib has no alias primitive; the `sys.modules[__name__] = other` idiom covers a single module but
not a package's submodules (each would load twice).
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
from types import CodeType, ModuleType

#: old name -> new name. A package entry covers every submodule under it.
MOVED: dict[str, str] = {
    "edge_platform.budget": "platform_kit.budget",
    "edge_platform.config": "platform_kit.config",
    "edge_platform.notify.ledger": "platform_kit.notify.ledger",
    "edge_platform.notify.local": "platform_kit.notify.local",
    "edge_platform.notify.pipeline": "platform_kit.notify.pipeline",
    "edge_platform.notify.types": "platform_kit.notify.types",
    "edge_platform.plugins": "platform_kit.plugins",
    "edge_platform.registry": "platform_kit.registry",
    "edge_platform.service": "platform_kit.service",
    "edge_platform.sqlkit": "platform_kit.sqlkit",
    "edge_platform.stamp": "platform_kit.stamp",
    "edge_platform.testing": "platform_kit.testing",
    "edge_platform.watch": "platform_kit.watch",
}


def target(fullname: str) -> str | None:
    """The platform_kit name `fullname` resolves to, or None when it did not move."""
    for old, new in MOVED.items():
        if fullname == old or fullname.startswith(old + "."):
            return new + fullname[len(old) :]
    return None


class _Alias(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(
        self, fullname: str, path: object = None, target_module: object = None
    ) -> importlib.machinery.ModuleSpec | None:
        new = target(fullname)
        if new is None:
            return None
        real = importlib.util.find_spec(new)  # platform_kit absent -> ModuleNotFoundError naming it
        if real is None:
            return None  # the old name then fails exactly as a missing module does
        return importlib.machinery.ModuleSpec(
            fullname, self, origin=real.origin, is_package=real.submodule_search_locations is not None
        )

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        return importlib.import_module(target(spec.name) or spec.name)

    def exec_module(self, module: ModuleType) -> None:
        """Nothing to run: `create_module` returned the imported platform_kit module."""

    def get_code(self, fullname: str) -> CodeType | None:
        """For runpy (`python -m edge_platform.config`): the target module's code."""
        new = target(fullname) or fullname
        spec = importlib.util.find_spec(new)
        loader = spec.loader if spec is not None else None
        get = getattr(loader, "get_code", None)
        return get(new) if get is not None else None


def install() -> None:
    """Put the alias finder first on `sys.meta_path`, once per interpreter."""
    if not any(isinstance(f, _Alias) for f in sys.meta_path):
        sys.meta_path.insert(0, _Alias())
