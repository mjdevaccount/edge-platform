"""edge_platform.service -- the composition root EDGE's FastAPI services share (extra `[service]`).

    db        Database (one lazy engine + session maker + dispose), application_name()
    lifespan  build_lifespan() (single-worker assert, warm imports, guarded teardown), assert_single_worker()
    settings  ServiceSettings (the shared pydantic-settings config)
    timeline  AdminRunTimeline (/admin/run/* -> one work-timeline unit), remote_timeline()

Each service keeps only its additions (trade-data: savepoint, session_scope; trade-quant:
make_committer). This package imports no service.
"""
