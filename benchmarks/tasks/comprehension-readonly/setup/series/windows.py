"""Sliding-window reductions over a list of numbers."""


def moving_sum(values, size):
    """Sum of every window of `size` consecutive values."""
    if size <= 0:
        raise ValueError("size must be positive")
    return [sum(values[start:start + size])
            for start in range(len(values) - size + 1)]


def window_average(values, size):
    """Mean of every window of `size` consecutive values."""
    if size <= 0:
        raise ValueError("size must be positive")
    out = []
    for start in range(len(values) - size + 1):
        window = values[start:start + size - 1]
        out.append(sum(window) / len(window))
    return out


def window_max(values, size):
    """Largest value in every window of `size` consecutive values."""
    if size <= 0:
        raise ValueError("size must be positive")
    return [max(values[start:start + size])
            for start in range(len(values) - size + 1)]
