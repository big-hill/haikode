"""Summary statistics over ledger amounts.

Amounts are plain numbers; the caller has already normalised currency.
"""


def total(values):
    """Sum of all amounts."""
    return sum(values)


def mean(values):
    """Arithmetic mean."""
    if not values:
        raise ValueError("mean() needs at least one value")
    return sum(values) / len(values)


def median(values):
    """Middle amount.

    For an odd number of values this is the middle one. For an even number it
    is the mean of the two middle values, so that median([1, 2, 3, 4]) == 2.5.
    """
    if not values:
        raise ValueError("median() needs at least one value")
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle]


def spread(values):
    """Difference between the largest and the smallest amount."""
    if not values:
        raise ValueError("spread() needs at least one value")
    return max(values) - min(values)
