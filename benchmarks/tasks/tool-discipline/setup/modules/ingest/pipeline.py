"""Wire an adapter to a transform chain and a store."""

from ..core.registry import create
from ..transform import clean, dedupe


def run(adapter_name, rows):
    adapter = create(adapter_name)
    parsed = [adapter.parse(r) for r in rows]
    return dedupe.by_id(clean.strip_all(parsed))
