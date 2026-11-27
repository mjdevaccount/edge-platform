"""EDGE's shared platform package: the estate half. Tenants that live here: `edge_platform.fence` (the deploy fence), `edge_platform.env` (the .env reader), `edge_platform.notify` policy + publishers + CLI (the ntfy.sh budget), `edge_platform.lanes` (ingest lanes), `edge_platform.windmill` (Windmill step templates).

Since v0.13.0 the general planes live in platform-kit and are imported from there under their old names, the same module objects (`edge_platform._moved`): `registry`, `stamp`, `sqlkit`, `budget`, `plugins`, `config`, `service`, `watch`, `testing` and `notify`'s types / pipeline / local / ledger. Importing this package imports none of them."""

from edge_platform import _moved

_moved.install()

__version__ = "0.13.0"
