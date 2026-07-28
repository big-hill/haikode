"""Configuration defaults."""

DEFAULTS = {
    "batch_size": 500,
    "retry_limit": 3,
    "timeout_s": 30,
}


def get(name, overrides=None):
    return (overrides or {}).get(name, DEFAULTS[name])
