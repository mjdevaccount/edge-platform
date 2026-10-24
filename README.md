# edge-platform

EDGE's shared platform package: the pieces every box and repo in the estate needs to agree on,
in ONE versioned place instead of hand copies kept equal by comments.

| tenant | what | status |
|---|---|---|
| `edge_platform.notify` | the ntfy.sh push budget: the policy file, its reader, the declared sender registry, the Alertmanager night-window check | first tenant |
| `edge_platform.fence` | the trading-hours deploy fence (plans `deploy-fence-replacement`) | reserved |

Consumers pin a release: Python repos pin the wheel URL, everything else pins the release assets
by sha256. A vendored copy in a consumer is transitional and has an exit.
