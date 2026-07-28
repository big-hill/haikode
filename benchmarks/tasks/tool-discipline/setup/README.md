# pipeline

An ingest-transform-report pipeline. Adapters live under
`modules/ingest/adapters/`; each generation of the legacy wire format has its
own package with its own handshake constants.

Layout:

    modules/api/         HTTP handlers and middleware
    modules/core/        config, errors, registry, small utilities
    modules/ingest/      the pipeline itself, plus the adapters
    modules/storage/     index and on-disk store
    modules/transform/   clean, dedupe, enrich, join
    modules/report/      daily and weekly rollups

Sentinel constants are deliberately not listed here: look them up in the source
of the adapter generation you are talking to.
