"""The .env reader with docker compose's rules: one Python twin, one bash twin, one home.

    from edge_platform.env import lib_env
    lib_env.get("WINDMILL_TOKEN", path)   # -> str, or None when the key is absent; EnvBlind = refuse

`lib_env.py` and `lib-env.sh` are the reader (rules and refusals in lib_env.py's docstring). They
lived in edge-host `scripts/` with byte-identical copies in trade-infra and home-host until
v0.4.0; a copy that only a comment kept equal is how the observer parity run lost its import
(2026-10-29). Now a consumer that can install imports this package from the pinned wheel, and a
consumer that cannot (bash, the box's system python, the observer VM) keeps a VENDORED copy and
proves it in CI: `edge-env check-copy scripts/lib-env.sh scripts/lib_env.py` (cli.py).
"""

from . import lib_env
from .lib_env import EnvBlind, get, load, parse

__all__ = ["EnvBlind", "get", "lib_env", "load", "parse"]
