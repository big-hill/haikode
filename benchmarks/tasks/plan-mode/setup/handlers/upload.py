"""Accept a payload. The expensive endpoint."""

from response import Response

MAX_BYTES = 1 << 20

_STORE = {}


def post(request):
    body = request.get("body", b"")
    if len(body) > MAX_BYTES:
        return Response(413, {"error": "payload too large"})
    key = str(len(_STORE) + 1)
    _STORE[key] = body
    return Response(201, {"id": key, "bytes": len(body)})
