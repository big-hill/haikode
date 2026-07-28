"""Liveness."""

from response import Response


def get(_request):
    return Response(200, {"ok": True})
