"""Full-text-ish search endpoint."""

from ...storage.index import search_terms


def handle(request):
    terms = request.get("q", "").split()
    return {"status": 200, "body": {"hits": search_terms(terms)}}
