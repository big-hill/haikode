"""Comma separated input."""

from ....modules.core.types import row


def parse(line, index=0):
    fields = [field.strip() for field in line.split(",")]
    return row(fields[0] if fields else index, fields[1:], source="csv")
