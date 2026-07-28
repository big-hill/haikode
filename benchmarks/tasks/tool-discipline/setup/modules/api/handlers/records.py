"""Record listing endpoint."""

from ...storage.index import lookup


def handle(request):
    rows = lookup(request.get("query", ""))
    return {"status": 200, "body": {"rows": rows, "count": len(rows)}}
