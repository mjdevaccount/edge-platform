"""v0.13.0: the general planes are platform-kit's; the old names are the SAME module objects.

What fails silently if this breaks: a second copy of a moved module loaded under the old name (two
`Domain` classes, an `except` that misses, a monkeypatch that lands on a module nothing runs), or a
box CLI that starts importing platform_kit and dies under `python -S` on a bare checkout. Each is
asserted here; the duplicate check is also shown to fire on the copy it exists to find.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import platform_kit.notify
import platform_kit.notify.types
import primitives_kit
import pytest

from edge_platform import _moved
from edge_platform.notify import policy

ROOT = Path(__file__).resolve().parents[1]

#: one submodule per moved package: the case the path finder would load a second time
SUBMODULES = [
    "edge_platform.config.knobs",
    "edge_platform.config.render",
    "edge_platform.service.db",
    "edge_platform.service.envelope",
    "edge_platform.watch.runner",
    "edge_platform.watch.spec",
    "edge_platform.testing.plugin_conformance",
]


@pytest.mark.parametrize("old", [*_moved.MOVED, *SUBMODULES])
def test_every_moved_name_is_the_platform_kit_module(old: str) -> None:
    new = _moved.target(old)
    assert new is not None and new.startswith("platform_kit.")
    assert importlib.import_module(old) is importlib.import_module(new)


def _copies() -> list[str]:
    """Old names whose module is a module of its own, not the platform_kit one."""
    return [
        k for k, m in list(sys.modules.items())
        if _moved.target(k) is not None and getattr(m, "__name__", "").startswith("edge_platform")
    ]  # fmt: skip


def test_no_moved_module_is_loaded_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    for old in [*_moved.MOVED, *SUBMODULES]:
        importlib.import_module(old)
    assert _copies() == []
    copy = type(sys)("edge_platform.config.knobs")  # the shape the path finder would make
    monkeypatch.setitem(sys.modules, "edge_platform.config.knobs", copy)
    assert _copies() == ["edge_platform.config.knobs"]  # the check fires


def test_target_maps_a_package_and_its_submodules_and_nothing_else() -> None:
    assert _moved.target("edge_platform.config") == "platform_kit.config"
    assert _moved.target("edge_platform.config.knobs") == "platform_kit.config.knobs"
    for stayed in ("edge_platform", "edge_platform.fence", "edge_platform.notify", "edge_platform.notify.policy",
                   "edge_platform.configx", "edge_platform.lanes.spec"):  # fmt: skip
        assert _moved.target(stayed) is None, stayed


def test_install_is_idempotent_and_first() -> None:
    _moved.install()
    finders = [f for f in sys.meta_path if isinstance(f, _moved._Alias)]
    assert len(finders) == 1 and sys.meta_path.index(finders[0]) == 0


def test_a_monkeypatch_through_the_old_name_lands_where_the_code_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    import edge_platform.stamp as old
    import platform_kit.stamp as new

    monkeypatch.setattr(old, "sha", lambda *a, **k: "patched")
    assert new.sha("x") == "patched"


def test_the_notify_package_resolves_moved_names_lazily_and_identically() -> None:
    import edge_platform.notify as n

    for name in n._MOVED_NAMES:
        assert getattr(n, name) is getattr(platform_kit.notify, name), name
        assert name in n.__all__ and name in dir(n)
    assert not set(n._MOVED_MODULES) & set(dir(n)) - set(vars(n))
    assert n.pipeline is importlib.import_module("platform_kit.notify.pipeline")
    with pytest.raises(AttributeError, match="no attribute 'Nope'"):
        _ = n.Nope


def test_the_policy_tiers_are_the_push_path_tiers() -> None:
    assert tuple(policy.TIERS) == tuple(platform_kit.notify.types.TIERS)


def test_a_fence_clock_failure_is_the_push_path_s_clock_failure() -> None:
    from edge_platform.fence import ClockBlind

    assert issubclass(ClockBlind, platform_kit.notify.types.ClockBlind)
    assert issubclass(ClockBlind, RuntimeError)


def test_a_budget_refusal_under_the_old_name_is_a_primitives_kit_budget_error() -> None:
    from edge_platform.budget import BudgetExhausted, BudgetUnavailable

    for cls in (BudgetExhausted, BudgetUnavailable):
        assert issubclass(cls, primitives_kit.BudgetError) and issubclass(cls, RuntimeError)


def _bare(code: str) -> subprocess.CompletedProcess[str]:
    """`code` the way the box runs the CLIs: a bare checkout on PYTHONPATH, `python -S -P`."""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run(
        [sys.executable, "-S", "-P", "-c", code], capture_output=True, text=True, env=env, check=False
    )


def test_the_box_planes_import_from_a_bare_checkout_without_platform_kit() -> None:
    """The import succeeding is the check: under -S there is no platform_kit to find, so an eager
    import of it anywhere on these paths fails here (proven by making notify import eagerly)."""
    r = _bare(
        "import edge_platform, edge_platform.notify, edge_platform.notify.cli, edge_platform.fence.cli\n"
        "import edge_platform.env.cli\n"
        "from edge_platform.fence import ClockBlind\n"
        "assert ClockBlind.__mro__[1] is RuntimeError, ClockBlind.__mro__\n"
        "print('ok')\n"
    )
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


@pytest.mark.parametrize(
    ("argv", "rcs"),
    [
        (["edge_platform.notify.cli", "check-pin"], {0}),
        (["edge_platform.env.cli", "path", "sh"], {0}),
        (["edge_platform.fence.cli", "status"], {0, 2}),  # 2 = blind (no zone data under -S off Linux)
    ],
)
def test_the_box_clis_run_a_command_from_a_bare_checkout(argv: list[str], rcs: set[int]) -> None:
    """A command body, not just the import: a lazy platform_kit import inside one would fail here."""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    r = subprocess.run([sys.executable, "-S", "-P", "-m", *argv], capture_output=True, text=True, env=env, check=False)
    assert r.returncode in rcs and "Traceback" not in r.stderr, r.stderr


def test_an_old_name_import_leaves_the_platform_kit_module_s_own_spec() -> None:
    """The import system stamps the alias spec on the module it is handed; left there it breaks
    `importlib.resources` and `reload` for both names. Fresh interpreter: import order matters."""
    r = subprocess.run(
        [sys.executable, "-W", "error", "-c",
         "import importlib, importlib.resources as res\n"
         "import edge_platform.service as es, edge_platform.config.knobs\n"
         "import platform_kit.service as ps, platform_kit.config.knobs as pk\n"
         "assert ps.__spec__.name == 'platform_kit.service' and pk.__spec__.name == 'platform_kit.config.knobs'\n"
         "assert ps.__name__ == 'platform_kit.service' and list(ps.__path__)\n"
         "assert res.files('platform_kit.service').joinpath('db.py').is_file()\n"
         "importlib.reload(es)\n"
         "assert list(ps.__path__) and ps.__spec__.name == 'platform_kit.service'\n"
         "import platform_kit.service.admin_run, edge_platform.service.admin_run as a\n"
         "assert a is platform_kit.service.admin_run\n"
         "print('ok')\n"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


def test_a_moved_name_without_platform_kit_fails_naming_it() -> None:
    r = _bare("import edge_platform.registry")
    assert r.returncode == 1 and "No module named 'platform_kit'" in r.stderr, r.stderr


def test_python_dash_m_runs_a_moved_main() -> None:
    r = subprocess.run([sys.executable, "-m", "edge_platform.config", "--help"], capture_output=True, text=True,
                       check=False)  # fmt: skip
    assert r.returncode == 0 and "lint" in r.stdout, r.stderr
