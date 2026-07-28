"""Structured request logging."""

import json


def line(request, response):
    return json.dumps({"path": request.get("path"),
                       "status": response.get("status")})
