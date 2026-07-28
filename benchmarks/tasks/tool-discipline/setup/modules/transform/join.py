"""Left join two row lists on id."""


def left(rows, others):
    index = {entry["id"]: entry for entry in others}
    return [dict(entry, extra=index.get(entry["id"])) for entry in rows]
