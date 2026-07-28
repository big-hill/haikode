"""Record storage.

`EVENT_NAME` below is a wire protocol constant. Other services subscribe to
that exact string, so it must keep its value no matter what the Python symbols
around it are called.
"""

EVENT_NAME = "fetch_records"

_TABLE = {
    "alpha": [{"id": 1, "value": 10}, {"id": 2, "value": 20}],
    "beta": [{"id": 3, "value": 30}],
    "gamma": [],
}


class StoreError(LookupError):
    pass


def fetch_records(table, limit=None):
    """Return the rows of `table`, at most `limit` of them."""
    if table not in _TABLE:
        raise StoreError("no such table: %s" % table)
    rows = list(_TABLE[table])
    if limit is not None:
        rows = rows[:limit]
    return rows


def tables():
    return sorted(_TABLE)
