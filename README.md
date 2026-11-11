# edge-platform

EDGE's shared platform package: the pieces every box and repo in the estate needs to agree on,
in ONE versioned place instead of hand copies kept equal by comments.

| tenant | what | status |
|---|---|---|
| `edge_platform.notify` | the ntfy.sh push budget: the policy file, its reader, the declared sender registry, the Alertmanager night-window check | v0.3.2: every home-IP sender gated, counted or exempt (0 uncounted) |
| `edge_platform.fence` | the trading-hours deploy fence: "is a change to this tier allowed now, and until when?" (plans `deploy-fence-replacement` §6) | v0.3.4: callers edge-host `scripts/edge-fence` (box, pin `edge-platform.pin`) and mcp `observer-reconcile.py` (wheel); textfile samples exact and world-readable (node_exporter runs as another user) |

| `edge_platform.env` | the `.env` reader with docker compose's rules: `lib_env.py` and its bash twin `lib-env.sh` | v0.4.0: moved here from edge-host `scripts/`; trade-infra / home-host python imports the wheel, every bash / box-python copy is vendored and proven with `edge-env check-copy` |
| `edge_platform.registry` | the duplicate-guarded "specs declared at import, keyed by id" table (`Registry`: strict or identical_ok), stdlib only | v0.7.0: lifted from trade-data `service/platform/registry.py` (M6 PL2) |
| `edge_platform.service` | the composition root EDGE's FastAPI services share (extra `[service]`): `Database` (lazy engine + session maker + dispose), `build_lifespan` (single-worker assert, warm imports, GUARDED teardown: a raising hook logs `shutdown_hook_failed` and `dispose` still runs), `ServiceSettings`, `AdminRunTimeline` + `remote_timeline` (`/admin/run/*` -> one work-timeline unit; logs `work timeline DISABLED` once when inert) | v0.5.0: consumers trade-data and trade-quant (inventory row S4). v0.7.0 (M6 PL2) adds the service kit: `flags` (typed env families), `envelope` + `http_errors` (the `fresh` block and the ONE error body), `db.savepoint` + `db.statement_cache_args(ENV)`, `pg_identity.PgIdentity(prefix)`, `auth.install_api_key_guard`, `health.add_health_routes` -- every env name, key and prefix passed in. v0.8.0 (M6 PL3): `admin_run.AdminRuns` -- the `/admin/run/*` declaration table beside `ADMIN_RUN_PREFIX` (one table per service; an idempotency claim needs its reason; `kind_of` the one path rule; `undeclared(routes)` the conformance check) |
| `edge_platform.windmill` | shipped Windmill step templates, copied into a consumer tree: `python -m edge_platform.windmill copy post_admin_run <root> u/admin/post_admin_run [--check]` (`--check` = exit 1 on drift). `post_admin_run` POSTs `<service_url>/admin/run/<kind>` with `service_url` and `key_variable` REQUIRED (no default service, no default key), keeps the body on failure, drops blank params, `fail_when` fails a 200 that says it failed, `require_key` makes the key fail-closed | v0.8.0 (M6 PL3): lifted from trade-data `windmill/u/admin/post_admin_run.py` + `trade_data_headers.py`; adoption is a later row |

```
pip install "edge-platform @ https://github.com/mjdevaccount/edge-platform/releases/download/v0.5.0/edge_platform-0.5.0-py3-none-any.whl"
pip install "edge-platform[db] @ ..."   # the edge box: also reads quant.market_sessions (psycopg)
pip install "edge-platform[service] @ ..."   # a FastAPI service: fastapi, sqlalchemy[asyncio], pydantic-settings, structlog
```

## env

One reader for every `.env` in the estate, with compose-go v2.16.2's rules (last duplicate wins,
`a#b` keeps its `#`, `$$` is one `$`, quotes, interpolation from the process env first) and
refusals instead of guesses (exit 2 / `EnvBlind`, the message names a line, never a value).
`tests/env/lib-env.test.sh` holds both twins to answers docker compose itself produced and
re-asks compose in CI.

```
from edge_platform.env import lib_env          # python that can install the wheel
lib_env.get("WINDMILL_TOKEN", "/path/.env")    # str | None; raises lib_env.EnvBlind
lib_env.get("WINDMILL_TOKEN")                  # FILE = $EDGE_INFRA_ENV at call time; unset = EnvBlind (v0.6.0: no built-in path)

edge-env get KEY [FILE]                        # exit 0 assigned / 1 not assigned / 2 BLIND
edge-env digest FILE KEY...                    # sha256[:12] per key, for comparing readers
edge-env check-copy scripts/lib-env.sh scripts/lib_env.py   # a vendored copy == THIS release
```

Bash sources a file, and the box's system python and the observer VM's Debian python3 have no
pip, so those consumers keep a VENDORED copy beside their scripts (edge-host `scripts/`,
trade-infra `scripts/lib-env.sh`, home-host `observer/lib-env.sh`). Each proves it in CI with
`uvx --from <its pinned wheel URL> edge-env check-copy <copies>` -- exact bytes, CRLF is a
finding -- and ci-kit's `ci-pin-drift` holds that wheel pin to the newest release. Edit the twins
HERE, release, then re-vendor.

## fence

One implementation of the deploy fence for every box and delivery path. It replaces hand twins
that each compute the same window: edge-host `rth-guard.sh` / `lib-clock.sh` / `lib_clock.py` /
`jobs_gate.py fence-minutes`, trade-infra `deploy-bluegreen.sh rth_fence_active`, mcp
`RthFence.cs` and `observer-reconcile.py in_rth`. Swapping each caller is plans §9 step 1.

| file | says |
|---|---|
| the policy (CONSUMER-SUPPLIED since v0.6.0: `--policy PATH` / `$EDGE_FENCE_POLICY`; none = exit 2) | per tier: fenced or not, pads before the open / after the close, blackouts, and the blind superset. Schema 1; `tests/fence/fixtures/fence-policy.json` is a worked example (EDGE's own lives in edge-host `policy/fence-policy.json`). An unknown tier is exit 2 |
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
`decision_id`. `--textfile` keeps `edge_fence_decisions_total{tier,result,source}` (result allow / refuse / blind / invalid -- an
unknown tier is `invalid`, still exit 2, never `blind`) and
`edge_fence_last_decision_timestamp_seconds{tier}` for node_exporter; the rules that read them
(`FenceRunningBlind`, `FenceOverrideUsed`) belong to the alert owners and are not written yet.

## notify

ntfy.sh gives an anonymous publisher **250 messages per UTC day per source IP**, and every sender
on the home IP shares them. Three files say everything about that budget:

| file | says | read by |
|---|---|---|
| `ntfy-policy.json` | HOW MUCH: reserve, per-publisher cap and its exemptions, the night window, tiers, the send-ledger layout | `policy.py` (stdlib only), edge-host `lib-notify.sh`, trade-data `ntfy_client.py`, llm box `lib-notify.ps1` |
| `ntfy-policy.cases.json` | the literal cases every reader runs in its own language, and `policy_sha256` pinning the policy bytes | each consumer's CI |
| the sender registry (CONSUMER-SUPPLIED since v0.6.0: `--registry PATH` / `$EDGE_NOTIFY_PUBLISHERS`; none = exit 2) | WHO spends it: every sender, its route (`library` / `direct` / `am-webhook` / `gatus` / `ci`) and mode (`gated` / `counted` / `exempt` + why / `uncounted` + what is owed). Example: `tests/fixtures/ntfy-publishers.json`; EDGE's lives in edge-host `policy/ntfy-publishers.json` | `publishers.py` |

CANONICAL TODAY: edge-host `policy/ntfy-publishers.json` for the registry (v0.6.0); **edge-host's `policy/ntfy-policy.json`
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
| `edge-notify senders [--registry FILE]` | the registry (FILE or `$EDGE_NOTIFY_PUBLISHERS`), validated against the policy's exemptions |

Exit 0 = clean, 1 = findings, 2 = BLIND (unreadable or invalid input) -- a crashed check never
reads as a passing one. Run it without installing: `uvx --from <wheel URL> edge-notify ...`.

## Release

Bump `version` in `pyproject.toml` and `edge_platform/__init__.py`, merge, tag `vX.Y.Z`. The
release workflow refuses a tag that disagrees with either, then attaches the wheel, the sdist,
the two notify policy files, the env twins and `SHA256SUMS`.

**No estate data in the package (v0.6.0).** A consumer's tiers, senders and file paths are its own
config, passed in by path or environment; a missing one is exit 2 / an exception naming the knob,
never a default. The wheel CI step fails if `fence-policy` or `ntfy-publishers` reappear in it.
