"""Shipped Windmill script templates (M6 PL3). Stdlib only: copying a template needs no extra.

A template is three files under `templates/` -- `<name>.py`, `<name>.script.yaml`, `<name>.script.lock`
-- copied into a consumer's Windmill tree at the script path the consumer chooses. The yaml's lock
reference names that path, so it is rewritten on copy (`__SCRIPT_PATH__`).

    python -m edge_platform.windmill list
    python -m edge_platform.windmill copy post_admin_run windmill u/admin/post_admin_run [--check]

`--check` writes nothing and exits 1 when the tree's copy differs from the shipped template: a
vendored template a consumer edited by hand (or forgot to re-copy after a pin bump) is drift that
otherwise surfaces only as a flow behaving like an older version.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

PLACEHOLDER = "__SCRIPT_PATH__"
SUFFIXES = (".py", ".script.yaml", ".script.lock")


def names() -> list[str]:
    root = resources.files(__package__).joinpath("templates")
    return sorted(p.name[: -len(".py")] for p in root.iterdir() if p.name.endswith(".py"))


def render(name: str, script_path: str) -> dict[str, str]:
    """`{suffix: text}` for template `name` placed at Windmill `script_path` (e.g. u/admin/x)."""
    if name not in names():
        raise KeyError(f"no windmill template {name!r}; shipped: {', '.join(names())}")
    script_path = script_path.strip("/")
    if not script_path or script_path.endswith(SUFFIXES):
        raise ValueError(f"script path {script_path!r} must be a Windmill path without a suffix, e.g. u/admin/{name}")
    root = resources.files(__package__).joinpath("templates")
    return {s: root.joinpath(name + s).read_text(encoding="utf-8").replace(PLACEHOLDER, script_path) for s in SUFFIXES}


def copy(name: str, windmill_root: str | Path, script_path: str, *, check: bool = False) -> list[Path]:
    """Write (or with `check`, compare) the template's files; returns the paths that were written or
    that differ. Bytes are written with LF line endings whatever the platform."""
    out: list[Path] = []
    for suffix, text in render(name, script_path).items():
        dest = Path(windmill_root) / (script_path.strip("/") + suffix)
        current = dest.read_bytes().decode("utf-8") if dest.exists() else None
        if current == text:
            continue
        out.append(dest)
        if not check:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(text.encode("utf-8"))
    return out
