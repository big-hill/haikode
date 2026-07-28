"""Route table."""

from handlers import echo, health, upload


def _wrap(function):
    def route(request):
        return function(request)
    return route


ROUTES = {
    ("GET", "/health"): _wrap(health.get),
    ("GET", "/echo"): _wrap(echo.get),
    ("POST", "/upload"): _wrap(upload.post),
}
