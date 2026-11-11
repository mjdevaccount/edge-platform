"""The postgres dlt destination with TRUNCATE swapped for DELETE (extra `[dlt]`).

Lifted from trade-data `sources/pg_destination.py` (M6 PL4). dlt's SQL client emits
`TRUNCATE TABLE staging.x` whenever the destination capability `supports_truncate_command` is true
(hard-coded for postgres, no config switch). TRUNCATE takes ACCESS EXCLUSIVE: on 2026-07-29 two
staging TRUNCATEs queued behind a slow compression job's chunk lock, and Postgres's FIFO lock queue
then parked EVERY later reader behind them -- the full "backend down" convoy, three times in a day.
`DELETE FROM x WHERE 1=1` takes ROW EXCLUSIVE; readers sail past, a convoy is structurally
impossible. Cost: dead tuples on small staging tables, reclaimed by autovacuum. Subclass-and-flip is
dlt's documented override path; there is deliberately no upstream knob.

Use `nolock_postgres()` anywhere a pipeline would say `destination="postgres"`.
"""

from __future__ import annotations

import os
from typing import Any

from dlt.destinations import postgres

#: dlt's own env contract for the stock postgres destination (not an estate name).
CREDENTIALS_ENV_PREFIX = "DESTINATION__POSTGRES__CREDENTIALS__"


def _env_credentials(env: Any = os.environ) -> dict[str, Any] | None:
    p = CREDENTIALS_ENV_PREFIX
    username = env.get(p + "USERNAME")
    if not username:
        return None
    return {
        "username": username,
        "password": env.get(p + "PASSWORD"),
        "host": env.get(p + "HOST"),
        "port": int(env.get(p + "PORT", "5432")),
        "database": env.get(p + "DATABASE"),
    }


class nolock_postgres(postgres):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # destination_name "postgres" so dlt resolves the SAME config section the stock destination
        # uses; derived from the class name, every pipeline died "Missing 1 field(s): credentials".
        kwargs.setdefault("destination_name", "postgres")
        # A factory INSTANCE resolves credentials from explicit values only (the string form reads
        # env); observed live 2026-07-30 with the env vars present. Wire them from the same contract.
        if "credentials" not in kwargs:
            creds = _env_credentials()
            if creds is not None:
                kwargs["credentials"] = creds
        super().__init__(*args, **kwargs)

    # INSTANCE method, matching dlt's signature exactly: an @classmethod override imports fine and
    # calls the upstream instance method unbound ("missing 1 required positional argument: 'self'")
    # the first time a pipeline instantiates the destination (found live 2026-07-30).
    def _raw_capabilities(self):  # type: ignore[no-untyped-def]
        caps = super()._raw_capabilities()
        caps.supports_truncate_command = False  # DELETE (ROW EXCLUSIVE), never TRUNCATE (ACCESS EXCLUSIVE)
        return caps
