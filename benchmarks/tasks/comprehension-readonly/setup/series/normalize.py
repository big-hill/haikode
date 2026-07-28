"""Rescaling helpers."""


def normalize_series(values):
    """Rescale to the range 0..1. A flat series becomes all zeros."""
    if not values:
        return []
    low, high = min(values), max(values)
    if high == low:
        return [0.0 for _ in values]
    span = high - low
    return [(value - low) / span for value in values]


def clamp_series(values, low, high):
    """Clamp every value into [low, high]."""
    if low > high:
        raise ValueError("low must not exceed high")
    return [min(max(value, low), high) for value in values]
