"""A tiny HTTP-shaped request router.

No sockets: `handle` is called directly with a request dict. Every request
that arrives goes through `handle` — there is exactly one dispatch point.
"""

from response import Response
from routes import ROUTES


def handle(request):
    """Dispatch one request dict to a route handler."""
    path = request.get("path", "/")
    method = request.get("method", "GET").upper()
    handler = ROUTES.get((method, path))
    if handler is None:
        return Response(404, {"error": "no route for %s %s" % (method, path)})
    try:
        return handler(request)
    except Exception as error:  # noqa: BLE001 - the router is the last line
        return Response(500, {"error": str(error)})


def serve(requests):
    return [handle(request).as_dict() for request in requests]
