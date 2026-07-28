"""Event dispatch. Handlers are keyed by the wire event name."""

from .store import EVENT_NAME, fetch_records

_HANDLERS = {}


def register(name, handler):
    _HANDLERS[name] = handler


def dispatch(name, payload):
    handler = _HANDLERS.get(name)
    if handler is None:
        raise KeyError("no handler for event %r" % name)
    return handler(payload)


def _on_fetch(payload):
    return fetch_records(payload["table"], limit=payload.get("limit"))


register(EVENT_NAME, _on_fetch)
