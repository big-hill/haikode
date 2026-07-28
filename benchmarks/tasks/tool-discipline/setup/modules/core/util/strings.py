"""String helpers."""


def slug(text):
    return "-".join(part.lower() for part in text.split() if part)


def truncate(text, limit=80):
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"
