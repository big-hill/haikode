"""Echo the query back."""

from response import Response


def get(request):
    return Response(200, {"query": request.get("query", {})})
