"""edge_platform.service -- the composition root EDGE's FastAPI services share (extra `[service]`).

    db           Database (one lazy engine + session maker + dispose), application_name(),
                 savepoint() (one SAVEPOINT per member of a fan-out), statement_cache_args(ENV)
    pg_identity  PgIdentity(prefix): application_name for asyncpg AND libpq (install())
    lifespan     build_lifespan() (single-worker assert, warm imports, guarded teardown), assert_single_worker()
    settings     ServiceSettings (the shared pydantic-settings config)
    flags        Family / Flag01 / Truthy: typed raw-env flag families, strict at construction
    envelope     Freshness / Fresh[T] / FreshStamped (the `fresh` block), ApiError / api_error()
    http_errors  install_error_handlers(), json_error(), ERROR_RESPONSES (the ONE error body)
    auth         install_api_key_guard() (shared secret on mutating methods, outside the timeline)
    health       add_health_routes(): /healthz liveness (no I/O), /readyz (DB round-trip, 503)
    timeline     AdminRunTimeline (/admin/run/* -> one work-timeline unit), remote_timeline()

Every env NAME, key, prefix and URL is the consumer's, passed in; this package imports no service.
`edge_platform.registry` (stdlib, no extra) is the duplicate-guarded spec table beside it.
"""
