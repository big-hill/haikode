"""Weekly rollup of the daily counts."""

from .daily import counts


def rollup(days):
    total = {}
    for day in days:
        for source, count in counts(day).items():
            total[source] = total.get(source, 0) + count
    return total
