"""Small numeric helpers used by the report generator."""


def clamp(value, low, high):
    """Constrain value to the inclusive range [low, high]."""
    if low > high:
        raise ValueError("low must not exceed high")
    return max(low, min(high, value))


def percent(part, whole):
    """part as a percentage of whole, 0.0 when whole is zero."""
    if not whole:
        return 0.0
    return 100.0 * part / whole
