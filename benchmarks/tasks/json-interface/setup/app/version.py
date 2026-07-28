"""Single source of truth for the package version."""

VERSION = "0.9.0"


def version_tuple():
    return tuple(int(part) for part in VERSION.split("."))
