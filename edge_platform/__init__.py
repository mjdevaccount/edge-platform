"""EDGE's shared platform package: the estate half. Tenants that live here: `edge_platform.fence` (the deploy fence), `edge_platform.env` (the .env reader), `edge_platform.notify` policy + publishers + CLI (the ntfy.sh budget), `edge_platform.lanes` (ingest lanes), `edge_platform.windmill` (Windmill step templates).

Since v0.13.0 the general planes live in platform-kit and are imported from there under their old names, the same module objects (`edge_platform._moved`): `registry`, `stamp`, `sqlkit`, `budget`, `plugins`, `config`, `service`, `watch`, `testing` and `notify`'s types / pipeline / local / ledger. Importing this package imports none of them.

A partial copy is allowed: a box that ships only the planes that stayed (this root, `env/`) without `_moved.py` still imports them; only an old name of a moved plane then fails, as a missing module, which it would anyway with no platform_kit beside it. Any other failure inside `_moved` raises."""

try:
    import edge_platform._moved as _moved
except ModuleNotFoundError as exc:  # pragma: no cover - exercised in a subprocess (tests/test_moved.py)
    if exc.name != "edge_platform._moved":
        raise
else:
    _moved.install()

__version__ = "0.14.0"
