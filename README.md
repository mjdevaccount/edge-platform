# edge-platform

EDGE's shared platform package: the pieces every box and repo in the estate needs to agree on,
in ONE versioned place instead of hand copies kept equal by comments.

| tenant | what | status |
|---|---|---|
| `edge_platform.notify` | the ntfy.sh push budget: the policy file, its reader, the declared sender registry, the Alertmanager night-window check | v0.2.0 |
| `edge_platform.fence` | the trading-hours deploy fence: "is a change to this tier allowed now, and until when?" (plans `deploy-fence-replacement` §6) | v0.3.0, no caller yet |

```
pip install "edge-platform @ https://github.com/mjdevaccount/edge-platform/releases/download/v0.3.0/edge_platform-0.3.0-py3-none-any.whl"
pip install "edge-platform[db] @ ..."   # the edge box: also reads quant.market_sessions (psycopg)
```

## fence

One implementation of the deploy fence for every box and delivery path. It replaces hand twins
that each compute the same window: edge-host `rth-guard.sh` / `lib-clock.sh` / `lib_clock.py` /
`jobs_gate.py fence-minutes`, trade-infra `deploy-bluegreen.sh rth_fence_active`, mcp
`RthFence.cs` and `observer-reconcile.py in_rth`. Swapping each caller is plans §9 step 1.

| file | says |
|---|---|
| `fence-policy.json` | per tier: fenced or not, pads before the open / after the close (15 / 35 min), blackouts, and the blind superset (Mon-Fri 09:15-16:35 ET). Holidays and half-day afternoons are OPEN (operator call 1, 2026-10-24) |
| `sessions-bundled.json` | XNYS sessions ~2 years ahead from exchange_calendars (`scripts/gen_bundled_sessions.py`); CI fails when < 180 days remain |

The session table comes from the first source that answers: `quant.market_sessions` (`--dsn` /
`$EDGE_FENCE_DSN`, select-only; a good read refreshes the cache) -> the schema-1 cache file
(`--cache` / `$CLOCK_SESSIONS_FILE`, default `/var/lib/edge-clock/sessions.json`, trusted 336h)
-> the bundled table -> the weekday superset. Blind never ships into a session (the superset
contains every real window) and never deadlocks. No tzdata = exit 2: nothing ET-based can be
decided.

```
edge-fence check --tier service --need 45m      # one JSON decision line; exit 0 allow / 1 refuse / 2 cannot evaluate
edge-fence status                               # which source answers, its horizon
edge-fence --textfile /var/lib/node_exporter/textfile/edge_fence.prom check --tier ui
```

A refusal is a deferral: record it and try the next slot. Exit 2 is a refusal to every caller.
`--override "<reason>"` allows and says so in the record (`source: override`). The record
carries `until` (allowed: when the next window starts), `next_open_window` (refused: the
earliest allowed instant with `--need` free), `source`, `horizon`, `policy_sha256`,
`decision_id`. `--textfile` keeps `edge_fence_decisions_total{tier,result,source}` and
`edge_fence_last_decision_timestamp_seconds{tier}` for node_exporter; the rules that read them
(`FenceRunningBlind`, `FenceOverrideUsed`) belong to the alert owners and are not written yet.

## notify

ntfy.sh gives an anonymous publisher **250 messages per UTC day per source IP**, and every sender
on the home IP shares them. Three files say everything about that budget:

| file | says | read by |
|---|---|---|
| `ntfy-policy.json` | HOW MUCH: reserve, per-publisher cap and its exemptions, the night window, tiers, the send-ledger layout | `policy.py` (stdlib only), edge-host `lib-notify.sh`, trade-data `ntfy_client.py`, llm box `lib-notify.ps1` |
| `ntfy-policy.cases.json` | the literal cases every reader runs in its own language, and `policy_sha256` pinning the policy bytes | each consumer's CI |
| `ntfy-publishers.json` | WHO spends it: every sender, its route (`library` / `direct` / `am-webhook` / `gatus` / `ci`) and mode (`gated` / `counted` / `exempt` + why / `uncounted` + what is owed) | `publishers.py` |

CANONICAL TODAY: this repo for `ntfy-publishers.json`; **edge-host's `policy/ntfy-policy.json`
for the policy and its cases** -- the copy here is a parity-checked copy (mcp
`scripts/lint_script_parity.py` PARITY_PATHS), byte-identical (sha256 `186af626…`), so no consumer
had to move. The policy's canonical home moves HERE at v0.2, in the same release that edits the
policy's `about` text, and every copy changes in that round. A vendored copy in a consumer is
transitional: it proves itself equal with `edge-notify check-copy`, and its exit is pinning this
release instead.

### `edge-notify` (the CLI consumers run)

| command | for |
|---|---|
| `edge-notify am-night alertmanager.yml` | trade-infra CI: the Alertmanager `quiet-night` mute equals `policy.night`, minute-exact, AND synthetic p2 / unlabelled / p1 alerts routed through the tree the way Alertmanager does land on a route muted with exactly that interval (p2) or never time-gated (p1). LIMITS (reviewer 404, owed v0.2): probes vary ONE extra label at a time and only from `=` / literal `=~` matchers, never `tier` itself, so nested multi-label routes, `x!=""` presence matchers and an unmuted odd tier can still pass; one matcher per string |
| `edge-notify check-copy FILE` | a vendored policy copy is byte-identical (LF) to this release |
| `edge-notify check-pin [POLICY [CASES]]` | the cases file pins the policy's sha256 |
| `edge-notify shell` / `rows` | `_ENP_*` lines for lib-notify.sh; the shared cases as TAB rows for a bash test |
| `edge-notify senders` | the registry, validated against the policy's exemptions |

Exit 0 = clean, 1 = findings, 2 = BLIND (unreadable or invalid input) -- a crashed check never
reads as a passing one. Run it without installing: `uvx --from <wheel URL> edge-notify ...`.

## Release

Bump `version` in `pyproject.toml` and `edge_platform/__init__.py`, merge, tag `vX.Y.Z`. The
release workflow refuses a tag that disagrees with either, then attaches the wheel, the sdist,
the three notify files and `SHA256SUMS`.
