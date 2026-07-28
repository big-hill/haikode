"""A retry decorator with no dependencies."""

from ..config import get


def retry(function, limit=None):
    limit = limit if limit is not None else get("retry_limit")

    def wrapper(*args, **kwargs):
        last = None
        for _ in range(limit):
            try:
                return function(*args, **kwargs)
            except Exception as error:  # noqa: BLE001 - deliberate
                last = error
        raise last

    return wrapper
