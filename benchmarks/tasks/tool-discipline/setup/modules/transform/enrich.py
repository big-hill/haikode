"""Add derived fields."""

from ..core.util.times import stamp


def with_timestamp(rows):
    moment = stamp()
    return [dict(entry, ingested_at=moment) for entry in rows]
