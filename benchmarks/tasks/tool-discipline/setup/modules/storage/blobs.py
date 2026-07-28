"""Content addressed blob names."""

import hashlib


def name(data):
    return hashlib.sha256(data).hexdigest()[:32]
