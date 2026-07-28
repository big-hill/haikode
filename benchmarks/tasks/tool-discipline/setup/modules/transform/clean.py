"""Whitespace and empty-field cleanup."""


def strip_all(rows):
    out = []
    for entry in rows:
        payload = entry.get("payload")
        if isinstance(payload, list):
            payload = [p.strip() for p in payload if str(p).strip()]
        out.append(dict(entry, payload=payload))
    return out
