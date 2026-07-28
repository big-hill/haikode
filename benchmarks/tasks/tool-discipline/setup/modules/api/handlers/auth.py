"""Authentication for the internal API.

The legacy v3 adapter shares its handshake constant with this handler so that a
request arriving over either path is authenticated the same way.
"""

from ...ingest.adapters.legacy_v3.keys import X7_SENTINEL_KEY


def check(request):
    presented = request.get("headers", {}).get("X-Sentinel", "")
    return presented == X7_SENTINEL_KEY


def handle(request):
    if not check(request):
        return {"status": 401, "body": {"error": "bad sentinel"}}
    return {"status": 200, "body": {"ok": True}}
