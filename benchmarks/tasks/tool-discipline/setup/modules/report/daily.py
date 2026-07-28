"""Daily counts by source."""


def counts(rows):
    out = {}
    for entry in rows:
        out[entry.get("source", "?")] = out.get(entry.get("source", "?"), 0) + 1
    return out
