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
| `edge_platform.windmill` | shipped Windmill step templates, copied into a consumer tree: `python -m edge_platform.windmill copy post_admin_run <root> u/admin/post_admin_run [--check]` (`--check` = exit 1 on drift in any of the three files, the lock included). `post_admin_run` POSTs `<service_url>/admin/run/<kind>` with `service_url` REQUIRED and `key_variable` required unless `send_key: false` (no default service, no default key; v0.10.1), keeps the body on failure, drops blank params, `fail_when` fails a 200 that says it failed, `require_key` makes the key fail-closed | v0.8.0 (M6 PL3): lifted from trade-data `windmill/u/admin/post_admin_run.py` + `trade_data_headers.py`; adoption is a later row |
| `edge_platform.lanes` | ingest lanes: `spec` (`LaneSpec` + `LaneVocab` + `LaneRegistry` -- a lane as declaration; structure refused at construction, the consumer's estate names -- homes, daemons, timers, decision gates, UI surfaces -- passed in once and refused at registration, empty = refuse all), `gate` (`SourceGate`: switched off / input absent, both reads injected, fail-open LOUD with rollback), `freshness` (the output-age verdict: ttl x3 or ttl+24h, 36h floor, max-silence fallback, a closed state list, the `Clock` protocol -- `register_clock()`, no exchange calendar inside) -- all stdlib; `land` (`land_and_promote`, zero-is-not-success, `Heartbeat(table, source)` beaten in the promote's transaction) and `destination` (`nolock_postgres`, DELETE-not-TRUNCATE) need extra `[dlt]` | v0.9.0 (M6 PL4): lifted from trade-data `sources/dlt_land.py`, `sources/pg_destination.py`, the `SourceSpec` core of `platform/content_sources.py`, `usecases/source_gate.py` and the verdict of `usecases/producer_freshness.py`; adoption is a later row. v0.11.0 (M6 PL9): `judge(spec, Observation, now=)` is the ONE verdict -- on the lane's own registered `clock` (a session clock counts session hours; a clock that cannot answer is `unknown`, never the wall fallback), `due_h` (a declared limit, required off the wall clock), `NEVER_RAN` (registered longer than one limit with zero runs), `repo`; `market_gated` removed (nothing read it); `SourceGate.input_absent` goes through `judge`: a lane that declares `due_h` is gated on exactly the page's limit on its own clock, a lane that does NOT is gated at `cadence_h` wall hours (the v0.9 rule -- fail closed; the page's 36h-floor rule would call an hourly lane with 20h-old output present); `register` refuses an unregistered clock and a judged lane with no run ledger; `land_and_promote` always passes an explicit dlt `schema_contract` (`DEFAULT_CONTRACT`: tables evolve, columns + data types freeze) |
| `edge_platform.watch` | the watchdog plane: `spec` (`Status`, `Probe`, `WatchdogSpec` -- p2/p3 only, the alert-budget law -- `WatchVocab` with the consumer's owners / watch kinds / push severities, `WatchRegistry` strict), `runner` (`WatchRunner`: the market gate a consumer callable, each probe in a savepoint, transition-only + 6h re-alert dedup against the stored state, a pager and an investigate hook that can never take the sweep down, `run_subset`, `read_stored` with staleness; telemetry through `on_degraded` / `sweep_scope`), `pager` (`notify_pager(notifier, channel)`) -- all stdlib; `state` (`WatchdogStateStore` over the consumer's table + `ddl(table)`) needs extra `[sql]` | v0.10.0 (M6 PL5): lifted from trade-data `platform/watchdog.py`, `usecases/watchdog_runner.py`, `repositories/watchdog_state_repo.py`; adoption is a later row. v0.11.0: `Status.IDLE` (an off-session or never-run watchdog is idle, never `ok`), a probe UNKNOWN for `unknown_after` sweeps (default 3) reports `watchdog_probe_unknown:<id>`, the result carries `blind` / `stuck_unknown`, and `exit_code(result)` = 2 did not finish looking (no results, any blind / UNKNOWN row, any stale verdict) / 1 findings / 0; `read_stored` derives `blind` / `stuck_unknown` from the stored rows, so `exit_code(read_stored(...))` fires on a dead plane |
| `edge_platform.notify` (pipeline) | the one push path: `Notifier.notify(Notice) -> Delivery` -- dedup (`Dedup.caller/once/window`), per-scope `Budget`, publish through an injected `Transport`, an append-only delivery row, never raises, fail-open on a dead ledger (or `on_ledger_down="local"`: `LocalLedger` keeps the channel's policy); `first_sight`; `ledger.SqlLedger` over the consumer's `NotifyTables(claims, budget, deliveries)` + `ddl` + `outcome_series` (blind is None, never 0) needs extra `[sql]` | v0.10.0 (M6 PL6): lifted from trade-data `platform/notify.py`, `platform/notify_local.py`, `domain/notify.py`, `repositories/notify_repo.py`, the shape of `observability/notify_gauge.py` |
| `edge_platform.stamp` | fingerprints, one definition: `fingerprint(obj, n)`, `sha(text, n)`, `canon(v)`, `doc_sha(path, n)`, `git_sha(ENV, short=)` (the variable name is the caller's) -- bit-for-bit pinned | v0.10.0 (M6 PL7): lifted from trade-data `platform/stamp.py` (its SHA-1 `config_fingerprint` stays a private trade-data wrapper) |
| `edge_platform.budget` | `PgWindowBudget`: ONE fixed-window rate budget per vendor account, shared by every process through one Postgres row -- one statement per attempt on the DB clock (`clock_timestamp()`, no local fallback), row lock only, the wait outside any transaction; FAILS CLOSED (`BudgetUnavailable`, never a silent grant); `BudgetExhausted` past `max_wait`; `observe()` + `flush_observed()` let the vendor's counter and reset time win; `stats.series(key)` = `budget_*_total{key}`. The table is the consumer's (`ddl(table)` for its migration). Extra `[sql]` | v0.11.0 (M6 PL10). Not pyrate-limiter's `PostgresBucket` (sync, runtime DDL, local-clock fallback, sliding log). A vendor reading whose reset is already past on the DB clock merges nothing (`flush_stale`); a reset is clamped to `RESET_HORIZON_PERIODS` (2) of the row's period and `observe` drops an implausible one; each attempt's `lock_timeout` / `statement_timeout` is SET LOCAL to the remaining `max_wait` (a held row lock is `BudgetUnavailable`, never an unbounded block); waiters wake with a little jitter |
| `edge_platform.plugins` | the strict loader over pluggy: `load(pm, group=, hookspecs=, builtins=, pin=, entry_points=) -> Loaded` (hookspecs first, built-ins in order then entry points SORTED, the pin-env semantics, `check_pending`), `collect(pm, hook, Registry, loaded=)` (load order, a duplicate names both plugins, freezes the store); every failure is `PluginLoadError(group, plugin, stage)`; `Loaded.series()` = `plugins_loaded{group,plugin,origin}`. `edge_platform.testing.plugin_conformance.PluginConformance` is the R1-R10 suite any loader subclasses. Extra `[plugins]` | v0.11.0 (M6 PL11); `Registry.freeze()` lands with it |
| `edge_platform.config` | typed configuration over pydantic-settings: `knob(default, cls=, doc=, env=, unit=, affects=, ge=, le=, on_unreadable=)`, `Domain` (frozen; precedence default < committed TOML (POLICY only) < env < runtime override (OPERATIONAL only) < init), `effective()` (value + `Source`, secrets masked by CLASS), `fingerprint()` (tunable/policy/operational/sealed only), `schema()`, `catalog()`, `lint()`, `load_all(domains, overrides=, on_boot=, on_degraded=)`; `python -m edge_platform.config lint|schema|catalog module:ATTR [--check|--write]` exits 0/1/2. Extra `[service]` | v0.11.0 (M6 CF1). `SECRET_LOOKING` covers `*_KEY`, `KEY_ID`, `AUTH`, `PLAINTEXT`, `DATABASE_URL` too; every rendered value has `scheme://user:pass@` userinfo masked whatever the knob is called (`redact_userinfo`), and lint refuses a non-SECRET default carrying one |
| `edge_platform.sqlkit` | `qualified(name)` (a consumer table name, validated because it is interpolated), `savepoint(session)` (moved from `service.db`, re-exported there), `json_text(value)` (jsonb text, NaN -> null, never raises) -- stdlib | v0.10.0 |
```
pip install "edge-platform @ https://github.com/mjdevaccount/edge-platform/releases/download/v0.5.0/edge_platform-0.5.0-py3-none-any.whl"
pip install "edge-platform[db] @ ..."   # the edge box: also reads quant.market_sessions (psycopg)
pip install "edge-platform[service] @ ..."   # a FastAPI service: fastapi, sqlalchemy[asyncio], pydantic-settings, structlog
pip install "edge-platform[sql] @ ..."       # the SQL stores: watch.state, notify.ledger, budget (sqlalchemy[asyncio]; you bring the driver)
pip install "edge-platform[plugins] @ ..."   # the plugin loader + its conformance suite (pluggy)
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
