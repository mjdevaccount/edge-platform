"""Who is holding this Postgres connection? -- one authority for `application_name`.

Lifted from trade-data `service/pg_identity.py` (M6 PL2); `db.application_name` is the one-line
form trade-quant already used, this is the rest: the prefix, the per-connection suffix, the
entrypoint fallback, and `install()` for libpq.

WHY. On 2026-08-06 an options-chain capture was dark for 5h15m because one transaction sat `idle in
transaction` until the close. Postgres named the holder precisely -- a PID and an xid -- and that
was the end of the trail, because EVERY application connection presented an EMPTY
`application_name`. `pg_stat_activity` could say a backend was wedged; nothing could say WHICH
service it was.

TWO MECHANISMS, BECAUSE THE TWO DRIVERS DISAGREE (measured on a live cluster 2026-08-07):

    psycopg (libpq)  + PGAPPNAME env    -> honoured
    asyncpg          + PGAPPNAME env    -> IGNORED (connect_utils reads PG* vars, not PGAPPNAME)
    asyncpg          + server_settings  -> honoured

So `install()` exports PGAPPNAME for every libpq connection in the process with no per-callsite
edit, and `server_settings()` / `connect_args()` hand the same value to every asyncpg path
explicitly. It survives pgbouncer: application_name is a startup parameter pgbouncer tracks per
client and re-applies to whichever server connection a transaction lands on.

NAMING -- ONE env var (`PGAPPNAME` by default, libpq's own), set by the deployment, never derived
from a container name: a rename would then silently produce a WRONG label, which is worse than a
blank one. Unset, the name falls back to the entrypoint's module stem (`<prefix>-py:grade_hist`),
still far better than "". The PREFIX is the consumer's (`PgIdentity("trade-data")`); this module
names no service.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .db import APP_NAME_MAX


def entrypoint() -> str:
    """Best-effort name for a process nobody labelled: the script/module that started it."""
    argv0 = sys.argv[0] if sys.argv else ""
    stem = Path(argv0).stem if argv0 else ""
    # `python -m pkg.main` gives argv[0] = ".../pkg/main.py"; a bare REPL gives "".
    if not stem or stem in {"python", "python3", "-c"}:
        return "unknown"
    return stem


class PgIdentity:
    """The `application_name` a family of processes presents: `<prefix>-<$PGAPPNAME or py:entry>`.

    `prefix` names the consumer (never doubled: a value already starting with it is used verbatim);
    `env_var` is the variable the deployment sets (libpq's PGAPPNAME unless told otherwise)."""

    def __init__(self, prefix: str, *, env_var: str = "PGAPPNAME", max_len: int = APP_NAME_MAX) -> None:
        if not prefix.strip():
            raise ValueError("PgIdentity needs a non-empty prefix: an empty one is the anonymous name")
        self.prefix = prefix.strip()
        self.env_var = env_var
        self.max_len = max_len

    def app_name(self, suffix: str = "") -> str:
        """The name this process presents, optionally narrowed for one connection (`suffix`: a
        chain writer tags per symbol, so a stuck holder names the symbol that will go dark)."""
        base = (os.environ.get(self.env_var) or "").strip()
        if not base:
            base = f"py:{entrypoint()}"
        name = base if base.startswith(self.prefix) else f"{self.prefix}-{base}"
        if suffix:
            name = f"{name}:{suffix}"
        return name[: self.max_len]

    def server_settings(self, suffix: str = "", **extra: str) -> dict[str, str]:
        """asyncpg `server_settings` carrying application_name (asyncpg ignores PGAPPNAME)."""
        return {"application_name": self.app_name(suffix), **extra}

    def connect_args(self, suffix: str = "") -> dict[str, object]:
        """SQLAlchemy `connect_args` fragment for the asyncpg dialect."""
        return {"server_settings": self.server_settings(suffix)}

    def install(self) -> str:
        """Export the env var so every libpq connection in this process inherits it. Idempotent, and
        it never clobbers an already-set value (the deployment said something more specific).
        Returns the effective name."""
        existing = (os.environ.get(self.env_var) or "").strip()
        if existing:
            return existing
        name = self.app_name()
        os.environ[self.env_var] = name
        return name
