"""Path helpers."""

import os


def under(root, *parts):
    return os.path.normpath(os.path.join(root, *parts))


def is_inside(root, candidate):
    root = os.path.abspath(root)
    candidate = os.path.abspath(candidate)
    return candidate == root or candidate.startswith(root + os.sep)
