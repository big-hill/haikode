"""Name -> factory registry used by the adapters."""

_FACTORIES = {}


def register(name, factory):
    _FACTORIES[name] = factory
    return factory


def create(name, *args, **kwargs):
    return _FACTORIES[name](*args, **kwargs)


def names():
    return sorted(_FACTORIES)
