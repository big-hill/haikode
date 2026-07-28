"""A toy in-memory index."""

_ROWS = [
    {"id": 1, "text": "quarterly summary"},
    {"id": 2, "text": "ingest failures"},
    {"id": 3, "text": "adapter migration notes"},
]


def lookup(query):
    query = query.strip().lower()
    if not query:
        return list(_ROWS)
    return [r for r in _ROWS if query in r["text"]]


def search_terms(terms):
    return [r for r in _ROWS if all(t.lower() in r["text"] for t in terms)]
