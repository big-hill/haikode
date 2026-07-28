"""Permissive CORS headers for the internal dashboard."""

HEADERS = {
    "Access-Control-Allow-Origin": "https://dash.internal",
    "Access-Control-Allow-Methods": "GET, POST",
}


def apply(response):
    response.setdefault("headers", {}).update(HEADERS)
    return response
