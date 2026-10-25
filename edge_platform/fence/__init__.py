"""The trading-hours deploy fence: "is a change to this tier allowed now, and until when?"

One implementation for every box and every delivery path (plans `deploy-fence-replacement` §6),
replacing the hand twins: edge-host rth-guard.sh / lib-clock.sh / lib_clock.py / jobs_gate
fence-minutes, trade-infra deploy-bluegreen.sh rth_fence_active, mcp RthFence.cs and
observer-reconcile.py in_rth.

    from edge_platform.fence import decide, load_sources
    d = decide("service", at=now, need=dt.timedelta(minutes=45), loaded=load_sources(dsn=..., cache=...))

CLI: `edge-fence check --tier service --need 45m` (cli.py). Policy: `fence-policy.json`.
"""

from .fence import Decision, Policy, PolicyError, decide, load_policy
from .sessions import ClockBlind, Loaded, Sessions, load_sources

__all__ = [
    "ClockBlind",
    "Decision",
    "Loaded",
    "Policy",
    "PolicyError",
    "Sessions",
    "decide",
    "load_policy",
    "load_sources",
]
