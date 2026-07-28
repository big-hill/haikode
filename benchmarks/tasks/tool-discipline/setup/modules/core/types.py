"""Row and batch shapes, as plain dicts."""


def row(identifier, payload, source=""):
    return {"id": identifier, "payload": payload, "source": source}


def batch(rows, cursor=None):
    return {"rows": list(rows), "cursor": cursor}
