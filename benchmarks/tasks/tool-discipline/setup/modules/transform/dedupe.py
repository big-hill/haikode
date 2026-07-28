"""Duplicate removal, keeping the first occurrence."""


def by_id(rows):
    seen = set()
    out = []
    for entry in rows:
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        out.append(entry)
    return out
