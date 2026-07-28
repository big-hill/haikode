"""Aggregate reports built on top of the store."""

from . import store


def totals(table):
    rows = store.fetch_records(table)
    return sum(row["value"] for row in rows)


def summary():
    return {name: totals(name) for name in store.tables()}
