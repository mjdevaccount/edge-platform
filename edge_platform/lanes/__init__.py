"""edge_platform.lanes -- ingest lanes: declare them, gate them, judge them, land them (M6 PL4).

    spec        LaneSpec + LaneVocab + LaneRegistry: a lane as declaration, checked against the
                consumer's estate names (stdlib)
    gate        SourceGate: switched off / input absent, reads injected (stdlib)
    freshness   the output-age verdict, a closed state list, the Clock protocol (stdlib)
    land        land_and_promote + Heartbeat (extra `[dlt]`)
    destination nolock_postgres, the DELETE-not-TRUNCATE dlt destination (extra `[dlt]`)

Importing this package imports nothing optional; `land` and `destination` need `edge-platform[dlt]`.
Lifted from trade-data (sources/dlt_land.py, sources/pg_destination.py, platform/content_sources.py,
usecases/source_gate.py, usecases/producer_freshness.py) with every table, estate name list and the
exchange calendar turned into inputs.
"""
