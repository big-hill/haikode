"""HTTP-shaped facade over the store."""

from .store import StoreError, fetch_records, tables


def handle(request):
    table = request.get("table", "")
    limit = request.get("limit")
    try:
        rows = fetch_records(table, limit=limit)
    except StoreError as e:
        return {"status": 404, "error": str(e)}
    return {"status": 200, "rows": rows, "count": len(rows)}


def index():
    return {"status": 200, "tables": tables()}
