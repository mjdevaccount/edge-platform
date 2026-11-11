"""EDGE's shared platform package. Tenants: `edge_platform.notify` (the ntfy.sh budget), `edge_platform.fence` (the deploy fence), `edge_platform.env` (the .env reader), `edge_platform.registry` (the duplicate-guarded spec table), `edge_platform.service` (the FastAPI composition root, extra `[service]`), `edge_platform.lanes` (ingest lanes), `edge_platform.watch` (the watchdog plane), `edge_platform.stamp` (fingerprints), `edge_platform.sqlkit` (table-name validation, savepoint, jsonb text)."""

__version__ = "0.10.1"
