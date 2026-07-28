"""Runtime configuration."""

DEFAULTS = {"poll_ms": 250, "retries": 2, "channel": "stable"}


def merged(overrides=None):
    merged_config = dict(DEFAULTS)
    merged_config.update(overrides or {})
    return merged_config
